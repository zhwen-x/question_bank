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

import importlib.util
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from qbank.core.enums import QuestionType, parse_question_type
from qbank.exchange.importers.loader import SUPPORTED_SUFFIXES
from qbank.exchange.importers.sections import canonical_label
from qbank.exchange.importers.template import parse_document

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
SPEC = ROOT / "docs" / "导入模板规范.md"
DESIGN = ROOT / "docs" / "设计说明.md"
BLUEPRINT_SPEC = ROOT / "docs" / "组卷蓝图规范.md"
BLUEPRINT_EXAMPLE = ROOT / "docs" / "examples" / "组卷蓝图示例.yaml"
TEMPLATE_GUIDE = ROOT / "docs" / "导出模板制作指南.md"
PACKAGING_GUIDE = ROOT / "docs" / "打包分发指南.md"
DEPLOY_GUIDE = ROOT / "docs" / "Web部署指南.md"
CHANGELOG = ROOT / "CHANGELOG.md"

#: 需要检查的全部文档
DOCS = [
    README,
    SPEC,
    DESIGN,
    BLUEPRINT_SPEC,
    TEMPLATE_GUIDE,
    PACKAGING_GUIDE,
    DEPLOY_GUIDE,
    CHANGELOG,
    ROOT / "docs" / "examples" / "导入模板示例.md",
]

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
def _heading_slugs(markdown: str) -> set[str]:
    """标题 → GitHub 风格锚点（只保留字母数字与中日韩，其余去掉）。"""
    return {
        re.sub(r"[^\w\u4e00-\u9fff]", "", heading).lower()
        for heading in re.findall(r"^#+\s*(.+?)\s*$", markdown, re.M)
    }


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_links_resolve(doc: Path) -> None:
    """相对链接要指向真实存在的文件，**而且锚点也要真的在**。

    只查文件存在是不够的：`设计说明.md#八还没做的` 这个链接在章节从「八」
    重编成「十」之后依然指向一个存在的文件，却跳不到任何地方——而读者点下去
    只会落到文档开头，完全不知道自己错过了什么。
    """
    broken = []
    for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", strip_code(doc.read_text("utf-8"))):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path, _, anchor = target.partition("#")
        # 文件名里有空格时链接会写成 %20，渲染器会解码，路径检查也得解码
        resolved = (doc.parent / unquote(path)).resolve()
        if not resolved.exists():
            broken.append(f"[{label}]({target}) → 文件不存在")
        elif anchor and resolved.suffix == ".md":
            slug = anchor.replace("-", "").lower()
            if slug not in _heading_slugs(resolved.read_text("utf-8")):
                broken.append(f"[{label}]({target}) → 目标文件里没有这个标题")
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


def _pending_section() -> str:
    text = README.read_text("utf-8")
    assert "还没做" in text, "README 需要一个明确的「还没做」清单"
    return text.split("还没做")[1][:400]


def test_readme_pending_list_tracks_the_code() -> None:
    """「还没做」清单的两头都要跟着代码走。

    不写死「组卷还没做」——那正是上一次文档腐烂的形态（实现换了，断言还停在
    上一版）。这里从代码反推：有没有 ``qbank paper`` 命令、``exporters`` 包里
    有没有东西，决定了 README 该怎么写。M5 把导出做出来时，这条会反过来要求
    把「导出」从清单里删掉。
    """
    from qbank.cli import app

    groups = {group.name for group in app.registered_groups}
    pending = _pending_section()

    has_paper_commands = "paper" in groups
    assert has_paper_commands, "组卷命令没了？那 README 的说法也要跟着改"
    assert "组卷" not in pending, "`qbank paper` 已经存在，组卷不该还列在「还没做」里"

    exporters = ROOT / "src" / "qbank" / "exchange" / "exporters"
    has_exporters = any(p.stem != "__init__" for p in exporters.glob("*.py"))
    assert has_exporters, "导出模块没了？那 README 的说法也要跟着改"
    assert "导出" not in pending, "导出已经实现了，不该还列在「还没做」里"


@pytest.mark.parametrize("feature", ["组卷", "试卷", "蓝图"])
def test_readme_documents_assembly(feature: str) -> None:
    """组卷已经可用，README 的「当前状态」里要有它。"""
    status = README.read_text("utf-8").split("## 当前状态")[1].split("## 安装")[0]
    assert feature in status, f"「{feature}」已经能用了，当前状态里却没提"


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


# ---------------------------------------------------- 组卷蓝图规范
def _spec_table_fields(heading: str) -> set[str]:
    """抽出规范里某张字段表的第一列（表格里写成行内代码的字段名）。"""
    table = BLUEPRINT_SPEC.read_text("utf-8").split(heading)[1].split("\n## ")[0]
    return set(re.findall(r"^\| `([a-z_]+)`", table, re.M))


def test_documented_blueprint_fields_exist() -> None:
    """顶层字段表里的每个字段都要真的存在于 Blueprint 上。"""
    from qbank.core.blueprint import Blueprint

    documented = _spec_table_fields("## 顶层字段")
    assert len(documented) >= 8, f"表没解析出来，只拿到 {documented}"
    unknown = documented - set(Blueprint.model_fields)
    assert not unknown, f"规范里写了但 Blueprint 没有的字段：{unknown}"


def test_documented_section_fields_exist() -> None:
    from qbank.core.blueprint import SectionSpec

    documented = _spec_table_fields("## 大题字段")
    assert len(documented) >= 10, f"表没解析出来，只拿到 {documented}"
    unknown = documented - set(SectionSpec.model_fields)
    assert not unknown, f"规范里写了但 SectionSpec 没有的字段：{unknown}"


def test_every_blueprint_field_is_documented() -> None:
    """反过来也要查：新加了字段却忘了写进规范，文档就开始腐烂了。"""
    from qbank.core.blueprint import Blueprint, SectionSpec

    missing_top = set(Blueprint.model_fields) - _spec_table_fields("## 顶层字段")
    missing_section = set(SectionSpec.model_fields) - _spec_table_fields("## 大题字段")
    assert not missing_top, f"Blueprint 有但规范没写的字段：{missing_top}"
    assert not missing_section, f"SectionSpec 有但规范没写的字段：{missing_section}"


def test_documented_question_type_values_are_real() -> None:
    """规范里列的题型英文值要和枚举对得上。"""
    text = BLUEPRINT_SPEC.read_text("utf-8")
    listed = set(re.findall(r"`(single_choice|[a-z_]+)`", text.split("题型写")[1].split("。")[0]))
    assert listed == {kind.value for kind in QuestionType}


def test_blueprint_example_parses_and_adds_up() -> None:
    """示例蓝图必须真的能用——它是用户最可能直接拿去改的那份。"""
    import yaml

    from qbank.core.blueprint import Blueprint

    blueprint = Blueprint.model_validate(yaml.safe_load(BLUEPRINT_EXAMPLE.read_text("utf-8")))
    assert blueprint.total_score == 100
    assert blueprint.question_count == 34
    for section in blueprint.sections:
        quotas = section.quotas()
        assert sum(count for _, count in quotas) == section.count


def test_blueprint_snippets_in_docs_actually_validate() -> None:
    """文档里的蓝图片段必须真的能用——它们就是用户要复制去改的那几段。

    这条第一次跑就抓到 README 里的片段声明了 100 分而各节配分之和是 20，
    照抄下来会被校验拦住。
    """
    import yaml

    from qbank.core.blueprint import Blueprint

    checked = 0
    for doc in (BLUEPRINT_SPEC, README):
        for line, block in fenced_blocks(doc.read_text("utf-8")):
            if "title:" not in block or "sections:" not in block:
                continue
            data = yaml.safe_load(block)
            assert isinstance(data, dict), f"{doc.name} 第 {line} 行的示例不是一组键值对"
            Blueprint.model_validate(data)
            checked += 1
    assert checked >= 2, f"只检查了 {checked} 段 YAML，抽取逻辑可能失效了"


def test_quoted_shortage_report_matches_the_real_wording() -> None:
    """规范里抄的那段缺额报告，用词要和代码真的会输出的一致。"""
    from qbank.core.services.selection import Shortage

    text = BLUEPRINT_SPEC.read_text("utf-8")
    alone = Shortage(label="X", needed=8, filled=2, available=2).describe()
    together = Shortage(label="X", needed=4, filled=3, available=5, competing=("Y",)).describe()
    for fragment in ("只排进", "库内符合条件的只有", "是同一批题", "不够几节分"):
        assert fragment in alone + together
        assert fragment in text, f"规范里的报告示例与实际用词不符，缺「{fragment}」"


# ---------------------------------------------- 导出模板制作指南
def test_documented_template_files_are_the_real_ones() -> None:
    """指南里写的文件名要和代码里找的那几个一致。"""
    from qbank.exchange.exporters.templates import (
        DESCRIPTION,
        HTML_TEMPLATE,
        MD_TEMPLATE,
        REFERENCE_DOCX,
    )

    text = TEMPLATE_GUIDE.read_text("utf-8")
    for name in (MD_TEMPLATE, HTML_TEMPLATE, REFERENCE_DOCX, DESCRIPTION):
        assert f"`{name}`" in text, f"指南里没写 {name}"


def test_documented_context_fields_exist() -> None:
    """指南里列的上下文字段必须真的能在模板里取到。

    这张表是模板作者唯一的参考；列了一个不存在的字段，他写出来的模板会在
    StrictUndefined 下当场炸掉。
    """
    from qbank.exchange.exporters.context import ItemContext, PaperContext, SectionContext

    text = TEMPLATE_GUIDE.read_text("utf-8")
    owners = {"paper": PaperContext, "section": SectionContext, "item": ItemContext}
    checked = 0
    for owner, field in re.findall(r"`(paper|section|item)\.([a-z_]+)`", text):
        target = owners[owner]
        names = set(getattr(target, "__annotations__", {})) | set(dir(target))
        assert field in names, f"指南里写了 {owner}.{field}，但 {target.__name__} 上没有"
        checked += 1
    assert checked >= 20, f"只检查了 {checked} 个字段，抽取逻辑可能失效了"


def test_documented_filters_exist() -> None:
    """指南里列的三个过滤器要真的注册了。"""
    from qbank.exchange.exporters.templates import (
        BUILTIN_DIR,
        ExportFormat,
        PaperTemplate,
        make_environment,
    )

    template = PaperTemplate(key="standard", directory=BUILTIN_DIR / "standard", builtin=True)
    environment = make_environment(template, ExportFormat.HTML)
    text = TEMPLATE_GUIDE.read_text("utf-8")
    for name in ("md", "g", "cn"):
        assert name in environment.filters, f"指南里写了 | {name} 过滤器，但没注册"
        assert f"| `{name}` |" in text


def test_documented_template_blocks_exist() -> None:
    """指南里说可以 override 的块，内置模板里要真的有。"""
    html = (
        ROOT / "src" / "qbank" / "templates" / "paper" / "standard" / "paper.html.j2"
    ).read_text("utf-8")
    markdown = (
        ROOT / "src" / "qbank" / "templates" / "paper" / "standard" / "paper.md.j2"
    ).read_text("utf-8")
    for block in (
        "page_size",
        "page_margin",
        "body_size",
        "print_size",
        "title_size",
        "styles",
        "extra_styles",
        "header",
        "body",
        "answer_key",
    ):
        assert f"{{% block {block} %}}" in html, f"html 模板里没有 {block} 块"
    for block in ("header", "body", "answer_key"):
        assert f"{{% block {block} %}}" in markdown, f"md 模板里没有 {block} 块"


def test_preset_templates_match_the_readme() -> None:
    """README 列的三套模板要和内置的对得上。"""
    from qbank.exchange.exporters.templates import discover

    builtin = {key for key, item in discover().items() if item.builtin}
    assert builtin == {"standard", "exam", "compact"}
    table = README.read_text("utf-8").split("## 导出")[1]
    for key in builtin:
        assert f"`{key}`" in table, f"README 的模板表里没有 {key}"


def test_documented_export_formats_match_the_enum() -> None:
    from qbank.exchange.exporters.templates import ExportFormat

    table = README.read_text("utf-8").split("## 导出")[1].split("| 模板 |")[0]
    for fmt in ExportFormat:
        assert f"**{fmt.value}**" in table, f"README 的格式表里没有 {fmt.value}"


def test_quoted_template_errors_match_the_real_wording() -> None:
    """指南里抄的两段报错要和程序真的会说的话一致。

    「差不多」不行：读者遇到问题时是拿报错去文档里搜的，搜不到就等于没写。
    StrictUndefined 那条尤其容易抄短——Jinja 用的是完整限定名。
    """
    import tempfile

    from jinja2 import UndefinedError

    from qbank.config import Paths
    from qbank.exchange.exporters.context import ItemContext
    from qbank.exchange.exporters.templates import (
        BUILTIN_DIR,
        ExportFormat,
        PaperTemplate,
        TemplateNotFoundError,
        get_template,
        make_environment,
    )

    guide = TEMPLATE_GUIDE.read_text("utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        paths = Paths(root=Path(tmp)).ensure()
        half = paths.templates_dir / "paper" / "我的"
        half.mkdir(parents=True)
        (half / "paper.html.j2").write_text("x", encoding="utf-8")
        with pytest.raises(TemplateNotFoundError) as excinfo:
            get_template("我的", ExportFormat.DOCX, paths=paths)
    assert str(excinfo.value) in guide, "「模板缺少…」的报错措辞和文档对不上"

    template = PaperTemplate(key="standard", directory=BUILTIN_DIR / "standard", builtin=True)
    environment = make_environment(template, ExportFormat.HTML)
    item = ItemContext(
        number=1, index=1, score=1, type_label="单选", is_objective=True, difficulty=3, stem="x"
    )
    with pytest.raises(UndefinedError) as undefined:
        environment.from_string("{{ item.scores }}").render(item=item)
    assert str(undefined.value) in guide, "StrictUndefined 的报错措辞和文档对不上"


def test_documented_block_defaults_match_the_template() -> None:
    """块默认值表里的数字要和模板里写的一致——改了字号忘了改文档是常事。"""
    from qbank.exchange.exporters.templates import BUILTIN_DIR

    html = (BUILTIN_DIR / "standard" / "paper.html.j2").read_text("utf-8")
    actual = dict(re.findall(r"\{% block (\w+) %\}([^{]*)\{% endblock %\}", html))
    table = TEMPLATE_GUIDE.read_text("utf-8").split("预留的块")[1].split("\n## ")[0]
    checked = 0
    for block in ("page_size", "page_margin", "body_size", "print_size", "title_size"):
        value = actual.get(block, "").strip()
        assert value, f"模板里 {block} 没有默认值"
        assert f"`{value}`" in table, f"{block} 的默认值是 {value}，文档里没写这个数"
        checked += 1
    assert checked == 5


# ------------------------------------------------------------ 变更记录
def _pending_topics(text: str) -> set[str]:
    """把一份「还没做」清单归一成话题集合。

    三处的措辞不同——设计说明会多带一段括号解释——所以只比每条的**开头**，
    即第一个全角括号或逗号之前的部分。
    """
    return {
        re.split(r"[（，]", item.strip())[0].strip() for item in re.findall(r"^- (.+)$", text, re.M)
    }


def test_the_three_pending_lists_agree() -> None:
    """「还没做」清单写在三个地方，必须是同一份。

    各写一份的下场是其中一处先腐烂，而读者不知道该信哪个。审计时实际撞上的就是
    这个：README 漏了「限流」那条，而原先的断言只硬编码比对了两个关键词，
    正好绕过了它。所以这里改成整份比对。
    """
    lists = {
        "README.md": _pending_topics(_pending_section()),
        "CHANGELOG.md": _pending_topics(
            CHANGELOG.read_text("utf-8").split("### 还没做")[1].split("\n---")[0]
        ),
        "docs/设计说明.md": _pending_topics(
            DESIGN.read_text("utf-8").split("## 十二、还没做的")[1]
        ),
    }
    for label, topics in lists.items():
        assert len(topics) >= 3, f"{label} 的清单里只抽到 {topics}，抽取逻辑可能失效了"

    reference = lists["README.md"]
    mismatched = {
        label: sorted(topics ^ reference) for label, topics in lists.items() if topics != reference
    }
    assert not mismatched, "三处「还没做」清单对不上（以 README 为准）：\n  " + "\n  ".join(
        f"{label} 多出或缺少：{diff}" for label, diff in mismatched.items()
    )


def test_finished_work_is_not_still_listed_as_pending() -> None:
    """已经做完的不该还挂在任何一处清单里。"""
    everywhere = (
        _pending_section()
        + CHANGELOG.read_text("utf-8").split("### 还没做")[1].split("\n---")[0]
        + DESIGN.read_text("utf-8").split("## 十二、还没做的")[1]
    )
    for done in ("qbz", "pywebview", "Postgres", "认证接入点", "部署文档"):
        assert done not in everywhere, f"「{done}」已经做完了，从未完成清单里删掉"


def test_changelog_covers_every_milestone() -> None:
    """每个已完成的里程碑都要有一节。漏记一个版本等于那段历史没人写。"""
    text = CHANGELOG.read_text("utf-8")
    for milestone in ("M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7"):
        assert f"## {milestone} " in text, f"变更记录里缺 {milestone}"


# ---------------------------------------------- 打包分发指南
def test_packaging_guide_matches_the_spec() -> None:
    """指南里说捆了什么，spec 里就得真的捆了什么。"""
    guide = PACKAGING_GUIDE.read_text("utf-8")
    spec = (ROOT / "packaging" / "qbank.spec").read_text("utf-8")

    assert "QBANK_BUNDLE_PDF" in guide
    assert "QBANK_BUNDLE_PDF" in spec, "指南说有这个开关，spec 里却没有"
    assert "console=False" in spec
    assert "console=False" in guide, "指南提到不要做成 console 程序，要和 spec 对得上"
    assert "NSAllowsLocalNetworking" in spec
    assert "NSAllowsLocalNetworking" in guide


def test_packaging_guide_lists_the_collected_packages() -> None:
    """指南里点名的那几个包，spec 里要真的收了它们的数据文件。"""
    guide = PACKAGING_GUIDE.read_text("utf-8")
    spec = (ROOT / "packaging" / "qbank.spec").read_text("utf-8")
    listed = set(re.findall(r"`(qbank|pypandoc|latex2mathml|webview|alembic|markdown_it)`", guide))
    assert len(listed) >= 5, f"指南里只点了 {listed}，抽取逻辑可能失效了"
    for package in listed:
        assert f'collect_data_files("{package}"' in spec, f"指南说收了 {package}，spec 里没有"


def test_entry_point_keeps_the_cli_reachable() -> None:
    """打包版必须还能当命令行用——否则 doctor 和 bundle export 都够不着。"""
    entry = (ROOT / "packaging" / "entry.py").read_text("utf-8")
    assert "qbank.cli" in entry
    assert "sys.argv" in entry
    assert "doctor" in PACKAGING_GUIDE.read_text("utf-8")


# ---------------------------------------------- Web 部署指南
def test_deploy_guide_documents_every_environment_variable() -> None:
    """代码里认的每个 ``QBANK_*`` 变量，部署指南的速查表里都要有。

    漏掉一个的后果不是「文档不全」，而是那个开关实际上不存在——没人知道可以设它。
    """
    from qbank import config
    from qbank.web import auth

    known = {
        value
        for module in (config, auth)
        for name, value in vars(module).items()
        if name.startswith("ENV_") and isinstance(value, str)
    }
    assert len(known) >= 8, f"只收集到 {len(known)} 个环境变量常量，抽取逻辑可能失效了"

    guide = DEPLOY_GUIDE.read_text("utf-8")
    missing = sorted(name for name in known if f"`{name}`" not in guide)
    assert not missing, f"部署指南没写这些环境变量：{missing}"


def test_deploy_guide_lists_only_real_environment_variables() -> None:
    """反过来：指南里写的变量必须真的被读。写了个不存在的开关比没写更坏。"""
    from qbank import config
    from qbank.web import auth

    known = {
        value
        for module in (config, auth)
        for name, value in vars(module).items()
        if name.startswith("ENV_") and isinstance(value, str)
    }
    # 测试用的那个不进部署指南，它只在 pytest 里有意义
    known.add("QBANK_TEST_DATABASE_URL")

    mentioned = set(re.findall(r"`(QBANK_[A-Z_]+)`", DEPLOY_GUIDE.read_text("utf-8")))
    assert mentioned, "指南里一个环境变量都没抽到，抽取逻辑可能失效了"
    unknown = sorted(mentioned - known)
    assert not unknown, f"指南里写了但代码不认的变量：{unknown}"


def test_deploy_guide_auth_modes_match_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """指南里列的三种模式，``policy_from_env`` 要真的都认。"""
    from qbank.web.auth import ENV_MODE, AuthError, policy_from_env

    modes = set(re.findall(r"### `(\w+)`——", DEPLOY_GUIDE.read_text("utf-8")))
    assert modes == {"open", "password", "header"}, f"指南里讲的模式是 {modes}"

    for mode in sorted(modes):
        monkeypatch.setenv(ENV_MODE, mode)
        try:
            policy_from_env()
        except AuthError as exc:
            # 缺配置而报错是对的（password 没给口令就该拒绝启动）；
            # 要挡住的是「指南写了一个代码根本不认的模式名」
            assert "不是已知模式" not in str(exc), f"指南写了 {mode}，代码却不认"


def test_deploy_guide_upload_limit_matches_the_code() -> None:
    """指南里的上传上限是个具体数字，改了代码要跟着改。"""
    from qbank.web.staging import MAX_UPLOAD_BYTES

    megabytes = MAX_UPLOAD_BYTES // 1024 // 1024
    assert f"{megabytes} MB" in DEPLOY_GUIDE.read_text("utf-8"), (
        f"代码里的上传上限是 {megabytes} MB，指南里写的不是这个数"
    )


# -------------------------------- 代码注释里的「还没做」必须真的还没做
#: 声称某样东西没做的说法
_UNIMPLEMENTED = re.compile(r"尚未实现|还没实现|还未实现|都没有实现")

SRC = ROOT / "src" / "qbank"


def test_unimplemented_claims_in_code_are_still_true() -> None:
    """注释里说「还没做」的东西，必须真的还没做。

    这条是 M7 的文档审计逼出来的：M6 把 ``.qbz`` 交换包和桌面外壳都做完了，
    而 ``__init__.py`` 与 ``config.py`` 里还写着「尚未实现」——新人照着读会以为
    备份功能不存在，而那恰恰是这个项目唯一的备份手段。

    注释比文档更容易腐烂：改功能的人不会顺手去读隔壁模块的模块级 docstring。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if not _UNIMPLEMENTED.search(line):
                continue
            where = f"{path.relative_to(SRC)}:{number}"
            for module in re.findall(r"qbank\.([a-z_]+(?:\.[a-z_]+)*)", line):
                target = SRC / Path(module.replace(".", "/"))
                if target.is_dir() or target.with_suffix(".py").is_file():
                    offenders.append(f"{where} 说 qbank.{module} 还没做，但它已经在了")
            if ".qbz" in line and (SRC / "exchange" / "bundle.py").is_file():
                offenders.append(f"{where} 说 .qbz 还没做，但 exchange/bundle.py 已经在了")
    assert not offenders, "注释已经腐烂：\n  " + "\n  ".join(offenders)


# -------------------------------------------------- README 对鉴权的说法
def test_readme_does_not_claim_there_is_no_authentication() -> None:
    """M7 之后「还没有鉴权」是一句假话，而它恰好出现在最显眼的地方。

    不是简单禁掉「不鉴权」三个字——默认模式确实不鉴权，那句话是对的。
    要挡的是「这个系统做不到鉴权」这个意思。
    """
    from qbank.web.auth import ENV_MODE, SharedPassword, TrustedHeader

    assert SharedPassword and TrustedHeader, "鉴权策略没了？那 README 的说法也要跟着改"

    readme = README.read_text("utf-8")
    for stale in ("还没有鉴权", "尚无鉴权", "还没有任何鉴权", "暂时没有鉴权"):
        assert stale not in readme, f"README 里还写着「{stale}」，而鉴权已经做了"
    assert ENV_MODE in readme, f"README 没提 {ENV_MODE}，读者不会知道鉴权怎么开"


# ------------------------------------------- 设计说明里的 JS 规模
def test_design_notes_js_size_matches_reality() -> None:
    """界面脚本的行数是个会悄悄长大的数字。

    审计时实际撞上的是更糟的一种：同一段里先写「约 400 行」，又写「一共一百多行」
    ——两句话互相打架，读者没法判断该信哪句。
    """
    source = (SRC / "web" / "static" / "app.js").read_text("utf-8").splitlines()
    code_lines = sum(
        1 for line in source if line.strip() and not line.strip().startswith(("/*", "*", "//"))
    )

    claimed = re.search(r"约 (\d+) 行原生 JS", DESIGN.read_text("utf-8"))
    assert claimed, "设计说明里找不到 JS 行数的说法，抽取逻辑可能失效了"
    stated = int(claimed.group(1))
    assert abs(stated - code_lines) <= 100, (
        f"设计说明说约 {stated} 行，实际 {code_lines} 行（不含空行与注释）"
    )


# ------------------------------------------- README 里的 doctor 检查项
def test_readme_doctor_row_lists_real_checks() -> None:
    """README 那一行里点的名字，都要对得上真实的检查项。"""
    from qbank.doctor import run_checks

    real = {check.name for check in run_checks()}
    row = re.search(r"`qbank doctor` \| 环境自检：([^|]+)\|", README.read_text("utf-8"))
    assert row, "README 的命令一览里找不到 doctor 那一行"

    listed = [part.strip() for part in re.split(r"[、，与]", row.group(1)) if part.strip()]
    assert len(listed) >= 4, f"只抽出 {listed}，抽取逻辑可能失效了"
    phantom = [name for name in listed if not any(name in actual for actual in real)]
    assert not phantom, (
        f"README 点了这些检查项，但 doctor 里没有：{phantom}；实际有：{sorted(real)}"
    )


# ------------------------------------------- 部署指南里的具体数字
def test_deploy_guide_session_lifetime_matches_the_code() -> None:
    """会话有效期是部署指南承诺给用户的数字。"""
    import inspect

    from qbank.web.app import _install_access_control

    source = inspect.getsource(_install_access_control)
    hours = int(re.search(r"max_age=(\d+) \* 3600", source).group(1))  # type: ignore[union-attr]
    assert f"{hours} 小时" in DEPLOY_GUIDE.read_text("utf-8"), (
        f"代码里会话有效期是 {hours} 小时，部署指南里写的不是这个数"
    )


@pytest.mark.skipif(
    importlib.util.find_spec("psycopg2") is not None,
    reason="装了 psycopg2 就触发不了这条错误",
)
def test_missing_driver_error_matches_the_guide() -> None:
    """指南里引用的报错要和真实报错一字不差——否则搜不到。"""
    from qbank.core.db import make_engine

    with pytest.raises(ModuleNotFoundError) as caught:
        make_engine("postgresql+psycopg2://localhost/x")
    quoted = f"`ModuleNotFoundError: {caught.value}`"
    assert quoted in DEPLOY_GUIDE.read_text("utf-8"), (
        f"指南里引用的报错和真实的对不上。真实的是：{quoted}"
    )


def test_every_documented_command_exists_in_every_doc() -> None:
    """所有文档（不只 README）里提到的 qbank 子命令都要真的存在。

    原先只查了 README。部署指南里的命令全写在 ```bash 围栏里，而围栏里的命令
    才是读者会直接复制去执行的那些——写错一个，对方得到的是「No such command」。

    两种来源都收：行内的 `qbank X`，以及 bash 围栏里行首的 qbank X。只认行首是
    为了避开 `createdb --owner=qbank qbank` 这类把 qbank 当库名的误配。
    """
    from qbank.cli import app

    registered = {command.name or command.callback.__name__ for command in app.registered_commands}
    registered |= {group.name for group in app.registered_groups}

    missing: dict[str, set[str]] = {}
    for doc in DOCS:
        text = doc.read_text("utf-8")
        mentioned = set(re.findall(r"`qbank ([a-z]+)", text))
        for _, block in fenced_blocks(text):
            mentioned |= set(re.findall(r"^\s*qbank ([a-z]+)", block, re.M))
        unknown = {name for name in mentioned if name.replace("_", "-") not in registered}
        if unknown:
            missing[doc.name] = unknown
    assert not missing, f"文档里提到但不存在的命令：{missing}；现有：{sorted(registered)}"


def test_tests_named_in_docs_really_exist() -> None:
    """文档里点名的测试文件与测试函数都要真的在。

    文档好几处用「这条有测试守着：``test_xxx``」来支撑一个论断。测试改个名字
    是重构时最顺手的事，而那一刻论断就失去了依据——读者去找会扑空，更糟的是
    论断本身可能也已经不成立了。
    """
    tests_root = ROOT / "tests"
    defined = {
        name
        for path in tests_root.rglob("test_*.py")
        for name in re.findall(r"^def (test_\w+)", path.read_text("utf-8"), re.M)
    }
    assert len(defined) > 300, f"只收集到 {len(defined)} 个测试函数，抽取逻辑可能失效了"

    missing: list[str] = []
    for doc in DOCS:
        text = doc.read_text("utf-8")
        for relative in re.findall(r"`?(tests/[\w/]+\.py)", text):
            if not (ROOT / relative).is_file():
                missing.append(f"{doc.name} → {relative} 不存在")
        # 先把文件路径整段摘掉再找函数名，否则 tests/unit/test_layering.py 里的
        # test_layering 会被当成一个不存在的测试函数
        prose = re.sub(r"tests/[\w/]+\.py", lambda m: m.group(0).rsplit("/", 1)[0], text)
        for name in re.findall(r"\b(test_\w{8,})", prose):
            if name not in defined:
                missing.append(f"{doc.name} → {name}() 不存在")
    assert not missing, "文档点名了不存在的测试：\n  " + "\n  ".join(missing)
