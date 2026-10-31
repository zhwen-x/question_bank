"""导出模板的发现与加载。

一套模板就是一个目录：

    <key>/
      paper.md.j2      导出 md 与 docx 用（docx = 这份 Markdown 交给 pandoc）
      paper.html.j2    导出 html 与 pdf 用（pdf = 这份 HTML 交给 Chromium 打印）
      reference.docx   可选。pandoc 从它取样式（字体、页边距、页眉页脚）
      description.txt  可选。一行说明，列模板时显示

**用户目录优先**：``<数据目录>/templates/paper/<key>/`` 里的同名模板会整套覆盖
内置的同名模板。改一套自己的，从内置的拷一份出来改即可
（``qbank export templates --copy standard``）。

为什么 md 和 docx 共用一份模板：docx 是把 Markdown 交给 pandoc 转出来的，
题号怎么排、分值印在哪只定义一次。否则迟早出现「Word 版和 PDF 版题号不一样」
这种谁也说不清是哪边错了的问题。
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from qbank.config import Paths, get_paths
from qbank.core.content.render import MathMode, RenderOptions, render_markdown
from qbank.exchange.exporters.context import chinese_number

#: 内置模板所在目录
BUILTIN_DIR = Path(__file__).resolve().parents[1].parent / "templates" / "paper"

MD_TEMPLATE = "paper.md.j2"
HTML_TEMPLATE = "paper.html.j2"
REFERENCE_DOCX = "reference.docx"
DESCRIPTION = "description.txt"

#: 找不到模板时推荐这一套
DEFAULT_TEMPLATE = "standard"


class ExportFormat(str, Enum):
    MD = "md"
    HTML = "html"
    DOCX = "docx"
    PDF = "pdf"

    @property
    def suffix(self) -> str:
        return f".{self.value}"

    @property
    def label(self) -> str:
        return {"md": "Markdown", "html": "HTML", "docx": "Word", "pdf": "PDF"}[self.value]

    @property
    def media_type(self) -> str:
        """下载时的 Content-Type。写错会让浏览器把 docx 当文本打开。"""
        return {
            "md": "text/markdown; charset=utf-8",
            "html": "text/html; charset=utf-8",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pdf": "application/pdf",
        }[self.value]

    @property
    def source_template(self) -> str:
        """这个格式由哪份模板产出。docx 走 Markdown，pdf 走 HTML。"""
        return MD_TEMPLATE if self in {ExportFormat.MD, ExportFormat.DOCX} else HTML_TEMPLATE


class TemplateNotFoundError(LookupError):
    """模板不存在，或它缺少这个格式需要的那份文件。"""


@dataclass(frozen=True, slots=True)
class PaperTemplate:
    key: str
    directory: Path
    builtin: bool
    description: str = ""

    def supports(self, fmt: ExportFormat) -> bool:
        return (self.directory / fmt.source_template).is_file()

    @property
    def formats(self) -> tuple[ExportFormat, ...]:
        return tuple(fmt for fmt in ExportFormat if self.supports(fmt))

    @property
    def reference_docx(self) -> Path | None:
        """这套模板自带的 Word 参考文档；没有就用 pandoc 的默认样式。"""
        found = self.directory / REFERENCE_DOCX
        return found if found.is_file() else None

    @property
    def origin(self) -> str:
        return "内置" if self.builtin else "自定义"


def user_dir(paths: Paths | None = None) -> Path:
    return (paths or get_paths()).templates_dir / "paper"


def discover(paths: Paths | None = None) -> dict[str, PaperTemplate]:
    """列出可用模板。用户目录里的同名模板覆盖内置。"""
    found: dict[str, PaperTemplate] = {}
    for directory, builtin in ((BUILTIN_DIR, True), (user_dir(paths), False)):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not any((entry / name).is_file() for name in (MD_TEMPLATE, HTML_TEMPLATE)):
                continue
            note = entry / DESCRIPTION
            found[entry.name] = PaperTemplate(
                key=entry.name,
                directory=entry,
                builtin=builtin,
                description=note.read_text("utf-8").strip().splitlines()[0]
                if note.is_file() and note.read_text("utf-8").strip()
                else "",
            )
    return found


def get_template(key: str, fmt: ExportFormat, *, paths: Paths | None = None) -> PaperTemplate:
    """取一套模板，并确认它能产出这个格式。"""
    available = discover(paths)
    found = available.get(key)
    if found is None:
        names = "、".join(sorted(available)) or "（一套都没有）"
        raise TemplateNotFoundError(f"找不到模板「{key}」。现有模板：{names}")
    if not found.supports(fmt):
        usable = "、".join(f.value for f in found.formats) or "（无）"
        raise TemplateNotFoundError(
            f"模板「{key}」缺少 {found.directory.name}/{fmt.source_template}，"
            f"导不出 {fmt.label}。它支持：{usable}"
        )
    return found


def copy_for_editing(key: str, *, paths: Paths | None = None, new_key: str = "") -> Path:
    """把一套内置模板拷到用户目录，供修改。

    返回新目录。已存在时不覆盖——改了一半的模板被一条命令抹掉是不可接受的。
    """
    source = discover(paths).get(key)
    if source is None:
        raise TemplateNotFoundError(f"找不到模板「{key}」")
    target = user_dir(paths) / (new_key or key)
    if target.exists():
        raise FileExistsError(f"{target} 已存在；换个名字，或先把它删掉")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source.directory, target)
    return target


# ------------------------------------------------------------ Jinja 环境
def _markdown_filter(markdown: str) -> Markup:
    """模板里的 ``| md``：Markdown → HTML。

    公式走 MathML：浏览器原生渲染，不需要 KaTeX 的 JS 与字体文件。导出的单文件
    HTML 因此**断网也能看**，而且 Chromium 打印 PDF 时不必等任何外部资源。

    图片不在这里解析——``asset://`` 在建上下文时就已按目标形式重写过了
    （自包含 HTML 换成了 ``data:`` 内联）。
    """
    return Markup(render_markdown(markdown, RenderOptions(math=MathMode.MATHML)))


def _format_number(value: float) -> str:
    """分值去掉多余的小数位：``2.0`` → ``2``，``1.5`` → ``1.5``。

    试卷上印「2.0 分」会显得像是程序没写完。
    """
    return f"{value:g}"


def make_environment(
    template: PaperTemplate, fmt: ExportFormat, *, paths: Paths | None = None
) -> Environment:
    """为一次导出建 Jinja 环境。

    搜索路径里同时放用户目录与内置目录，这样自定义模板可以
    ``{% extends "standard/paper.html.j2" %}`` 继承内置的那套，只改自己要改的部分。

    ``StrictUndefined``：模板里写错一个字段名必须当场报错。默默渲染成空白的
    后果是老师印完一百份才发现没有分数。
    """
    search: list[Path] = [template.directory, user_dir(paths), BUILTIN_DIR]
    environment = Environment(
        loader=FileSystemLoader([str(path) for path in search if path.is_dir()]),
        autoescape=fmt in {ExportFormat.HTML, ExportFormat.PDF},
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    environment.filters["md"] = _markdown_filter
    environment.filters["g"] = _format_number
    environment.filters["cn"] = chinese_number
    return environment


def render_template(
    template: PaperTemplate,
    fmt: ExportFormat,
    context: Any,
    *,
    paths: Paths | None = None,
    extra: Sequence[tuple[str, Any]] = (),
) -> str:
    environment = make_environment(template, fmt, paths=paths)
    rendered = environment.get_template(fmt.source_template).render(
        paper=context, fmt=fmt.value, **dict(extra)
    )
    return str(rendered)
