"""试卷仓储：试卷、分节、题目项与使用记录。

**冻结快照是这一层的硬约定**：``paper_item.snapshot`` 必须在成卷那一刻写满，
不允许先存个空壳等以后补。去年的卷子不该因为今年改了题目而变样——教学场景里
这不是「最好有」，而是卷子存在的前提（学生来查分时要看到当时那张卷）。

**使用记录与题目项保持同步**：``question_usage`` 不是手工维护的流水，而是
「这份卷子现在用了哪些题」的镜像。试着组几次卷再删掉，不该把那批题封印几个月。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from qbank.core.models import (
    Paper,
    PaperItem,
    PaperSection,
    QuestionUsage,
    utcnow,
)
from qbank.core.repo.base import NotFoundError, Repo


class PaperRepo(Repo):
    # ------------------------------------------------------------ 读
    def get(self, paper_id: str, *, include_deleted: bool = False) -> Paper:
        paper = self.session.get(Paper, paper_id)
        if paper is None or (paper.deleted_at is not None and not include_deleted):
            raise NotFoundError("试卷", paper_id)
        return paper

    def get_detailed(self, paper_id: str, *, include_deleted: bool = False) -> Paper:
        """一次把分节与题目项都加载出来。

        试卷详情页要渲染几十道题，逐节惰性加载会退化成上百条 SQL。
        """
        paper = self.session.scalar(
            select(Paper)
            .where(Paper.id == paper_id)
            .options(selectinload(Paper.sections).selectinload(PaperSection.items))
        )
        if paper is None or (paper.deleted_at is not None and not include_deleted):
            raise NotFoundError("试卷", paper_id)
        return paper

    def list_all(
        self, course_id: str | None = None, *, include_deleted: bool = False
    ) -> Sequence[Paper]:
        statement = select(Paper)
        if not include_deleted:
            statement = statement.where(Paper.deleted_at.is_(None))
        if course_id:
            statement = statement.where(Paper.course_id == course_id)
        return self.session.scalars(statement.order_by(Paper.created_at.desc())).all()

    def summaries(self, course_id: str | None = None) -> dict[str, tuple[int, float]]:
        """每份卷子的 ``(题数, 总分)``。

        列表页用。走 ``Paper.total_score`` 那个属性的话，每份卷子都要把分节和
        题目项整棵加载出来——十几份卷子就是上百条 SQL，而列表页只需要两个数。
        """
        statement = (
            select(
                PaperSection.paper_id,
                func.count(PaperItem.id),
                func.coalesce(func.sum(PaperItem.score), 0.0),
            )
            .join(PaperItem, PaperItem.section_id == PaperSection.id)
            .group_by(PaperSection.paper_id)
        )
        if course_id:
            statement = statement.join(Paper, Paper.id == PaperSection.paper_id).where(
                Paper.course_id == course_id
            )
        return {row[0]: (int(row[1]), float(row[2])) for row in self.session.execute(statement)}

    def get_item(self, item_id: str) -> PaperItem:
        item = self.session.get(PaperItem, item_id)
        if item is None:
            raise NotFoundError("试卷题目", item_id)
        return item

    def get_section(self, section_id: str) -> PaperSection:
        section = self.session.get(PaperSection, section_id)
        if section is None:
            raise NotFoundError("试卷分节", section_id)
        return section

    def used_question_ids(self, paper_id: str) -> set[str]:
        """这份卷子已经用掉的题。手工加题时据此拦住重复选同一道题。"""
        rows = self.session.execute(
            select(PaperItem.question_id)
            .join(PaperSection, PaperSection.id == PaperItem.section_id)
            .where(PaperSection.paper_id == paper_id, PaperItem.question_id.is_not(None))
        ).all()
        return {row[0] for row in rows if row[0]}

    # ------------------------------------------------------------ 写
    def create(
        self,
        course_id: str,
        title: str,
        *,
        meta: dict[str, Any] | None = None,
        template_key: str = "standard",
        seed: int | None = None,
        blueprint: dict[str, Any] | None = None,
    ) -> Paper:
        paper = Paper(
            course_id=course_id,
            title=title,
            meta=meta or {},
            template_key=template_key,
            seed=seed,
            blueprint=blueprint or {},
        )
        self.session.add(paper)
        self.session.flush()
        return paper

    def add_section(self, paper_id: str, title: str, *, instructions: str = "") -> PaperSection:
        paper = self.get(paper_id)
        section = PaperSection(
            paper_id=paper.id,
            title=title,
            instructions=instructions,
            sort_order=len(paper.sections),
        )
        paper.sections.append(section)
        self.session.flush()
        return section

    def add_item(
        self,
        section_id: str,
        *,
        question_id: str | None,
        snapshot: dict[str, Any],
        score: float = 0.0,
        option_order: Sequence[str] = (),
    ) -> PaperItem:
        section = self.get_section(section_id)
        item = PaperItem(
            section_id=section.id,
            question_id=question_id,
            sort_order=len(section.items),
            score=score,
            snapshot=snapshot,
            option_order=list(option_order),
        )
        section.items.append(item)
        self.session.flush()
        return item

    def remove_item(self, item_id: str) -> PaperSection:
        item = self.get_item(item_id)
        section = item.section
        section.items.remove(item)
        self.session.flush()
        self.renumber(section)
        return section

    def remove_section(self, section_id: str) -> Paper:
        section = self.get_section(section_id)
        paper = section.paper
        paper.sections.remove(section)
        self.session.flush()
        for order, remaining in enumerate(paper.sections):
            remaining.sort_order = order
        self.session.flush()
        return paper

    def renumber(self, section: PaperSection) -> None:
        """把 ``sort_order`` 压回 0..n-1。

        删掉中间一道题后留出的空号本身无害，但「移动一道题」是按相邻序号交换
        实现的，空号会让交换看起来没反应。
        """
        for order, item in enumerate(section.items):
            item.sort_order = order
        self.session.flush()

    def move_item(self, item_id: str, offset: int) -> PaperItem:
        """在本节内上移或下移一道题。到头了就原地不动，不报错。"""
        item = self.get_item(item_id)
        items = list(item.section.items)
        index = items.index(item)
        target = index + offset
        if 0 <= target < len(items):
            items[index], items[target] = items[target], items[index]
            for order, moved in enumerate(items):
                moved.sort_order = order
            self.session.flush()
            self.session.expire(item.section, ["items"])
        return item

    def move_item_to_section(self, item_id: str, section_id: str) -> PaperItem:
        """把一道题挪到另一大题下，接在末尾。"""
        item = self.get_item(item_id)
        if item.section_id == section_id:
            return item
        source = item.section
        target = self.get_section(section_id)
        if target.paper_id != source.paper_id:
            raise ValueError("不能把题目挪到另一份试卷的大题里")
        item.section_id = target.id
        item.sort_order = len(target.items)
        self.session.flush()
        self.session.expire(source, ["items"])
        self.session.expire(target, ["items"])
        self.renumber(source)
        return item

    def soft_delete(self, paper_id: str, *, at: datetime | None = None) -> Paper:
        paper = self.get(paper_id)
        paper.deleted_at = at or utcnow()
        self.session.flush()
        return paper

    def restore(self, paper_id: str) -> Paper:
        paper = self.get(paper_id, include_deleted=True)
        paper.deleted_at = None
        self.session.flush()
        return paper

    def purge(self, paper_id: str) -> None:
        self.session.delete(self.get(paper_id, include_deleted=True))
        self.session.flush()

    # ------------------------------------------------------- 使用记录
    def sync_usage(self, paper_id: str) -> int:
        """让使用记录与当前题目项一致，返回记录条数。

        重算而不是增量维护：加题、删题、挪题、整节删除都会影响它，每处各写一遍
        增删迟早会漏。重算是一条 SQL 的事，且永远不会漂移。
        """
        wanted = self.used_question_ids(paper_id)
        existing = {
            row[0]
            for row in self.session.execute(
                select(QuestionUsage.question_id).where(QuestionUsage.paper_id == paper_id)
            )
        }
        stale = existing - wanted
        if stale:
            self.session.execute(
                delete(QuestionUsage).where(
                    QuestionUsage.paper_id == paper_id,
                    QuestionUsage.question_id.in_(list(stale)),
                )
            )
        for question_id in sorted(wanted - existing):
            self.session.add(QuestionUsage(question_id=question_id, paper_id=paper_id))
        self.session.flush()
        return len(wanted)
