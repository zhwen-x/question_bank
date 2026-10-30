"""pandoc 封装：docx / html → Markdown，以及 Markdown → docx。

把 pandoc 的调用细节关在这一个模块里，因为**每个参数都有踩过的坑**：

``gfm-tex_math_gfm+tex_math_dollars``
    gfm 默认开 ``tex_math_gfm``，公式会输出成 GitHub 的 ``$`x^2`$`` 形式而不是
    ``$x^2$``。内部只认 ``$...$``，所以必须显式关掉前者、开启后者。

**保留** ``raw_html``
    关掉它能让图片输出成 Markdown 语法（省事），但合并单元格的表格会被悄悄
    拍平成普通表格——试卷里这种表很常见，丢了就是实质性的内容损失。实测：

    ====================  ================================================
    ``+raw_html``（现用）  ``<table>…<th colspan="2">`` 合并关系完整保留
    ``-raw_html``          合并关系丢失，变成三个独立单元格
    ====================  ================================================

    代价是带尺寸的图片以 ``<img src="…" style="…">`` 形式出现，由
    :meth:`AssetService.absorb_local_images` 一并处理。

``--wrap=none``
    pandoc 默认在 72 列处折行。模板解析器是按行识别 ``【题干】`` 这类标记的，
    折行会把标记行拦腰截断，解析直接错位。

``--extract-media``
    docx 里的图片是内嵌的，不抽出来就拿不到。抽到临时目录，随后必须由
    AssetService 收进题库——临时目录一删，图就全丢了。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

#: 转成 Markdown 时的目标格式（原因见模块文档）
MARKDOWN_TARGET = "gfm-tex_math_gfm+tex_math_dollars"

#: 从 Markdown 转出去时的源格式。
#:
#: ``tex_math_dollars`` 让 ``$...$`` 被识别为公式——这是导出 docx 时能得到
#: Word 原生可编辑公式（而不是一串字面的美元符号）的前提。gfm 默认已启用它，
#: 这里显式写出来是为了不受 pandoc 版本默认值变化的影响。
#: 关掉 ``tex_math_gfm``，否则读入时 ``$`x`$`` 这种 GitHub 写法会参与竞争。
#:
#: 注意：``tex_math_single_backslash`` 对 gfm **不支持**（pandoc 会直接报错
#: 退出），别想当然加上。
MARKDOWN_SOURCE = "gfm+tex_math_dollars-tex_math_gfm"

#: 文件后缀 → pandoc 输入格式
INPUT_FORMATS: dict[str, str] = {
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".odt": "odt",
    ".rtf": "rtf",
    ".tex": "latex",
    ".md": MARKDOWN_SOURCE,
    ".markdown": MARKDOWN_SOURCE,
}


class PandocUnavailableError(RuntimeError):
    """pandoc 不可用。

    错误信息要给出可操作的修复方式——这个异常会直接显示给用户，而「pandoc not
    found」对不了解它的老师毫无意义。
    """

    def __init__(self, detail: str = "") -> None:
        message = (
            "需要 pandoc 才能读写 Word/HTML 文件。"
            '安装方式：pip install "pypandoc_binary"（自带二进制，无需单独装 pandoc）。'
            "在此之前，Markdown 格式的导入导出仍然可用。"
        )
        if detail:
            message = f"{message}\n原始错误：{detail}"
        super().__init__(message)


class PandocConversionError(RuntimeError):
    """pandoc 运行了但转换失败（文件损坏、格式不符等）。"""


@dataclass(frozen=True, slots=True)
class Capability:
    available: bool
    version: str = ""
    detail: str = ""


def capability() -> Capability:
    """探测 pandoc 是否可用。不抛异常，供 doctor 与界面做能力降级判断。"""
    try:
        import pypandoc
    except ImportError:
        return Capability(False, detail="未安装 pypandoc")
    try:
        version = str(pypandoc.get_pandoc_version())
    except OSError as exc:
        return Capability(False, detail=f"找不到 pandoc 二进制：{exc}")
    return Capability(True, version=version)


def is_available() -> bool:
    return capability().available


def require() -> None:
    """需要 pandoc 的入口先调一次，把「不可用」变成一句人话。"""
    found = capability()
    if not found.available:
        raise PandocUnavailableError(found.detail)


def input_format_for(path: Path) -> str:
    """按后缀判断输入格式。

    >>> input_format_for(Path("a.DOCX"))
    'docx'
    >>> input_format_for(Path("a.htm"))
    'html'
    """
    suffix = path.suffix.lower()
    if suffix not in INPUT_FORMATS:
        supported = "、".join(sorted(INPUT_FORMATS))
        raise ValueError(f"不支持的文件类型 {suffix or '(无后缀)'}；支持：{supported}")
    return INPUT_FORMATS[suffix]


def to_markdown(path: Path, *, media_dir: Path, input_format: str | None = None) -> str:
    """把 docx / html / odt 等转成内部用的 Markdown。

    图片会被抽到 ``media_dir``，正文里是指向它们的路径（可能是绝对路径）。
    调用方**必须**随后用 AssetService 把它们收进题库，否则临时目录清理后图就丢了。
    """
    require()
    import pypandoc

    media_dir.mkdir(parents=True, exist_ok=True)
    try:
        return str(
            pypandoc.convert_file(
                str(path),
                MARKDOWN_TARGET,
                format=input_format or input_format_for(path),
                extra_args=[f"--extract-media={media_dir}", "--wrap=none"],
            )
        )
    except RuntimeError as exc:
        raise PandocConversionError(f"无法读取 {path.name}：{exc}") from exc


def html_text_to_markdown(html: str) -> str:
    """直接转一段 HTML 文本（用于粘贴导入，不涉及本地图片）。"""
    require()
    import pypandoc

    try:
        return str(
            pypandoc.convert_text(html, MARKDOWN_TARGET, format="html", extra_args=["--wrap=none"])
        )
    except RuntimeError as exc:
        raise PandocConversionError(f"HTML 转换失败：{exc}") from exc


def markdown_to_docx(
    markdown: str,
    output: Path,
    *,
    reference_doc: Path | None = None,
    resource_path: Path | None = None,
) -> Path:
    """Markdown → Word。

    ``reference_doc`` 是一份普通 ``.docx``，pandoc 从里面取样式（字体、标题、
    页边距、页眉页脚）。这是自定义导出模板的实现方式——老师改一份 Word 文档
    就能换版式，不需要懂任何模板语法。

    ``resource_path`` 是图片的查找根目录。正文里的图片引用必须已经是本地相对
    路径（``asset://`` 要先解析掉），否则 pandoc 找不到图，而且**不会报错**，
    只会静默输出一个没有图的文档。
    """
    require()
    import pypandoc

    output.parent.mkdir(parents=True, exist_ok=True)
    extra_args = ["--wrap=none"]
    if reference_doc is not None:
        if not reference_doc.is_file():
            raise FileNotFoundError(f"找不到 Word 模板：{reference_doc}")
        extra_args.append(f"--reference-doc={reference_doc}")
    if resource_path is not None:
        extra_args.append(f"--resource-path={resource_path}")
    try:
        pypandoc.convert_text(
            markdown,
            "docx",
            format=MARKDOWN_SOURCE,
            outputfile=str(output),
            extra_args=extra_args,
        )
    except RuntimeError as exc:
        raise PandocConversionError(f"导出 Word 失败：{exc}") from exc
    return output


def cleanup_media(media_dir: Path) -> None:
    """删掉 ``--extract-media`` 留下的临时目录。

    删不掉就算了——它在临时目录里，操作系统迟早会清。为此中断导入不值得。
    """
    shutil.rmtree(media_dir, ignore_errors=True)
