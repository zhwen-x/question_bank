"""差异比对测试。

这个功能存在的理由：M1 实测确认相似重复无法自动判定，只能交给人。既然要人
判断，就必须让人**一眼看出差在哪**——面对两段几乎一样的中文逐字比对，正是
最容易看漏的场景。所以这里的测试重点是「最危险的一字之差能不能被看见」。
"""

from __future__ import annotations

import pytest

from qbank.core.content.diff import (
    Change,
    change_count,
    inline_diff,
    is_tiny_difference,
)


def rendered(left: str, right: str) -> str:
    """把差异渲染成可读形式：``[-删除-]`` / ``{+插入+}``。"""
    pieces = []
    for segment in inline_diff(left, right):
        if segment.change is Change.DELETE:
            pieces.append(f"[-{segment.text}-]")
        elif segment.change is Change.INSERT:
            pieces.append(f"{{+{segment.text}+}}")
        else:
            pieces.append(segment.text)
    return "".join(pieces)


# ------------------------------------------------- 最危险的一类：题意反转
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (
            "下列关于 while 循环的说法，正确的是",
            "下列关于 while 循环的说法，错误的是",
            "下列关于 while 循环的说法，[-正确-]{+错误+}的是",
        ),
        ("求 f(x) 的导数", "求 f(x) 的积分", "求 f(x) 的[-导数-]{+积分+}"),
        (
            "栈是一种先进先出的线性表",
            "栈是一种后进先出的线性表",
            "栈是一种[-先-]{+后+}进先出的线性表",
        ),
        (
            "下列哪些属于线性结构",
            "下列哪些属于非线性结构",
            "下列哪些属于{+非+}线性结构",
        ),
    ],
)
def test_meaning_reversal_is_visible(left: str, right: str, expected: str) -> None:
    assert rendered(left, right) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("说法正确的是", "说法错误的是"),
        ("求 f(x) 的导数", "求 f(x) 的积分"),
        ("属于线性结构", "属于非线性结构"),
        ("二次函数的图像", "二次函数的图象"),
    ],
)
def test_tiny_differences_are_flagged(left: str, right: str) -> None:
    """只差几个字时界面要额外提醒——相似度反而很高，最容易被当成重复跳过。"""
    assert is_tiny_difference(inline_diff(left, right))


def test_large_differences_are_not_flagged_as_tiny() -> None:
    assert not is_tiny_difference(
        inline_diff("求导数", "简述深度优先遍历与广度优先遍历的区别，并各举一例")
    )


# ------------------------------------------------- 格式差异不算内容差异
@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("求 **最小值**", "求 __最小值__"),
        ("题干\n\n下一段", "题干\n下一段"),
        ("题干  有空格", "题干 有空格"),
        ("求 $x^2$ 的值", "求 $x^2$ 的值"),
    ],
)
def test_formatting_differences_are_not_content_differences(left: str, right: str) -> None:
    """同一道题一个人写 ``**粗体**`` 另一个写 ``__粗体__``，不该显示成差异。"""
    assert change_count(inline_diff(left, right)) == 0


def test_identical_text_yields_single_equal_segment() -> None:
    segments = inline_diff("完全一样的题干", "完全一样的题干")
    assert len(segments) == 1
    assert segments[0].change is Change.EQUAL


def test_empty_inputs() -> None:
    assert inline_diff("", "") == []
    assert change_count(inline_diff("", "")) == 0


def test_completely_different_text() -> None:
    segments = inline_diff("二次函数的性质", "冒泡排序的复杂度")
    assert any(s.change is Change.DELETE for s in segments)
    assert any(s.change is Change.INSERT for s in segments)
    assert not is_tiny_difference(segments)


def test_formula_content_participates() -> None:
    """公式不同是实质差异——「求 $x^2$ 的导数」和「求 $x^3$ 的导数」是两道题。"""
    assert change_count(inline_diff("求 $x^2$ 的导数", "求 $x^3$ 的导数")) > 0


def test_adjacent_changes_are_merged() -> None:
    """逐字比对会把「正确→错误」切成两处，合并后读作一处，符合人的认知。"""
    segments = inline_diff("说法正确的是", "说法错误的是")
    deletes = [s.text for s in segments if s.change is Change.DELETE]
    inserts = [s.text for s in segments if s.change is Change.INSERT]
    assert deletes == ["正确"]
    assert inserts == ["错误"]


def test_high_frequency_chars_do_not_hide_changes() -> None:
    """``的``、``是`` 这类高频字不能让比对算法跳过真正的差异。

    difflib 的 autojunk 默认会把高频元素当「垃圾」，对中文恰好是灾难。
    """
    left = "这是一道题，题目的内容是关于栈的，栈的特点是后进先出的"
    right = "这是一道题，题目的内容是关于队列的，队列的特点是先进先出的"
    assert change_count(inline_diff(left, right)) > 0
    assert "队列" in rendered(left, right)


def test_raw_mode_keeps_markup() -> None:
    assert change_count(inline_diff("**粗**", "__粗__", plain=False)) > 0
