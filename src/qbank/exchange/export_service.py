"""导出：试卷 → md / html / docx / pdf。

四种格式共用一条管道，只在最后一段分叉：

    Paper → 上下文 → md 模板  → Markdown ─┬→ .md（图片旁挂）
                                          └→ pandoc → .docx
                  → html 模板 → HTML ─────┬→ .html（图片内联，单文件）
                                          └→ Chromium → .pdf

**导出的是试卷快照，不是题库里的当前版本。** 去年的卷子导出来就该是去年那张。

**同一份试卷导出两次应当字节一致**（docx 除外——pandoc 会写入时间戳）。这让
「改了模板重新导出、diff 一下看改了什么」成为可能，也让导出能进测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from qbank.config import Paths, get_paths, safe_filename
from qbank.core.models import Paper
from qbank.core.services.asset import AssetService
from qbank.exchange.exporters import pdf as pdf_backend
from qbank.exchange.exporters.assets import AssetReport, ExportAssets
from qbank.exchange.exporters.context import Layout, build_context
from qbank.exchange.exporters.templates import (
    DEFAULT_TEMPLATE,
    ExportFormat,
    PaperTemplate,
    get_template,
    render_template,
)
from qbank.exchange.pandoc import markdown_to_docx

#: 版面 → 文件名后缀。导出两种版面时文件名必须能分辨，否则互相覆盖。
_LAYOUT_SUFFIX = {
    Layout.PAPER: "",
    Layout.FULL: "（含答案）",
    Layout.ANSWERS: "（答案）",
}


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    fmt: ExportFormat
    layout: Layout
    template: str
    assets: AssetReport

    @property
    def extra_files(self) -> tuple[Path, ...]:
        """正文之外还写了哪些文件（md 的旁挂图片目录）。"""
        return tuple(self.assets.written)

    @property
    def summary(self) -> str:
        parts = [f"{self.fmt.label} → {self.path.name}"]
        if self.assets.count:
            parts.append(f"{self.assets.count} 张图")
        if self.assets.missing:
            parts.append(f"{len(self.assets.missing)} 张图未找到")
        return "，".join(parts)


def default_filename(paper: Paper, fmt: ExportFormat, layout: Layout) -> str:
    """默认文件名：``<试卷标题><版面后缀>.<扩展名>``。"""
    return safe_filename(f"{paper.title}{_LAYOUT_SUFFIX[layout]}") + fmt.suffix


class ExportService:
    def __init__(self, session: Session, paths: Paths | None = None) -> None:
        self.session = session
        self.paths = paths or get_paths()
        self.assets = ExportAssets(AssetService(session, self.paths))

    def export(
        self,
        paper: Paper,
        fmt: ExportFormat,
        *,
        layout: Layout = Layout.PAPER,
        template: str = DEFAULT_TEMPLATE,
        output: Path | None = None,
    ) -> ExportResult:
        """导出一份试卷。``output`` 为空时落到数据目录的 ``exports/``。"""
        chosen = get_template(template, fmt, paths=self.paths)
        target = output or (self.paths.exports_dir / default_filename(paper, fmt, layout))
        target.parent.mkdir(parents=True, exist_ok=True)

        if fmt is ExportFormat.MD:
            self._write_markdown(paper, chosen, layout, target)
        elif fmt is ExportFormat.DOCX:
            self._write_docx(paper, chosen, layout, target)
        elif fmt is ExportFormat.HTML:
            target.write_text(self._html(paper, chosen, layout), encoding="utf-8")
        else:
            self._write_pdf(paper, chosen, layout, target)

        return ExportResult(
            path=target,
            fmt=fmt,
            layout=layout,
            template=chosen.key,
            assets=self.assets.report,
        )

    # ------------------------------------------------------------ 各格式
    def _write_markdown(
        self, paper: Paper, template: PaperTemplate, layout: Layout, target: Path
    ) -> None:
        """.md + 旁挂图片目录。

        不内联 base64：.md 的用途是发给别人、提交进 git、再加工，而一行几十万
        字符的 data URI 会让文本文件既读不了也 diff 不了。
        """
        resolver = self.assets.sidecar(target.parent / f"{target.stem}.assets")
        context = build_context(paper, layout=layout, asset_resolver=resolver)
        target.write_text(render_template(template, ExportFormat.MD, context), encoding="utf-8")

    def _write_docx(
        self, paper: Paper, template: PaperTemplate, layout: Layout, target: Path
    ) -> None:
        """Markdown → pandoc → Word。公式是 Word 原生可编辑的 OMML。

        图片用绝对路径：pandoc 找不到图时**不报错**，只会静默输出一个没有图的
        文档，所以不能依赖相对路径碰运气。
        """
        context = build_context(paper, layout=layout, asset_resolver=self.assets.absolute)
        markdown = render_template(template, ExportFormat.DOCX, context)
        markdown_to_docx(
            markdown,
            target,
            reference_doc=template.reference_docx,
            resource_path=self.paths.assets_dir,
        )

    def _html(self, paper: Paper, template: PaperTemplate, layout: Layout) -> str:
        """自包含 HTML：图片内联、样式内联、公式用 MathML。断网也能看。"""
        context = build_context(paper, layout=layout, asset_resolver=self.assets.inline)
        return render_template(template, ExportFormat.HTML, context)

    def _write_pdf(
        self, paper: Paper, template: PaperTemplate, layout: Layout, target: Path
    ) -> None:
        html = self._html(paper, template, layout)
        pdf_backend.html_to_pdf(
            html,
            target,
            options=pdf_backend.PdfOptions(footer_left=paper.title),
        )


def available_formats() -> dict[ExportFormat, tuple[bool, str]]:
    """每种格式当前能不能用，以及不能用的话为什么。

    界面与 ``qbank doctor`` 共用这一处判断，免得两边对「能不能导出 PDF」的说法
    不一致。
    """
    from qbank.exchange import pandoc

    pandoc_found = pandoc.capability()
    pdf_found = pdf_backend.capability()
    return {
        ExportFormat.MD: (True, ""),
        ExportFormat.HTML: (True, ""),
        ExportFormat.DOCX: (pandoc_found.available, pandoc_found.detail),
        ExportFormat.PDF: (pdf_found.available, pdf_found.detail),
    }
