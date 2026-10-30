"""把带标记的 Markdown 切成「题目块 → 带标签的小节」。

这是 md / docx / html 三条导入路径的共同汇合点：docx 与 html 先由 pandoc 转成
Markdown，然后都走这里。三种格式共用一个解析器，维护成本降为三分之一。

----

两种写法，一套结构
==================

**标记式**（给不写 Markdown 的老师；Word 里直接敲中文方括号即可）::

    【题型】单选  【章节】3.2 循环结构  【难度】2  【分值】2
    【标签】循环, 边界条件
    【题干】下列代码的输出是？
    【选项】
    A. 1024
    *B. 1023
    【答案】B
    【解析】循环在 i = n 时已退出。

**围栏式**（给熟手；元数据写 YAML，正文用三级标题）::

    ## Q
    ```yaml
    type: single_choice
    chapter: 3.2 循环结构
    difficulty: 2
    ```
    ### 题干
    下列代码的输出是？
    ### 选项
    - [ ] A. 1024
    - [x] B. 1023

两者产出同一个 :class:`QuestionBlock`，后续处理完全一致。不是两个解析器——
是同一个解析器认两种标签写法。

----

按行扫描而不是用正则整体匹配，原因是**围栏代码块**：编程题的题干里出现
``【``、``###``、``A.`` 都很正常，必须按行跟踪围栏状态才能跳过它们。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

#: 行首的 ``【标签】`` 或 ``[标签]``，后面可以紧跟内容。
#: 标签名限长 12 字符，避免把正文里的方括号引用（如「见【附录三】的说明」）误判成标签。
_LABEL_LINE = re.compile(
    r"^[ \t>*]*[【\[]\s*(?P<label>[^】\]\n]{1,12}?)\s*[】\]]\s*[:：]?\s*(?P<rest>.*)$"
)

#: 行**中间**的标签只认全角 ``【】``。
#:
#: ASCII ``[...]`` 在行中太危险：``【函数签名】def f(arr: list[int])`` 里的
#: ``[int]`` 会被当成标签，签名直接被截断；``- [x]`` 复选框同理。全角方括号
#: 在代码和公式里几乎不会出现，所以只在行首才接受 ASCII 形式。
_INLINE_LABEL = re.compile(r"【\s*([^】\n]{1,12}?)\s*】")

#: Markdown 标题
_HEADING = re.compile(r"^[ \t]*(?P<level>#{1,6})\s+(?P<title>.+?)\s*#*$")

#: 围栏代码块的起止
_FENCE = re.compile(r"^[ \t]*(?P<ticks>`{3,}|~{3,})\s*(?P<info>[^\n]*)$")

#: 水平分割线，可用来显式分隔两道题
_HR = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")

#: 标签别名 → 规范键。
#:
#: 列得这么全是因为导入源是几十份不同老师写的历年卷子，用词不可能统一。
#: 多花几十行表换来的是「老师不需要改文件就能导入」。
LABEL_ALIASES: dict[str, str] = {
    # 元数据
    "题型": "type",
    "类型": "type",
    "题目类型": "type",
    "type": "type",
    "章节": "chapter",
    "所属章节": "chapter",
    "章": "chapter",
    "chapter": "chapter",
    "难度": "difficulty",
    "难度等级": "difficulty",
    "difficulty": "difficulty",
    "分值": "score",
    "分数": "score",
    "score": "score",
    "标签": "tags",
    "知识点": "tags",
    "考点": "tags",
    "tags": "tags",
    "tag": "tags",
    "来源": "source",
    "出处": "source",
    "source": "source",
    "用时": "est_minutes",
    "预计用时": "est_minutes",
    "建议用时": "est_minutes",
    "编号": "external_id",
    "题号": "external_id",
    "id": "external_id",
    "external_id": "external_id",
    "状态": "status",
    "status": "status",
    # 正文
    "题干": "stem",
    "题目": "stem",
    "正文": "stem",
    "材料": "stem",
    "stem": "stem",
    "question": "stem",
    "选项": "options",
    "选择项": "options",
    "options": "options",
    "答案": "answer",
    "参考答案": "answer",
    "正确答案": "answer",
    "answer": "answer",
    "解析": "explanation",
    "分析": "explanation",
    "解答": "explanation",
    "说明": "explanation",
    "explanation": "explanation",
    "评分标准": "rubric",
    "评分": "rubric",
    "给分点": "rubric",
    "rubric": "rubric",
    # 子题与编程题
    "子题": "sub",
    "小问": "sub",
    "小题": "sub",
    "sub": "sub",
    "语言": "language",
    "编程语言": "language",
    "language": "language",
    "函数签名": "signature",
    "signature": "signature",
    "初始代码": "starter",
    "模板代码": "starter",
    "starter": "starter",
    "测试用例": "test_cases",
    "样例": "test_cases",
    "test_cases": "test_cases",
}

#: 出现它就意味着「一道新题开始了」
QUESTION_START_KEYS = frozenset({"type"})

#: 这些键的值是整段正文，可以跨多行
MULTILINE_KEYS = frozenset(
    {"stem", "options", "answer", "explanation", "rubric", "starter", "test_cases"}
)

#: 进入「子题作用域」的键。
#:
#: 一道综合题的子题各自有题干、分值、答案，但**不能**用 ``【题型】`` 分隔——
#: ``【题型】`` 是题目边界的标志，用在子题里会让子题变成独立的题目（综合题
#: 则丢掉全部子题）。所以约定：``【子题】`` 行上直接写题型与分值，
#: 形如 ``【子题】简答 5分``；此后的题干/答案/解析都归属该子题，
#: 直到下一个 ``【子题】`` 或下一道题。
SUB_KEY = "sub"


def canonical_label(raw: str) -> str | None:
    """标签名 → 规范键；认不出来返回 ``None``。

    >>> canonical_label("题干")
    'stem'
    >>> canonical_label(" 参考答案 ")
    'answer'
    >>> canonical_label("Difficulty")
    'difficulty'
    >>> canonical_label("附录三") is None
    True
    """
    cleaned = raw.strip().replace(" ", "").replace("　", "").lower()
    return LABEL_ALIASES.get(cleaned)


@dataclass(slots=True)
class Section:
    """块内的一个小节。"""

    key: str
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """合并成文本，去掉首尾空行但保留内部结构（代码块缩进不能动）。"""
        return "\n".join(self.lines).strip("\n").rstrip()


@dataclass(slots=True)
class SubBlock:
    """综合题的一个子题。

    结构与 :class:`QuestionBlock` 相同但不嵌套——只支持两层，这是产品决定：
    三层以上的题目结构在试卷上根本排不开。
    """

    line_number: int
    header: str = ""
    sections: list[Section] = field(default_factory=list)
    inline: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str:
        parts = [section.text for section in self.sections if section.key == key]
        if parts:
            return "\n\n".join(part for part in parts if part)
        return self.inline.get(key, "")

    @property
    def is_empty(self) -> bool:
        return not self.header and not self.inline and all(not s.text for s in self.sections)


@dataclass(slots=True)
class QuestionBlock:
    """一道题的原始内容，尚未做类型校验。

    ``line_number`` 是块在源文件里的起始行。导入报告必须报出行号——不然老师
    拿到「第 7 条校验失败」这种提示，根本不知道去文件哪里改。
    """

    line_number: int
    sections: list[Section] = field(default_factory=list)
    #: 元数据行里一次写多个标签时的键值（如 ``【题型】单选 【难度】2``）
    inline: dict[str, str] = field(default_factory=dict)
    #: YAML 围栏里的元数据
    meta: dict[str, object] = field(default_factory=dict)
    #: 子题（仅综合题用）
    subs: list[SubBlock] = field(default_factory=list)

    def get(self, key: str) -> str:
        """取某个小节的文本；同名小节出现多次时按出现顺序拼接。"""
        parts = [section.text for section in self.sections if section.key == key]
        if parts:
            return "\n\n".join(part for part in parts if part)
        return self.inline.get(key, "")

    def has(self, key: str) -> bool:
        return any(section.key == key for section in self.sections) or key in self.inline

    def all_of(self, key: str) -> list[str]:
        """同名小节的各段内容（子题要逐个取出来）。"""
        return [section.text for section in self.sections if section.key == key]

    @property
    def is_empty(self) -> bool:
        return (
            not self.meta
            and not self.inline
            and not self.subs
            and all(not s.text for s in self.sections)
        )


class _LineScanner:
    """按行扫描，跟踪围栏状态。

    围栏内的一切原样收集——编程题题干里的 ``### include``、``A. foo``、
    ``【占位】`` 都是正常内容，当成标记处理会把代码毁掉。
    """

    def __init__(self, text: str) -> None:
        self._lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def __iter__(self) -> Iterator[tuple[int, str, bool]]:
        """产出 ``(行号从1开始, 行内容, 是否在围栏内)``。"""
        fence: str | None = None
        for number, line in enumerate(self._lines, start=1):
            matched = _FENCE.match(line)
            if fence is None:
                if matched:
                    fence = matched.group("ticks")
                    yield number, line, True
                    continue
                yield number, line, False
            else:
                # 结束围栏需要同种且不短于起始的符号
                if (
                    matched
                    and matched.group("ticks")[0] == fence[0]
                    and len(matched.group("ticks")) >= len(fence)
                    and not matched.group("info")
                ):
                    fence = None
                yield number, line, True


def _split_inline_labels(rest: str) -> list[tuple[str, str]]:
    """拆开一行里的多个标签，按出现顺序返回。

    返回有序列表而不是字典，是因为调用方需要知道**最后一个**标签是谁：行末的
    ``【答案】`` 后面没有内容时，值在接下来的几行里。

    >>> _split_inline_labels("单选  【难度】2  【分值】2")
    [('difficulty', '2'), ('score', '2')]
    >>> _split_inline_labels("只有值")
    []
    >>> _split_inline_labels("甲 【答案】")
    [('answer', '')]
    """
    found: list[tuple[str, str]] = []
    pieces = _INLINE_LABEL.split(rest)
    # pieces = [第一段值, 标签1, 值1, 标签2, 值2, ...]
    for index in range(1, len(pieces) - 1, 2):
        key = canonical_label(pieces[index])
        if key:
            found.append((key, pieces[index + 1].strip()))
    return found


def _find_known_label(text: str, start: int = 0) -> re.Match[str] | None:
    """找到第一个**已识别**的行中标签。

    必须跳过不认识的方括号内容：题干里出现「见【附录三】的说明」「如【图1】所示」
    很正常，在那里截断会把题干砍掉一截，而且用户完全看不出为什么。
    """
    for match in _INLINE_LABEL.finditer(text, start):
        if canonical_label(match.group(1)):
            return match
    return None


def _first_inline_value(rest: str) -> str:
    """一行里第一个标签对应的值（后面若还有**已识别**的标签就截断）。

    >>> _first_inline_value("单选  【难度】2")
    '单选'
    >>> _first_inline_value("下列代码的输出是？")
    '下列代码的输出是？'
    >>> _first_inline_value("def f(arr: list[int]) -> list[int]")
    'def f(arr: list[int]) -> list[int]'
    >>> _first_inline_value("见【附录三】的说明")
    '见【附录三】的说明'
    """
    found = _find_known_label(rest)
    return (rest[: found.start()] if found else rest).strip()


def _parse_yaml(lines: Sequence[str]) -> dict[str, object]:
    import yaml

    try:
        loaded = yaml.safe_load("\n".join(lines))
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def split_blocks(text: str) -> list[QuestionBlock]:
    """把整份文档切成题目块。

    题目边界的判定（按优先级）：

    1. 又出现一次 ``【题型】`` —— 最可靠，老师每道题都会写
    2. 又出现一个带 ``type`` 的 YAML 围栏
    3. 一级或二级标题 —— 围栏式写法用 ``## Q`` 分题
    4. 水平分割线

    文档开头的说明文字（模板里常有「填写说明」）会落在第一个边界之前，
    作为空块被丢掉。
    """
    blocks: list[QuestionBlock] = []
    current = QuestionBlock(line_number=1)
    section: Section | None = None
    sub: SubBlock | None = None
    yaml_lines: list[str] | None = None
    yaml_indent = ""

    def flush_section() -> None:
        """把正在收集的小节落到当前作用域（子题优先于题目本身）。"""
        nonlocal section
        if section is not None:
            (sub.sections if sub is not None else current.sections).append(section)
            section = None

    def flush_sub() -> None:
        nonlocal sub
        flush_section()
        if sub is not None and not sub.is_empty:
            current.subs.append(sub)
        sub = None

    def start_block(line_number: int) -> None:
        nonlocal current
        flush_sub()
        if not current.is_empty:
            blocks.append(current)
        current = QuestionBlock(line_number=line_number)

    def put_inline(key: str, value: str) -> None:
        (sub.inline if sub is not None else current.inline)[key] = value

    for number, line, in_fence in _LineScanner(text):
        # ---- YAML 围栏
        if yaml_lines is not None:
            if _FENCE.match(line):
                meta = _parse_yaml(yaml_lines)
                fence_start_line = max(1, number - len(yaml_lines) - 1)
                yaml_lines = None
                if "type" in meta:
                    if current.is_empty:
                        current.line_number = fence_start_line
                    else:
                        start_block(number)
                current.meta.update(meta)
            else:
                yaml_lines.append(
                    line[len(yaml_indent) :] if line.startswith(yaml_indent) else line
                )
            continue

        fence_start = _FENCE.match(line)
        if fence_start and fence_start.group("info").strip().lower() in {"yaml", "yml"}:
            flush_section()
            yaml_lines = []
            yaml_indent = line[: len(line) - len(line.lstrip())]
            continue

        if in_fence:
            if section is not None:
                section.lines.append(line)
            continue

        # ---- 标记行
        label_match = _LABEL_LINE.match(line)
        if label_match:
            key = canonical_label(label_match.group("label"))
            if key:
                rest = label_match.group("rest")
                if key in QUESTION_START_KEYS:
                    if current.is_empty:
                        # 文档开头的说明文字被丢掉了，但行号要跟上真正的起始行，
                        # 否则报错信息会把用户指到文件开头
                        current.line_number = number
                    else:
                        start_block(number)
                elif key == SUB_KEY:
                    flush_sub()
                    sub = SubBlock(line_number=number, header=_first_inline_value(rest))
                    for extra_key, extra_value in _split_inline_labels(rest):
                        sub.inline[extra_key] = extra_value
                    continue
                inline_extras = _split_inline_labels(rest)
                value = _first_inline_value(rest)
                flush_section()
                if key in MULTILINE_KEYS and not inline_extras:
                    section = Section(key=key, lines=[value] if value else [])
                else:
                    put_inline(key, value)
                for extra_key, extra_value in inline_extras:
                    put_inline(extra_key, extra_value)
                # 行末的多行标签后面没有内容时，值在接下来的几行里，
                # 所以要为它开一个小节。典型情形是 docx 往返后整道题挤成一行、
                # 【答案】落在行尾、答案本身是下一段的编号列表。
                last_key, last_value = inline_extras[-1] if inline_extras else (key, value)
                if last_key in MULTILINE_KEYS and not last_value:
                    section = Section(key=last_key)
                continue

        # ---- 标题
        heading = _HEADING.match(line)
        if heading:
            key = canonical_label(heading.group("title"))
            level = len(heading.group("level"))
            if key:
                flush_section()
                if key in QUESTION_START_KEYS and not current.is_empty:
                    start_block(number)
                elif key == SUB_KEY:
                    flush_sub()
                    sub = SubBlock(line_number=number)
                    continue
                if key in MULTILINE_KEYS:
                    section = Section(key=key)
                continue
            if level <= 2:
                start_block(number)
                continue
            # 三级以上且不是已知标签：当作正文的一部分
            if section is not None:
                section.lines.append(line)
            continue

        if _HR.match(line) and not current.is_empty:
            start_block(number)
            continue

        # 行**中间**出现已知标记时，把这一行切开：标记之前的内容归当前小节，
        # 之后的按标签处理。典型情形是输入的换行被吃掉了（从 PDF 粘贴、
        # Markdown 经 docx 往返），于是 ``…$O(n^2)$ 【解析】最坏情况…``
        # 挤在一行——不切开的话解析就会被吞进答案里。
        if section is not None:
            embedded = _find_known_label(line)
            if embedded is not None:
                head = line[: embedded.start()].rstrip()
                if head:
                    section.lines.append(head)
                flush_section()
                for extra_key, extra_value in _split_inline_labels(line[embedded.start() :]):
                    if extra_key in MULTILINE_KEYS:
                        section = Section(key=extra_key, lines=[extra_value] if extra_value else [])
                    else:
                        put_inline(extra_key, extra_value)
                continue

        if section is not None:
            section.lines.append(line)
        elif line.strip() and (current.inline or current.meta or sub is not None):
            # 元数据之后、还没遇到【题干】就出现了正文——按题干处理，
            # 因为「元数据写完直接接题干」是很常见的省写法
            section = Section(key="stem", lines=[line])

    flush_sub()
    if not current.is_empty:
        blocks.append(current)
    return blocks
