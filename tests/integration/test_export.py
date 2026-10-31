"""导出的端到端测试：md / html / docx / pdf 四种格式各出一份。

最要紧的一条是**发给学生的那一份不能带答案**。其余的（图片带没带上、公式有没有
变成一串美元符号、单文件 HTML 是不是真的自包含）都属于「导出来看着像、用起来
才发现不对」的类型，所以逐条断言。
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from qbank.config import Paths
from qbank.core.enums import QuestionStatus, QuestionType
from qbank.core.models import Paper
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.asset import AssetService
from qbank.core.services.paper import PaperService
from qbank.core.services.question import QuestionService
from qbank.exchange.export_service import ExportService, available_formats, default_filename
from qbank.exchange.exporters import pdf as pdf_backend
from qbank.exchange.exporters.context import Layout
from qbank.exchange.exporters.templates import (
    ExportFormat,
    TemplateNotFoundError,
    copy_for_editing,
    discover,
)

#: 只出现在答案/解析/评分标准里的字串。卷子版面里出现任何一个都是泄题
ANSWER_ONLY = "后进先出而队列先进先出"
EXPLAIN_ONLY = "这里考的是栈的基本性质"
RUBRIC_ONLY = "答出两点各给五分"

pytestmark = pytest.mark.usefixtures("isolate_data_dir")


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (60, 40), (20, 90, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def paper(session: Session, isolate_data_dir: Paths) -> Paper:
    """一份覆盖「图、公式、表格、客观题、主观题」的卷子。"""
    course = CourseRepo(session).create("数据结构")
    assets = AssetService(session, isolate_data_dir)
    asset, _ = assets.store_bytes(png(), original_name="图.png")
    questions = QuestionService(session)
    papers = PaperService(session)

    choice = questions.create(
        course.id,
        QuestionDraft.model_validate(
            {
                "type": QuestionType.SINGLE_CHOICE,
                "stem": f"下图所示结构的时间复杂度是 $O(\\log n)$ 吗？\n\n![图](asset://{asset.sha256})",
                "payload": {
                    "type": "single_choice",
                    "options": [
                        {"label": "A", "content": "是", "is_correct": True},
                        {"label": "B", "content": "否"},
                        {"label": "C", "content": "不一定"},
                        {"label": "D", "content": "题目有误"},
                    ],
                },
                "answer": "A",
                "explanation": EXPLAIN_ONLY,
                "status": QuestionStatus.ACTIVE,
            }
        ),
    )
    short = questions.create(
        course.id,
        QuestionDraft.model_validate(
            {
                "type": QuestionType.SHORT_ANSWER,
                "stem": "比较栈与队列。\n\n| 结构 | 特点 |\n|---|---|\n| 栈 | ? |",
                "payload": {"type": "short_answer", "key_points": ["顺序"]},
                "answer": f"栈{ANSWER_ONLY}。",
                "scoring_rubric": RUBRIC_ONLY,
                "status": QuestionStatus.ACTIVE,
            }
        ),
    )
    session.flush()
    made = papers.create_empty(
        course.id, "数据结构 期末", sections=["一、单项选择题", "二、简答题"]
    )
    papers.add_questions(made.id, made.sections[0].id, [choice.id], score=3)
    papers.add_questions(made.id, made.sections[1].id, [short.id], score=7)
    session.flush()
    return PaperRepo(session).get_detailed(made.id)


@pytest.fixture
def service(session: Session, isolate_data_dir: Paths) -> ExportService:
    return ExportService(session, isolate_data_dir)


# ======================================================== 不能泄题
@pytest.mark.parametrize("fmt", [ExportFormat.MD, ExportFormat.HTML])
def test_student_copy_contains_no_answers(
    service: ExportService, paper: Paper, tmp_path: Path, fmt: ExportFormat
) -> None:
    """发给学生的那一份里不能有答案、解析或评分标准。"""
    out = service.export(paper, fmt, layout=Layout.PAPER, output=tmp_path / f"卷{fmt.suffix}")
    text = out.path.read_text("utf-8")
    for secret in (ANSWER_ONLY, EXPLAIN_ONLY, RUBRIC_ONLY):
        assert secret not in text, f"卷面泄露了「{secret}」"
    assert "比较栈与队列" in text, "题干本身要在"


def test_full_layout_has_everything(service: ExportService, paper: Paper, tmp_path: Path) -> None:
    out = service.export(paper, ExportFormat.MD, layout=Layout.FULL, output=tmp_path / "f.md")
    text = out.path.read_text("utf-8")
    for shown in (ANSWER_ONLY, EXPLAIN_ONLY, RUBRIC_ONLY, "比较栈与队列"):
        assert shown in text


def test_answers_layout_drops_the_questions(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    """答案卷只要答案。题干整段再印一遍既费纸也没用——阅卷时对的是题号。"""
    out = service.export(paper, ExportFormat.MD, layout=Layout.ANSWERS, output=tmp_path / "a.md")
    text = out.path.read_text("utf-8")
    assert ANSWER_ONLY in text
    assert RUBRIC_ONLY in text
    assert "比较栈与队列" not in text
    assert "1．A" in text, "客观题要排成紧凑的一行"


# ======================================================== Markdown
def test_markdown_writes_images_beside_the_file(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    out = service.export(paper, ExportFormat.MD, output=tmp_path / "卷.md")
    text = out.path.read_text("utf-8")
    assert "asset://" not in text, "导出的文件里不该还留着内部协议"
    assert "卷.assets/" in text
    assert out.extra_files and out.extra_files[0].exists()
    assert out.extra_files[0].parent.name == "卷.assets"
    assert out.assets.count == 1


def test_markdown_export_is_byte_identical_across_runs(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    """同一份卷导出两次应当完全一样。

    这不是洁癖：它让「改了模板重新导出、diff 一下看改了什么」成为可能，
    也是导出能进测试的前提。刻意不往产物里写导出时间就是为了这个。
    """
    # 必须导到同名文件（放在不同目录），因为旁挂图片目录是按文件名取的，
    # 换个文件名正文里的图片路径本来就会跟着变
    first = service.export(paper, ExportFormat.MD, output=tmp_path / "一" / "卷.md")
    second = service.export(paper, ExportFormat.MD, output=tmp_path / "二" / "卷.md")
    assert first.path.read_bytes() == second.path.read_bytes()


def test_markdown_keeps_formulas_as_dollar_math(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    out = service.export(paper, ExportFormat.MD, output=tmp_path / "卷.md")
    assert "$O(\\log n)$" in out.path.read_text("utf-8")


# ======================================================== HTML
def test_html_is_a_single_self_contained_file(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    """单文件：发给学生要能直接打开，断网也能看。"""
    out = service.export(paper, ExportFormat.HTML, output=tmp_path / "卷.html")
    text = out.path.read_text("utf-8")
    assert not out.extra_files, "HTML 不该在旁边留文件"
    assert "data:image/png;base64," in text, "图片要内联"
    assert "<math" in text, "公式要是 MathML，而不是等前端 JS 来渲染"
    assert "<table" in text
    external = re.findall(r'(?:src|href)="(?!data:|#)([^"]+)"', text)
    assert not external, f"还有外部引用，断网就坏了：{external}"


def test_html_escapes_nothing_it_should_not(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    """正文是渲染后的 HTML，不能被二次转义成一堆 &lt;p&gt;。"""
    out = service.export(paper, ExportFormat.HTML, output=tmp_path / "卷.html")
    text = out.path.read_text("utf-8")
    assert "&lt;p&gt;" not in text
    assert "<p>" in text


# ======================================================== Word
@pytest.mark.pandoc
def test_docx_has_native_editable_formulas(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    """公式要是 Word 原生的 OMML，而不是一串字面的美元符号。"""
    out = service.export(paper, ExportFormat.DOCX, output=tmp_path / "卷.docx")
    with zipfile.ZipFile(out.path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        media = [name for name in archive.namelist() if name.startswith("word/media/")]
    assert "<m:oMath" in document, "公式没转成 Word 公式"
    assert "$" not in document, "正文里还留着字面的美元符号"
    assert media, "图片没带进 Word"


@pytest.mark.pandoc
def test_docx_contains_the_table(service: ExportService, paper: Paper, tmp_path: Path) -> None:
    out = service.export(paper, ExportFormat.DOCX, output=tmp_path / "卷.docx")
    with zipfile.ZipFile(out.path) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "<w:tbl>" in document, "表格被拍平了"


# ======================================================== PDF
@pytest.mark.pdf
@pytest.mark.skipif(not pdf_backend.is_available(), reason="没有可用的浏览器内核")
def test_pdf_is_a_real_multipage_document(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    out = service.export(paper, ExportFormat.PDF, output=tmp_path / "卷.pdf")
    data = out.path.read_bytes()
    assert data.startswith(b"%PDF-")
    assert re.search(rb"/Count\s+\d+", data), "没有页树，PDF 是空的"
    assert len(data) > 2000


def test_pdf_failure_explains_the_fix() -> None:
    """没有内核时的报错必须给出可操作的下一步，而不是一句 playwright 的栈。"""
    error = pdf_backend.PdfUnavailableError("Chromium 内核尚未下载")
    message = str(error)
    assert "playwright install chromium" in message
    assert "Google Chrome" in message
    assert "导出 HTML" in message, "要给出不装任何东西也能用的退路"


# ======================================================== 模板
@pytest.mark.parametrize("template", ["standard", "exam", "compact"])
def test_every_preset_template_renders(
    service: ExportService, paper: Paper, tmp_path: Path, template: str
) -> None:
    for fmt in (ExportFormat.MD, ExportFormat.HTML):
        out = service.export(
            paper, fmt, template=template, output=tmp_path / f"{template}{fmt.suffix}"
        )
        assert "数据结构 期末" in out.path.read_text("utf-8")
        assert out.template == template


def test_exam_template_adds_the_sealing_line(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    out = service.export(paper, ExportFormat.HTML, template="exam", output=tmp_path / "e.html")
    text = out.path.read_text("utf-8")
    assert "密 封 线" in text
    assert "座位号" in text
    assert "padding: 0 0 0 26mm" in text, "密封线那条带子必须在打印时也留出来"


def test_exam_template_hides_the_seal_on_the_answer_key(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    """答案卷不发给学生，印密封线没有意义。"""
    out = service.export(
        paper, ExportFormat.HTML, template="exam", layout=Layout.ANSWERS, output=tmp_path / "e.html"
    )
    assert "密 封 线" not in out.path.read_text("utf-8")


def test_unknown_template_lists_the_available_ones(
    service: ExportService, paper: Paper, tmp_path: Path
) -> None:
    with pytest.raises(TemplateNotFoundError) as excinfo:
        service.export(paper, ExportFormat.MD, template="没这套", output=tmp_path / "x.md")
    message = str(excinfo.value)
    assert "没这套" in message
    assert "standard" in message


def test_user_template_overrides_the_builtin(
    service: ExportService, paper: Paper, tmp_path: Path, isolate_data_dir: Paths
) -> None:
    """用户目录里的同名模板整套覆盖内置的那一套。"""
    target = copy_for_editing("standard", paths=isolate_data_dir)
    md = target / "paper.md.j2"
    md.write_text("我改过的模板：{{ paper.title }}\n", encoding="utf-8")

    found = discover(isolate_data_dir)["standard"]
    assert not found.builtin
    out = service.export(paper, ExportFormat.MD, output=tmp_path / "x.md")
    assert out.path.read_text("utf-8").startswith("我改过的模板：数据结构 期末")


def test_copying_a_template_twice_refuses(isolate_data_dir: Paths) -> None:
    """改了一半的模板被一条命令抹掉是不可接受的。"""
    copy_for_editing("standard", paths=isolate_data_dir)
    with pytest.raises(FileExistsError):
        copy_for_editing("standard", paths=isolate_data_dir)


def test_template_can_extend_a_builtin(
    service: ExportService, paper: Paper, tmp_path: Path, isolate_data_dir: Paths
) -> None:
    """自定义模板可以继承内置的那套，只改自己要改的部分。"""
    mine = isolate_data_dir.templates_dir / "paper" / "mine"
    mine.mkdir(parents=True)
    (mine / "paper.html.j2").write_text(
        '{% extends "standard/paper.html.j2" %}\n'
        "{% block extra_styles %}.paper-head h1 { color: #036; }{% endblock %}\n",
        encoding="utf-8",
    )
    out = service.export(paper, ExportFormat.HTML, template="mine", output=tmp_path / "m.html")
    text = out.path.read_text("utf-8")
    assert "color: #036" in text
    assert "数据结构 期末" in text, "继承来的正文也要在"


def test_template_missing_a_format_says_so(
    service: ExportService, paper: Paper, tmp_path: Path, isolate_data_dir: Paths
) -> None:
    only_html = isolate_data_dir.templates_dir / "paper" / "htmlonly"
    only_html.mkdir(parents=True)
    (only_html / "paper.html.j2").write_text("<p>{{ paper.title }}</p>", encoding="utf-8")
    with pytest.raises(TemplateNotFoundError, match=re.escape("paper.md.j2")):
        service.export(paper, ExportFormat.MD, template="htmlonly", output=tmp_path / "x.md")


# ======================================================== 杂项
def test_default_filename_distinguishes_layouts(paper: Paper) -> None:
    """两种版面导到同一个目录时文件名必须能分辨，否则互相覆盖。"""
    names = {default_filename(paper, ExportFormat.PDF, layout) for layout in Layout}
    assert len(names) == 3
    assert "数据结构 期末（答案）.pdf" in names


def test_export_lands_in_the_data_directory_by_default(
    service: ExportService, paper: Paper, isolate_data_dir: Paths
) -> None:
    out = service.export(paper, ExportFormat.MD)
    assert out.path.parent == isolate_data_dir.exports_dir


def test_missing_image_becomes_a_visible_placeholder(
    session: Session, isolate_data_dir: Paths, tmp_path: Path
) -> None:
    """图丢了要印出「图片缺失」，而不是一个谁也看不懂的破图。"""
    course = CourseRepo(session).create("c")
    questions = QuestionService(session)
    papers = PaperService(session)
    made = papers.create_empty(course.id, "p", sections=["一"])
    q = questions.create(
        course.id,
        QuestionDraft.model_validate(
            {
                "type": QuestionType.SHORT_ANSWER,
                "stem": "看图：![图](asset://0123456789abcdef)",
                "payload": {"type": "short_answer"},
            }
        ),
    )
    session.flush()
    papers.add_questions(made.id, made.sections[0].id, [q.id])
    session.flush()

    service = ExportService(session, isolate_data_dir)
    out = service.export(
        PaperRepo(session).get_detailed(made.id), ExportFormat.MD, output=tmp_path / "x.md"
    )
    assert "［图片缺失］" in out.path.read_text("utf-8")
    assert out.assets.missing == {"0123456789abcdef"}
    assert "1 张图未找到" in out.summary


def test_available_formats_reports_md_and_html_as_always_usable() -> None:
    """md 与 html 不依赖任何外部程序，任何环境下都该能导。"""
    capabilities = available_formats()
    assert capabilities[ExportFormat.MD][0]
    assert capabilities[ExportFormat.HTML][0]
