"""查重测试。

``test_no_threshold_separates_variants_from_distinct_questions`` 是本文件的
核心：它把「表面相似度无法自动判定近似重复」这个实测结论固化下来。如果将来
有人想把相似度判定改成自动跳过/自动覆盖，这个测试会告诉他为什么不行。
"""

from __future__ import annotations

import pytest

from qbank.core.services.dedup import (
    SIMILARITY_THRESHOLD,
    Candidate,
    canonical_text,
    fingerprint,
    rank_similar,
    shingles,
    similarity,
)

#: 应被判为「同一道题的变体」的成对题干
SAME_QUESTION_PAIRS = [
    ("二次函数的图像与性质", "二次函数的图象与性质", "像/象 异体字"),
    ("求函数 f(x)=x^2+2x+1 的最小值", "求函数 f(x)=x^2+2x+3 的最小值", "换数字"),
    ("冒泡排序的平均时间复杂度是多少", "冒泡排序的平均时间复杂度为多少", "换虚词"),
    ("简述 TCP 三次握手的过程", "简述 TCP 三次握手过程", "少一字"),
]

#: 表面高度相似但**语义相反/不同**的成对题干
DISTINCT_QUESTION_PAIRS = [
    ("下列关于 while 循环的说法正确的是", "下列关于 while 循环的说法错误的是", "正确↔错误"),
    ("求函数 f(x)=x^2 的导数", "求函数 f(x)=x^2 的积分", "导数↔积分"),
    ("简述 TCP 三次握手的过程", "简述 TCP 四次挥手的过程", "握手↔挥手"),
]


# -------------------------------------------------------------- 规范化与指纹
@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("下列说法正确的是（  ）", "下列说法正确的是 ( )"),
        ("题干", "题干　"),  # 全角空格
        ("A. 对", "A.对"),
        ("ＡＢＣ", "abc"),  # 全角字母
        ("多行\n题干", "多行 题干"),
    ],
)
def test_canonical_text_ignores_formatting_noise(left: str, right: str) -> None:
    assert canonical_text([left]) == canonical_text([right])
    assert fingerprint([left]) == fingerprint([right])


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("求 $x^2$ 的导数", "求 $x^3$ 的导数"),  # 公式不同
        ("看图 ![](asset://aaa)", "看图 ![](asset://bbb)"),  # 配图不同
        ("题干甲", "题干乙"),
    ],
)
def test_fingerprint_distinguishes_real_differences(left: str, right: str) -> None:
    assert fingerprint([left]) != fingerprint([right])


def test_fingerprint_covers_all_parts() -> None:
    """同题干不同选项必须是不同的题，所以选项也要进指纹。"""
    assert fingerprint(["题干", "A. 甲", "B. 乙"]) != fingerprint(["题干", "A. 甲", "B. 丙"])


def test_empty_content_has_no_fingerprint() -> None:
    assert fingerprint([""]) == ""
    assert fingerprint(["", "   "]) == ""


# -------------------------------------------------------------- 相似度
def test_shingles_handles_text_shorter_than_window() -> None:
    """判断题常只有十几个字，短于窗口时不能退化成空集合。"""
    assert shingles(["对"]) == frozenset({"对"})
    assert shingles([""]) == frozenset()


def test_similarity_bounds() -> None:
    s = shingles(["二次函数的图像与性质"])
    assert similarity(s, s) == 1.0
    assert similarity(s, shingles(["完全不相干的内容"])) < 0.2
    assert similarity(frozenset(), s) == 0.0


@pytest.mark.parametrize(("left", "right", "desc"), SAME_QUESTION_PAIRS)
def test_variants_are_surfaced_as_candidates(left: str, right: str, desc: str) -> None:
    """同题变体必须进入候选列表（阈值偏向召回）。"""
    score = similarity(shingles([left]), shingles([right]))
    assert score >= SIMILARITY_THRESHOLD, f"{desc} 相似度仅 {score:.3f}，会被漏报"


def test_no_threshold_separates_variants_from_distinct_questions() -> None:
    """固化实测结论：表面相似度**不能**自动判定近似重复。

    同题变体的相似度区间与「语义相反的不同题」交叠，因此不存在可用的分界线。
    改一个字就能把题意反过来（正确↔错误、导数↔积分），这是语义问题，表面
    相似度看不出来。

    结论：相似候选只排序给人确认，绝不驱动自动动作。若哪天这个断言失败了
    （说明找到了可分的度量），才可以重新考虑自动判定。
    """
    same_scores = [similarity(shingles([a]), shingles([b])) for a, b, _ in SAME_QUESTION_PAIRS]
    distinct_scores = [
        similarity(shingles([a]), shingles([b])) for a, b, _ in DISTINCT_QUESTION_PAIRS
    ]
    assert min(same_scores) <= max(distinct_scores), (
        "同题变体与异题的相似度区间不再交叠，可以重新评估自动判重：\n"
        f"  同题最低 {min(same_scores):.3f} / 异题最高 {max(distinct_scores):.3f}"
    )


# -------------------------------------------------------------- 候选排序
def test_rank_similar_puts_exact_first() -> None:
    target_fp = fingerprint(["题干甲"])
    near = shingles(["二次函数的图像与性质"])
    hints = rank_similar(
        target_fp,
        near,
        [
            Candidate("q-near", "other", shingles(["二次函数的图象与性质"])),
            Candidate("q-exact", target_fp, frozenset()),
            Candidate("q-unrelated", "other2", shingles(["冒泡排序的时间复杂度"])),
        ],
    )
    assert [h.question_id for h in hints] == ["q-exact", "q-near"]
    assert hints[0].exact and hints[0].label == "完全相同"
    assert not hints[1].exact


def test_rank_similar_respects_limit_and_order() -> None:
    target = shingles(["二次函数的图像与性质"])
    candidates = [
        Candidate(f"q{i}", f"fp{i}", shingles(["二次函数的图像与性质" + "。" * i]))
        for i in range(6)
    ]
    hints = rank_similar("none", target, candidates, limit=3)
    assert len(hints) == 3
    assert [h.score for h in hints] == sorted((h.score for h in hints), reverse=True)


def test_rank_similar_empty_target_yields_nothing() -> None:
    """新题没有可比内容时不该报出一堆噪声候选。"""
    assert rank_similar("", frozenset(), [Candidate("q1", "fp", shingles(["任意"]))]) == []


# -------------------------------------------------------- 剪枝的正确性
def test_length_bound_pruning_never_changes_results() -> None:
    """长度界剪枝必须是**无损**的。

    ``rank_similar`` 用 ``Jaccard ≤ min(|A|,|B|)/max(|A|,|B|)`` 跳过不可能达标的
    候选。这是个可证明的上界，但写错一个不等号就会静默漏掉真正的重复——而
    漏掉重复正是这整套机制要防的事。所以用随机数据与朴素实现逐一对照。
    """
    import random

    random.seed(20261031)
    alphabet = "数据结构算法栈队列图树排序查找复杂度递归循环分支函数指针链表哈希"

    def text(length: int) -> str:
        return "".join(random.choice(alphabet) for _ in range(length))

    candidates = [
        Candidate(f"q{i}", f"fp{i}", shingles([text(random.randint(5, 80))])) for i in range(300)
    ]

    def without_pruning(
        target_fingerprint: str, target: frozenset[str]
    ) -> list[tuple[str, bool, float]]:
        found: list[tuple[str, bool, float]] = []
        for candidate in candidates:
            if target_fingerprint and candidate.fingerprint == target_fingerprint:
                found.append((candidate.question_id, True, 1.0))
                continue
            score = similarity(target, candidate.shingle_set)
            if score >= SIMILARITY_THRESHOLD:
                found.append((candidate.question_id, False, score))
        found.sort(key=lambda item: (not item[1], -item[2], item[0]))
        return found[:10]

    for trial in range(200):
        target = shingles([text(random.randint(5, 80))])
        fingerprint_value = "fp7" if trial % 40 == 0 else "no-match"
        actual = [
            (hint.question_id, hint.exact, round(hint.score, 9))
            for hint in rank_similar(fingerprint_value, target, candidates)
        ]
        expected = [
            (qid, exact, round(score, 9))
            for qid, exact, score in without_pruning(fingerprint_value, target)
        ]
        assert actual == expected, f"第 {trial} 轮剪枝改变了结果"


def test_length_bound_does_not_prune_exact_fingerprint() -> None:
    """长度悬殊但指纹相同时，必须仍然认出是同一道题。

    指纹相同意味着规范化文本完全一致，长度也必然一致——但剪枝逻辑放在指纹
    判定**之后**才是安全的，这个测试锁住这个顺序。
    """
    target_fingerprint = fingerprint(["很短"])
    hints = rank_similar(
        target_fingerprint,
        shingles(["很短"]),
        [Candidate("q1", target_fingerprint, shingles(["完全不同而且长得多的一段题干内容"]))],
    )
    assert [h.question_id for h in hints] == ["q1"]
    assert hints[0].exact
