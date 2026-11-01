"""全文检索的两种后端：SQLite FTS5 与 Postgres tsvector。

**为什么两种后端的检索结果是一致的，而不只是相似**

索引端的切分在 Python 里做完（字符 n-gram，见 :mod:`qbank.core.content.segment`），
写进数据库的已经是空格分隔的 token 串。两种后端都**不使用数据库自己的分词器**：

* SQLite 用 ``unicode61``，它不切中日韩字符，只按空格断开——正好拿到我们给的 token。
* Postgres 直接把 token 拼成 ``tsvector`` 字面量再转型写入，绕过 ``to_tsvector``
  连同它背后的解析器与词典。

于是两边索引的是同一批 token，查询也由同一套 ``tokenize`` 产出，**命中集合完全
相同**。排序分数的绝对值不同（bm25 与 ts_rank 是两套公式），相对次序的意图一致：
题干 > 标签 > 答案与解析。

**Postgres 为什么必须绕开自己的解析器**

实测（本机 PostgreSQL 16.15，macOS，UTF8 库）：``default`` 解析器把中日韩字符
判成 ``blank``（token 类型 12，「Space symbols」），于是

.. code-block:: text

    SELECT to_tsvector('simple', '栈 图 最短 cpp');
     -> 'cpp':4

中文 token 一个都没进去。这条路如果不验一遍，部署到 Postgres 之后的症状是
「搜英文能搜到、搜中文永远空」——而索引表看起来是满的。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from qbank.core.content.segment import to_match_query, tokenize


@dataclass(frozen=True, slots=True)
class SearchHit:
    """一条检索结果。``score`` 越小越相关。

    这个方向是跟着 SQLite 的 ``bm25()`` 定的（它返回负数，越负越相关）。
    Postgres 的 ``ts_rank`` 相反——越大越相关，所以那一侧取了负号。
    两种后端都得遵守同一个契约，否则调用方按分数排序时会把最相关的排到最后。
    """

    question_id: str
    score: float


@dataclass(frozen=True, slots=True)
class Document:
    """一道题切好的 token，按列分开。

    分列不是为了好看：检索时要让题干命中比解析命中更相关，而加权必须在列上做。
    """

    stem: tuple[str, ...]
    answer: tuple[str, ...]
    tags: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.stem or self.answer or self.tags)


class SearchBackend(Protocol):
    """一种后端要实现的全部操作。SQL 只出现在实现里，不出现在服务层。"""

    def upsert(self, question_id: str, document: Document) -> None: ...

    def remove(self, question_id: str) -> None: ...

    def query(self, user_input: str, *, limit: int) -> list[SearchHit]: ...

    def clear(self) -> None: ...

    def count(self) -> int: ...

    def optimize(self) -> None: ...


# ===================================================================== SQLite
#: bm25 的列权重：题干 10、答案+解析 2、标签 5。
#:
#: 题干命中显然最相关。标签权重高于答案，是因为标签是人工标注的、信噪比最高；
#: 答案与解析往往很长，词多而散，权重给低一点避免长解析把短题干压下去。
COLUMN_WEIGHTS: Sequence[float] = (10.0, 2.0, 5.0)


class Fts5Backend:
    """SQLite 的 FTS5 虚表。

    **rowid 映射那一层是干什么的**：FTS5 表按整型 rowid 定位最快，而题目主键是
    UUID 文本。``question_search`` 就是 ``question_id`` ↔ 整型 rowid 的映射表，
    FTS 行通过 rowid 与它对应。
    """

    name = "SQLite FTS5"

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, question_id: str, document: Document) -> None:
        rowid = self._rowid_for(question_id, create=True)
        assert rowid is not None
        # FTS5 虚表不支持 UPDATE ... SET 的部分更新语义，统一「先删后插」
        self.session.execute(
            text("DELETE FROM question_fts WHERE rowid = :rowid"), {"rowid": rowid}
        )
        self.session.execute(
            text(
                "INSERT INTO question_fts(rowid, stem, answer, tags) "
                "VALUES (:rowid, :stem, :answer, :tags)"
            ),
            {
                "rowid": rowid,
                "stem": " ".join(document.stem),
                "answer": " ".join(document.answer),
                "tags": " ".join(document.tags),
            },
        )

    def remove(self, question_id: str) -> None:
        rowid = self._rowid_for(question_id, create=False)
        if rowid is None:
            return
        self.session.execute(
            text("DELETE FROM question_fts WHERE rowid = :rowid"), {"rowid": rowid}
        )
        self.session.execute(
            text("DELETE FROM question_search WHERE id = :rowid"), {"rowid": rowid}
        )

    def _rowid_for(self, question_id: str, *, create: bool) -> int | None:
        found = self.session.execute(
            text("SELECT id FROM question_search WHERE question_id = :qid"),
            {"qid": question_id},
        ).scalar()
        if found is not None:
            return int(found)
        if not create:
            return None
        # 不用 lastrowid：那是 DBAPI cursor 的属性，不同方言行为不一。
        # 回查一次反而是方言无关的写法。
        self.session.execute(
            text("INSERT INTO question_search(question_id) VALUES (:qid)"),
            {"qid": question_id},
        )
        created = self.session.execute(
            text("SELECT id FROM question_search WHERE question_id = :qid"),
            {"qid": question_id},
        ).scalar()
        return int(created) if created is not None else None

    def query(self, user_input: str, *, limit: int) -> list[SearchHit]:
        match_query = to_match_query(user_input)
        if not match_query:
            return []
        rows = self.session.execute(
            text(
                """
                SELECT s.question_id AS question_id,
                       bm25(question_fts, 10.0, 2.0, 5.0) AS score
                  FROM question_fts
                  JOIN question_search AS s ON s.id = question_fts.rowid
                 WHERE question_fts MATCH :q
                 ORDER BY score
                 LIMIT :limit
                """
            ),
            {"q": match_query, "limit": limit},
        ).all()
        return [SearchHit(question_id=row.question_id, score=float(row.score)) for row in rows]

    def clear(self) -> None:
        self.session.execute(text("DELETE FROM question_fts"))
        self.session.execute(text("DELETE FROM question_search"))

    def count(self) -> int:
        return int(self.session.execute(text("SELECT count(*) FROM question_search")).scalar() or 0)

    def optimize(self) -> None:
        """合并 FTS 的内部 b-tree。大批导入之后调用一次，查询会明显变快。"""
        self.session.execute(text("INSERT INTO question_fts(question_fts) VALUES ('optimize')"))


# =================================================================== Postgres
#: ``ts_rank`` 的权重数组，顺序是 **D、C、B、A**（Postgres 的规定）。
#:
#: 列到标签的映射：题干 → A、标签 → B、答案与解析 → C，D 用不到。
#: 数值 A=1.0、B=0.5、C=0.2 与 SQLite 侧的 bm25 权重 10 / 5 / 2 等比，
#: 两种后端的加权意图因此是同一个。
TS_RANK_WEIGHTS = "{0.1,0.2,0.5,1.0}"

#: 列 → tsvector 权重标签。顺序决定了字面量里 token 的位置编号。
_WEIGHTED_COLUMNS: tuple[tuple[str, str], ...] = (("stem", "A"), ("tags", "B"), ("answer", "C"))


def _quote(lexeme: str) -> str:
    """把一个 token 写成 tsvector / tsquery 字面量里的单引号串。

    ``tokenize`` 只产出中日韩字符段与 ``[0-9a-z#+._-]`` 组成的段，按说不会出现
    单引号。仍然转义，是因为「按说不会」不是不变量——这里一旦漏掉，拼出来的就是
    一条语法错误的字面量，而不是一条可注入的 SQL（字面量整体是绑定参数）。

    >>> _quote("函数")
    "'函数'"
    >>> _quote("it's")
    "'it''s'"
    """
    return "'" + lexeme.replace("'", "''") + "'"


def to_tsvector_literal(document: Document) -> str:
    """把切好的 token 拼成 ``tsvector`` 字面量。

    位置编号从 1 起连续递增，只为承载权重标签——没有位置的 tsvector 存不下权重
    （实测 ``setweight(array_to_tsvector(...), 'A')`` 是空操作）。

    超过 16383 的位置会被 Postgres 静默压到 16383（实测），不会报错：超长题目的
    词频统计到那里就饱和，检索仍然正确。

    >>> to_tsvector_literal(Document(stem=("栈", "图"), answer=("解",), tags=("树",)))
    "'栈':1A '图':2A '树':3B '解':4C"
    >>> to_tsvector_literal(Document((), (), ()))
    ''
    """
    parts: list[str] = []
    position = 0
    columns = {"stem": document.stem, "tags": document.tags, "answer": document.answer}
    for column, weight in _WEIGHTED_COLUMNS:
        for lexeme in columns[column]:
            position += 1
            parts.append(f"{_quote(lexeme)}:{position}{weight}")
    return " ".join(parts)


def to_tsquery_literal(user_input: str) -> str:
    """把用户输入转成 ``tsquery`` 字面量。各 token 之间是 AND。

    与 SQLite 侧的 :func:`qbank.core.content.segment.to_match_query` 是同一个
    语义，只是换了语法。空串表示「无有效检索词」，含义同样是「本次不加全文条件」。

    >>> to_tsquery_literal("最短路径")
    "'最' & '短' & '路' & '径' & '最短' & '短路' & '路径'"
    >>> to_tsquery_literal("栈")
    "'栈'"
    >>> to_tsquery_literal("！！！")
    ''
    """
    tokens = tokenize(user_input)
    if not tokens:
        return ""
    unique = dict.fromkeys(tokens)
    return " & ".join(_quote(token) for token in unique)


class TsvectorBackend:
    """Postgres 的 ``tsvector`` 列 + GIN 索引。

    索引表只有 ``question_id`` 与 ``document`` 两列，不需要 SQLite 那层 rowid 映射。
    """

    name = "PostgreSQL tsvector"

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, question_id: str, document: Document) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO question_fts (question_id, document)
                VALUES (:qid, CAST(:document AS tsvector))
                ON CONFLICT (question_id)
                DO UPDATE SET document = EXCLUDED.document
                """
            ),
            {"qid": question_id, "document": to_tsvector_literal(document)},
        )

    def remove(self, question_id: str) -> None:
        self.session.execute(
            text("DELETE FROM question_fts WHERE question_id = :qid"), {"qid": question_id}
        )

    def query(self, user_input: str, *, limit: int) -> list[SearchHit]:
        tsquery = to_tsquery_literal(user_input)
        if not tsquery:
            return []
        rows = self.session.execute(
            text(
                """
                SELECT question_id,
                       -ts_rank(
                           CAST(:weights AS float4[]),
                           document,
                           CAST(:q AS tsquery)
                       ) AS score
                  FROM question_fts
                 WHERE document @@ CAST(:q AS tsquery)
                 ORDER BY score
                 LIMIT :limit
                """
            ),
            {"weights": TS_RANK_WEIGHTS, "q": tsquery, "limit": limit},
        ).all()
        return [SearchHit(question_id=row.question_id, score=float(row.score)) for row in rows]

    def clear(self) -> None:
        self.session.execute(text("DELETE FROM question_fts"))

    def count(self) -> int:
        return int(self.session.execute(text("SELECT count(*) FROM question_fts")).scalar() or 0)

    def optimize(self) -> None:
        """Postgres 侧没有 FTS5 那样的索引合并，刷新统计信息是等价的那一步。

        ``ANALYZE`` 可以跑在事务里（``VACUUM`` 不行），所以不必跳出当前会话。
        """
        self.session.execute(text("ANALYZE question_fts"))


#: 方言名 → 后端
BACKENDS: dict[str, type[Fts5Backend] | type[TsvectorBackend]] = {
    "sqlite": Fts5Backend,
    "postgresql": TsvectorBackend,
}


def for_session(session: Session) -> SearchBackend:
    """按会话绑定的方言挑后端。

    未知方言直接报错，理由同 :func:`qbank.core.db.search_ddl`：一个搜不到任何东西
    的题库，比一个开不起来的题库更难查。
    """
    dialect = session.get_bind().dialect.name
    try:
        backend = BACKENDS[dialect]
    except KeyError:
        supported = "、".join(BACKENDS)
        raise ValueError(f"不支持的数据库后端 {dialect!r}；目前只支持 {supported}") from None
    return backend(session)
