"""章节管理页面。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from qbank.core.repo.base import ConflictError, NotFoundError
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.web.deps import get_session
from qbank.web.nav import build_nav
from qbank.web.routers.common import flashes, redirect_with
from qbank.web.templating import render

router = APIRouter()


@router.get("/chapters", response_class=HTMLResponse, response_model=None)
def chapter_page(
    request: Request,
    course_id: str = "",
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> Response:
    courses = CourseRepo(session)
    if not course_id:
        first = next(iter(courses.list_all()), None)
        if first is None:
            return redirect_with("/courses", "先创建一门课程", "warn")
        course_id = first.id
    course = courses.get(course_id)
    chapters = ChapterRepo(session).list_tree(course_id)
    repo = QuestionRepo(session)
    rows = [
        {
            "chapter": chapter,
            "depth": chapter.path.count("/"),
            "count": repo.count(QuestionFilter(course_id=course_id, chapter_ids=[chapter.id])),
        }
        for chapter in chapters
    ]
    return render(
        request,
        "chapters.html",
        {
            "nav": build_nav(session, course_id=course_id),
            "page_title": f"{course.name} · 章节",
            "course": course,
            "rows": rows,
            "flashes": flashes(msg, level),
        },
    )


@router.post("/chapters")
def chapter_create(
    course_id: str = Form(...),
    path: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        chapter = ChapterRepo(session).ensure_path(course_id, path)
    except (ConflictError, ValueError) as exc:
        return redirect_with("/chapters", str(exc), "error", course_id=course_id)
    if chapter is None:
        return redirect_with("/chapters", "章节路径不能为空", "error", course_id=course_id)
    return redirect_with("/chapters", f"已创建章节「{chapter.path}」", course_id=course_id)


@router.post("/chapters/{chapter_id}/rename")
def chapter_rename(
    chapter_id: str,
    title: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    repo = ChapterRepo(session)
    course_id = repo.get(chapter_id).course_id
    try:
        repo.rename(chapter_id, title)
    except (ConflictError, ValueError) as exc:
        return redirect_with("/chapters", str(exc), "error", course_id=course_id)
    return redirect_with("/chapters", "已重命名（子章节路径已同步更新）", course_id=course_id)


@router.post("/chapters/{chapter_id}/move")
def chapter_move(
    chapter_id: str,
    parent_id: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    repo = ChapterRepo(session)
    course_id = repo.get(chapter_id).course_id
    try:
        repo.move(chapter_id, parent_id or None)
    except (ConflictError, ValueError, NotFoundError) as exc:
        return redirect_with("/chapters", str(exc), "error", course_id=course_id)
    return redirect_with("/chapters", "已移动（子章节路径已同步更新）", course_id=course_id)


@router.post("/chapters/{chapter_id}/delete")
def chapter_delete(
    chapter_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """删除章节。其下题目不会丢，只是失去章节归属（会出现在「未归类」里）。"""
    repo = ChapterRepo(session)
    chapter = repo.get(chapter_id)
    course_id = chapter.course_id
    title = chapter.title
    repo.delete(chapter_id)
    return redirect_with(
        "/chapters",
        f"已删除章节「{title}」；其下题目移入「未归类」，未被删除",
        course_id=course_id,
    )
