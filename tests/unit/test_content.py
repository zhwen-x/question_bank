"""内容层测试：公式归一化、纯文本提取、HTML 渲染。

这里的大部分用例来自实际会出问题的输入，而不是教科书式的样例。最重要的一组是
「题干里字面的 ``$``」——编程题里 ``$HOME``、``$1``、金额 ``$5`` 都很常见，
一旦被当成公式，正文就被改坏了，而且错得很隐蔽。
"""

from __future__ import annotations

import pytest

from qbank.core.content.math import iter_math, normalize_math, replace_math, split_protected
from qbank.core.content.render import (
    MathMode,
    RenderOptions,
    render_markdown,
)
from qbank.core.content.text import (
    FOR_FINGERPRINT,
    FOR_SEARCH,
    Keep,
    PlainTextOptions,
    to_plain_text,
)


# ------------------------------------------------------------------ 归一化
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"设 \(x > 0\)", "设 $x > 0$"),
        (r"则 \[y = x^2\]", "则 $$y = x^2$$"),
        (
            r"\begin{equation} E = mc^2 \end{equation}",
            r"$$\begin{equation} E = mc^2 \end{equation}$$",
        ),
        # 已经包好的不能再套一层
        (r"$$\begin{align} a &= b \end{align}$$", r"$$\begin{align} a &= b \end{align}$$"),
        # 带空格的行内公式收紧，否则解析器认不出来
        (r"$ x^2 $", "$x^2$"),
        (r"$ \frac{a}{b} $", r"$\frac{a}{b}$"),
    ],
)
def test_normalize_math(source: str, expected: str) -> None:
    assert normalize_math(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "价格 $5 和 $10，变量 $HOME",
        "带空格但是正文 $5 和 $ 不动",
        r"转义的 \$100",
        "```sh\necho \\(x\\) $PATH\n```",
        "行内代码 `grep '\\$HOME'`",
        "~~~\n\\[not math\\]\n~~~",
        "<!-- \\(注释里的\\) -->",
    ],
)
def test_normalize_math_leaves_non_math_alone(source: str) -> None:
    """字面的 ``$``、转义的 ``\\$``、代码与注释里的内容都不许被改写。"""
    assert normalize_math(source) == source
    assert list(iter_math(source)) == []


def test_split_protected_isolates_code() -> None:
    segments = split_protected("前 `a$b` 中 ```\n$c$\n``` 后")
    protected = [s.text for s in segments if s.protected]
    assert protected == ["`a$b`", "```\n$c$\n```"]


def test_iter_math_reports_usable_offsets() -> None:
    """偏移量必须能直接用于切片，否则 replace_math 会错位。"""
    source = normalize_math(r"设 \(a\) 与 \[b\] 成立")
    spans = list(iter_math(source))
    assert [source[s.start : s.end] for s in spans] == ["$a$", "$$b$$"]
    assert [s.display for s in spans] == [False, True]


def test_replace_math() -> None:
    assert replace_math("设 $x>0$ 且 $$y=1$$", placeholder="[M]") == "设 [M] 且 [M]"


def test_nested_dollar_block_not_double_counted() -> None:
    """``$$a$$`` 不该既算一个块级公式、又算两个行内公式。"""
    assert [s.latex for s in iter_math("$$a$$")] == ["a"]


# ------------------------------------------------------------------ 纯文本
def test_plain_text_strips_markup_but_keeps_table_cells() -> None:
    source = "**重** 与 *斜*\n\n| 甲 | 乙 |\n|---|---|\n| 1 | 2 |"
    assert to_plain_text(source) == "重 与 斜 甲 乙 1 2"


def test_plain_text_keeps_code_for_programming_questions() -> None:
    """编程题里搜 ``malloc``、``while`` 是真实需求，代码必须进索引。"""
    source = "```c\nint *p = malloc(n);\n```"
    assert "malloc" in to_plain_text(source, FOR_SEARCH)
    assert "malloc" not in to_plain_text(source, PlainTextOptions(keep_code=False))


def test_search_text_drops_latex_noise() -> None:
    """``\\frac``、``\\alpha`` 进了索引只会干扰检索。"""
    text = to_plain_text(r"求 $\frac{\alpha}{\beta}$ 的值", FOR_SEARCH)
    assert "frac" not in text and "alpha" not in text
    assert "求" in text and "的值" in text


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("求 $x^2$ 的导数", "求 $x^3$ 的导数"),
        ("看图 ![](asset://aaa)", "看图 ![](asset://bbb)"),
    ],
)
def test_fingerprint_text_distinguishes_math_and_images(left: str, right: str) -> None:
    """只有公式或配图不同的两道题是**不同**的题，指纹必须区分它们。

    用占位符会把它们判成重复——这正是 FOR_FINGERPRINT 保留公式源码与
    asset 哈希的原因。
    """
    assert to_plain_text(left, FOR_FINGERPRINT) != to_plain_text(right, FOR_FINGERPRINT)


def test_fingerprint_text_ignores_whitespace_noise() -> None:
    """换行、缩进、全角空格的差异不该影响判重。"""
    a = to_plain_text("求  $x^2$\n\n 的导数", FOR_FINGERPRINT)
    b = to_plain_text("求 $x^2$ 的导数　", FOR_FINGERPRINT)
    assert a == b


def test_plain_text_placeholder_mode() -> None:
    out = to_plain_text(
        "有 $x$ 和 ![](asset://a)",
        PlainTextOptions(math=Keep.PLACEHOLDER, image=Keep.PLACEHOLDER),
    )
    assert "⟦公式⟧" in out and "⟦图⟧" in out


def test_plain_text_strips_embedded_html() -> None:
    assert to_plain_text("<div>内嵌 <b>HTML</b></div>") == "内嵌 HTML"


# ------------------------------------------------------------------ 渲染
def test_render_uses_backslash_delimiters_not_dollars() -> None:
    r"""交给前端的是 ``\(...\)``，这样 KaTeX 不必去扫 ``$``，字面 ``$`` 才安全。"""
    html = render_markdown("行内 $x^2$")
    assert r"\(x^2\)" in html
    assert 'class="math inline"' in html


def test_render_leaves_literal_dollars_untouched() -> None:
    html = render_markdown("价格 $5 和 $10，变量 $HOME")
    assert "math" not in html
    assert "$5" in html and "$HOME" in html


def test_render_resolves_asset_scheme() -> None:
    html = render_markdown(
        "![图](asset://abc123)", RenderOptions(asset_resolver=lambda sha: f"/assets/{sha}")
    )
    assert 'src="/assets/abc123"' in html
    assert "asset://" not in html


def test_render_flags_missing_asset_instead_of_breaking() -> None:
    """图丢了要看得见，不能静默渲染出一个坏图标。"""
    html = render_markdown("![图](asset://gone)", RenderOptions(asset_resolver=lambda _: None))
    assert "图片缺失" in html
    assert "<img" not in html


def test_render_strips_script_and_dangerous_urls() -> None:
    """题目可能来自同事发来的 Word/HTML，必须当作不可信内容处理。"""
    html = render_markdown(
        '<script>alert(1)</script><img src="javascript:alert(1)">'
        '<a href="javascript:alert(1)">x</a><iframe src="http://evil"></iframe>正文'
    )
    assert "script" not in html.lower()
    assert "javascript:" not in html.lower()
    assert "iframe" not in html.lower()
    assert "正文" in html


def test_render_keeps_tables_and_code() -> None:
    html = render_markdown("| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n\n```python\nx = 1\n```")
    assert "<table>" in html and "<td>" in html
    assert 'class="language-python"' in html


def test_render_mathml_is_self_contained() -> None:
    """MathML 模式用于「发给学生的单文件 HTML」：不能依赖任何 JS 或字体。"""
    html = render_markdown(r"$\frac{a}{b}$", RenderOptions(math=MathMode.MATHML))
    assert "<math" in html and "<mfrac>" in html
    assert "\\frac" not in html


def test_render_mathml_degrades_on_unsupported_macro() -> None:
    """不认识的宏要降级显示，不能让整页渲染崩掉。"""
    html = render_markdown(r"$\someMacroThatDoesNotExist{x}$", RenderOptions(math=MathMode.MATHML))
    assert html  # 没抛异常就算过
    assert "math" in html


def test_render_inline_display_math_stays_inline_level() -> None:
    """段落中间的 ``$$...$$`` 不能产出块级 div——那会把 <p> 截断。"""
    html = render_markdown("段中 $$\\sum x$$ 继续")
    assert '<div class="math inline">' not in html
    assert "段中" in html and "继续" in html
    # 同一个段落里，不该出现空的 </p><p>
    assert "<p></p>" not in html


def test_render_block_math_stays_block() -> None:
    html = render_markdown("独立\n\n$$\\sum x$$\n\n结束")
    assert 'class="math block"' in html
