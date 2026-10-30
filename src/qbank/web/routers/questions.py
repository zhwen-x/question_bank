"""题目页面：列表、详情、新建/编辑、回收站、修改历史。

列表页的筛选条件来自查询参数，整页都可通过 URL 复现——老师可以把「数据结构
第 2 章的难题」这样一个视图收藏成书签，也便于把问题反馈时直接贴链接。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from qbank.core.content.text import to_plain_text
from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.core.repo.base import NotFoundError
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.services.asset import AssetService
from qbank.core.services.question import QuestionService
from qbank.web.deps import get_context, get_session
from qbank.web.forms import FormError, parse_question_form
from qbank.web.nav import build_nav
from qbank.web.presenters import build_rows, build_view, web_render_options
from qbank.web.routers.assets import asset_url
from qbank.web.routers.common import flashes, redirect_with
from qbank.web.templating import render

router = APIRouter()

#: 每页题目数。给得不大，因为每行都要渲染 Markdown 与公式。
PAGE_SIZE = 25


def _options() -> Any:
    return web_render_options(asset_url)


def _query_string(params: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        if value in (None, "", False):
            continue
        if isinstance(value, (list, tuple)):
            flat.extend((key, str(item)) for item in value)
        else:
            flat.append((key, str(value)))
    return urlencode(flat)


@router.get("/questions", response_class=HTMLResponse)
def question_list(
    request: Request,
    course_id: str = "",
    chapter_id: str = "",
    unchaptered: bool = False,
    q: str = "",
    type: str = "",  # noqa: A002 - 查询参数名就叫 type，改了会破坏书签
    difficulty: int | None = None,
    status: str = "",
    tag: list[str] = Query(default=[]),
    page: int = 1,
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    courses = CourseRepo(session)
    available = list(courses.list_all())
    if not available:
        return render(
            request,
            "questions_empty.html",
            {"nav": build_nav(session), "page_title": "题库", "flashes": flashes(msg, level)},
        )
    if not course_id:
        course_id = available[0].id
    course = next((c for c in available if c.id == course_id), None)
    if course is None:
        raise HTTPException(status_code=404, detail=f"找不到课程：{course_id}")

    service = QuestionService(session)
    tags_repo = TagRepo(session)
    selected_tags = [found for name in tag if (found := tags_repo.find_by_name(name))]

    chapter_ids: list[str] = []
    if chapter_id:
        # 选了某一章就连带它的子章节——老师点「第2章」想看的是整章
        chapter_ids = ChapterRepo(session).subtree_ids([chapter_id])

    criteria = QuestionFilter(
        course_id=course_id,
        chapter_ids=chapter_ids,
        include_unchaptered=unchaptered,
        types=[QuestionType(type)] if type else (),
        difficulties=[Difficulty(difficulty)] if difficulty else (),
        statuses=[QuestionStatus(status)] if status else (),
        all_tag_ids=[found.id for found in selected_tags],
    )
    ordered_ids: list[str] | None = None
    if q.strip():
        # 用 search_ids：命中子题时要显示它的父题，否则表现为「什么都没搜到」
        ordered_ids = service.search_ids(q, limit=PAGE_SIZE * 20)
        criteria.restrict_ids = ordered_ids

    page = max(1, page)
    result = service.questions.list_page(
        criteria,
        offset=(page - 1) * PAGE_SIZE,
        limit=PAGE_SIZE,
        order_by_ids=ordered_ids,
    )
    base_params = {
        "course_id": course_id,
        "chapter_id": chapter_id,
        "unchaptered": "1" if unchaptered else "",
        "q": q,
        "type": type,
        "difficulty": difficulty,
        "status": status,
        "tag": tag,
    }
    return render(
        request,
        "questions.html",
        {
            "nav": build_nav(
                session,
                course_id=course_id,
                chapter_id=chapter_id,
                unchaptered=unchaptered,
                query=q,
            ),
            "page_title": f"{course.name} · 题库",
            "course": course,
            "rows": build_rows(list(result.items), _options()),
            "result": result,
            "page": page,
            "page_size": PAGE_SIZE,
            "total_pages": max(1, -(-result.total // PAGE_SIZE)),
            "filters": base_params,
            "query_string": _query_string(base_params),
            "types": list(QuestionType),
            "difficulties": list(Difficulty),
            "statuses": list(QuestionStatus),
            "all_tags": list(tags_repo.list_all()),
            "selected_tag_names": [found.name for found in selected_tags],
            "flashes": flashes(msg, level),
        },
    )


@router.get("/questions/new", response_class=HTMLResponse, response_model=None)
def question_new(
    request: Request,
    course_id: str = "",
    type: str = QuestionType.SINGLE_CHOICE.value,  # noqa: A002
    chapter_id: str = "",
    session: Session = Depends(get_session),
) -> Response:
    courses = CourseRepo(session)
    if not course_id:
        first = next(iter(courses.list_all()), None)
        if first is None:
            return redirect_with("/courses", "先创建一门课程", "warn")
        course_id = first.id
    course = courses.get(course_id)
    chapter_path = ""
    if chapter_id:
        try:
            chapter_path = ChapterRepo(session).get(chapter_id).path
        except NotFoundError:
            chapter_path = ""
    return render(
        request,
        "question_form.html",
        {
            "nav": build_nav(session, course_id=course_id),
            "page_title": f"{course.name} · 新建题目",
            "course": course,
            "mode": "new",
            "action": f"/questions?course_id={course_id}",
            "values": _blank_values(type, chapter_path),
            "errors": FormError(),
            **_form_choices(session, course_id),
        },
    )


def _blank_values(type_value: str, chapter_path: str) -> dict[str, Any]:
    """新建表单的初始值。

    默认给两个空选项：选择题是最常用的题型，开局就有两行可填比让用户先点两次
    「添加选项」顺手得多。
    """
    try:
        kind = QuestionType(type_value)
    except ValueError:
        kind = QuestionType.SINGLE_CHOICE
    return {
        "type": kind.value,
        "stem": "",
        "chapter": chapter_path,
        "difficulty": 3,
        "score": "",
        "est_minutes": "",
        "tags": "",
        "source": "",
        "status": QuestionStatus.DRAFT.value,
        "answer": "",
        "explanation": "",
        "scoring_rubric": "",
        "external_id": "",
        "options": [
            {"label": "A", "content": "", "is_correct": False},
            {"label": "B", "content": "", "is_correct": False},
        ],
        "shuffle_ok": True,
        "tf_correct": "true",
        "blanks": [{"answers": "", "match": "exact", "tolerance": ""}],
        "language": "",
        "signature": "",
        "starter_code": "",
        "key_points": "",
        "material_label": "",
        "sub_questions": [],
    }


def _form_choices(session: Session, course_id: str) -> dict[str, Any]:
    return {
        "types": list(QuestionType),
        "difficulties": list(Difficulty),
        "statuses": list(QuestionStatus),
        "chapters": [
            {"path": chapter.path, "depth": chapter.path.count("/")}
            for chapter in ChapterRepo(session).list_tree(course_id)
        ],
        "known_tags": [tag.name for tag in TagRepo(session).list_all()],
    }


def _values_from_form(form: Any) -> dict[str, Any]:
    """校验失败时回填表单。

    老师写一道综合题可能花十分钟，不能因为漏填一个分值就把整页清空。
    """

    def texts(key: str) -> list[str]:
        return [value for value in form.getlist(key) if isinstance(value, str)]

    correct = {value.upper() for value in texts("correct")}
    labels = texts("option_label")
    contents = texts("option_content")
    hidden = set(texts("tc_hidden"))
    return {
        "type": form.get("type") or QuestionType.SINGLE_CHOICE.value,
        "stem": form.get("stem") or "",
        "chapter": form.get("chapter") or "",
        "difficulty": form.get("difficulty") or 3,
        "score": form.get("score") or "",
        "est_minutes": form.get("est_minutes") or "",
        "tags": form.get("tags") or "",
        "source": form.get("source") or "",
        "status": form.get("status") or QuestionStatus.DRAFT.value,
        "answer": form.get("answer") or "",
        "explanation": form.get("explanation") or "",
        "scoring_rubric": form.get("scoring_rubric") or "",
        "external_id": form.get("external_id") or "",
        "options": [
            {
                "label": label,
                "content": contents[index] if index < len(contents) else "",
                "is_correct": label.strip().upper() in correct,
            }
            for index, label in enumerate(labels)
        ]
        or [{"label": "A", "content": "", "is_correct": False}],
        "shuffle_ok": form.get("shuffle_ok") is not None,
        "tf_correct": form.get("tf_correct") or "true",
        "blanks": [
            {
                "answers": answer,
                "match": (texts("blank_match") + ["exact"] * 20)[index],
                "tolerance": (texts("blank_tolerance") + [""] * 20)[index],
            }
            for index, answer in enumerate(texts("blank_answer"))
        ]
        or [{"answers": "", "match": "exact", "tolerance": ""}],
        "language": form.get("language") or "",
        "signature": form.get("signature") or "",
        "starter_code": form.get("starter_code") or "",
        "key_points": form.get("key_points") or "",
        "material_label": form.get("material_label") or "",
        "test_cases": [
            {
                "name": (texts("tc_name") + [""] * 20)[index],
                "input": value,
                "expected": (texts("tc_expected") + [""] * 20)[index],
                "hidden": str(index) in hidden,
            }
            for index, value in enumerate(texts("tc_input"))
        ],
        "sub_questions": [
            {
                "type": (texts("sub_type") + ["short_answer"] * 40)[index],
                "stem": stem,
                "answer": (texts("sub_answer") + [""] * 40)[index],
                "explanation": (texts("sub_explanation") + [""] * 40)[index],
                "score": (texts("sub_score") + [""] * 40)[index],
            }
            for index, stem in enumerate(texts("sub_stem"))
        ],
    }


@router.post("/questions", response_model=None)
async def question_create(
    request: Request,
    course_id: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    course = CourseRepo(session).get(course_id)
    try:
        draft = parse_question_form(form)
    except FormError as error:
        return render(
            request,
            "question_form.html",
            {
                "nav": build_nav(session, course_id=course_id),
                "page_title": f"{course.name} · 新建题目",
                "course": course,
                "mode": "new",
                "action": f"/questions?course_id={course_id}",
                "values": _values_from_form(form),
                "errors": error,
                **_form_choices(session, course_id),
            },
            status_code=422,
        )
    service = QuestionService(session)
    question = service.create(course_id, draft)
    _link_assets(session, request, question.id, draft)
    return redirect_with(f"/questions/{question.id}", "已创建题目")


def _link_assets(session: Session, request: Request, question_id: str, draft: Any) -> None:
    """建立题目与图片的引用关系。

    必须在保存后做：引用计数决定孤儿清理删不删文件，漏了会把还在用的图删掉。
    """
    context = get_context(request)
    assets = AssetService(session, context.paths)
    texts = [draft.stem, draft.answer, draft.explanation]
    options = getattr(draft.payload, "options", None)
    if options:
        texts.extend(option.content for option in options)
    texts.extend(sub.stem for sub in draft.sub_questions)
    referenced: list[str] = []
    for text in texts:
        referenced.extend(assets.referenced_assets(text))
    assets.link(question_id, referenced)


@router.get("/questions/{question_id}", response_class=HTMLResponse)
def question_detail(
    request: Request,
    question_id: str,
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    repo = QuestionRepo(session)
    question = repo.get_detailed(question_id, include_deleted=True)
    service = QuestionService(session)
    draft = service.to_draft(question)
    return render(
        request,
        "question_detail.html",
        {
            "nav": build_nav(
                session,
                course_id=question.course_id,
                chapter_id=question.chapter_id or "",
            ),
            # 标题走纯文本：原始 Markdown 里的 $x^2$、``` 直接进 <title>
            # 会变成一串没人看得懂的符号
            "page_title": to_plain_text(draft.stem)[:30] or "题目",
            "view": build_view(question, draft, _options()),
            "revisions": list(service.revisions(question_id)),
            "usage_count": repo.usage_count(question_id),
            "flashes": flashes(msg, level),
        },
    )


@router.get("/questions/{question_id}/edit", response_class=HTMLResponse)
def question_edit(
    request: Request,
    question_id: str,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    question = QuestionRepo(session).get_detailed(question_id)
    draft = QuestionService(session).to_draft(question)
    return render(
        request,
        "question_form.html",
        {
            "nav": build_nav(session, course_id=question.course_id),
            "page_title": "编辑题目",
            "course": question.course,
            "mode": "edit",
            "action": f"/questions/{question_id}/edit",
            "question": question,
            "values": _values_from_draft(draft),
            "errors": FormError(),
            **_form_choices(session, question.course_id),
        },
    )


def _values_from_draft(draft: Any) -> dict[str, Any]:
    payload: Any = draft.payload
    options = getattr(payload, "options", None)
    blanks = getattr(payload, "blanks", None)
    return {
        "type": draft.type.value,
        "stem": draft.stem,
        "chapter": draft.chapter,
        "difficulty": draft.difficulty.value,
        "score": draft.score or "",
        "est_minutes": draft.est_minutes or "",
        "tags": "、".join(draft.tags),
        "source": draft.source,
        "status": draft.status.value,
        "answer": draft.answer,
        "explanation": draft.explanation,
        "scoring_rubric": draft.scoring_rubric,
        "external_id": draft.external_id,
        "options": [
            {"label": o.label, "content": o.content, "is_correct": o.is_correct}
            for o in (options or [])
        ]
        or [
            {"label": "A", "content": "", "is_correct": False},
            {"label": "B", "content": "", "is_correct": False},
        ],
        "shuffle_ok": getattr(payload, "shuffle_ok", True),
        "tf_correct": "true" if getattr(payload, "correct", True) else "false",
        "blanks": [
            {"answers": "/".join(b.answers), "match": b.match.value, "tolerance": b.tolerance or ""}
            for b in (blanks or [])
        ]
        or [{"answers": "", "match": "exact", "tolerance": ""}],
        "language": getattr(payload, "language", ""),
        "signature": getattr(payload, "function_signature", ""),
        "starter_code": getattr(payload, "starter_code", ""),
        "key_points": "\n".join(getattr(payload, "key_points", ())),
        "material_label": getattr(payload, "material_label", ""),
        "test_cases": [
            {"name": c.name, "input": c.input, "expected": c.expected, "hidden": c.hidden}
            for c in getattr(payload, "test_cases", ())
        ],
        "sub_questions": [
            {
                "type": sub.type.value,
                "stem": sub.stem,
                "answer": sub.answer,
                "explanation": sub.explanation,
                "score": sub.score or "",
            }
            for sub in draft.sub_questions
        ],
    }


@router.post("/questions/{question_id}/edit", response_model=None)
async def question_update(
    request: Request,
    question_id: str,
    session: Session = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    question = QuestionRepo(session).get_detailed(question_id)
    try:
        draft = parse_question_form(form)
    except FormError as error:
        return render(
            request,
            "question_form.html",
            {
                "nav": build_nav(session, course_id=question.course_id),
                "page_title": "编辑题目",
                "course": question.course,
                "mode": "edit",
                "action": f"/questions/{question_id}/edit",
                "question": question,
                "values": _values_from_form(form),
                "errors": error,
                **_form_choices(session, question.course_id),
            },
            status_code=422,
        )
    note = str(form.get("note") or "界面编辑")
    QuestionService(session).update(question_id, draft, note=note)
    _link_assets(session, request, question_id, draft)
    return redirect_with(f"/questions/{question_id}", "已保存（修改前的版本已存入历史）")


@router.post("/questions/{question_id}/delete")
def question_delete(
    question_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    question = QuestionRepo(session).get(question_id)
    course_id = question.course_id
    QuestionService(session).delete(question_id)
    return redirect_with("/questions", "已移入回收站，可在回收站恢复", course_id=course_id)


@router.post("/questions/{question_id}/restore")
def question_restore(
    question_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    QuestionService(session).restore(question_id)
    return redirect_with(f"/questions/{question_id}", "已恢复")


@router.post("/questions/{question_id}/rollback")
def question_rollback(
    question_id: str,
    revision: int = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        QuestionService(session).rollback(question_id, revision)
    except ValueError as exc:
        return redirect_with(f"/questions/{question_id}", str(exc), "error")
    return redirect_with(f"/questions/{question_id}", f"已回滚到第 {revision} 版")


@router.get("/trash", response_class=HTMLResponse)
def trash_page(
    request: Request,
    course_id: str = "",
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    repo = QuestionRepo(session)
    items = repo.list_trash(course_id or None)
    return render(
        request,
        "trash.html",
        {
            "nav": build_nav(session, course_id=course_id),
            "page_title": "回收站",
            "rows": build_rows(list(items), _options()),
            "active": "trash",
            "flashes": flashes(msg, level),
        },
    )


@router.post("/trash/purge")
def trash_purge(
    course_id: str = Form(""),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """清空回收站。

    要求输入「清空」两个字才执行——这是本应用里唯一真正不可撤销的批量删除。
    """
    if confirm.strip() != "清空":
        return redirect_with("/trash", "未输入「清空」，操作已取消", "error")
    repo = QuestionRepo(session)
    service = QuestionService(session)
    items = list(repo.list_trash(course_id or None))
    for question in items:
        service.purge(question.id)
    return redirect_with("/trash", f"已永久删除 {len(items)} 道题")
