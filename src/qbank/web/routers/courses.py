"""课程页面。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from qbank.core.repo.base import ConflictError
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.web.deps import get_session
from qbank.web.nav import build_nav
from qbank.web.routers.common import flashes, redirect_with
from qbank.web.templating import render

router = APIRouter()


@router.get("/courses", response_class=HTMLResponse)
def course_list(
    request: Request,
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    repo = CourseRepo(session)
    questions = QuestionRepo(session)
    courses = [
        {
            "course": course,
            "total": questions.count(QuestionFilter(course_id=course.id)),
            "all_levels": questions.count(
                QuestionFilter(course_id=course.id, top_level_only=False)
            ),
        }
        for course in repo.list_all()
    ]
    return render(
        request,
        "courses.html",
        {
            "nav": build_nav(session),
            "page_title": "课程",
            "courses": courses,
            "flashes": flashes(msg, level),
        },
    )


@router.post("/courses")
def course_create(
    name: str = Form(""),
    code: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        course = CourseRepo(session).create(name, code=code)
    except (ConflictError, ValueError) as exc:
        return redirect_with("/courses", str(exc), "error")
    return redirect_with("/courses", f"已创建课程「{course.name}」")


@router.post("/courses/{course_id}/rename")
def course_rename(
    course_id: str,
    name: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        CourseRepo(session).rename(course_id, name)
    except (ConflictError, ValueError) as exc:
        return redirect_with("/courses", str(exc), "error")
    return redirect_with("/courses", "已重命名")


@router.post("/courses/{course_id}/delete")
def course_delete(
    course_id: str,
    confirm_name: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """删除课程。

    要求把课程名**手打一遍**才执行。这是不可撤销且会连带删除全部题目的操作，
    一个「确定吗」对话框挡不住误点——题库是老师唯一的一份资料。
    """
    repo = CourseRepo(session)
    course = repo.get(course_id)
    if confirm_name.strip() != course.name:
        return redirect_with("/courses", "课程名输入不一致，未执行删除", "error")
    name = course.name
    repo.delete(course_id)
    return redirect_with("/courses", f"已删除课程「{name}」及其全部题目")
