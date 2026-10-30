"""中文检索的分词：索引端与查询端的唯一入口。

**为什么不用 SQLite 自带的分词器**：实测（本机 SQLite 3.37）``trigram`` 要求
查询串 ≥ 3 字符，``函数``、``性质`` 这类 2 字中文词返回 0 条；``unicode61``
根本不切中文，整句话是一个 token。

**为什么不用 jieba 分词**：一开始就是 jieba 方案，实测踩了一个很隐蔽的坑——
**jieba 的切分结果依赖上下文**：

================================  ===============================
输入                              ``cut_for_search`` 结果
================================  ===============================
``用 Dijkstra 求最短路径``        ``用 dijkstra 求 最 短 路径``
``最短路径``（单独作为查询串）    ``最短 路径``
================================  ===============================

于是索引里只有 ``最``、``短``，查询却要求 ``最短``，这道题**在库里但搜不到**。
「索引端和查询端调同一个函数」挡不住这个问题，因为函数本身不是上下文无关的。

**现在的方案：字符 n-gram**（Lucene 的 CJK 分析器用的也是这个思路）

* 连续中日韩字符 → 拆成**一元 + 二元**。一元是必须的：``栈``、``图``、``树``、
  ``环``这些单字术语在计算机和数学题库里极常见，只有二元的话单字查询全部失效。
* 拉丁字母与数字的连续段 → 整段保留，并套用别名表（``C++`` → ``cpp``，
  因为 unicode61 会把 ``+`` 当分隔符，不处理的话 ``C++`` 和 ``C`` 撞在一起）。

这个切分是**上下文无关**的：同样的字符串在任何位置都切出同样的 token，所以
两端必然一致。代价是索引变大，以及子串式的误配——搜 ``短路`` 会命中
「最**短路**径」。对题库而言这是可接受的：召回比精确更重要，多出几条让人扫一眼
的成本远低于「明明有这道题却搜不到、只好重新录一遍」。
"""

from __future__ import annotations

import re
import unicodedata

#: 连续的中日韩字符（含中文、日文假名、韩文）
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]+")

#: 连续的拉丁字母、数字与技术标识里常见的符号
_LATIN_RUN = re.compile(r"[0-9a-z][0-9a-z#+._\-]*")

#: 一次扫描同时切出两类 token
_TOKEN_RUN = re.compile(f"{_CJK_RUN.pattern}|{_LATIN_RUN.pattern}")

_WS = re.compile(r"\s+")

#: unicode61 把 ``+ # .`` 当分隔符，于是 ``C++`` 和 ``C#`` 都退化成 ``c``。
#: 对编程类课程这是硬伤，所以在归一化阶段换成可索引的别名。
#: 索引端与查询端共用本表，两边始终一致。
_ALIASES = (
    ("c++", " cpp "),
    ("c#", " csharp "),
    ("f#", " fsharp "),
    (".net", " dotnet "),
    ("objective-c", " objectivec "),
)

#: CJK 段落拆 n-gram 时的最大 n。只到 2：三元及以上对召回没有额外帮助
#: （二元的 AND 已经等价于子串匹配），只会让索引膨胀。
MAX_NGRAM = 2


def normalize(text: str) -> str:
    """全角转半角、统一大小写与空白，并把 ``C++`` 一类符号名换成可索引别名。

    >>> normalize("ＡＢＣ")
    'abc'
    >>> normalize("C++ 与 C#")
    'cpp 与 csharp'
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    for literal, alias in _ALIASES:
        folded = folded.replace(literal, alias)
    return _WS.sub(" ", folded).strip()


def _run_tokens(run: str) -> list[str]:
    """把一个连续段切成 token。"""
    if not _CJK_RUN.fullmatch(run):
        return [run]
    tokens = list(run)  # 一元：支撑「栈」「图」「树」这类单字术语的检索
    tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # 二元
    return tokens


def tokenize(text: str) -> list[str]:
    """切出 token，保持出现顺序，不去重。

    >>> tokenize("求最短路径")
    ['求', '最', '短', '路', '径', '求最', '最短', '短路', '路径']
    >>> tokenize("用 C++ 写")
    ['用', 'cpp', '写']
    >>> tokenize("Dijkstra 算法")
    ['dijkstra', '算', '法', '算法']
    >>> tokenize("！！！")
    []
    """
    normalized = normalize(text)
    tokens: list[str] = []
    for match in _TOKEN_RUN.finditer(normalized):
        tokens.extend(_run_tokens(match.group(0)))
    return tokens


def segment(text: str) -> str:
    """供 FTS5 ``unicode61`` **写索引**用的 token 串。

    >>> segment("二次函数")
    '二 次 函 数 二次 次函 函数'
    >>> segment("")
    ''
    """
    return " ".join(tokenize(text))


def to_match_query(user_input: str) -> str:
    """把用户输入转成 FTS5 ``MATCH`` 表达式。

    各 token 之间是 AND。对 CJK 而言，「全部二元组都出现」近似等价于子串匹配，
    这正是用户对搜索框的预期。

    返回空串表示「无有效检索词」，调用方应改为**不加全文条件**，而不是认为
    「没有任何题匹配」——这两种语义混淆会让搜索框看起来完全失效。

    >>> to_match_query("最短路径")
    '"最" AND "短" AND "路" AND "径" AND "最短" AND "短路" AND "路径"'
    >>> to_match_query("栈")
    '"栈"'
    >>> to_match_query("！！！")
    ''
    """
    tokens = tokenize(user_input)
    if not tokens:
        return ""
    # 去重但保持顺序，避免重复条件
    unique = dict.fromkeys(tokens)
    return " AND ".join(f'"{token}"' for token in unique)
