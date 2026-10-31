r"""Markdown → HTML 渲染。

三个与别处不同的处理，都是题库场景逼出来的：

公式用 ``\(...\)`` 而不是 ``$...$`` 交给前端
    题干里字面的 ``$`` 很常见（金额、shell 变量、C 的格式串）。如果让 KaTeX
    直接去扫 ``$``，这些地方会被误当公式。所以服务端把公式识别好，包成
    ``<span class="math inline">\(...\)</span>``，前端只认 ``\(`` ``\[``
    两种分隔符，不再碰 ``$``。

图片走 ``asset://<sha256>`` 解析器
    库里存的是内容哈希而不是路径。不同输出目标需要不同的 URL：Web 界面要
    ``/assets/<sha>``，自包含 HTML 要 ``data:`` 内联，Markdown 导出要相对路径。
    所以渲染时注入一个解析函数，而不是把路径烧进内容里。

输出一律过 nh3 白名单
    题目内容可以来自同事发来的 Word/HTML 文件，里面可能带 ``<script>``。
    Markdown 本身允许内嵌 HTML，所以渲染结果**必须**过滤，哪怕它只在本地显示。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from html import escape
from typing import Any

import nh3
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import OptionsDict
from mdit_py_plugins.dollarmath import dollarmath_plugin

from qbank.core.content.math import normalize_math

#: ``asset://`` 协议前缀
ASSET_SCHEME = "asset://"

#: 允许出现在渲染结果里的标签。
#: 刻意不含 ``script`` / ``style`` / ``iframe`` / ``object`` / ``form``。
ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "hr",
        "div",
        "span",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "s",
        "del",
        "ins",
        "mark",
        "sub",
        "sup",
        "small",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "blockquote",
        "pre",
        "code",
        "kbd",
        "samp",
        "var",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "colgroup",
        "col",
        "a",
        "img",
        "figure",
        "figcaption",
        # MathML（Chromium 109+ 原生支持；自包含导出时不需要任何 JS 与字体）
        "math",
        "semantics",
        "annotation",
        "mrow",
        "mi",
        "mn",
        "mo",
        "ms",
        "mtext",
        "mspace",
        "msqrt",
        "mroot",
        "mfrac",
        "msub",
        "msup",
        "msubsup",
        "munder",
        "mover",
        "munderover",
        "mmultiscripts",
        "mtable",
        "mtr",
        "mtd",
        "mstyle",
        "mpadded",
        "mphantom",
        "menclose",
        "mfenced",
        "merror",
        "maction",
    }
)

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "*": {"class", "id", "title", "dir", "lang"},
    "a": {"href", "target"},  # rel 由 nh3 的 link_rel 统一加，不能在此声明
    "img": {"src", "alt", "width", "height", "loading"},
    "td": {"colspan", "rowspan", "align", "valign"},
    "th": {"colspan", "rowspan", "align", "valign", "scope"},
    "col": {"span", "width"},
    "ol": {"start", "type"},
    "code": {"class"},
    "span": {"data-latex"},
    "div": {"data-latex"},
    "math": {"display", "xmlns"},
    "annotation": {"encoding"},
    "mo": {"stretchy", "fence", "separator", "lspace", "rspace"},
    "mstyle": {"displaystyle", "scriptlevel", "mathvariant"},
    "mtable": {"columnalign", "rowspacing", "columnspacing"},
    "mtd": {"columnalign", "columnspan", "rowspan"},
}


class MathMode(str, Enum):
    """公式的输出形态。

    ``KATEX``
        输出 ``\\(...\\)``，由前端 KaTeX 渲染。质量最好，但需要 KaTeX 的 JS、
        CSS 与字体文件——界面与 Chromium 打印 PDF 都能满足。
    ``MATHML``
        服务端转成 MathML。零 JS、零字体，适合「发给学生的单文件 HTML」；
        复杂公式的排版不如 KaTeX。
    ``RAW``
        原样输出 LaTeX 源码，供调试与纯文本场景。
    """

    KATEX = "katex"
    MATHML = "mathml"
    RAW = "raw"


#: ``asset://<sha256>`` → 实际 URL。返回 ``None`` 表示图片丢失，渲染成占位提示。
AssetResolver = Callable[[str], str | None]


def _keep_asset_uri(sha256: str) -> str | None:
    """默认解析器：原样保留，便于调试与单元测试。"""
    return f"{ASSET_SCHEME}{sha256}"


@dataclass(frozen=True, slots=True)
class RenderOptions:
    math: MathMode = MathMode.KATEX
    asset_resolver: AssetResolver = _keep_asset_uri
    sanitize: bool = True
    #: 缺图时渲染的提示文字。导出前靠它一眼看出哪张图没带上。
    missing_asset_text: str = "［图片缺失］"
    #: 额外注入到根元素的 class
    wrapper_class: str = ""
    extra_allowed_attributes: Mapping[str, Sequence[str]] = field(default_factory=dict)


def _latex_to_mathml(latex: str, *, display: bool) -> str:
    """LaTeX → MathML。转换失败时退回转义后的源码，绝不抛异常打断渲染。"""
    try:
        import latex2mathml.converter
    except ImportError:
        return f'<code class="math-fallback">{escape(latex)}</code>'
    try:
        return str(latex2mathml.converter.convert(latex, display="block" if display else "inline"))
    except Exception:  # latex2mathml 对不支持的宏会抛各种异常类型，一律兜住
        return f'<code class="math-error" title="公式转换失败">{escape(latex)}</code>'


def _make_math_renderer(options: RenderOptions) -> Callable[[str, dict[str, Any]], str]:
    r"""构造交给 dollarmath 的公式渲染函数。

    **必须走插件的 ``renderer`` 参数**，不能在 Renderer 子类里覆写 ``math_inline``：
    ``dollarmath_plugin`` 在构造之后用 ``add_render_rule`` 注册自己的规则，会把
    子类方法悄悄盖掉——盖掉之后公式照样渲染出东西，只是全都变成默认样式，
    排查起来很费时间。

    插件负责外层的 ``<span class="math inline">`` / ``<div class="math block">``，
    本函数只决定里面的内容。
    """

    def render(latex: str, inline_options: dict[str, Any]) -> str:
        display = bool(inline_options.get("display_mode"))
        text = latex.strip()
        if options.math is MathMode.MATHML:
            return _latex_to_mathml(text, display=display)
        if options.math is MathMode.RAW:
            return escape(text)
        # KATEX：包成 \( \) / \[ \]，前端只认这两种分隔符，不去碰字面的 $
        opener, closer = (r"\[", r"\]") if display else (r"\(", r"\)")
        return f"{opener}{escape(text)}{closer}"

    return render


class _Renderer(RendererHTML):
    """覆写图片渲染，把 ``asset://`` 解析成实际 URL。"""

    def __init__(self, parser: MarkdownIt | None, options: RenderOptions) -> None:
        super().__init__(parser)
        self._opts = options

    # ---- 图片
    def image(self, tokens: Sequence[Token], idx: int, options: OptionsDict, env: Any) -> str:
        token = tokens[idx]
        src = token.attrGet("src") or ""
        if isinstance(src, str) and src.startswith(ASSET_SCHEME):
            resolved = self._opts.asset_resolver(src[len(ASSET_SCHEME) :])
            if resolved is None:
                digest = src[len(ASSET_SCHEME) :]
                # 同上：title 里不写 asset://
                return (
                    f'<span class="asset-missing" title="图片 {escape(digest[:12])} 未找到">'
                    f"{escape(self._opts.missing_asset_text)}</span>"
                )
            token.attrSet("src", resolved)
        return super().image(tokens, idx, options, env)


def _build_parser(options: RenderOptions) -> MarkdownIt:
    """构造解析器。

    用 ``commonmark`` 预设而非 ``gfm-like``：后者默认开 linkify，会把题干里的
    裸 URL 自动变成链接，对题目内容是不请自来的改写（而且要多一个依赖）。
    表格和删除线单独打开。
    """
    parser = MarkdownIt(
        "commonmark",
        renderer_cls=lambda md: _Renderer(md, options),
    )
    parser.enable(["table", "strikethrough"])
    parser.use(
        dollarmath_plugin,
        double_inline=True,
        # allow_space=False 是关键：开着的话 "价格 $5 和 $10，变量 $HOME" 里
        # 的 "$5 和 $" 会被当成行内公式，正文直接被改坏。带空格的 "$ x^2 $"
        # 由 normalize_math 预先收紧成 "$x^2$"，不受影响。
        allow_space=False,
        allow_digits=True,
        renderer=_make_math_renderer(options),
    )
    return parser


#: dollarmath 处理「段落中间的 $$...$$」时，产出的是 ``<div class="math inline">``
#: ——块级 div 嵌在 <p> 里，是非法结构，浏览器会把段落截断。这里改写成
#: 行内级的 span，并把 class 修正为 block（它语义上确实是 display 公式）。
_INLINE_DOUBLE_OPEN = '<div class="math inline">'
_INLINE_DOUBLE_FIXED = '<span class="math block-inline">'


def _fix_inline_display_math(html: str) -> str:
    """修正 double_inline 产出的非法嵌套。

    只在确实出现过那个开标签时才动手，并且只替换紧随其后的那个 </div>，
    避免误伤正常的块级公式 ``<div class="math block">``。
    """
    if _INLINE_DOUBLE_OPEN not in html:
        return html
    pieces = html.split(_INLINE_DOUBLE_OPEN)
    rebuilt = [pieces[0]]
    for piece in pieces[1:]:
        head, sep, tail = piece.partition("</div>")
        rebuilt.append(_INLINE_DOUBLE_FIXED + head + ("</span>" if sep else "") + tail)
    return "".join(rebuilt)


#: 任意属性里的 ``asset://<sha256>``。
#:
#: 下限刻意放到 4 位而不是 64 位：真实哈希总是 64 位，但测试与示例里常用
#: ``asset://aa`` 这样的短串。要求满 64 位会让这些场景静默失配——``asset://``
#: 原样活到 sanitize 阶段，被当作非法协议整个剥掉，表现为「图莫名消失」。
_ASSET_URL = re.compile(rf"{re.escape(ASSET_SCHEME)}(?P<sha>[0-9a-fA-F]{{4,64}})")


#: Markdown 图片语法里引用 asset:// 的整条引用（导出时要连 alt 一起重写）
_MARKDOWN_ASSET_IMAGE = re.compile(
    rf"!\[(?P<alt>[^\]]*)\]\((?P<src>{re.escape(ASSET_SCHEME)}[0-9a-fA-F]{{4,64}})\)"
)


#: 整个引用了 asset:// 的 ``<img>`` 标签
_ASSET_IMG_TAG = re.compile(
    rf"<img\b[^>]*?{re.escape(ASSET_SCHEME)}(?P<sha>[0-9a-fA-F]{{4,64}})[^>]*?>",
    re.IGNORECASE,
)


def rewrite_asset_links(text: str, resolver: AssetResolver, *, missing: str = "") -> str:
    """把 Markdown/HTML **原文**里的 ``asset://`` 换成目标形式。

    这是给导出用的：md 导出要换成相对路径、docx 要换成绝对路径（pandoc 才找得到
    图）、自包含 HTML 要换成 ``data:`` 内联。渲染期的解析器只对「渲染成 HTML」
    这条路有用，而 md 与 docx 都不经过渲染。

    ``resolver`` 返回 ``None``（图找不到）时整条引用换成 ``missing`` 文本——
    留一个指向 ``asset://`` 的死链接，在 Word 里会变成一个找不到文件的红叉，
    而用户看不出那原本是一张图。

    >>> rewrite_asset_links("![图](asset://abcd)", lambda sha: f"img/{sha}.png")
    '![图](img/abcd.png)'
    >>> rewrite_asset_links("![图](asset://abcd)", lambda _: None, missing="［图片缺失］")
    '［图片缺失］'
    """

    def replace_markdown(match: re.Match[str]) -> str:
        sha = match.group("src")[len(ASSET_SCHEME) :]
        target = resolver(sha)
        if target is None:
            return missing
        return f"![{match.group('alt')}]({target})"

    def replace_tag(match: re.Match[str]) -> str:
        sha = match.group("sha")
        return match.group(0) if resolver(sha) is not None else missing

    def replace_url(match: re.Match[str]) -> str:
        return resolver(match.group("sha")) or ""

    text = _MARKDOWN_ASSET_IMAGE.sub(replace_markdown, text)
    text = _ASSET_IMG_TAG.sub(replace_tag, text)
    return _ASSET_URL.sub(replace_url, text)


def _resolve_asset_urls(html: str, options: RenderOptions) -> str:
    """解析**原始 HTML** 里的 ``asset://`` 引用。

    Markdown 的 ``![](asset://x)`` 走的是 token 渲染那条路径；但从 Word 导入的
    内容里会有 pandoc 输出的 ``<img src="asset://x" style="width:1.5in" />``
    ——它作为原始 HTML 整块透传，碰不到 token 渲染。不在这里补一刀的话，
    ``asset://`` 会一路活到 sanitize 阶段被当成非法协议剥掉，图就没了。
    """

    def replace_missing_tag(match: re.Match[str]) -> str:
        """图找不到时整个标签换成提示，与 Markdown 那条路径表现一致。

        只留一个 src 为空的 ``<img>`` 会渲染成一个破图图标——用户看到的是
        「这里本来有张图但坏了」，而不是「这张图没带过来」，排查方向完全不同。
        """
        if options.asset_resolver(match.group("sha")) is not None:
            return match.group(0)
        # title 里不能再写 asset://：紧随其后的 URL 替换会把它当引用吃掉，
        # title 就变成空串了
        return (
            f'<span class="asset-missing" title="图片 {match.group("sha")[:12]} 未找到">'
            f"{escape(options.missing_asset_text)}</span>"
        )

    def replace_url(match: re.Match[str]) -> str:
        return options.asset_resolver(match.group("sha")) or ""

    html = _ASSET_IMG_TAG.sub(replace_missing_tag, html)
    return _ASSET_URL.sub(replace_url, html)


def _sanitize(html: str, options: RenderOptions) -> str:
    attributes = {tag: set(attrs) for tag, attrs in ALLOWED_ATTRIBUTES.items()}
    for tag, attrs in options.extra_allowed_attributes.items():
        attributes.setdefault(tag, set()).update(attrs)
    return nh3.clean(
        html,
        tags=set(ALLOWED_TAGS),
        attributes=attributes,
        # 只允许这些 URL 协议；asset:// 在渲染阶段已被解析掉，data: 供自包含导出
        url_schemes={"http", "https", "data", "mailto"},
        link_rel="noopener noreferrer",
    )


def render_markdown(markdown: str, options: RenderOptions | None = None) -> str:
    """把题目内容渲染成 HTML。

    >>> render_markdown("**粗** 与 $x^2$", RenderOptions(math=MathMode.RAW))
    '<p><strong>粗</strong> 与 <span class="math inline">x^2</span></p>'
    >>> render_markdown("![图](asset://aaaa)", RenderOptions(asset_resolver=lambda _: None))
    '<p><span class="asset-missing" title="图片 aaaa 未找到">［图片缺失］</span></p>'
    >>> render_markdown("<script>alert(1)</script>正文")
    '正文'
    """
    opts = options or RenderOptions()
    normalized = normalize_math(markdown)
    html = _fix_inline_display_math(_build_parser(opts).render(normalized))
    html = _resolve_asset_urls(html, opts).strip()
    if opts.sanitize:
        html = _sanitize(html, opts).strip()
    if opts.wrapper_class:
        html = f'<div class="{escape(opts.wrapper_class)}">{html}</div>'
    return html
