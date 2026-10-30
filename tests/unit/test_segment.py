"""中文检索的回归测试。

``test_two_char_cjk_terms_are_matchable`` 锁死的是项目最早期的一个关键发现：
SQLite FTS5 的 ``trigram`` 分词器对 2 字中文词返回 0 条。若有人日后把分词层
换回 trigram、或让索引端与查询端的分词方式不一致，这个测试会立刻失败。
"""

from __future__ import annotations

import sqlite3

import pytest

from qbank.core.content.segment import normalize, segment, to_match_query


def _fts_hits(indexed_text: str, user_query: str, *, tokenize: str = "unicode61") -> int:
    con = sqlite3.connect(":memory:")
    try:
        con.execute(f"CREATE VIRTUAL TABLE probe USING fts5(x, tokenize='{tokenize}')")
        con.execute("INSERT INTO probe(x) VALUES (?)", (indexed_text,))
        query = to_match_query(user_query)
        if not query:
            return 0
        return int(
            con.execute("SELECT count(*) FROM probe WHERE probe MATCH ?", (query,)).fetchone()[0]
        )
    finally:
        con.close()


STEM = "二次函数的图像与性质，以及 while 循环在 C++ 中的边界条件"


@pytest.mark.parametrize("term", ["函数", "性质", "图像", "二次函数", "边界条件", "while", "C++"])
def test_two_char_cjk_terms_are_matchable(term: str) -> None:
    """分词后入索引，2 字中文词与符号型技术名都应命中。"""
    assert _fts_hits(segment(STEM), term) == 1, f"{term!r} 未命中"


def test_trigram_fails_on_two_char_cjk() -> None:
    """固化「为什么不能用 trigram」这一结论。

    一旦某天 SQLite 放宽了 trigram 的 3 字符下限，这个测试会失败，
    提醒我们可以重新评估分词方案。
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


def test_index_and_query_use_same_normalization() -> None:
    """全角输入应能命中半角内容，反之亦然。"""
    assert _fts_hits(segment("Python 循环"), "ｐｙｔｈｏｎ") == 1
    assert _fts_hits(segment("ＰＹＴＨＯＮ 循环"), "python") == 1


def test_cpp_and_csharp_do_not_collide() -> None:
    """``C++`` 不应被 unicode61 退化成 ``c`` 而和 ``C#`` 混淆。"""
    assert _fts_hits(segment("用 C++ 实现栈"), "C++") == 1
    assert _fts_hits(segment("用 C++ 实现栈"), "C#") == 0
    assert _fts_hits(segment("用 C# 实现栈"), "C#") == 1


def test_pure_punctuation_yields_no_query() -> None:
    assert segment("！！！？？") == ""
    assert to_match_query("！！！？？") == ""
    assert to_match_query("") == ""


def test_stopwords_dropped_from_query_only() -> None:
    query = to_match_query("下列关于 while 循环的说法")
    assert "的" not in query and "下列" not in query and "关于" not in query
    assert '"while"' in query and '"循环"' in query
    # 索引端必须保留虚词
    assert "的" in segment("下列关于 while 循环的说法").split()


def test_all_stopword_query_falls_back() -> None:
    """用户就是想搜虚词时，不能返回空查询。"""
    assert to_match_query("的") == '"的"'


def test_normalize_aliases() -> None:
    assert normalize("C++ 与 C# 和 .NET") == "cpp 与 csharp 和 dotnet"
