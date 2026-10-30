"""Markdown → 纯文本。

两处用到，需求不同，所以用参数区分：

全文检索索引
    要的是「人会搜的词」。公式的 LaTeX 源码（``\\frac``、``\\alpha``）、图片
    占位符进了索引只会制造噪声，所以都丢掉。代码**保留**——编程题里搜
    ``while``、``malloc`` 是真实需求。

查重指纹
    要的是「这两道题是不是同一道」。这里必须保留公式的**源码**和图片的
    **哈希**，而不是占位符——「求 $x^2$ 的导数」和「求 $x^3$ 的导数」是两道
    不同的题，用占位符会把它们判成重复。

实现走 markdown-it 的 token 流而不是正则。正则剥 Markdown 在嵌套结构
（表格里的加粗链接、列表里的代码块）上必然出错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin

from qbank.core.content.math import normalize_math

_WS = re.compile(r"[ \t　]+")
_BLANK_LINES = re.compile(r"\n{2,}")


class Keep(str, Enum):
    """非文字内容的处理方式。"""

    #: 整段丢掉（检索：LaTeX 源码进索引只是噪声）
    DROP = "drop"
    #: 换成固定占位符，只保留「这里有个东西」及其位置
    PLACEHOLDER = "placeholder"
    #: 保留原始内容：公式的 LaTeX 源码、图片的 asset 哈希（查重必须这样）
    SOURCE = "source"


@dataclass(frozen=True, slots=True)
class PlainTextOptions:
    math: Keep = Keep.DROP
    image: Keep = Keep.DROP
    keep_code: bool = True
    math_placeholder: str = "⟦公式⟧"
    image_placeholder: str = "⟦图⟧"


#: 全文检索用：丢掉公式与图片，保留代码（搜 ``while``、``malloc`` 是真实需求）
FOR_SEARCH = PlainTextOptions(math=Keep.DROP, image=Keep.DROP, keep_code=True)

#: 查重指纹用：公式保源码、图片保哈希，否则「只有公式/配图不同」会被误判成重复
FOR_FINGERPRINT = PlainTextOptions(math=Keep.SOURCE, image=Keep.SOURCE, keep_code=True)

#: 这些 token 结束后需要插入分隔，否则「表格相邻单元格」「相邻列表项」的文字
#: 会黏成一个词，分词后产出不存在的词条
_BREAK_AFTER = frozenset(
    {
        "paragraph_close",
        "heading_close",
        "td_close",
        "th_close",
        "tr_close",
        "list_item_close",
        "blockquote_close",
        "fence",
        "code_block",
        "math_block",
        "hr",
    }
)

_parser: MarkdownIt | None = None


def _get_parser() -> MarkdownIt:
    """解析器构造一次就复用——它无状态，而构造有可观开销（批量导入时按题调用）。"""
    global _parser
    if _parser is None:
        _parser = (
            MarkdownIt("commonmark")
            .enable(["table", "strikethrough"])
            .use(
                dollarmath_plugin,
                double_inline=True,
                allow_space=False,
                allow_digits=True,
            )
        )
    return _parser


def _walk(tokens: list[Token], options: PlainTextOptions, out: list[str]) -> None:
    for token in tokens:
        if token.type == "inline":
            _walk(token.children or [], options, out)
        elif token.type == "text":
            out.append(token.content)
        elif token.type in {"code_inline", "fence", "code_block"}:
            if options.keep_code:
                out.append(token.content)
        elif token.type in {"math_inline", "math_block", "math_inline_double"}:
            if options.math is Keep.SOURCE:
                out.append(f" {token.content.strip()} ")
            elif options.math is Keep.PLACEHOLDER:
                out.append(f" {options.math_placeholder} ")
        elif token.type == "image":
            # alt 文字往往就是「图」「图1」，对检索无益，故一律不收
            if options.image is Keep.SOURCE:
                out.append(f" {token.attrGet('src') or ''} ")
            elif options.image is Keep.PLACEHOLDER:
                out.append(f" {options.image_placeholder} ")
        elif token.type == "softbreak":
            out.append(" ")
        elif token.type == "hardbreak":
            out.append("\n")
        elif token.type in {"html_inline", "html_block"}:
            # 内嵌 HTML：只取标签之间的文字
            out.append(re.sub(r"<[^>]*>", " ", token.content))

        if token.type in _BREAK_AFTER:
            out.append("\n")


def to_plain_text(markdown: str, options: PlainTextOptions | None = None) -> str:
    """提取纯文本。

    >>> to_plain_text("**重点**：求 $x^2$ 的导数")
    '重点：求 的导数'
    >>> to_plain_text("| 甲 | 乙 |\\n|---|---|\\n| 1 | 2 |")
    '甲 乙 1 2'
    >>> to_plain_text("```python\\nwhile True:\\n    pass\\n```")
    'while True: pass'
    >>> to_plain_text("去掉代码", PlainTextOptions(keep_code=False))
    '去掉代码'

    查重指纹保留公式源码与图片哈希，所以「只有公式不同」的两道题能被区分：

    >>> to_plain_text("求 $x^2$ 的导数", FOR_FINGERPRINT)
    '求 x^2 的导数'
    >>> to_plain_text("![图](asset://deadbeef)", FOR_FINGERPRINT)
    'asset://deadbeef'
    """
    opts = options or FOR_SEARCH
    pieces: list[str] = []
    _walk(_get_parser().parse(normalize_math(markdown)), opts, pieces)
    text = "".join(pieces)
    text = _WS.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text)
    # 单个换行在检索与指纹里都没有意义，统一压成空格
    return " ".join(line.strip() for line in text.splitlines() if line.strip())
