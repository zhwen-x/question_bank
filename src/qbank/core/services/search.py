"""全文检索：索引的维护与查询。

本模块只做两件事：**切分**，以及把操作转交给当前数据库的后端。两种后端
（SQLite FTS5 / Postgres tsvector）与它们各自的 SQL 都在
:mod:`qbank.core.services.search_backend` 里，调用方不需要知道跑在哪一种上。

**为什么索引不用 SQL 触发器维护**：索引里存的是字符 n-gram 切分后的 token
（原因见 :mod:`qbank.core.content.segment`），切分要跑 Python，触发器做不到。
所以写题的事务里必须显式调用 :meth:`SearchService.index`。

**索引端与查询端必须用同一套切分**，而且这套切分必须是**上下文无关**的。
最早用 jieba 时两端调的是同一个函数，照样出了「题在库里却搜不到」——因为
jieba 的结果取决于上下文。现在的字符 n-gram 没有这个问题。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from qbank.core.content.segment import tokenize
from qbank.core.content.text import FOR_SEARCH, to_plain_text
from qbank.core.services.search_backend import (
    COLUMN_WEIGHTS,
    Document,
    SearchBackend,
    SearchHit,
    for_session,
)

__all__ = ("COLUMN_WEIGHTS", "Document", "SearchHit", "SearchService")


class SearchService:
    """检索索引的唯一出入口。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.backend: SearchBackend = for_session(session)

    @property
    def backend_name(self) -> str:
        """当前后端的名字，给 ``qbank doctor`` 与状态页显示。"""
        return getattr(self.backend, "name", type(self.backend).__name__)

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

        题干、答案、标签分列存放，便于按字段加权（题干命中应比解析命中更相关）。
        传入的是**原始 Markdown**，分词与去格式在本方法内完成，调用方不需要操心。
        """
        self.backend.upsert(question_id, segment_question(stem, answer, explanation, tags))

    def remove(self, question_id: str) -> None:
        """从索引里删掉一道题（软删除时也要调用，否则回收站里的题还能被搜到）。"""
        self.backend.remove(question_id)

    # ------------------------------------------------------------ 查询
    def search(self, query: str, *, limit: int = 200) -> list[SearchHit]:
        """按相关度返回题目 id。

        查询串为空或只含标点时返回空列表——调用方应把这理解为「本次不加全文
        条件」，而不是「没有任何题目匹配」。两者的区别很要紧：前者该返回全部
        题目，后者该返回零条。
        """
        return self.backend.query(query, limit=limit)

    def matching_ids(self, query: str, *, limit: int = 200) -> list[str]:
        """只要 id，顺序即相关度顺序。"""
        return [hit.question_id for hit in self.search(query, limit=limit)]

    # ------------------------------------------------------------ 维护
    def clear(self) -> None:
        """清空索引。重建前调用。"""
        self.backend.clear()

    def indexed_count(self) -> int:
        return self.backend.count()

    def optimize(self) -> None:
        """大批导入之后调用一次，查询会明显变快。"""
        self.backend.optimize()


def segment_question(
    stem: str, answer: str = "", explanation: str = "", tags: Iterable[str] = ()
) -> Document:
    """把一道题的各字段切成待索引的 token。

    答案与解析合在一列：用户不会分开搜「只在解析里出现的词」，分两列只是多一个
    需要调权重的旋钮。

    >>> doc = segment_question("求 $x^2$ 的导数", answer="2x", tags=["微积分"])
    >>> doc.stem
    ('求', '的', '导', '数', '的导', '导数')
    >>> doc.tags
    ('微', '积', '分', '微积', '积分')
    """
    return Document(
        stem=tuple(tokenize(to_plain_text(stem, FOR_SEARCH))),
        answer=tuple(tokenize(to_plain_text(f"{answer}\n\n{explanation}".strip(), FOR_SEARCH))),
        tags=tuple(tokenize(" ".join(tags))),
    )
