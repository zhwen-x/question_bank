"""中文检索分词的回归测试。

这组测试锁住的是项目早期踩到的两个坑：

1. SQLite FTS5 的 ``trigram`` 分词器对 2 字中文词返回 0 条
   （:func:`test_trigram_fails_on_two_char_cjk`）。
2. jieba 的切分**依赖上下文**，导致索引端切出 ``最 短 路径``、查询端切出
   ``最短 路径``，于是「题在库里却搜不到」
   （:func:`test_tokenize_is_context_free`）。

第 2 条是本文件最重要的一条：它保证任何未来的分词改动都必须是上下文无关的。
"""

from __future__ import annotations

import sqlite3

import pytest

from qbank.core.content.segment import normalize, segment, to_match_query, tokenize


def _hits(indexed_text: str, user_query: str) -> int:
    """按线上同一条路径建索引并查询。"""
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE probe USING fts5(x, tokenize='unicode61')")
        con.execute("INSERT INTO probe(x) VALUES (?)", (segment(indexed_text),))
        query = to_match_query(user_query)
        if not query:
            return 0
        return int(
            con.execute("SELECT count(*) FROM probe WHERE probe MATCH ?", (query,)).fetchone()[0]
        )
    finally:
        con.close()


STEM = "用 Dijkstra 求最短路径，并讨论二次函数的图像与性质，以及 C++ 中栈的边界条件"


# --------------------------------------------------------- 上下文无关（核心）
@pytest.mark.parametrize(
    "fragment",
    ["最短路径", "最短", "路径", "二次函数", "函数", "性质", "图像", "边界条件", "栈"],
)
def test_tokenize_is_context_free(fragment: str) -> None:
    """片段单独切分得到的 token，必须全部出现在整句切分的结果里。

    这是「索引端与查询端一致」的充分条件。jieba 方案正是在这里失败的：
    ``最短路径`` 单独切出 ``最短``，而整句里切出的是 ``最``、``短``。
    """
    whole = set(tokenize(STEM))
    missing = [token for token in tokenize(fragment) if token not in whole]
    assert not missing, f"片段 {fragment!r} 切出的 {missing} 不在整句 token 中，索引与查询会不一致"


@pytest.mark.parametrize(
    "term",
    ["函数", "性质", "图像", "最短路径", "最短", "二次函数", "边界条件", "栈", "C++", "Dijkstra"],
)
def test_terms_are_searchable(term: str) -> None:
    assert _hits(STEM, term) == 1, f"{term!r} 未命中"


def test_single_cjk_character_terms_work() -> None:
    """``栈``、``图``、``树``、``环`` 这类单字术语在理工科题库里极常见。

    只索引二元组的话这些查询会全部失效，所以必须同时索引一元组。
    """
    for term in ["栈", "图", "树"]:
        assert _hits("栈与队列、图与树的遍历", term) == 1, f"单字 {term!r} 搜不到"


# --------------------------------------------------------- trigram 的失效
def test_trigram_fails_on_two_char_cjk() -> None:
    """固化「为什么不能直接用 trigram」。

    一旦 SQLite 放宽 trigram 的 3 字符下限，这个测试会失败，提醒我们可以
    重新评估分词方案。
    """
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
        con.execute("INSERT INTO t(x) VALUES (?)", (STEM,))
        two_char = con.execute("SELECT count(*) FROM t WHERE t MATCH '函数'").fetchone()[0]
        three_char = con.execute("SELECT count(*) FROM t WHERE t MATCH '二次函'").fetchone()[0]
    finally:
        con.close()
    assert two_char == 0, "trigram 居然命中了 2 字词，可重新评估分词方案"
    assert three_char == 1


# --------------------------------------------------------- 归一化
def test_fullwidth_and_halfwidth_match_each_other() -> None:
    assert _hits("Python 循环", "ｐｙｔｈｏｎ") == 1
    assert _hits("ＰＹＴＨＯＮ 循环", "python") == 1


def test_cpp_and_csharp_do_not_collide() -> None:
    """unicode61 把 ``+`` 当分隔符，不处理的话 ``C++`` 会退化成 ``c``。"""
    assert _hits("用 C++ 实现栈", "C++") == 1
    assert _hits("用 C++ 实现栈", "C#") == 0
    assert _hits("用 C# 实现栈", "C#") == 1
    assert _hits("用 C# 实现栈", "C++") == 0


def test_normalize_aliases() -> None:
    assert normalize("C++ 与 C# 和 .NET") == "cpp 与 csharp 和 dotnet"


# --------------------------------------------------------- 边界
def test_pure_punctuation_yields_no_query() -> None:
    """空查询与「查无结果」是两回事：前者应不加全文条件，后者应返回零条。"""
    assert tokenize("！！！？？") == []
    assert to_match_query("！！！？？") == ""
    assert to_match_query("") == ""
    assert segment("　　") == ""


def test_unrelated_terms_do_not_match() -> None:
    assert _hits(STEM, "哈希冲突") == 0
    assert _hits(STEM, "动态规划") == 0


def test_substring_matching_is_expected_behaviour() -> None:
    """字符 n-gram 带来子串式命中：搜「短路」会命中「最短路径」。

    这是刻意接受的代价——题库场景里召回比精确重要，多出几条让人扫一眼远好过
    「明明有这道题却搜不到」。
    """
    assert _hits("用 Dijkstra 求最短路径", "短路") == 1


def test_multi_word_query_is_conjunctive() -> None:
    assert _hits(STEM, "函数 性质") == 1
    assert _hits(STEM, "函数 哈希") == 0


def test_tokens_are_deduplicated_in_query() -> None:
    query = to_match_query("函数函数")
    assert query.count('"函数"') == 1
