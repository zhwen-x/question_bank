"""把各种源文件读成内部用的 Markdown。

三种格式在这里汇合，之后共用同一个解析器：

* ``.md`` / ``.markdown``：直接读，只做换行符归一
* ``.docx`` / ``.odt`` / ``.rtf``：pandoc 转换，内嵌图片抽到临时目录
* ``.html`` / ``.htm``：pandoc 转换

``media_dir`` 里的图片**必须**在临时目录被清理之前由 AssetService 收进题库，
所以 :class:`SourceDocument` 带了 ``close()``，并且可以当上下文管理器用。
忘了收就会得到一堆「找不到图片」——而且是在用户已经点了「导入」之后。
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from qbank.exchange import pandoc

#: 不需要 pandoc 就能读的格式
NATIVE_SUFFIXES = frozenset({".md", ".markdown", ".txt"})

#: 需要 pandoc 的格式
PANDOC_SUFFIXES = frozenset({".docx", ".html", ".htm", ".xhtml", ".odt", ".rtf", ".tex"})

SUPPORTED_SUFFIXES = NATIVE_SUFFIXES | PANDOC_SUFFIXES


class UnsupportedSourceError(ValueError):
    """文件类型不支持。"""


@dataclass(slots=True)
class SourceDocument:
    """读好的源文档。

    ``base_dir`` 是解析图片相对路径的基准：Markdown 文件用它自己所在目录
    （老师的图片就放在文件旁边），pandoc 转出来的用媒体临时目录。
    """

    markdown: str
    base_dir: Path
    source_format: str
    source_name: str
    _media_dir: Path | None = None

    def close(self) -> None:
        if self._media_dir is not None:
            pandoc.cleanup_media(self._media_dir)
            self._media_dir = None

    def __enter__(self) -> SourceDocument:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def open_source(path: Path) -> SourceDocument:
    """读取源文件。**调用方负责 close**，否则临时媒体目录不会被清理。

    为什么单独提供这个非上下文管理器的版本：导入流程是 plan / commit 两步，
    文档必须跨越两步存活。如果只提供 ``@contextmanager`` 版本，调用方拿到
    document 却没持住 generator，generator 一被回收就会执行 ``finally``，
    媒体临时目录在 commit 之前就被删了——表现为「图片全部找不到」，
    而且因为依赖 GC 时机，这个故障时隐时现。
    """
    if not path.is_file():
        raise FileNotFoundError(f"找不到文件：{path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = "、".join(sorted(SUPPORTED_SUFFIXES))
        raise UnsupportedSourceError(f"不支持的文件类型 {suffix or '(无后缀)'}；支持：{supported}")

    if suffix in NATIVE_SUFFIXES:
        return SourceDocument(
            markdown=_normalize_newlines(path.read_text(encoding="utf-8")),
            base_dir=path.parent,
            source_format=suffix.lstrip("."),
            source_name=path.name,
        )

    media_dir = Path(tempfile.mkdtemp(prefix="qbank-media-"))
    document = SourceDocument(
        markdown="",
        base_dir=media_dir,
        source_format=pandoc.input_format_for(path),
        source_name=path.name,
        _media_dir=media_dir,
    )
    try:
        document.markdown = _normalize_newlines(pandoc.to_markdown(path, media_dir=media_dir))
    except Exception:
        document.close()
        raise
    return document


@contextmanager
def read_source(path: Path) -> Iterator[SourceDocument]:
    """``open_source`` 的上下文管理器版本，用于「读完即用完」的场景。"""
    document = open_source(path)
    try:
        yield document
    finally:
        document.close()


def read_markdown_text(text: str, *, base_dir: Path | None = None) -> SourceDocument:
    """从一段 Markdown 文本构造源文档（粘贴导入用）。"""
    return SourceDocument(
        markdown=_normalize_newlines(text),
        base_dir=base_dir or Path.cwd(),
        source_format="md",
        source_name="(粘贴的内容)",
    )


def read_html_text(html: str) -> SourceDocument:
    """从一段 HTML 文本构造源文档（从网页复制时用）。"""
    return SourceDocument(
        markdown=_normalize_newlines(pandoc.html_text_to_markdown(html)),
        base_dir=Path.cwd(),
        source_format="html",
        source_name="(粘贴的 HTML)",
    )
