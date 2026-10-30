"""题目仓储：筛选、分页、软删除。

**软删除的约定**：所有读方法默认只返回 ``deleted_at IS NULL`` 的行。忘记这层
过滤的后果是已删除的题悄悄出现在组卷结果里，而反过来最多是回收站看起来空的。

**子题的约定**：综合应用题的子题也是 ``question`` 表里的行（``parent_id`` 非空）。
题目列表默认**不显示**子题（``top_level_only=True``），否则一道综合题会在列表
里散成好几行，但检索时**要**能命中子题——老师搜「最短路」时，那道大题里第 2
小问才是真正相关的内容。所以是两种默认值，不是一种。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import selectinload

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.core.models import (
    Question,
    QuestionAnswer,
    QuestionTag,
    QuestionUsage,
    utcnow,
)
from qbank.core.repo.base import NotFoundError, Repo


@dataclass(slots=True)
class QuestionFilter:
    """题目列表与自动组卷共用的筛选条件。

    所有字段都是「可选 + 空表示不限」。用 dataclass 而不是一长串关键字参数，
    是因为这套条件要在服务层、CLI、Web 路由与组卷算法之间整体传递。
    """

    course_id: str | None = None
    chapter_ids: Sequence[str] = ()
    #: 为真时把 ``chapter_ids`` 里为空的那部分理解为「未归类章节」
    include_unchaptered: bool = False
    types: Sequence[QuestionType] = ()
    difficulties: Sequence[Difficulty] = ()
    statuses: Sequence[QuestionStatus] = ()
    #: 需要同时具备的全部标签（AND）
    all_tag_ids: Sequence[str] = ()
    #: 具备任一即可的标签（OR）
    any_tag_ids: Sequence[str] = ()
    #: 限定在这批 id 内（全文检索的结果交进来）
    restrict_ids: Sequence[str] | None = None
    #: 排除这批 id（已在选题篮里的、或指定不要的）
    exclude_ids: Sequence[str] = ()
    source_contains: str = ""
    #: 只要有答案的题。组卷导出答案卷时很需要。
    require_answer: bool = False
    top_level_only: bool = True
    include_deleted: bool = False
    #: 排除最近 N 天内被用过的题（避免同一题反复出现）
    exclude_used_within_days: int | None = None


@dataclass(frozen=True, slots=True)
class Page:
    """一页结果。``total`` 是满足条件的总数，用于前端分页器。"""

    items: Sequence[Question]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class QuestionRepo(Repo):
    # ------------------------------------------------------------ 读
    def get(self, question_id: str, *, include_deleted: bool = False) -> Question:
        question = self.session.get(Question, question_id)
        if question is None or (question.is_deleted and not include_deleted):
            raise NotFoundError("题目", question_id)
        return question

    def get_detailed(self, question_id: str, *, include_deleted: bool = False) -> Question:
        """一次把答案、标签、子题都加载出来，避免详情页的 N+1 查询。"""
        question = self.session.scalar(
            select(Question)
            .where(Question.id == question_id)
            .options(
                selectinload(Question.answer),
                selectinload(Question.tags),
                selectinload(Question.sub_questions).selectinload(Question.answer),
            )
        )
        if question is None or (question.is_deleted and not include_deleted):
            raise NotFoundError("题目", question_id)
        return question

    def find_by_fingerprint(self, course_id: str, fingerprint: str) -> Question | None:
        """按精确指纹找同一道题。指纹为空时直接返回 ``None``（空内容不算重复）。"""
        if not fingerprint:
            return None
        return self.session.scalar(
            select(Question).where(
                Question.course_id == course_id,
                Question.fingerprint == fingerprint,
                Question.deleted_at.is_(None),
            )
        )

    def find_by_external_id(self, course_id: str, external_id: str) -> Question | None:
        """按来源文件里的稳定标识找题，用于同一文件的重复导入。"""
        if not external_id:
            return None
        return self.session.scalar(
            select(Question).where(
                Question.course_id == course_id,
                Question.external_id == external_id,
                Question.deleted_at.is_(None),
            )
        )

    def list_page(
        self,
        criteria: QuestionFilter,
        *,
        offset: int = 0,
        limit: int = 50,
        order_by_ids: Sequence[str] | None = None,
    ) -> Page:
        """分页查询。

        ``order_by_ids`` 用于保持全文检索的相关度顺序：SQL 无法表达「按给定
        id 列表的顺序排」，所以在 Python 侧重排。只有在传了检索结果时才这么做。
        """
        statement = self._apply(select(Question), criteria)
        total = int(
            self.session.scalar(self._apply(select(func.count(Question.id)), criteria)) or 0
        )
        if order_by_ids is not None:
            rank = {qid: index for index, qid in enumerate(order_by_ids)}
            rows = self.session.scalars(
                statement.options(selectinload(Question.tags), selectinload(Question.answer))
            ).all()
            ordered = sorted(rows, key=lambda q: rank.get(q.id, len(rank)))
            return Page(
                items=ordered[offset : offset + limit], total=total, offset=offset, limit=limit
            )
        items = self.session.scalars(
            statement.options(selectinload(Question.tags), selectinload(Question.answer))
            .order_by(Question.updated_at.desc(), Question.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return Page(items=items, total=total, offset=offset, limit=limit)

    def list_all(self, criteria: QuestionFilter) -> Sequence[Question]:
        """不分页地取全部匹配项。自动组卷需要完整候选池。"""
        return self.session.scalars(
            self._apply(select(Question), criteria).options(selectinload(Question.tags))
        ).all()

    def count(self, criteria: QuestionFilter) -> int:
        return int(self.session.scalar(self._apply(select(func.count(Question.id)), criteria)) or 0)

    def dedup_candidates(
        self, course_id: str, question_type: QuestionType, *, restrict_ids: Sequence[str] = ()
    ) -> Sequence[Question]:
        """查重的候选池：同课程 + 同题型（可再用检索结果收窄）。

        不做全表扫描——相似度是在这个收窄后的集合上精确计算的
        （见 :mod:`qbank.core.services.dedup`）。
        """
        statement = select(Question).where(
            Question.course_id == course_id,
            Question.type == question_type,
            Question.deleted_at.is_(None),
        )
        if restrict_ids:
            statement = statement.where(Question.id.in_(list(restrict_ids)))
        return self.session.scalars(statement.options(selectinload(Question.answer))).all()

    def _apply(self, statement: Select, criteria: QuestionFilter) -> Select:  # type: ignore[type-arg]
        if not criteria.include_deleted:
            statement = statement.where(Question.deleted_at.is_(None))
        if criteria.top_level_only:
            statement = statement.where(Question.parent_id.is_(None))
        if criteria.course_id:
            statement = statement.where(Question.course_id == criteria.course_id)
        if criteria.chapter_ids or criteria.include_unchaptered:
            conditions = []
            if criteria.chapter_ids:
                conditions.append(Question.chapter_id.in_(list(criteria.chapter_ids)))
            if criteria.include_unchaptered:
                conditions.append(Question.chapter_id.is_(None))
            statement = statement.where(or_(*conditions))
        if criteria.types:
            statement = statement.where(Question.type.in_(list(criteria.types)))
        if criteria.difficulties:
            statement = statement.where(Question.difficulty.in_(list(criteria.difficulties)))
        if criteria.statuses:
            statement = statement.where(Question.status.in_(list(criteria.statuses)))
        if criteria.source_contains:
            statement = statement.where(Question.source.contains(criteria.source_contains))
        if criteria.restrict_ids is not None:
            # 显式传了空列表意味着「检索无结果」，必须真的返回零条，
            # 不能当成「不限」——这两种语义的混淆会让搜索框形同失效
            statement = statement.where(Question.id.in_(list(criteria.restrict_ids)))
        if criteria.exclude_ids:
            statement = statement.where(Question.id.notin_(list(criteria.exclude_ids)))
        if criteria.require_answer:
            statement = statement.where(
                Question.id.in_(
                    select(QuestionAnswer.question_id).where(QuestionAnswer.answer != "")
                )
            )
        for tag_id in criteria.all_tag_ids:
            statement = statement.where(
                Question.id.in_(select(QuestionTag.question_id).where(QuestionTag.tag_id == tag_id))
            )
        if criteria.any_tag_ids:
            statement = statement.where(
                Question.id.in_(
                    select(QuestionTag.question_id).where(
                        QuestionTag.tag_id.in_(list(criteria.any_tag_ids))
                    )
                )
            )
        if criteria.exclude_used_within_days is not None:
            cutoff = utcnow() - timedelta(days=criteria.exclude_used_within_days)
            statement = statement.where(
                Question.id.notin_(
                    select(QuestionUsage.question_id).where(QuestionUsage.used_at >= cutoff)
                )
            )
        return statement

    def to_top_level_ids(self, question_ids: Sequence[str]) -> list[str]:
        """把题目 id 列表里的子题换成它们的父题，去重并保持原顺序。

        检索会命中子题（这是有意的：搜「最短路」时真正相关的是那道大题里的第 2
        小问），但题目列表按题展示、一道综合题只占一行。所以检索结果必须先经过
        这一步映射，否则「命中了子题」会表现为「列表里什么都没有」。
        """
        if not question_ids:
            return []
        rows = self.session.execute(
            select(Question.id, Question.parent_id).where(Question.id.in_(list(question_ids)))
        ).all()
        parent_of = {row[0]: row[1] for row in rows}
        resolved: dict[str, None] = {}
        for question_id in question_ids:
            resolved[parent_of.get(question_id) or question_id] = None
        return list(resolved)

    # ------------------------------------------------------------ 写
    def add(self, question: Question) -> Question:
        self.session.add(question)
        self.session.flush()
        return question

    def soft_delete(self, question_id: str, *, at: datetime | None = None) -> Question:
        """移入回收站。子题跟着父题一起删，否则会剩下无主的子题。"""
        question = self.get(question_id)
        stamp = at or utcnow()
        question.deleted_at = stamp
        for sub in question.sub_questions:
            sub.deleted_at = stamp
        self.session.flush()
        return question

    def restore(self, question_id: str) -> Question:
        question = self.get(question_id, include_deleted=True)
        question.deleted_at = None
        for sub in question.sub_questions:
            sub.deleted_at = None
        self.session.flush()
        return question

    def purge(self, question_id: str) -> None:
        """真删除。只应由「清空回收站」调用。"""
        self.session.delete(self.get(question_id, include_deleted=True))
        self.session.flush()

    def list_trash(self, course_id: str | None = None) -> Sequence[Question]:
        statement = select(Question).where(
            Question.deleted_at.is_not(None), Question.parent_id.is_(None)
        )
        if course_id:
            statement = statement.where(Question.course_id == course_id)
        return self.session.scalars(statement.order_by(Question.deleted_at.desc())).all()

    def record_usage(self, question_id: str, paper_id: str) -> None:
        """记录一次使用。同一题同一卷只记一次（唯一约束保证）。"""
        existing = self.session.scalar(
            select(QuestionUsage).where(
                QuestionUsage.question_id == question_id, QuestionUsage.paper_id == paper_id
            )
        )
        if existing is None:
            self.session.add(QuestionUsage(question_id=question_id, paper_id=paper_id))
            self.session.flush()

    def usage_count(self, question_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count(QuestionUsage.id)).where(QuestionUsage.question_id == question_id)
            )
            or 0
        )
