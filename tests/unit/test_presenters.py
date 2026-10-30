"""视图模型测试。

``stem_excerpt`` 是其中最容易出事的一环：列表页要截断题干，而从中间切断
Markdown 会留下未闭合的 ``$`` 或代码围栏，渲染出来是一团乱码，还会把后面
整个列表的排版带歪。
"""

from __future__ import annotations

from qbank.core.content.render import MathMode, RenderOptions, render_markdown
from qbank.web.presenters import stem_excerpt


def test_short_stem_is_unchanged() -> None:
    assert stem_excerpt("下列代码的输出是？") == "下列代码的输出是？"


def test_excerpt_stops_at_paragraph_boundary() -> None:
    """按段落截断而不是按字符硬截——第一段通常既短又完整。"""
    stem = "下列代码的输出是？\n\n```python\nn = 1\nfor i in range(10):\n    n *= 2\n```"
    assert stem_excerpt(stem) == "下列代码的输出是？"
    assert "```" not in stem_excerpt(stem)


def test_excerpt_keeps_formula_intact() -> None:
    assert stem_excerpt("求 $x^2 + 2x + 1$ 的最小值") == "求 $x^2 + 2x + 1$ 的最小值"


def test_long_single_paragraph_is_clipped_safely() -> None:
    """超长的单段才会硬截，此时必须补回未闭合的符号。"""
    stem = "甲" * 300 + " $x^2$ 乙"
    excerpt = stem_excerpt(stem, limit=50)
    assert len(excerpt) <= 60
    assert excerpt.endswith("…")
    # 截断处不会留下未配对的 $
    assert excerpt.count("$") % 2 == 0


def test_clipped_code_fence_is_closed() -> None:
    stem = "```python\n" + "x = 1\n" * 80
    excerpt = stem_excerpt(stem, limit=40)
    assert excerpt.count("```") % 2 == 0


def test_excerpt_renders_without_leaking() -> None:
    """截断后的片段渲染出来不能带未闭合标签——那会把后面的列表撑坏。"""
    stem = "甲" * 300
    html = render_markdown(stem_excerpt(stem, limit=50), RenderOptions(math=MathMode.RAW))
    assert html.count("<p>") == html.count("</p>")


def test_empty_stem() -> None:
    assert stem_excerpt("") == ""
    assert stem_excerpt("   \n\n  ") == ""
