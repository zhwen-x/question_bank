"""文档与实现的一致性测试。

文档是最容易腐烂的东西：实现换了，文档还停在上一版。这个项目已经发生过——
中文检索从 jieba 换成字符 n-gram 之后，README 还写着「jieba 分词」；
界面从未用过 HTMX，分层表里却列着它；`qbank doctor` 把「PDF 导出」报成
「降级」，而导出功能根本还没实现。

所以把「文档说了什么」变成可执行的断言。这里只检查**能自动验证**的部分：
链接是否有效、列出的标记/题型是否真被识别、引用的错误信息是否真会出现、
示例代码块是否真能解析、提到的命令是否真存在。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from qbank.core.enums import QuestionType, parse_question_type
from qbank.exchange.importers.loader import SUPPORTED_SUFFIXES
from qbank.exchange.importers.sections import canonical_label
from qbank.exchange.importers.template import parse_document

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
SPEC = ROOT / "docs" / "导入模板规范.md"
DESIGN = ROOT / "docs" / "设计说明.md"

#: 需要检查的全部文档
DOCS = [README, SPEC, DESIGN, ROOT / "docs" / "examples" / "导入模板示例.md"]

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*(\S*)\s*$")


def fenced_blocks(text: str) -> list[tuple[int, str]]:
    """按行跟踪围栏状态抽取代码块，返回 ``(起始行号, 内容)``。

    不能用正则整体匹配：规范里有四反引号包三反引号的嵌套块。
    """
    blocks: list[tuple[int, str]] = []
    fence: str | None = None
    buffer: list[str] = []
    start = 0
    for number, line in enumerate(text.split("\n"), 1):
        matched = _FENCE.match(line)
        if fence is None:
            if matched:
                fence, buffer, start = matched.group(1), [], number
        elif matched and matched.group(1)[0] == fence[0] and len(matched.group(1)) >= len(fence):
            blocks.append((start, "\n".join(buffer)))
            fence = None
        else:
            buffer.append(line)
    return blocks


def strip_code(text: str) -> str:
    """去掉围栏块与行内代码，避免把示例当成真实内容检查。"""
    without_fences = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", without_fences)


# ------------------------------------------------------------------ 链接
@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_links_resolve(doc: Path) -> None:
    """文档里的相对链接都要指向真实存在的文件。"""
    broken = []
    for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", strip_code(doc.read_text("utf-8"))):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        if not (doc.parent / target.split("#")[0]).resolve().exists():
            broken.append(f"[{label}]({target})")
    assert not broken, f"{doc.name} 里有断链：{broken}"


# ------------------------------------------------------- 规范列出的东西必须存在
def test_every_documented_marker_is_recognized() -> None:
    """「标记一览」表里的每个标记都要真能被解析器识别。"""
    table = SPEC.read_text("utf-8").split("### 标记一览")[1].split("### 标记名的同义写法")[0]
    markers = re.findall(r"`【([^】]+)】`", table)
    assert len(markers) >= 15, f"只从表里抽出 {len(markers)} 个标记，抽取逻辑可能失效了"
    unknown = [marker for marker in markers if canonical_label(marker) is None]
    assert not unknown, f"文档列了但解析器不认的标记：{unknown}"


def test_every_documented_alias_is_recognized() -> None:
    """「标记名的同义写法」里的每个别名都要真的等价。"""
    # 按真正的水平分割线切。不能用裸 "---"：Markdown 表格的分隔行里也有它
    section = SPEC.read_text("utf-8").split("### 标记名的同义写法")[1].split("\n---\n")[0]
    checked = 0
    for line in section.splitlines():
        if not line.strip().startswith("- "):
            continue
        names = [name.strip() for name in line.lstrip("- ").split("=")]
        canonical = {canonical_label(name) for name in names}
        assert len(canonical) == 1 and None not in canonical, f"这行的别名不等价：{line}"
        checked += 1
    assert checked >= 5, f"只检查了 {checked} 行别名，抽取逻辑可能失效了"


def test_every_documented_question_type_parses() -> None:
    """题型表里的每种写法都要能解析，且落到正确的题型上。"""
    table = SPEC.read_text("utf-8").split("## 三、题型")[1].split("\n---\n")[0]
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", table, re.M)
    labels = {kind.label: kind for kind in QuestionType}
    checked = 0
    for written, meaning in rows:
        if written.startswith("-") or written == "写":
            continue
        expected = labels.get(meaning.strip()) or labels.get(meaning.strip() + "题")
        if expected is None:
            continue
        for alias in (piece.strip() for piece in written.split("/")):
            assert parse_question_type(alias) is expected, f"「{alias}」没有解析成{meaning}"
            checked += 1
    assert checked >= 15, f"只检查了 {checked} 种写法，抽取逻辑可能失效了"


def test_documented_formats_are_supported() -> None:
    """规范里点名的文件格式都要真的能导入。"""
    mentioned = {
        suffix
        for suffix in re.findall(r"`(\.[a-z]+)`", SPEC.read_text("utf-8"))
        if suffix in SUPPORTED_SUFFIXES or suffix in {".pdf", ".doc"}
    }
    unsupported = mentioned - SUPPORTED_SUFFIXES
    assert not unsupported, f"规范提到但不支持的格式：{unsupported}"


# --------------------------------------------------- 引用的错误信息必须真会出现
@pytest.mark.parametrize(
    ("fragment", "source"),
    [
        ("缺少【题型】", "【题干】没有题型\n"),
        ("没有解析出任何选项", "【题型】单选\n【题干】甲\n【选项】\n【答案】A\n"),
        ("无法确定正确选项", "【题型】单选\n【题干】甲\n【选项】\nA. 一\nB. 二\n【答案】见解析\n"),
        ("无法识别为对或错", "【题型】判断\n【题干】甲\n【答案】不一定\n"),
        (
            "两处不一致，请核对源文件",
            "【题型】单选\n【题干】甲\n【选项】\nA. 一\n**B. 二**\n【答案】A\n",
        ),
        ("至少一个【子题】", "【题型】综合应用\n【题干】材料\n"),
    ],
)
def test_quoted_error_messages_really_occur(fragment: str, source: str) -> None:
    """规范里逐字引用的提示语，必须真的会出现——否则用户照着查也查不到。"""
    assert fragment in SPEC.read_text("utf-8"), f"规范里已经没有「{fragment}」了，测试该更新"
    messages = [error.message for error in parse_document(source).errors]
    assert any(fragment in message for message in messages), (
        f"规范引用了「{fragment}」，但实际报的是：{messages}"
    )


# ----------------------------------------------------- 示例代码块必须真能解析
def test_example_blocks_parse() -> None:
    """文档里的模板示例必须真能导进去——照着错的例子写是最糟的体验。"""
    checked = 0
    for doc in (SPEC, ROOT / "docs" / "examples" / "导入模板示例.md"):
        for line_number, block in fenced_blocks(doc.read_text("utf-8")):
            if "【题型】" not in block and not ("type:" in block and "###" in block):
                continue
            report = parse_document(block)
            assert report.drafts, f"{doc.name} 第 {line_number} 行起的示例解析不出题目"
            assert not report.errors, (
                f"{doc.name} 第 {line_number} 行起的示例有错：{[e.message for e in report.errors]}"
            )
            checked += 1
    assert checked >= 3, f"只检查了 {checked} 个示例块，抽取逻辑可能失效了"


def test_example_file_parses_cleanly() -> None:
    report = parse_document((ROOT / "docs" / "examples" / "导入模板示例.md").read_text("utf-8"))
    assert report.ok, [str(error) for error in report.errors]
    assert len(report.drafts) == 7, "示例文件应覆盖全部七种题型"
    assert {draft.type for draft in report.drafts} == set(QuestionType)


# ------------------------------------------------- README 不能描述不存在的东西
@pytest.mark.parametrize("term", ["jieba", "HTMX", "htmx"])
def test_readme_does_not_mention_removed_technology(term: str) -> None:
    """这些都曾写进 README，但实现里从未有过或已被替换掉。"""
    assert term not in README.read_text("utf-8"), f"README 还在提「{term}」，但实现里没有它"


def test_readme_marks_unbuilt_features_as_unbuilt() -> None:
    """组卷与导出尚未实现，README 必须说清楚，不能列成已有特性。"""
    text = README.read_text("utf-8")
    assert "还没做" in text, "README 需要一个明确的「还没做」清单"
    pending = text.split("还没做")[1][:400]
    for feature in ("组卷", "导出"):
        assert feature in pending, f"「{feature}」还没实现，应列在「还没做」里"


def test_readme_commands_exist() -> None:
    """README 里提到的 qbank 子命令都要真的存在。"""
    from qbank.cli import app

    registered = {command.name or command.callback.__name__ for command in app.registered_commands}
    registered |= {group.name for group in app.registered_groups}
    mentioned = set(re.findall(r"`qbank ([a-z]+)", README.read_text("utf-8")))
    missing = {name for name in mentioned if name.replace("_", "-") not in registered}
    assert not missing, f"README 提到但不存在的命令：{missing}；现有：{sorted(registered)}"


# -------------------------------------------- 设计说明里的数字要和代码一致
def test_design_notes_numbers_match_code() -> None:
    """设计说明引用了阈值等常量，改了代码要记得同步。"""
    from qbank.core.content.segment import MAX_NGRAM
    from qbank.core.services.dedup import SHINGLE_SIZE, SIMILARITY_THRESHOLD

    text = DESIGN.read_text("utf-8")
    assert str(SIMILARITY_THRESHOLD) in text, f"相似度阈值已改为 {SIMILARITY_THRESHOLD}"
    assert f"{SHINGLE_SIZE} 元组" in text, f"shingle 大小已改为 {SHINGLE_SIZE}"
    assert MAX_NGRAM == 2 and "一元 + 二元" in text, "n-gram 方案已变，设计说明要同步"


def test_design_notes_diff_examples_are_real() -> None:
    """设计说明里展示的差异高亮效果必须和实际输出一致。"""
    from qbank.core.content.diff import Change, inline_diff

    def rendered(left: str, right: str) -> str:
        pieces = []
        for segment in inline_diff(left, right):
            if segment.change is Change.DELETE:
                pieces.append(f"[-{segment.text}-]")
            elif segment.change is Change.INSERT:
                pieces.append(f"{{+{segment.text}+}}")
            else:
                pieces.append(segment.text)
        return "".join(pieces)

    text = DESIGN.read_text("utf-8")
    for left, right in [
        ("下列哪些属于线性结构？", "下列哪些属于非线性结构？"),
        ("栈是一种先进先出的线性表。", "栈是一种后进先出的线性表。"),
    ]:
        assert rendered(left, right) in text, (
            f"设计说明里的示例与实际不符，实际是：{rendered(left, right)}"
        )
