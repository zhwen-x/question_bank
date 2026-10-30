"""课程与章节仓储。

章节是一棵树，同时维护 ``parent_id``（结构）与 ``path``（物化路径）。``path``
的作用是把「某章及其全部子章节下的题目」变成一次 ``LIKE 'path/%'`` 查询——
组卷时按章节范围抽题非常频繁，递归查询在 SQLite 上既慢又啰嗦。

代价是**移动或重命名节点必须重写整棵子树的 path**。这件事只在这里做，
:meth:`ChapterRepo.move` 和 :meth:`ChapterRepo.rename` 是唯一入口。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from qbank.core.models import PATH_SEP, Chapter, Course
from qbank.core.repo.base import ConflictError, NotFoundError, Repo


class CourseRepo(Repo):
    def create(self, name: str, *, code: str = "", description: str = "") -> Course:
        course = Course(
            name=name.strip(),
            code=code.strip(),
            description=description,
            sort_order=self._next_sort_order(),
        )
        if not course.name:
            raise ValueError("课程名不能为空")
        self.session.add(course)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"课程「{name}」已存在") from exc
        return course

    def _next_sort_order(self) -> int:
        existing = self.session.scalars(select(Course.sort_order)).all()
        return max(existing, default=-1) + 1

    def get(self, course_id: str) -> Course:
        course = self.session.get(Course, course_id)
        if course is None:
            raise NotFoundError("课程", course_id)
        return course

    def find_by_name(self, name: str) -> Course | None:
        return self.session.scalar(select(Course).where(Course.name == name.strip()))

    def get_or_create(self, name: str, *, code: str = "") -> Course:
        """导入时用：课程不存在就建。"""
        return self.find_by_name(name) or self.create(name, code=code)

    def list_all(self) -> Sequence[Course]:
        return self.session.scalars(select(Course).order_by(Course.sort_order, Course.name)).all()

    def rename(self, course_id: str, name: str) -> Course:
        course = self.get(course_id)
        course.name = name.strip()
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"课程「{name}」已存在") from exc
        return course

    def delete(self, course_id: str) -> None:
        """**硬删除**，连带章节与题目一起消失（靠 ondelete CASCADE）。

        课程不做软删除：删一门课是极少且后果明确的操作，留着半删状态只会让
        题目列表和组卷逻辑到处都要多一层过滤。误删的补救办法是从 .qbz 备份
        恢复，因此界面上必须二次确认并提示先导出备份。
        """
        self.session.delete(self.get(course_id))
        self.session.flush()


class ChapterRepo(Repo):
    def create(
        self,
        course_id: str,
        title: str,
        *,
        parent_id: str | None = None,
        code: str = "",
    ) -> Chapter:
        title = self._clean_title(title)
        parent = None
        if parent_id is not None:
            parent = self.get(parent_id)
            if parent.course_id != course_id:
                raise ValueError("父章节属于另一门课程")
        chapter = Chapter(
            course_id=course_id,
            parent_id=parent_id,
            title=title,
            code=code.strip(),
            path=self._join(parent.path if parent else "", title),
            sort_order=self._next_sort_order(course_id, parent_id),
        )
        self.session.add(chapter)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"同级已有章节「{title}」") from exc
        return chapter

    @staticmethod
    def _clean_title(title: str) -> str:
        """标题里不能出现路径分隔符，否则物化路径会被解析错。"""
        cleaned = title.strip().replace(PATH_SEP, "／")  # 换成全角斜杠，保留可读性
        if not cleaned:
            raise ValueError("章节标题不能为空")
        return cleaned

    @staticmethod
    def _join(parent_path: str, title: str) -> str:
        return f"{parent_path}{PATH_SEP}{title}" if parent_path else title

    def _next_sort_order(self, course_id: str, parent_id: str | None) -> int:
        existing = self.session.scalars(
            select(Chapter.sort_order).where(
                Chapter.course_id == course_id, Chapter.parent_id == parent_id
            )
        ).all()
        return max(existing, default=-1) + 1

    def get(self, chapter_id: str) -> Chapter:
        chapter = self.session.get(Chapter, chapter_id)
        if chapter is None:
            raise NotFoundError("章节", chapter_id)
        return chapter

    def find_by_path(self, course_id: str, path: str) -> Chapter | None:
        return self.session.scalar(
            select(Chapter).where(Chapter.course_id == course_id, Chapter.path == path.strip())
        )

    def ensure_path(self, course_id: str, path: str) -> Chapter | None:
        """按路径查找章节，不存在则逐级创建。

        导入文件里写的是「第3章 控制结构/3.2 循环结构」这样的人类可读路径，
        而不是数据库 id，所以需要这个方法把路径解析成（或补全成）章节。

        空路径返回 ``None``，表示题目不归属任何章节——这是合法状态，有些题
        就是跨章节的综合题。
        """
        path = path.strip().strip(PATH_SEP)
        if not path:
            return None
        parent: Chapter | None = None
        accumulated = ""
        for raw_title in path.split(PATH_SEP):
            title = self._clean_title(raw_title)
            accumulated = self._join(accumulated, title)
            existing = self.find_by_path(course_id, accumulated)
            if existing is None:
                existing = self.create(course_id, title, parent_id=parent.id if parent else None)
            parent = existing
        return parent

    def list_tree(self, course_id: str) -> Sequence[Chapter]:
        """按 path 排序返回全部章节，前端按 path 深度缩进即可画出树。"""
        return self.session.scalars(
            select(Chapter).where(Chapter.course_id == course_id).order_by(Chapter.path)
        ).all()

    def subtree_paths(self, chapter_id: str) -> list[str]:
        """某章节及其全部后代的 path。组卷按「这一章」抽题时用。"""
        chapter = self.get(chapter_id)
        prefix = f"{chapter.path}{PATH_SEP}"
        descendants = self.session.scalars(
            select(Chapter.path).where(
                Chapter.course_id == chapter.course_id, Chapter.path.startswith(prefix)
            )
        ).all()
        return [chapter.path, *descendants]

    def subtree_ids(self, chapter_ids: Iterable[str]) -> list[str]:
        """多个章节各自的子树 id 合集，去重。"""
        collected: dict[str, None] = {}
        for chapter_id in chapter_ids:
            chapter = self.get(chapter_id)
            prefix = f"{chapter.path}{PATH_SEP}"
            collected[chapter.id] = None
            for found in self.session.scalars(
                select(Chapter.id).where(
                    Chapter.course_id == chapter.course_id,
                    Chapter.path.startswith(prefix),
                )
            ).all():
                collected[found] = None
        return list(collected)

    def rename(self, chapter_id: str, title: str) -> Chapter:
        """重命名并重写整棵子树的 path。"""
        chapter = self.get(chapter_id)
        chapter.title = self._clean_title(title)
        self._rewrite_paths(chapter)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"同级已有章节「{title}」") from exc
        return chapter

    def move(self, chapter_id: str, new_parent_id: str | None) -> Chapter:
        """挂到新父节点下并重写整棵子树的 path。"""
        chapter = self.get(chapter_id)
        if new_parent_id is not None:
            new_parent = self.get(new_parent_id)
            if new_parent.course_id != chapter.course_id:
                raise ValueError("不能把章节移动到另一门课程")
            # 防止把节点挂到自己的后代下面，那样会生成一个脱离根的环
            if new_parent.path == chapter.path or new_parent.path.startswith(
                f"{chapter.path}{PATH_SEP}"
            ):
                raise ValueError("不能把章节移动到它自己的子章节下")
        chapter.parent_id = new_parent_id
        chapter.sort_order = self._next_sort_order(chapter.course_id, new_parent_id)
        self._rewrite_paths(chapter)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("目标位置已有同名章节") from exc
        return chapter

    def _rewrite_paths(self, chapter: Chapter) -> None:
        """自顶向下重算 path。

        必须用旧 path 先把后代捞出来，再改 chapter 自己的 path——顺序错了就
        找不到后代了。
        """
        old_prefix = f"{chapter.path}{PATH_SEP}"
        descendants = self.session.scalars(
            select(Chapter).where(
                Chapter.course_id == chapter.course_id,
                Chapter.path.startswith(old_prefix),
            )
        ).all()
        parent = self.session.get(Chapter, chapter.parent_id) if chapter.parent_id else None
        chapter.path = self._join(parent.path if parent else "", chapter.title)
        new_prefix = f"{chapter.path}{PATH_SEP}"
        for descendant in descendants:
            descendant.path = new_prefix + descendant.path[len(old_prefix) :]

    def delete(self, chapter_id: str) -> None:
        """删章节。其下题目的 ``chapter_id`` 被置空（ondelete SET NULL），**题目不丢**。

        子章节会一起删除（ondelete CASCADE），它们下面的题目同样只是失去章节归属。
        """
        self.session.delete(self.get(chapter_id))
        self.session.flush()
