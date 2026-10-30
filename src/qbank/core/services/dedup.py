"""查重：精确指纹 + 相似候选排序。

**为什么导入时必须查重**：批量导入的来源是一堆历年 Word 文件，同一道题在不同
年份的卷子里反复出现是常态。不查重，库用上两个学期就脏得没法自动组卷——组出来
的卷子里会出现两道一模一样的题。

----

实测结论：近似重复不能自动判定，只能排序给人看
=================================================

最初的设计是 simhash + 汉明距离阈值。实测（字符 2/3/4 元组与词级都试过）发现
**任何单一阈值都无法把「同一道题的变体」和「不同的题」分开**：

==========================================  ==============  ==============
一对题目                                    3 元组 Jaccard  判定应为
==========================================  ==============  ==============
「…的平均时间复杂度是多少」/「…为多少」     0.625           同一道题
「…说法**正确**的是」/「…说法**错误**的是」 0.600           **不同的题**
「求 f(x) 的**导数**」/「求 f(x) 的**积分**」 0.600         **不同的题**
「求 f(x)=x²+2x+**1**…」/「…+**3**…」       0.600           同一道题
「二次函数的图**像**」/「图**象**」         0.455           同一道题
==========================================  ==============  ==============

根因很简单：**改一个字就能把题意反过来**（正确↔错误、导数↔积分、握手↔挥手），
而任何表面相似度都看不出这层语义。同题变体最低 0.455，异题最高 0.600，区间
是交叠的，不存在可用的分界线。

所以本模块的职责划分是：

:func:`fingerprint`
    精确指纹。可靠，**可以**驱动自动动作（自动跳过、自动更新同一道题）。

:func:`rank_similar`
    相似候选排序。**绝不**自动处理，只把候选按相似度排好交给导入预览页，由人
    确认。阈值取得偏低（偏向召回），因为误报的代价只是用户多看一眼，而漏报的
    代价是库里多一道重复题。

界面上展示相似候选时应同时显示**差异高亮**——「正确」变「错误」这种一字之差
肉眼很容易看漏，而这正是最常见的误判来源。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from qbank.core.content.segment import normalize
from qbank.core.content.text import FOR_FINGERPRINT, to_plain_text

#: 字符 n 元组的 n。取 3 在实测里综合最好：2 太容易撞，4 对短题干过于敏感。
SHINGLE_SIZE = 3

#: 进入「相似候选」列表的下限。偏向召回，见模块文档。
SIMILARITY_THRESHOLD = 0.45

#: 规范化时丢弃的字符：各类空白与中英文标点。
#: 「下列说法正确的是（ ）」与「下列说法正确的是 ( )」必须算同一道题。
_NOISE = re.compile(r"[\s　-〿！-／：-＠［-｀｛-･!-/:-@\[-`{-~]+")


def canonical_text(parts: Iterable[str]) -> str:
    """把题目各部分拼成用于比较的规范文本。

    流程：Markdown → 纯文本（**保留**公式源码与图片哈希）→ 全角转半角、小写
    → 去掉全部空白与标点。

    >>> canonical_text(["下列说法正确的是（  ）", "A. 对"])
    '下列说法正确的是a对'
    >>> canonical_text(["下列说法正确的是 ( )", "A.对"])
    '下列说法正确的是a对'
    >>> canonical_text(["求 $x^2$ 的导数"]) != canonical_text(["求 $x^3$ 的导数"])
    True
    """
    plain = " ".join(to_plain_text(part, FOR_FINGERPRINT) for part in parts if part)
    return _NOISE.sub("", normalize(plain))


def fingerprint(parts: Iterable[str]) -> str:
    """精确指纹（SHA-256 十六进制）。空内容返回空串，调用方应视为「无指纹」。

    >>> fingerprint(["题干"]) == fingerprint(["题干 "])
    True
    >>> fingerprint(["题干"]) == fingerprint(["别的题干"])
    False
    >>> fingerprint([""])
    ''
    """
    text = canonical_text(parts)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def shingles(parts: Iterable[str], size: int = SHINGLE_SIZE) -> frozenset[str]:
    """规范文本的字符 n 元组集合。

    文本短于 n 时退化为整串，避免短题干（判断题常只有十来个字）得到空集合。

    >>> sorted(shingles(["abcd"], size=3))
    ['abc', 'bcd']
    >>> sorted(shingles(["ab"], size=3))
    ['ab']
    >>> shingles([""])
    frozenset()
    """
    text = canonical_text(parts)
    if not text:
        return frozenset()
    if len(text) <= size:
        return frozenset({text})
    return frozenset(text[i : i + size] for i in range(len(text) - size + 1))


def similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard 相似度，0.0 ~ 1.0。任一为空返回 0.0。

    >>> similarity(frozenset("abc"), frozenset("abc"))
    1.0
    >>> similarity(frozenset("abc"), frozenset("xyz"))
    0.0
    >>> round(similarity(frozenset("abc"), frozenset("abcd")), 3)
    0.75
    >>> similarity(frozenset(), frozenset("abc"))
    0.0
    """
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass(frozen=True, slots=True)
class DuplicateHint:
    """一条查重结果。

    ``exact`` 为真时才可以驱动自动动作；否则一律交给用户确认。
    """

    question_id: str
    exact: bool
    score: float

    @property
    def label(self) -> str:
        if self.exact:
            return "完全相同"
        if self.score >= 0.85:
            return "几乎相同"
        if self.score >= 0.65:
            return "高度相似"
        return "可能相似"


@dataclass(frozen=True, slots=True)
class Candidate:
    """参与比较的库内题目。``shingle_set`` 由调用方用 :func:`shingles` 算好。"""

    question_id: str
    fingerprint: str
    shingle_set: frozenset[str]


def rank_similar(
    target_fingerprint: str,
    target_shingles: frozenset[str],
    candidates: Sequence[Candidate],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
    limit: int = 10,
) -> list[DuplicateHint]:
    """找出重复与相似的题，按「精确优先、相似度降序」排列。

    候选集应由调用方先缩小（同课程 + 同题型，或全文检索的前 N 条）；本函数做
    精确的集合运算，不负责也不应该负责全表扫描。

    >>> a = shingles(["二次函数的图像与性质"])
    >>> b = shingles(["二次函数的图象与性质"])
    >>> c = shingles(["冒泡排序的时间复杂度"])
    >>> hints = rank_similar("fp-x", a, [
    ...     Candidate("q1", "fp-x", frozenset()),
    ...     Candidate("q2", "fp-y", b),
    ...     Candidate("q3", "fp-z", c),
    ... ])
    >>> [(h.question_id, h.exact) for h in hints]
    [('q1', True), ('q2', False)]
    """
    hints: list[DuplicateHint] = []
    for candidate in candidates:
        if target_fingerprint and candidate.fingerprint == target_fingerprint:
            hints.append(DuplicateHint(candidate.question_id, exact=True, score=1.0))
            continue
        score = similarity(target_shingles, candidate.shingle_set)
        if score >= threshold:
            hints.append(DuplicateHint(candidate.question_id, exact=False, score=score))
    hints.sort(key=lambda h: (not h.exact, -h.score, h.question_id))
    return hints[:limit]
