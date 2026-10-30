"""公式分隔符归一化与数学片段定位。

**为什么要归一化**：题目从三个来源进库，公式写法各不相同——

* Word（经 pandoc 转换）出来的是 ``\\(x\\)`` 和 ``\\[x\\]``
* 网页复制的可能是 ``$$x$$``，也可能是 ``\\begin{equation}``
* 老师手写 Markdown 一般用 ``$x$``

内部只认一种写法：行内 ``$...$``、块级 ``$$...$$``。统一之后，渲染、导出
（LaTeX→OMML）、查重指纹才有确定的输入。

**最容易写错的地方**：替换不能碰代码块。编程题的题干里出现 ``\\(`` 或 ``$``
是完全正常的（shell 变量、C 的格式串、正则），把它们当公式处理会直接破坏题目。
因此所有改写都先经 :func:`split_protected` 把代码段隔离出来。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

#: 受保护的片段：围栏代码块（``` 或 ~~~，可带语言标识）、HTML 注释、行内代码。
#: 顺序要紧：围栏块必须先匹配，否则块内的反引号会被当成行内代码。
_PROTECTED = re.compile(
    r"""
      (?P<fence>^[ \t]*(?P<ticks>`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*(?P=ticks)[ \t]*$|\Z))
    | (?P<comment><!--.*?-->)
    | (?P<inline>(?P<backticks>`+)(?:(?!(?P=backticks)).)*?(?P=backticks))
    """,
    re.VERBOSE | re.DOTALL | re.MULTILINE,
)

#: ``\begin{env}...\end{env}`` 形式的数学环境。这些环境本身不含 $ 分隔符，
#: 需要包进 $$...$$ 才能被 KaTeX / pandoc 当数学处理。
_MATH_ENVS = (
    "equation",
    "equation*",
    "align",
    "align*",
    "aligned",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "cases",
    "array",
    "matrix",
    "pmatrix",
    "bmatrix",
    "vmatrix",
    "split",
)

_ENV_PATTERN = re.compile(
    r"\\begin\{(?P<env>"
    + "|".join(re.escape(e) for e in _MATH_ENVS)
    + r")\}(?P<body>.*?)\\end\{\1\}",
    re.DOTALL,
)

#: 内侧带空格的行内公式，如 ``$ x^2 $``。
#: 解析器（dollarmath 的 allow_space=False）不认这种写法，所以在归一化阶段把
#: 内侧空格去掉。**但不能无条件去**：``价格 $5 和 $10`` 里的 ``$5 和 $`` 也符合
#: 这个形状，去掉空格就把正文改坏了。因此要求内容里出现强数学信号才动手。
_PADDED_INLINE = re.compile(r"(?<!\\)(?<!\$)\$(?P<body>[ \t]+[^$\n]*?[ \t]+)\$(?!\$)")

#: 强数学信号：LaTeX 命令、上下标、花括号、关系符。中文字符出现则判定为正文。
_MATH_SIGNAL = re.compile(r"\\[A-Za-z]+|[\^_{}]|\\\\|[<>=]=?|\\frac")
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

_PAREN_INLINE = re.compile(r"\\\((?P<body>.*?)\\\)", re.DOTALL)
_BRACKET_BLOCK = re.compile(r"\\\[(?P<body>.*?)\\\]", re.DOTALL)

#: 已经是 $ 形式的数学：先匹配 $$（块级），再匹配 $（行内）。
#: 行内的否定环视排除 ``\$``（转义的美元符号）和 ``$5`` 这类金额写法里的
#: 「$ 后紧跟数字且无配对」情况由「两端不得为空白」规则自然排除。
_DOLLAR_BLOCK = re.compile(r"(?<!\\)\$\$(?P<body>.+?)(?<!\\)\$\$", re.DOTALL)
_DOLLAR_INLINE = re.compile(r"(?<!\\)(?<!\$)\$(?P<body>(?!\s)(?:\\.|[^$\n\\])+?(?<!\s))\$(?!\$)")


@dataclass(frozen=True, slots=True)
class Segment:
    """文本的一段。``protected`` 为真表示代码/注释，任何改写都必须跳过。"""

    text: str
    protected: bool


def split_protected(text: str) -> list[Segment]:
    """把文本切成「可改写」与「受保护」交替的片段。

    >>> [(s.protected, s.text) for s in split_protected("前 `a$b` 后")]
    [(False, '前 '), (True, '`a$b`'), (False, ' 后')]
    """
    segments: list[Segment] = []
    cursor = 0
    for match in _PROTECTED.finditer(text):
        if match.start() > cursor:
            segments.append(Segment(text[cursor : match.start()], protected=False))
        segments.append(Segment(match.group(0), protected=True))
        cursor = match.end()
    if cursor < len(text):
        segments.append(Segment(text[cursor:], protected=False))
    return segments


def _rewrite_unprotected(text: str, rewrite: object) -> str:
    """只对未受保护的片段施加 ``rewrite``。"""
    assert callable(rewrite)
    return "".join(
        segment.text if segment.protected else rewrite(segment.text)
        for segment in split_protected(text)
    )


def normalize_math(markdown: str) -> str:
    r"""把各种公式写法统一成 ``$...$`` / ``$$...$$``。

    >>> normalize_math(r"设 \(x > 0\)，则 \[y = x^2\]")
    '设 $x > 0$，则 $$y = x^2$$'
    >>> normalize_math(r"\begin{align} a &= b \end{align}")
    '$$\\begin{align} a &= b \\end{align}$$'
    >>> normalize_math("代码里的 `\\(x\\)` 不动")
    '代码里的 `\\(x\\)` 不动'
    >>> normalize_math("已经是 $x$ 和 $$y$$ 的不重复包裹")
    '已经是 $x$ 和 $$y$$ 的不重复包裹'
    >>> normalize_math("带空格 $ x^2 $ 会被收紧")
    '带空格 $x^2$ 会被收紧'
    >>> normalize_math("价格 $5 和 $10 不是公式")
    '价格 $5 和 $10 不是公式'
    """

    def rewrite(chunk: str) -> str:
        # 先处理 \begin{env}，且跳过已经被 $$ 包住的，避免套成 $$$$
        def wrap_env(match: re.Match[str]) -> str:
            whole = match.group(0)
            start, end = match.span()
            before = chunk[max(0, start - 2) : start]
            after = chunk[end : end + 2]
            if before.endswith("$$") and after.startswith("$$"):
                return whole
            if before.endswith("$") and after.startswith("$"):
                return whole
            return f"$${whole}$$"

        chunk = _ENV_PATTERN.sub(wrap_env, chunk)
        chunk = _BRACKET_BLOCK.sub(lambda m: f"$${m.group('body')}$$", chunk)
        chunk = _PAREN_INLINE.sub(lambda m: f"${m.group('body')}$", chunk)
        chunk = _PADDED_INLINE.sub(_trim_padding, chunk)
        return chunk

    return _rewrite_unprotected(markdown, rewrite)


def _trim_padding(match: re.Match[str]) -> str:
    """去掉 ``$ x^2 $`` 内侧的空格；判定为正文时原样返回。"""
    body = match.group("body")
    stripped = body.strip()
    if not stripped or _CJK.search(stripped) or not _MATH_SIGNAL.search(stripped):
        return match.group(0)
    return f"${stripped}$"


@dataclass(frozen=True, slots=True)
class MathSpan:
    """一处公式。``display`` 为真表示块级（``$$``）。"""

    latex: str
    display: bool
    start: int
    end: int


def iter_math(markdown: str) -> Iterator[MathSpan]:
    r"""按出现顺序产出全部公式片段（代码块内的不算）。

    偏移量是相对**整个输入**的，可直接用来切片替换。

    >>> [(s.latex, s.display) for s in iter_math(r"行内 $a_1$ 块级 $$\int f$$")]
    [('a_1', False), ('\\int f', True)]
    >>> list(iter_math("`$not math$`"))
    []
    """
    offset = 0
    for segment in split_protected(markdown):
        if not segment.protected:
            spans: list[MathSpan] = []
            consumed: list[tuple[int, int]] = []
            for match in _DOLLAR_BLOCK.finditer(segment.text):
                spans.append(
                    MathSpan(
                        latex=match.group("body").strip(),
                        display=True,
                        start=offset + match.start(),
                        end=offset + match.end(),
                    )
                )
                consumed.append(match.span())
            for match in _DOLLAR_INLINE.finditer(segment.text):
                if any(lo <= match.start() < hi for lo, hi in consumed):
                    continue  # 落在 $$ 块里，已经算过了
                spans.append(
                    MathSpan(
                        latex=match.group("body").strip(),
                        display=False,
                        start=offset + match.start(),
                        end=offset + match.end(),
                    )
                )
            yield from sorted(spans, key=lambda s: s.start)
        offset += len(segment.text)


def replace_math(markdown: str, placeholder: str = " ") -> str:
    r"""把公式整段换成占位符。

    用途是生成**检索与查重用的纯文本**：公式的 LaTeX 源码（``\frac``、``\alpha``）
    进了全文索引只会产生噪声，而两道题「只有公式不同」时也不该被判成重复——
    所以查重指纹保留占位符，让公式的存在与位置仍然参与比较。

    >>> replace_math("设 $x>0$ 且 $$y=1$$ 成立", placeholder="[M]")
    '设 [M] 且 [M] 成立'
    """
    spans = list(iter_math(markdown))
    if not spans:
        return markdown
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.append(markdown[cursor : span.start])
        pieces.append(placeholder)
        cursor = span.end
    pieces.append(markdown[cursor:])
    return "".join(pieces)
