"""文本差异比对，用于导入预览里的相似题对照。

**为什么这个功能是必需的**：M1 的实测结论是相似重复无法自动判定——改一个字
就能把题意反过来（正确↔错误、导数↔积分、握手↔挥手），而这类「不同的题」
与「同一道题的变体」相似度区间是交叠的。既然最终要靠人判断，那就必须让人
**一眼看出差在哪**。只给一个「相似度 85%」的数字，用户要逐字比对两段几乎
一样的中文，这正是最容易看漏的场景。

按**字符**而不是按词比对：中文没有天然词边界，而这里要抓的恰恰是「正确→错误」
这种一两个字的改动，字符级粒度才看得见。

比对前先转成纯文本，因为要比的是「题目内容是否相同」，不是 Markdown 写法的
差异——同一道题一个人写 ``**粗体**`` 另一个写 ``__粗体__``，不该显示成差异。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

from qbank.core.content.text import FOR_FINGERPRINT, to_plain_text

#: 相邻的同类片段合并前，单个片段的最小长度。
#: 不合并的话中文差异会碎成一个个单字，读起来反而更费劲。
MERGE_GAP = 2


class Change(str, Enum):
    EQUAL = "equal"
    INSERT = "insert"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class Segment:
    """差异中的一段。

    ``DELETE`` 来自左边（库里已有的题），``INSERT`` 来自右边（要导入的题）。
    """

    text: str
    change: Change

    @property
    def is_change(self) -> bool:
        return self.change is not Change.EQUAL


def _merge(segments: list[Segment]) -> list[Segment]:
    """合并相邻的同类片段，并把夹在改动之间的极短相同片段也并进去。

    「正确的是」→「错误的是」逐字比对会切成「正/错」「确/误」两处改动，中间
    夹着空片段；合并后读作一处「正确→错误」，符合人的认知。
    """
    merged: list[Segment] = []
    for segment in segments:
        if not segment.text:
            continue
        if merged and merged[-1].change is segment.change:
            merged[-1] = Segment(merged[-1].text + segment.text, segment.change)
        else:
            merged.append(segment)

    # 第二遍：把被改动包夹的超短 EQUAL 片段并进改动里
    if len(merged) < 3:
        return merged
    result: list[Segment] = [merged[0]]
    index = 1
    while index < len(merged):
        current = merged[index]
        bridged = (
            current.change is Change.EQUAL
            and len(current.text) < MERGE_GAP
            and index + 1 < len(merged)
            and result[-1].is_change
            and merged[index + 1].is_change
        )
        if bridged:
            # 并到前一段改动里，并把后一段也接上
            result[-1] = Segment(result[-1].text + current.text, result[-1].change)
            index += 1
            continue
        if result and result[-1].change is current.change:
            result[-1] = Segment(result[-1].text + current.text, current.change)
        else:
            result.append(current)
        index += 1
    return result


def inline_diff(left: str, right: str, *, plain: bool = True) -> list[Segment]:
    """按字符比对两段文本，返回行内差异片段。

    ``plain=True``（默认）先剥掉 Markdown 标记再比——要比的是内容，不是写法。

    >>> [(s.change.value, s.text) for s in inline_diff("说法正确的是", "说法错误的是")]
    [('equal', '说法'), ('delete', '正确'), ('insert', '错误'), ('equal', '的是')]
    >>> [(s.change.value, s.text) for s in inline_diff("同样的题", "同样的题")]
    [('equal', '同样的题')]
    >>> inline_diff("", "")
    []
    """
    old = to_plain_text(left, FOR_FINGERPRINT) if plain else left
    new = to_plain_text(right, FOR_FINGERPRINT) if plain else right
    if not old and not new:
        return []

    # autojunk 会在长文本里把高频字符当「垃圾」跳过，中文题干里的「的」「是」
    # 正好是高频字——开着它会漏掉真正的差异
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    segments: list[Segment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segments.append(Segment(old[i1:i2], Change.EQUAL))
        elif tag == "delete":
            segments.append(Segment(old[i1:i2], Change.DELETE))
        elif tag == "insert":
            segments.append(Segment(new[j1:j2], Change.INSERT))
        else:  # replace
            segments.append(Segment(old[i1:i2], Change.DELETE))
            segments.append(Segment(new[j1:j2], Change.INSERT))
    return _merge(segments)


def change_count(segments: list[Segment]) -> int:
    """有多少处改动。用于「仅 1 处差异」这类提示。

    >>> change_count(inline_diff("说法正确的是", "说法错误的是"))
    2
    >>> change_count(inline_diff("一样", "一样"))
    0
    """
    return sum(1 for segment in segments if segment.is_change)


def is_tiny_difference(segments: list[Segment], *, max_chars: int = 4) -> bool:
    """差异是否「只有几个字」。

    这种情况最危险：肉眼几乎看不出来，但很可能正是「正确↔错误」这类把题意
    反过来的改动。界面要对它**额外**提醒，而不是因为相似度高就淡化处理。

    >>> is_tiny_difference(inline_diff("说法正确的是", "说法错误的是"))
    True
    >>> is_tiny_difference(inline_diff("求导数", "简述深度优先遍历与广度优先遍历的区别"))
    False
    """
    changed = sum(len(segment.text) for segment in segments if segment.is_change)
    total = sum(len(segment.text) for segment in segments)
    return bool(changed) and changed <= max_chars and total > changed
