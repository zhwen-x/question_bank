"""导入的端到端测试。

M2 的验收标准：一份含公式、图片、表格的 Word 文件能导入，且公式以 LaTeX 形式
入库。这里用 python-docx 现场构造带 **OMML 公式**的 docx——这是整条链路里风险
最高的一环（Word 的公式是 OMML XML，必须由 pandoc 转成 LaTeX），用静态 fixture
反而掩盖问题。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from qbank.core.enums import ConflictPolicy, QuestionType
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.asset import AssetService
from qbank.core.services.question import QuestionService
from qbank.exchange import pandoc
from qbank.exchange.import_service import ImportService, plan_file
from qbank.exchange.importers.loader import read_markdown_text, read_source
from qbank.exchange.importers.template import parse_document

pytestmark = pytest.mark.pandoc

MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

#: ``x²`` 的 OMML。Word 的公式就是这种结构，pandoc 应把它转成 ``x^{2}``。
OMML_SUPERSCRIPT = (
    "<m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup>"
)
#: ``n(n+1)/2`` 的 OMML
OMML_FRACTION = (
    "<m:f><m:num><m:r><m:t>n</m:t></m:r><m:r><m:t>+</m:t></m:r>"
    "<m:r><m:t>1</m:t></m:r></m:num><m:den><m:r><m:t>2</m:t></m:r></m:den></m:f>"
)


def png_bytes(size: tuple[int, int] = (160, 100)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (70, 110, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def require_pandoc() -> None:
    if not pandoc.is_available():
        pytest.skip("pandoc 不可用")


@pytest.fixture
def word_file(tmp_path: Path) -> Path:
    """构造一份真实的 Word 题库文件：OMML 公式 + 内嵌图片 + 合并单元格表格。"""
    from docx import Document
    from docx.shared import Inches
    from lxml import etree

    image = tmp_path / "fig.png"
    image.write_bytes(png_bytes())

    def omath(inner: str) -> object:
        return etree.fromstring(f'<m:oMath xmlns:m="{MATH_NS}">{inner}</m:oMath>')

    document = Document()
    document.add_paragraph("【题型】单选  【章节】第3章 函数/3.1 二次函数  【难度】2  【分值】2")
    document.add_paragraph("【标签】二次函数,最值    【编号】word-01")
    paragraph = document.add_paragraph("【题干】函数 f(x) = ")
    paragraph._p.append(omath(OMML_SUPERSCRIPT))
    paragraph.add_run(" + 2x + 1 的最小值是？见下图：")
    document.add_picture(str(image), width=Inches(1.5))

    table = document.add_table(rows=2, cols=3)
    table.style = "Table Grid"
    table.rows[0].cells[0].text = "阶段"
    table.rows[0].cells[1].merge(table.rows[0].cells[2])
    table.rows[0].cells[1].text = "取值"
    table.rows[1].cells[0].text = "顶点"
    table.rows[1].cells[1].text = "-1"
    table.rows[1].cells[2].text = "0"

    document.add_paragraph("【选项】")
    document.add_paragraph("A. 1")
    document.add_paragraph("*B. 0")
    document.add_paragraph("C. -1")
    document.add_paragraph("【答案】B")
    paragraph = document.add_paragraph("【解析】配方得 ")
    paragraph._p.append(omath(OMML_FRACTION))
    paragraph.add_run(" 处取得最小值。")

    document.add_paragraph("【题型】判断  【难度】1  【分值】1  【编号】word-02")
    document.add_paragraph("【题干】二次函数的图像是一条直线。")
    document.add_paragraph("【答案】错误")

    path = tmp_path / "题库.docx"
    document.save(str(path))
    return path


@pytest.fixture
def course_id(session: Session) -> str:
    return CourseRepo(session).create("高等数学").id


# ------------------------------------------------------- Word 的公式与图片
def test_word_equations_become_latex(word_file: Path) -> None:
    """最关键的一条：Word 的 OMML 公式必须变成 LaTeX，否则公式全丢。"""
    with read_source(word_file) as document:
        report = parse_document(document.markdown)
    assert report.ok, [str(e) for e in report.errors]
    stem = report.drafts[0].stem
    assert "x^{2}" in stem, f"OMML 没有转成 LaTeX：{stem!r}"
    assert "$" in stem, "公式应被 $ 包裹"
    assert "\\frac" in report.drafts[0].explanation


def test_word_merged_table_survives(word_file: Path) -> None:
    """试卷里合并单元格的表格很常见，丢了就是实质性的内容损失。"""
    with read_source(word_file) as document:
        report = parse_document(document.markdown)
    assert 'colspan="2"' in report.drafts[0].stem


def test_word_bold_marker_is_recognized(word_file: Path) -> None:
    draft = next(d for d in parse_source(word_file) if d.type is QuestionType.SINGLE_CHOICE)
    assert [o.label for o in draft.payload.options if o.is_correct] == ["B"]


def parse_source(path: Path) -> list:
    with read_source(path) as document:
        return parse_document(document.markdown).drafts


# ------------------------------------------------------------- 完整导入
def test_import_word_end_to_end(session: Session, course_id: str, word_file: Path) -> None:
    plan, document = plan_file(session, course_id, word_file)
    try:
        assert plan.total_parsed == 2
        assert plan.exact_duplicate_count == 0
        assert plan.image_count == 1
        result = ImportService(session).commit(plan, policy=ConflictPolicy.SKIP)
    finally:
        document.close()

    assert (result.created, result.updated, result.skipped, result.failed) == (2, 0, 0, 0)
    assert not result.image_errors, result.image_errors

    questions = QuestionRepo(session).list_all(QuestionFilter(course_id=course_id))
    assert len(questions) == 2
    # 章节按人类可读路径自动建出
    assert {c.path for c in ChapterRepo(session).list_tree(course_id)} == {
        "第3章 函数",
        "第3章 函数/3.1 二次函数",
    }


def test_imported_image_is_stored_and_referenced(
    session: Session, course_id: str, word_file: Path
) -> None:
    """图片必须在临时媒体目录被清理**之前**收进题库。

    这里曾经有个只在 GC 时机下才出现的 bug：plan 返回后源文档被回收，
    临时目录提前删掉，commit 时图片全部「找不到」。
    """
    plan, document = plan_file(session, course_id, word_file)
    try:
        ImportService(session).commit(plan, policy=ConflictPolicy.SKIP)
    finally:
        document.close()

    assets = AssetService(session)
    service = QuestionService(session)
    draft = service.to_draft(
        QuestionRepo(session).list_all(
            QuestionFilter(course_id=course_id, types=[QuestionType.SINGLE_CHOICE])
        )[0]
    )
    referenced = assets.referenced_assets(draft.stem)
    assert len(referenced) == 1
    asset = assets.get(referenced[0])
    assert asset is not None and asset.mime == "image/png"
    assert assets.path_of(referenced[0]) is not None, "图片记录在库但文件不在磁盘"
    assert asset.ref_count == 1


def test_imported_question_is_searchable(session: Session, course_id: str, word_file: Path) -> None:
    plan, document = plan_file(session, course_id, word_file)
    try:
        ImportService(session).commit(plan, policy=ConflictPolicy.SKIP)
    finally:
        document.close()
    service = QuestionService(session)
    assert service.search_ids("最小值")
    assert service.search_ids("二次函数")


# ------------------------------------------------------------- 重复导入
def test_reimport_detects_exact_duplicates(
    session: Session, course_id: str, word_file: Path
) -> None:
    for _ in range(1):
        plan, document = plan_file(session, course_id, word_file)
        try:
            ImportService(session).commit(plan, policy=ConflictPolicy.SKIP)
        finally:
            document.close()

    plan, document = plan_file(session, course_id, word_file)
    try:
        assert plan.exact_duplicate_count == 2
        result = ImportService(session).commit(plan, policy=ConflictPolicy.SKIP)
    finally:
        document.close()
    assert (result.created, result.skipped) == (0, 2)
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 2


def test_selected_subset_only(session: Session, course_id: str, word_file: Path) -> None:
    """老师常常只想导入一份文件里的某几道题。"""
    plan, document = plan_file(session, course_id, word_file)
    try:
        result = ImportService(session).commit(plan, selected=[2])
    finally:
        document.close()
    assert result.created == 1
    assert result.skipped == 1
    questions = QuestionRepo(session).list_all(QuestionFilter(course_id=course_id))
    assert [q.type for q in questions] == [QuestionType.TRUE_FALSE]


def test_batch_rollback_removes_created_questions(
    session: Session, course_id: str, word_file: Path
) -> None:
    plan, document = plan_file(session, course_id, word_file)
    try:
        result = ImportService(session).commit(plan)
    finally:
        document.close()
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 2

    removed = ImportService(session).rollback_batch(result.batch_id)
    assert removed == 2
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 0
    # 图片随之变成孤儿，可被清理
    assert AssetService(session).orphans()


def test_plan_has_no_side_effects(session: Session, course_id: str, word_file: Path) -> None:
    """预览必须真的什么都不写——用户点 dry-run 就是为了先看看。"""
    plan, document = plan_file(session, course_id, word_file)
    try:
        assert plan.total_parsed == 2
    finally:
        document.close()
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 0
    assert AssetService(session).orphans() == []


# --------------------------------------------- 三种格式产出一致的结果
EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"


def _digest(path: Path) -> list[tuple]:
    drafts = parse_source(path)
    result = []
    for draft in drafts:
        options = getattr(draft.payload, "options", None)
        result.append(
            (
                draft.type.value,
                int(draft.difficulty),
                draft.score,
                draft.chapter,
                draft.external_id,
                tuple(sorted(o.label for o in options if o.is_correct)) if options else (),
                tuple((s.type.value, s.score) for s in draft.sub_questions),
            )
        )
    return result


@pytest.mark.parametrize("suffix", [".docx", ".html"])
def test_example_formats_match_markdown(suffix: str) -> None:
    """三份示例必须表达同一批题目。

    这是「三种格式一套标记」的真正验收：示例由 ``scripts/生成模板示例.py``
    从同一份 Markdown 生成，这个测试守住它们不漂移。
    """
    source = EXAMPLES / "导入模板示例.md"
    target = EXAMPLES / f"导入模板示例{suffix}"
    if not target.exists():
        pytest.skip(f"{target.name} 未生成，运行 scripts/生成模板示例.py")
    expected = _digest(source)
    assert _digest(target) == expected, f"{target.name} 与 Markdown 示例不一致"


def test_markdown_example_parses_cleanly() -> None:
    report = parse_document((EXAMPLES / "导入模板示例.md").read_text(encoding="utf-8"))
    assert report.ok, [str(e) for e in report.errors]
    assert len(report.drafts) == 7


# ------------------------------------------------- 查重的规模与正确性
def _bulk_draft(index: int) -> QuestionDraft:
    from qbank.core.enums import QuestionStatus

    return QuestionDraft.model_validate(
        {
            "type": QuestionType.SINGLE_CHOICE,
            "stem": f"第 {index} 题：下列关于数据结构第 {index} 个知识点的说法，正确的是？",
            "payload": {
                "type": "single_choice",
                "options": [
                    {"label": "A", "content": f"选项甲 {index}", "is_correct": True},
                    {"label": "B", "content": f"选项乙 {index}"},
                ],
            },
            "answer": "A",
            "status": QuestionStatus.ACTIVE,
        }
    )


@pytest.mark.slow
def test_duplicate_index_is_built_once_per_plan(
    session: Session, course_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候选集必须整份文件共用，不能逐题重建。

    逐题重建是 O(待导入 × 库内)，而每次都要解析一遍 Markdown。实测一份 300 道题
    的文件导入 2000 道题的库需要 **843 秒**；建一次降到 15 秒。
    这个测试不量时间（机器性能差异太大），而是直接数「候选集建了几次」。
    """
    from qbank.core.services import question as question_module

    service = QuestionService(session)
    for index in range(30):
        service.create(course_id, _bulk_draft(index))

    builds = 0
    original = question_module.QuestionService.build_duplicate_index

    def counting_build(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal builds
        builds += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(question_module.QuestionService, "build_duplicate_index", counting_build)

    source = "\n".join(
        f"""【题型】单选  【难度】2
【题干】第 {index} 题：下列关于数据结构第 {index} 个知识点的说法，正确的是？
【选项】
*A. 选项甲 {index}
B. 选项乙 {index}
【答案】A
"""
        for index in range(100, 120)
    )
    document = read_markdown_text(source)
    plan = ImportService(session).plan_document(course_id, document)

    assert plan.total_parsed == 20
    assert builds == 1, f"候选集被重建了 {builds} 次，应该只建一次"


def test_duplicate_detection_still_correct_at_scale(session: Session, course_id: str) -> None:
    """优化不能改变判定结果：库里已有的题重新导入仍要认出「完全相同」。"""
    service = QuestionService(session)
    for index in range(40):
        service.create(course_id, _bulk_draft(index))

    source = "\n".join(
        f"""【题型】单选  【难度】2
【题干】第 {index} 题：下列关于数据结构第 {index} 个知识点的说法，正确的是？
【选项】
*A. 选项甲 {index}
B. 选项乙 {index}
【答案】A
"""
        for index in (3, 17, 999)
    )
    plan = ImportService(session).plan_document(course_id, read_markdown_text(source))
    assert plan.total_parsed == 3
    assert plan.exact_duplicate_count == 2, "第 3、17 题已在库里，应判为完全相同"
    actions = [item.exact_duplicate is not None for item in plan.questions]
    assert actions == [True, True, False]
