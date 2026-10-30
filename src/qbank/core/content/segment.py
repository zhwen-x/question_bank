"""中文分词：索引端与查询端的唯一入口。

**为什么需要这个模块**：SQLite FTS5 自带的分词器都处理不好中文。实测（本机
SQLite 3.37）：

====================  ==================  ============================
查询                  ``trigram``         jieba 分词 + ``unicode61``
====================  ==================  ============================
``函数``              0 条 ❌             1 条 ✅
``性质``              0 条 ❌             1 条 ✅
``二次函``            1 条                —
====================  ==================  ============================

``trigram`` 要求查询串 ≥ 3 字符，而中文检索词绝大多数是 2 字，因此只能走
「预分词 + unicode61」。

**务必**让写索引和查索引都调用本模块的 :func:`segment`——两端分词方式一旦
不一致，就会出现「库里明明有、却搜不到」这类极难排查的问题。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache

import jieba

# jieba 首次调用会往 stderr 打印词典构建进度，污染 CLI 输出与测试断言
jieba.setLogLevel(logging.WARNING)

#: FTS5 查询语法里的特殊字符，必须从用户输入中剔除，否则会构造出非法查询
_FTS_SPECIAL = re.compile(r"[\"'()*:^{}\[\]~]")

#: 连续空白
_WS = re.compile(r"\s+")

#: 至少含一个字母/数字/汉字才算有效 token，否则是纯标点噪声
_HAS_WORD_CHAR = re.compile(r"[0-9a-z\u4e00-\u9fff\u3040-\u30ff]")

#: unicode61 分词器会把 ``+ # .`` 当作分隔符，于是 "C++" 和 "C#" 都退化成 "c"。
#: 对编程类课程这是硬伤，因此在归一化阶段先把它们换成可索引的别名。
#: 索引端与查询端共用本表，所以两边始终一致。
_ALIASES = (
    ("c++", " cpp "),
    ("c#", " csharp "),
    (".net", " dotnet "),
    ("f#", " fsharp "),
    ("objective-c", " objectivec "),
)

#: 查询端剔除的虚词。**只在查询端剔除，索引端全部保留**——索引里留着它们
#: 才能支持将来的短语检索，而查询时把它们 AND 进去只会平白降低召回。
# 写成空格分隔的字符串而不是列表字面量：停用词表要常改，这种写法改起来最省事
_STOPWORD_TEXT = (
    "的 了 和 与 及 或 是 在 有 对 中 为 以 之 等 上 下 个 用 把 被 从 到 并 则 "
    "下列 下面 以下 关于 请 简述 说明 what which the a an of to is are"
)
_QUERY_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


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


@lru_cache(maxsize=4096)
def _cut(text: str) -> tuple[str, ...]:
    return tuple(
        token
        for raw in jieba.cut_for_search(text)
        if (token := raw.strip()) and _HAS_WORD_CHAR.search(token)
    )


def segment(text: str) -> str:
    """把文本切成空格分隔的 token 串，供 FTS5 ``unicode61`` 索引或查询。

    >>> segment("二次函数的图像与性质")
    '二次 函数 的 图像 与 性质'
    >>> segment("Ｐｙｔｈｏｎ 循环")
    'python 循环'
    >>> segment("用 C++ 实现")
    '用 cpp 实现'
    >>> segment("！！！")
    ''
    """
    cleaned = _FTS_SPECIAL.sub(" ", normalize(text))
    if not cleaned.strip():
        return ""
    return " ".join(_cut(cleaned))


def to_match_query(user_input: str) -> str:
    """把用户输入转成 FTS5 ``MATCH`` 表达式（各 token 之间为 AND）。

    虚词会被剔除以提高召回；若剔完为空则退回使用全部 token（例如用户就想搜
    「的」这种边缘情况）。返回空串表示「无有效检索词」，调用方应改为不加全文条件。

    >>> to_match_query("二次函数")
    '"二次" AND "函数"'
    >>> to_match_query("二次函数的图像与性质")
    '"二次" AND "函数" AND "图像" AND "性质"'
    >>> to_match_query("!!!")
    ''
    """
    tokens = segment(user_input).split()
    if not tokens:
        return ""
    meaningful = [t for t in tokens if t not in _QUERY_STOPWORDS]
    tokens = meaningful or tokens
    # 去重但保持顺序，避免 "函数 函数" 退化成重复条件
    seen: dict[str, None] = dict.fromkeys(tokens)
    return " AND ".join(f'"{t}"' for t in seen)
