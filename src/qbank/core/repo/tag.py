"""标签仓储。

标签按名字唯一。``get_or_create`` 是导入路径的主入口——导入文件里写的是标签
名，不是 id。

标签带 ``category``（知识点/能力/来源），否则用上一两个学期就会积累出几百个
平铺的标签，筛选器彻底没法用。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from qbank.core.enums import TagCategory
from qbank.core.models import QuestionTag, Tag
from qbank.core.repo.base import ConflictError, NotFoundError, Repo


@dataclass(frozen=True, slots=True)
class MergeResult:
    """标签合并的结果。``affected`` 才是「有多少道题的标签变了」。"""

    moved: int
    deduplicated: int

    @property
    def affected(self) -> int:
        return self.moved + self.deduplicated


class TagRepo(Repo):
    def create(
        self,
        name: str,
        *,
        category: TagCategory = TagCategory.KNOWLEDGE,
        color: str = "",
        description: str = "",
    ) -> Tag:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("标签名不能为空")
        tag = Tag(name=cleaned, category=category, color=color, description=description)
        self.session.add(tag)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"标签「{cleaned}」已存在") from exc
        return tag

    def get(self, tag_id: str) -> Tag:
        tag = self.session.get(Tag, tag_id)
        if tag is None:
            raise NotFoundError("标签", tag_id)
        return tag

    def find_by_name(self, name: str) -> Tag | None:
        return self.session.scalar(select(Tag).where(Tag.name == name.strip()))

    def get_or_create(self, name: str, *, category: TagCategory = TagCategory.KNOWLEDGE) -> Tag:
        return self.find_by_name(name) or self.create(name, category=category)

    def resolve_many(self, names: Iterable[str]) -> list[Tag]:
        """把标签名列表解析成标签对象，缺的自动建。保持传入顺序并去重。"""
        resolved: dict[str, Tag] = {}
        for raw in names:
            cleaned = raw.strip()
            if cleaned and cleaned not in resolved:
                resolved[cleaned] = self.get_or_create(cleaned)
        return list(resolved.values())

    def list_all(self, *, category: TagCategory | None = None) -> Sequence[Tag]:
        statement = select(Tag)
        if category is not None:
            statement = statement.where(Tag.category == category)
        return self.session.scalars(statement.order_by(Tag.category, Tag.name)).all()

    def usage_counts(self) -> dict[str, int]:
        """每个标签被多少道题用到。用于界面上排序与清理无用标签。"""
        rows = self.session.execute(
            select(QuestionTag.tag_id, func.count(QuestionTag.question_id)).group_by(
                QuestionTag.tag_id
            )
        ).all()
        return {row[0]: int(row[1]) for row in rows}

    def rename(self, tag_id: str, name: str) -> Tag:
        tag = self.get(tag_id)
        tag.name = name.strip()
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"标签「{name}」已存在") from exc
        return tag

    def merge(self, source_id: str, target_id: str) -> MergeResult:
        """把 ``source`` 标签的所有关联转到 ``target``，然后删掉 source。

        导入久了一定会出现「循环」「循环结构」「while循环」这类同义标签，必须
        能合并。

        返回值把「转移」和「去重」分开计数：一道题同时带了两个标签时，关联是被
        **删除**而不是转移的。只报转移数会出现「已合并，0 道题受影响」这种明明
        改了东西却说没改的提示。
        """
        if source_id == target_id:
            return MergeResult(moved=0, deduplicated=0)
        source = self.get(source_id)
        target = self.get(target_id)
        links = self.session.scalars(
            select(QuestionTag).where(QuestionTag.tag_id == source.id)
        ).all()
        already = set(
            self.session.scalars(
                select(QuestionTag.question_id).where(QuestionTag.tag_id == target.id)
            ).all()
        )
        moved = 0
        deduplicated = 0
        for link in links:
            if link.question_id in already:
                self.session.delete(link)  # 两个标签都有，去掉重复关联
                deduplicated += 1
            else:
                link.tag_id = target.id
                moved += 1
        self.session.flush()
        self.session.delete(source)
        self.session.flush()
        return MergeResult(moved=moved, deduplicated=deduplicated)

    def delete(self, tag_id: str) -> None:
        """删标签。题目不受影响，只是少一个标签（靠 ondelete CASCADE 清关联）。"""
        self.session.delete(self.get(tag_id))
        self.session.flush()

    def delete_unused(self) -> int:
        """清理没有任何题目使用的标签，返回清理数量。"""
        used = set(self.session.scalars(select(QuestionTag.tag_id)).all())
        removed = 0
        for tag in self.session.scalars(select(Tag)).all():
            if tag.id not in used:
                self.session.delete(tag)
                removed += 1
        self.session.flush()
        return removed
