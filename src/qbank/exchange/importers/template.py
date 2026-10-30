"""题目块 → :class:`QuestionDraft`。

本模块把 :mod:`.sections` 切出来的原始块变成校验过的草稿。设计上的几个取舍：

**答案以 ``【答案】`` 为准，选项标记只做交叉校验。**
    「哪个选项是对的」有两个来源：``【答案】B``，以及选项行上的标记（``*B.``、
    加粗、``[x]``）。以答案段为准，因为老师一定会填答案，而标记的写法五花八门；
    两者都有且**不一致**时报错而不是悄悄选一个——这种不一致几乎总是源文件
    本身错了（改过答案忘了改加粗），悄悄处理等于把错题导进库。

**``*`` 前缀与列表符号的歧义。**
    ``*B. 1023`` 是「正确项标记」，``* B. 1023`` 是 Markdown 列表项。靠紧跟与否
    区分，这条规则写进模板规范。真正的依赖仍是 ``【答案】``，所以即使老师写错
    也不影响结果。

**出错不抛异常，而是收集进报告。**
    一份文件里有一道题格式不对，不该让另外 49 道题也导不进来。每个块独立
    产出「草稿」或「错误」，错误带行号。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from qbank.core.enums import (
    BlankMatch,
    Difficulty,
    QuestionStatus,
    QuestionType,
    parse_question_type,
)
from qbank.core.schemas import QuestionDraft
from qbank.exchange.importers.sections import QuestionBlock, SubBlock, split_blocks

#: 选项行。支持 ``A.`` ``A、`` ``A)`` ``(A)`` ``A．`` 以及裸 ``A `` 开头。
#: 前缀部分一并吃掉 Markdown 列表符号与复选框。
_OPTION_LINE = re.compile(
    r"""^[ \t>]*
    (?P<bullet>[-+]|\*(?!\S)|\d+[.)、])?[ \t]*          # 列表符号（* 后必须有空白才算列表）
    (?P<checkbox>\[[ xX✓√]\])?[ \t]*
    \\?(?P<star>\*(?=\S))?[ \t]*                        # 紧跟内容的 * 是「正确项」标记
    \\?(?P<bold>\*\*)?[ \t]*
    # 选项标号。有分隔符时空格可省（``A、甲`` 是中文里最常见的写法），
    # 没有分隔符时必须有空格，否则 ``Apple 是一种水果`` 也会被当成选项 A
    [(（\[]?(?P<label>[A-Za-z])\\?
    (?:[)）\].、．:：][ \t]*|[ \t]+)
    (?P<content>.*?)
    (?P<bold_end>\*\*)?[ \t]*$
    """,
    re.VERBOSE,
)

#: 把挤在一行的选项切开的位置：下一个选项标号之前。
#:
#: 为什么需要：选项本该每行一个，但「挤成一行」在真实输入里很常见——从 PDF
#: 粘贴、从网页复制、Word 的自动格式，都可能把换行吃掉。实测 Markdown 经
#: docx 往返后软换行也会变成空格。不处理的话这种文件只会解析出 1 个选项，
#: 报「选择题至少需要 2 个选项」，而用户看源文件明明有四个。
#: ``\\?`` 是为了容忍 pandoc 的转义：Markdown 经 docx 往返后 ``*B.`` 会变成
#: ``\*B.``，``_`` 会变成 ``\_``。不认转义的话，用 ``*`` 标记正确项的 Word
#: 文件一律会报「至少需要 2 个选项」。
_OPTION_SPLIT = re.compile(
    r"(?<=\s)(?=\\?\*{0,2}[(（\[]?[A-Za-z]\\?[)）\].、．][ \t]?)"
    r"|(?<=\s)(?=\\?\*{0,2}[A-Za-z]\\?[.、．)][ \t]?)"
)

#: 选项内容末尾的「正确」标记
_CORRECT_SUFFIX = re.compile(r"[ \t]*[(（\[]?\s*(?:正确|对|✓|√|correct)\s*[)）\]]?[ \t]*$", re.I)

#: 判断题答案的各种写法
_TRUE_WORDS = frozenset({"对", "正确", "是", "真", "t", "true", "y", "yes", "√", "✓", "1"})
_FALSE_WORDS = frozenset(
    {"错", "错误", "否", "不对", "假", "f", "false", "n", "no", "×", "x", "✗", "0"}
)

#: 填空题答案的分隔符：分号、竖线、换行。顿号和逗号**不算**——
#: 「北京、上海」很可能是一个空的完整答案，拆开就错了。
_BLANK_SPLIT = re.compile(r"[;；|\n]+")

#: 一个空的多个可接受答案之间用斜杠或「或」分隔。
#:
#: ``(?![^<>]*>)`` 是为了不切开 HTML 标签：从网页或 pandoc 的 html 输出导入时
#: 正文里会有 ``</span>``，按 ``/`` 切开会把答案切成乱码。
_ALTERNATIVE_SPLIT = re.compile(r"(?:[/／](?![^<>]*>)|\bor\b|或)")

#: 带序号的答案行，如 ``1. 甲`` / ``(2) 乙``
_NUMBERED_ANSWER = re.compile(
    r"^[ \t]*[(（\[]?(?P<index>\d{1,2})[)）\].、．:：][ \t]*(?P<text>.*)$"
)

#: 难度的文字写法
_DIFFICULTY_WORDS: dict[str, int] = {
    "很易": 1,
    "极易": 1,
    "最易": 1,
    "简单": 2,
    "容易": 2,
    "较易": 2,
    "中等": 3,
    "一般": 3,
    "中": 3,
    "较难": 4,
    "难": 4,
    "偏难": 4,
    "很难": 5,
    "极难": 5,
    "最难": 5,
}

#: 状态的文字写法
_STATUS_WORDS: dict[str, QuestionStatus] = {
    "草稿": QuestionStatus.DRAFT,
    "draft": QuestionStatus.DRAFT,
    "启用": QuestionStatus.ACTIVE,
    "可用": QuestionStatus.ACTIVE,
    "active": QuestionStatus.ACTIVE,
    "停用": QuestionStatus.RETIRED,
    "作废": QuestionStatus.RETIRED,
    "retired": QuestionStatus.RETIRED,
}


@dataclass(frozen=True, slots=True)
class ParsedOption:
    label: str
    content: str
    marked_correct: bool


@dataclass(frozen=True, slots=True)
class BlockError:
    """一道题的解析失败。带行号，让用户知道去文件哪里改。"""

    line_number: int
    message: str
    excerpt: str = ""

    def __str__(self) -> str:
        head = f"第 {self.line_number} 行：{self.message}"
        return f"{head}\n    {self.excerpt}" if self.excerpt else head


@dataclass(slots=True)
class ParseReport:
    """一份文件的解析结果。"""

    drafts: list[QuestionDraft] = field(default_factory=list)
    errors: list[BlockError] = field(default_factory=list)
    #: 不阻断的提示（如答案与标记不一致之外的轻微问题）
    warnings: list[BlockError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [f"解析出 {len(self.drafts)} 道题"]
        if self.errors:
            parts.append(f"{len(self.errors)} 道失败")
        if self.warnings:
            parts.append(f"{len(self.warnings)} 条提示")
        return "，".join(parts)


# ---------------------------------------------------------------- 字段解析
def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value).strip()


def _parse_difficulty(raw: object) -> Difficulty:
    """难度可以写数字也可以写「较难」。认不出来按中等处理，不阻断导入。"""
    text = _as_text(raw)
    if not text:
        return Difficulty.MEDIUM
    digits = re.search(r"[1-5]", text)
    if digits:
        return Difficulty(int(digits.group(0)))
    normalized = text.replace(" ", "")
    # 按词长从长到短匹配：否则「很难」会先命中「难」而被判成 4 而不是 5
    for word in sorted(_DIFFICULTY_WORDS, key=len, reverse=True):
        if word in normalized:
            return Difficulty(_DIFFICULTY_WORDS[word])
    return Difficulty.MEDIUM


def _parse_score(raw: object) -> float:
    text = _as_text(raw)
    matched = re.search(r"\d+(?:\.\d+)?", text)
    return float(matched.group(0)) if matched else 0.0


def _parse_minutes(raw: object) -> int:
    text = _as_text(raw)
    matched = re.search(r"\d+", text)
    return int(matched.group(0)) if matched else 0


def _parse_tags(raw: object) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = re.split(r"[,，、;；/\s]+", _as_text(raw))
    seen: dict[str, None] = {}
    for item in items:
        cleaned = item.strip().strip("#")
        if cleaned:
            seen[cleaned] = None
    return tuple(seen)


def _parse_status(raw: object) -> QuestionStatus:
    text = _as_text(raw).replace(" ", "").lower()
    return _STATUS_WORDS.get(text, QuestionStatus.DRAFT)


# ---------------------------------------------------------------- 选项
def parse_options(text: str) -> list[ParsedOption]:
    """解析选项段。

    >>> [(o.label, o.content, o.marked_correct) for o in parse_options("A. 1024\\n*B. 1023")]
    [('A', '1024', False), ('B', '1023', True)]
    >>> [(o.label, o.marked_correct) for o in parse_options("- [ ] A. 甲\\n- [x] B. 乙")]
    [('A', False), ('B', True)]
    >>> [(o.label, o.marked_correct) for o in parse_options("A. 甲\\n**B. 乙**")]
    [('A', False), ('B', True)]
    >>> [o.content for o in parse_options("A. 甲（正确）")]
    ['甲']

    挤在一行也能切开（从 PDF 或网页粘贴时很常见）：

    >>> [(o.label, o.content) for o in parse_options("A. 1024 B. 1023 C. 2048")]
    [('A', '1024'), ('B', '1023'), ('C', '2048')]
    """
    lines = text.split("\n")
    # 整段只有一行却包含多个选项标号时，按标号切开
    if len(lines) == 1 and lines[0].strip():
        pieces = [piece.strip() for piece in _OPTION_SPLIT.split(lines[0]) if piece.strip()]
        if len(pieces) > 1:
            lines = pieces

    options: list[ParsedOption] = []
    for line in lines:
        if not line.strip():
            continue
        matched = _OPTION_LINE.match(line)
        if matched is None:
            # 不是选项行——可能是上一个选项的续行（选项里有公式块或长文本）
            if options:
                last = options[-1]
                options[-1] = ParsedOption(
                    label=last.label,
                    content=f"{last.content}\n{line.strip()}".strip(),
                    marked_correct=last.marked_correct,
                )
            continue
        checkbox = matched.group("checkbox") or ""
        content = matched.group("content").strip()
        correct = bool(
            matched.group("star")
            or (matched.group("bold") and matched.group("bold_end"))
            or checkbox.strip("[]").strip() not in ("", " ")
        )
        if _CORRECT_SUFFIX.search(content):
            correct = True
            content = _CORRECT_SUFFIX.sub("", content)
        options.append(
            ParsedOption(
                label=matched.group("label").upper(),
                content=content.rstrip("*").strip(),
                marked_correct=correct,
            )
        )
    return options


def parse_answer_labels(text: str, available: Sequence[str]) -> list[str]:
    """从答案段里抽出选项标号。

    ``available`` 是实际存在的标号，用来避免把答案里的普通字母当成选项
    （「答案：A，因为 B 项的 n 是 1」里的 n 不是选项）。

    >>> parse_answer_labels("B", ["A", "B", "C"])
    ['B']
    >>> parse_answer_labels("ACD", ["A", "B", "C", "D"])
    ['A', 'C', 'D']
    >>> parse_answer_labels("A、C 和 D", ["A", "B", "C", "D"])
    ['A', 'C', 'D']
    >>> parse_answer_labels("见解析", ["A", "B"])
    []
    """
    allowed = {label.upper() for label in available}
    found: dict[str, None] = {}
    # 只看答案段的第一行：后面常常是解释文字，里面的字母不是选项
    first_line = text.strip().split("\n")[0]
    for char in first_line.upper():
        if char in allowed:
            found[char] = None
        elif char.isalpha() or char.isdigit():
            # 出现了不在选项里的字母数字，说明这行不是纯答案（如「见解析」）
            # 但不立即放弃——「答案 B」里的「答」「案」是中文，不会走到这里
            continue
    return list(found)


def _parse_true_false(text: str) -> bool | None:
    """判断题答案 → 布尔。认不出来返回 ``None``。

    >>> _parse_true_false("错误")
    False
    >>> _parse_true_false("√")
    True
    >>> _parse_true_false("不确定") is None
    True
    """
    cleaned = text.strip().split("\n")[0].strip().strip("。.，,").lower()
    if cleaned in _TRUE_WORDS:
        return True
    if cleaned in _FALSE_WORDS:
        return False
    # 容错：「这句话是错误的」这类整句表述
    if any(word in cleaned for word in ("错误", "不对", "不正确")):
        return False
    if any(word in cleaned for word in ("正确", "对的")):
        return True
    return None


def _parse_blanks(text: str, stem: str) -> list[dict[str, Any]]:
    """填空题答案 → 各空的答案列表。

    支持三种写法：带序号（``1. 甲``）、分隔符（``甲；乙``）、每行一个。
    一个空的多个可接受答案用 ``/`` 或「或」分隔。
    """
    lines = [line for line in text.split("\n") if line.strip()]
    numbered: dict[int, str] = {}
    for line in lines:
        matched = _NUMBERED_ANSWER.match(line)
        if matched:
            numbered[int(matched.group("index"))] = matched.group("text").strip()
    if numbered:
        pieces = [numbered[key] for key in sorted(numbered)]
    elif len(lines) > 1:
        pieces = [line.strip() for line in lines]
    else:
        pieces = [piece.strip() for piece in _BLANK_SPLIT.split(text) if piece.strip()]

    blanks: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces, start=1):
        alternatives = [alt.strip() for alt in _ALTERNATIVE_SPLIT.split(piece) if alt.strip()]
        blanks.append(
            {
                "index": index,
                "answers": alternatives or [piece],
                "match": BlankMatch.EXACT,
            }
        )
    return blanks


def _count_blank_markers(stem: str) -> int:
    return len(re.findall(r"_{2,}|[（(]\s*[)）]", stem))


# ---------------------------------------------------------------- 主流程
def _value(block: QuestionBlock, key: str) -> object:
    """取字段值：YAML 元数据优先于标记行。

    理由是围栏式写法更明确——既然用了 YAML，就说明作者想精确控制。
    """
    if key in block.meta:
        return block.meta[key]
    return block.get(key)


def _build_payload(
    block: QuestionBlock, kind: QuestionType, stem: str
) -> tuple[dict[str, Any], str, list[str]]:
    """构造 payload，返回 ``(payload, 规范化后的答案文本, 错误列表)``。"""
    errors: list[str] = []
    answer_text = _as_text(_value(block, "answer"))

    if kind in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
        options = parse_options(_as_text(_value(block, "options")))
        if not options:
            errors.append("没有解析出任何选项；选项行应形如「A. 内容」")
            return {"type": kind.value, "options": []}, answer_text, errors
        labels = [option.label for option in options]
        from_answer = parse_answer_labels(answer_text, labels)
        from_marks = [option.label for option in options if option.marked_correct]

        if from_answer and from_marks and set(from_answer) != set(from_marks):
            errors.append(
                f"【答案】写的是 {'、'.join(from_answer)}，但选项上标记为正确的是 "
                f"{'、'.join(from_marks)}；两处不一致，请核对源文件"
            )
        correct = set(from_answer or from_marks)
        if not correct:
            errors.append("无法确定正确选项：【答案】里没有选项标号，选项上也没有标记")
        payload = {
            "type": kind.value,
            "options": [
                {
                    "label": option.label,
                    "content": option.content,
                    "is_correct": option.label in correct,
                }
                for option in options
            ],
        }
        return payload, answer_text or "、".join(sorted(correct)), errors

    if kind is QuestionType.TRUE_FALSE:
        parsed = _parse_true_false(answer_text)
        if parsed is None:
            errors.append(f"判断题的答案「{answer_text or '(空)'}」无法识别为对或错")
            parsed = True
        return {"type": kind.value, "correct": parsed}, answer_text, errors

    if kind is QuestionType.FILL_BLANK:
        blanks = _parse_blanks(answer_text, stem)
        if not blanks:
            errors.append("填空题没有答案")
            return {"type": kind.value, "blanks": []}, answer_text, errors
        markers = _count_blank_markers(stem)
        if markers and markers != len(blanks):
            errors.append(
                f"题干里有 {markers} 个空位标记，但答案给了 {len(blanks)} 个；"
                "多个答案之间请用分号或换行分隔"
            )
        return {"type": kind.value, "blanks": blanks}, answer_text, errors

    if kind is QuestionType.PROGRAMMING:
        payload = {
            "type": kind.value,
            "language": _as_text(_value(block, "language")),
            "function_signature": _as_text(_value(block, "signature")),
            "starter_code": _as_text(_value(block, "starter")),
        }
        return payload, answer_text, errors

    if kind is QuestionType.COMPREHENSIVE:
        return (
            {
                "type": kind.value,
                "material_label": _as_text(_value(block, "material_label")),
            },
            answer_text,
            errors,
        )

    key_points = _value(block, "rubric")
    points = (
        tuple(str(item) for item in key_points) if isinstance(key_points, (list, tuple)) else ()
    )
    return {"type": kind.value, "key_points": points}, answer_text, errors


#: 【子题】行上的「题型 + 分值」，如 ``简答 5分`` / ``编程 20`` / ``5分``
_SUB_HEADER = re.compile(r"^\s*(?P<kind>[^\d\s]+)?\s*(?P<score>\d+(?:\.\d+)?)?\s*分?\s*$")


def _sub_to_draft(sub: SubBlock) -> tuple[QuestionDraft | None, list[BlockError]]:
    """子题 → 草稿。

    子题的题型与分值写在 ``【子题】`` 行上（如 ``【子题】简答 5分``），而不是用
    ``【题型】``——后者是题目边界的标志，用在子题里会让子题变成独立的题目，
    综合题则丢掉全部子题。
    """
    errors: list[BlockError] = []
    header = _SUB_HEADER.match(sub.header or "")
    kind_text = (header.group("kind") if header else "") or sub.inline.get("type", "") or "简答"
    try:
        kind = parse_question_type(kind_text)
    except ValueError:
        kind = QuestionType.SHORT_ANSWER

    stem = sub.get("stem").strip()
    if not stem:
        errors.append(BlockError(sub.line_number, "子题缺少题干"))
        return None, errors

    score_text = sub.inline.get("score", "") or ((header.group("score") or "") if header else "")
    pseudo = QuestionBlock(line_number=sub.line_number, sections=sub.sections, inline=sub.inline)
    payload, answer_text, payload_errors = _build_payload(pseudo, kind, stem)
    errors.extend(BlockError(sub.line_number, f"子题：{msg}") for msg in payload_errors)
    if errors:
        return None, errors

    try:
        draft = QuestionDraft(
            type=kind,
            stem=stem,
            payload=payload,
            answer=answer_text,
            explanation=sub.get("explanation"),
            difficulty=_parse_difficulty(sub.inline.get("difficulty")),
            score=_parse_score(score_text),
            tags=_parse_tags(sub.inline.get("tags")),
        )
    except ValueError as exc:
        detail = str(exc).split("Value error, ")[-1].split(" [type=")[0].strip()
        errors.append(BlockError(sub.line_number, f"子题：{detail}"))
        return None, errors
    return draft, errors


def block_to_draft(block: QuestionBlock) -> tuple[QuestionDraft | None, list[BlockError]]:
    """单个块 → 草稿。失败返回 ``(None, 错误列表)``。"""
    errors: list[BlockError] = []

    def fail(message: str) -> tuple[None, list[BlockError]]:
        errors.append(BlockError(block.line_number, message, _excerpt(block)))
        return None, errors

    type_text = _as_text(_value(block, "type"))
    if not type_text:
        return fail("缺少【题型】")
    try:
        kind = parse_question_type(type_text)
    except ValueError as exc:
        return fail(str(exc))

    stem = _as_text(_value(block, "stem"))
    if not stem:
        return fail("缺少【题干】")

    payload, answer_text, payload_errors = _build_payload(block, kind, stem)
    if payload_errors:
        # 已经知道哪里不对了，不必再让 Pydantic 就同一个根因报第二遍
        # ——「没有解析出任何选项」后面紧跟一条「选择题至少需要 2 个选项」
        # 对用户是纯噪声
        errors.extend(
            BlockError(block.line_number, message, _excerpt(block)) for message in payload_errors
        )
        return None, errors

    sub_drafts: list[QuestionDraft] = []
    for sub_block in block.subs:
        sub_draft, sub_errors = _sub_to_draft(sub_block)
        errors.extend(sub_errors)
        if sub_draft is not None:
            sub_drafts.append(sub_draft)

    if kind is QuestionType.COMPREHENSIVE and not sub_drafts:
        return fail("综合应用题必须有至少一个【子题】")

    try:
        draft = QuestionDraft(
            type=kind,
            stem=stem,
            payload=payload,
            answer=answer_text,
            explanation=_as_text(_value(block, "explanation")),
            scoring_rubric=_as_text(_value(block, "rubric"))
            if kind is not QuestionType.SHORT_ANSWER
            else "",
            chapter=_as_text(_value(block, "chapter")),
            difficulty=_parse_difficulty(_value(block, "difficulty")),
            score=_parse_score(_value(block, "score")),
            est_minutes=_parse_minutes(_value(block, "est_minutes")),
            tags=_parse_tags(_value(block, "tags")),
            source=_as_text(_value(block, "source")),
            status=_parse_status(_value(block, "status")),
            external_id=_as_text(_value(block, "external_id")),
            sub_questions=tuple(sub_drafts),
        )
    except ValueError as exc:
        # Pydantic 的报错对终端用户太啰嗦，取最后一句（真正的原因）
        detail = str(exc).split("Value error, ")[-1].split(" [type=")[0].strip()
        return fail(detail)

    if errors:
        # payload 层面的错误（答案与标记冲突等）让这道题不进库，但错误已收集
        return None, errors
    return draft, errors


def _excerpt(block: QuestionBlock, limit: int = 60) -> str:
    """报错时附一段题干，方便用户在文件里定位。"""
    text = _as_text(_value(block, "stem")) or _as_text(_value(block, "type"))
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


def parse_document(text: str) -> ParseReport:
    """解析一整份文档。

    一道题失败不影响其余题目——一份文件里 50 道题有 1 道格式不对，不该让
    另外 49 道也进不来。
    """
    report = ParseReport()
    for block in split_blocks(text):
        draft, errors = block_to_draft(block)
        if draft is not None:
            report.drafts.append(draft)
        report.errors.extend(errors)
    return report
