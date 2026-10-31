"""侧栏导航的上下文。

每个页面都要渲染侧栏（课程列表、章节树、各处计数），所以集中在这里算一次。
计数用一次分组查询拿到，而不是每个章节查一遍——章节多的课程有上百个节点，
逐个查会让每次翻页都发出几百条 SQL。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from qbank.core.models import Chapter, Course, Paper, Question, Tag
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo


@dataclass(frozen=True, slots=True)
class ChapterNode:
    """章节树的一个节点。``depth`` 由物化路径算出，模板按它缩进。"""

    id: str
    title: str
    path: str
    depth: int


@dataclass(slots=True)
class Nav:
    courses: list[Course] = field(default_factory=list)
    course: Course | None = None
    course_counts: dict[str, int] = field(default_factory=dict)
    chapters: list[ChapterNode] = field(default_factory=list)
    chapter_counts: dict[str, int] = field(default_factory=dict)
    chapter_id: str | None = None
    unchaptered: bool = False
    unchaptered_count: int = 0
    tag_count: int = 0
    trash_count: int = 0
    paper_count: int = 0
    query: str = ""


def _counts_by(
    session: Session,
    column: InstrumentedAttribute[Any],
    course_id: str | None = None,
) -> dict[str, int]:
    """按某一列分组统计未删除的顶层题目数量。"""
    statement = select(column, func.count(Question.id)).where(
        Question.deleted_at.is_(None), Question.parent_id.is_(None)
    )
    if course_id:
        statement = statement.where(Question.course_id == course_id)
    rows = session.execute(statement.group_by(column)).all()
    return {row[0]: int(row[1]) for row in rows if row[0] is not None}


def build_nav(
    session: Session,
    *,
    course_id: str | None = None,
    chapter_id: str | None = None,
    unchaptered: bool = False,
    query: str = "",
) -> Nav:
    """组装侧栏上下文。``course_id`` 不存在时当作未选择课程，而不是报错。"""
    nav = Nav(
        courses=list(CourseRepo(session).list_all()),
        course_counts=_counts_by(session, Question.course_id),
        tag_count=int(session.scalar(select(func.count(Tag.id))) or 0),
        trash_count=len(QuestionRepo(session).list_trash()),
        chapter_id=chapter_id,
        unchaptered=unchaptered,
        query=query,
    )
    if course_id:
        nav.course = next((c for c in nav.courses if c.id == course_id), None)
    if nav.course is None:
        return nav
    nav.paper_count = int(
        session.scalar(
            select(func.count(Paper.id)).where(
                Paper.course_id == nav.course.id, Paper.deleted_at.is_(None)
            )
        )
        or 0
    )

    chapters = session.scalars(
        select(Chapter).where(Chapter.course_id == nav.course.id).order_by(Chapter.path)
    ).all()
    nav.chapters = [
        ChapterNode(
            id=chapter.id,
            title=chapter.title,
            path=chapter.path,
            depth=chapter.path.count("/"),
        )
        for chapter in chapters
    ]
    nav.chapter_counts = _counts_by(session, Question.chapter_id, nav.course.id)
    nav.unchaptered_count = QuestionRepo(session).count(
        QuestionFilter(course_id=nav.course.id, include_unchaptered=True)
    )
    return nav
