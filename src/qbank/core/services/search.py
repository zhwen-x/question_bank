"""全文检索：FTS5 索引的维护与查询。

**为什么索引不用 SQL 触发器维护**：索引里存的是字符 n-gram 切分后的 token 串
（原因见 :mod:`qbank.core.content.segment`），切分要跑 Python，触发器做不到。
所以写题的事务里必须显式调用 :meth:`SearchService.index`。

**rowid 映射那一层是干什么的**：FTS5 表按整型 rowid 定位最快，而题目主键是
UUID 文本。``question_search`` 就是 ``question_id`` ↔ 整型 rowid 的映射表，
FTS 行通过 rowid 与它对应。

**索引端与查询端必须用同一套切分**，而且这套切分必须是**上下文无关**的。
最早用 jieba 时两端调的是同一个函数，照样出了「题在库里却搜不到」——因为
jieba 的结果取决于上下文。现在的字符 n-gram 没有这个问题。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from qbank.core.content.segment import segment, to_match_query
from qbank.core.content.text import FOR_SEARCH, to_plain_text


@dataclass(frozen=True, slots=True)
class SearchHit:
    """一条检索结果。``score`` 越小越相关（直接用 FTS5 的 bm25 输出）。"""

    question_id: str
    score: float


class SearchService:
    """FTS5 索引的唯一出入口。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------ 写索引
    def index(
        self,
        question_id: str,
        *,
        stem: str,
        answer: str = "",
        explanation: str = "",
        tags: Iterable[str] = (),
    ) -> None:
        """建立或更新一道题的索引（幂等）。

        题干、答案、标签分列存放，便于将来按字段加权（题干命中应比解析命中
        更相关）。传入的是**原始 Markdown**，分词与去格式在本方法内完成，
        调用方不需要操心。
        """
        rowid = self._rowid_for(question_id, create=True)
        assert rowid is not None
        fields = self._segmented_fields(stem, answer, explanation, tags)
        # FTS5 虚表不支持 UPDATE ... SET 的部分更新语义，统一「先删后插」
        self.session.execute(
            text("DELETE FROM question_fts WHERE rowid = :rowid"), {"rowid": rowid}
        )
        self.session.execute(
            text(
                "INSERT INTO question_fts(rowid, stem, answer, tags) "
                "VALUES (:rowid, :stem, :answer, :tags)"
            ),
            {"rowid": rowid, **fields},
        )

    @staticmethod
    def _segmented_fields(
        stem: str, answer: str, explanation: str, tags: Iterable[str]
    ) -> dict[str, str]:
        return {
            "stem": segment(to_plain_text(stem, FOR_SEARCH)),
            # 答案与解析合在一列：用户不会分开搜「只在解析里出现的词」
            "answer": segment(to_plain_text(f"{answer}\n\n{explanation}".strip(), FOR_SEARCH)),
            "tags": segment(" ".join(tags)),
        }

    def remove(self, question_id: str) -> None:
        """从索引里删掉一道题（软删除时也要调用，否则回收站里的题还能被搜到）。"""
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
        # 不用 lastrowid：那是 DBAPI cursor 的属性，不同方言行为不一，换
        # Postgres 时会悄悄失效。回查一次反而是方言无关的写法。
        self.session.execute(
            text("INSERT INTO question_search(question_id) VALUES (:qid)"),
            {"qid": question_id},
        )
        created = self.session.execute(
            text("SELECT id FROM question_search WHERE question_id = :qid"),
            {"qid": question_id},
        ).scalar()
        return int(created) if created is not None else None

    # ------------------------------------------------------------ 查询
    def search(self, query: str, *, limit: int = 200) -> list[SearchHit]:
        """按相关度返回题目 id。

        查询串为空或只含标点时返回空列表——调用方应把这理解为「本次不加全文
        条件」，而不是「没有任何题目匹配」。两者的区别很要紧：前者该返回全部
        题目，后者该返回零条。
        """
        match_query = to_match_query(query)
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

    def matching_ids(self, query: str, *, limit: int = 200) -> list[str]:
        """只要 id，顺序即相关度顺序。"""
        return [hit.question_id for hit in self.search(query, limit=limit)]

    # ------------------------------------------------------------ 维护
    def clear(self) -> None:
        """清空索引。重建前调用。"""
        self.session.execute(text("DELETE FROM question_fts"))
        self.session.execute(text("DELETE FROM question_search"))

    def indexed_count(self) -> int:
        return int(self.session.execute(text("SELECT count(*) FROM question_search")).scalar() or 0)

    def optimize(self) -> None:
        """合并 FTS 的内部 b-tree。大批导入之后调用一次，查询会明显变快。"""
        self.session.execute(text("INSERT INTO question_fts(question_fts) VALUES ('optimize')"))


#: bm25 的列权重：题干 10、答案+解析 2、标签 5。
#:
#: 题干命中显然最相关。标签权重高于答案，是因为标签是人工标注的、信噪比最高；
#: 答案与解析往往很长，词多而散，权重给低一点避免长解析把短题干压下去。
COLUMN_WEIGHTS: Sequence[float] = (10.0, 2.0, 5.0)
