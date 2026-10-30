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
