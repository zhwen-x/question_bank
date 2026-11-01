"""导入向导。

流程是**上传 → 预览 → 确认**，中间那一步不可跳过。来源是几十份历年 Word 文件、
一次几百道题，静默导入一旦出错（格式误判、重复覆盖），用户发现时已经不知道
改了什么，而题库是他们唯一的一份资料。

预览页逐条显示「将要新建 / 已存在将跳过 / 第几行解析失败」，并对查重结果做
**字符级差异高亮**——M1 的实测结论是相似重复无法自动判定，只能交给人；
既然要人判断，就必须让人一眼看出差在哪，而不是面对两段几乎一样的中文逐字比对。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from qbank.core.content.diff import change_count, inline_diff, is_tiny_difference
from qbank.core.content.render import render_markdown
from qbank.core.enums import ConflictPolicy
from qbank.core.models import ImportBatch
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.question import QuestionRepo
from qbank.exchange.import_service import ImportPlan, ImportService, plan_file
from qbank.exchange.importers.loader import SUPPORTED_SUFFIXES, UnsupportedSourceError
from qbank.exchange.pandoc import PandocConversionError, PandocUnavailableError, capability
from qbank.web import staging
from qbank.web.deps import get_context, get_session
from qbank.web.nav import build_nav
from qbank.web.presenters import (
    DuplicateView,
    PlannedView,
    stem_excerpt,
    web_render_options,
)
from qbank.web.routers.assets import asset_url
from qbank.web.routers.common import flashes, redirect_with
from qbank.web.templating import render

router = APIRouter()

#: 最近的导入批次显示几条
RECENT_BATCHES = 10


def _recent_batches(session: Session, limit: int = RECENT_BATCHES) -> list[ImportBatch]:
    return list(
        session.scalars(
            select(ImportBatch).order_by(ImportBatch.started_at.desc()).limit(limit)
        ).all()
    )


@router.get("/import", response_class=HTMLResponse, response_model=None)
def import_page(
    request: Request,
    course_id: str = "",
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> Response:
    courses = CourseRepo(session)
    available = list(courses.list_all())
    if not available:
        return redirect_with("/courses", "先创建一门课程", "warn")
    if not course_id:
        course_id = available[0].id
    course = next((c for c in available if c.id == course_id), None)
    if course is None:
        raise HTTPException(status_code=404, detail=f"找不到课程：{course_id}")

    context = get_context(request)
    staging.sweep(context.paths)

    pandoc = capability()
    return render(
        request,
        "import.html",
        {
            "nav": build_nav(session, course_id=course_id),
            "page_title": f"{course.name} · 批量导入",
            "active": "import",
            "course": course,
            "courses": available,
            "native_suffixes": sorted({".md", ".markdown", ".txt"}),
            "pandoc_suffixes": sorted(SUPPORTED_SUFFIXES - {".md", ".markdown", ".txt"}),
            "pandoc": pandoc,
            "batches": _recent_batches(session),
            "flashes": flashes(msg, level),
        },
    )


@router.post("/import")
async def import_upload(
    request: Request,
    file: UploadFile,
    course_id: str = Form(...),
) -> RedirectResponse:
    """接收上传并落到暂存区，然后跳到预览页。

    不在这里解析：解析可能因为格式问题失败，而把失败展示在预览页上比展示在
    一个「上传失败」的红条里信息量大得多（预览页能报出具体第几行）。
    """
    context = get_context(request)
    try:
        data = await staging.read_upload(file)
        staged = staging.stage(context.paths, data, file.filename or "")
    except staging.StagingError as exc:
        return redirect_with("/import", str(exc), "error", course_id=course_id)
    return redirect_with(f"/import/{staged.token}", course_id=course_id)


def _build_preview(session: Session, plan: ImportPlan, policy: ConflictPolicy) -> list[PlannedView]:
    """把计划变成预览行，并为每条查重结果算出差异高亮。"""
    options = web_render_options(asset_url)
    repo = QuestionRepo(session)
    views: list[PlannedView] = []
    for item in plan.questions:
        duplicates: list[DuplicateView] = []
        for hint in item.duplicates:
            try:
                existing = repo.get(hint.question_id)
            except LookupError:
                continue
            segments = inline_diff(existing.stem, item.draft.stem)
            duplicates.append(
                DuplicateView(
                    question_id=hint.question_id,
                    label=hint.label,
                    score=hint.score,
                    exact=hint.exact,
                    matched_by=hint.matched_by.value,
                    existing_excerpt=stem_excerpt(existing.stem, limit=120),
                    diff=segments,
                    change_count=change_count(segments),
                    tiny=is_tiny_difference(segments),
                )
            )
        views.append(
            PlannedView(
                index=item.index,
                draft=item.draft,
                excerpt_html=render_markdown(stem_excerpt(item.draft.stem), options),
                action=item.action_under(policy),
                image_count=item.image_count,
                duplicates=duplicates,
            )
        )
    return views


@router.get("/import/{token}", response_class=HTMLResponse, response_model=None)
def import_preview(
    request: Request,
    token: str,
    course_id: str = "",
    policy: ConflictPolicy = Query(default=ConflictPolicy.SKIP),
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> Response:
    """预览页。**不写入任何内容**，可以反复刷新、换冲突策略重新看。"""
    context = get_context(request)
    try:
        staged = staging.find(context.paths, token)
    except staging.StagingError as exc:
        return redirect_with("/import", str(exc), "error", course_id=course_id)

    courses = CourseRepo(session)
    if not course_id:
        first = next(iter(courses.list_all()), None)
        if first is None:
            return redirect_with("/courses", "先创建一门课程", "warn")
        course_id = first.id
    course = courses.get(course_id)

    try:
        plan, document = plan_file(session, course_id, staged.path)
    except (
        UnsupportedSourceError,
        FileNotFoundError,
        PandocUnavailableError,
        PandocConversionError,
    ) as exc:
        staging.discard(context.paths, token)
        return redirect_with("/import", str(exc), "error", course_id=course_id)

    try:
        views = _build_preview(session, plan, policy)
        attention = sum(1 for view in views if view.needs_attention)
        return render(
            request,
            "import_preview.html",
            {
                "nav": build_nav(session, course_id=course_id),
                "page_title": f"导入预览 · {staged.original_name}",
                "active": "import",
                "course": course,
                "token": token,
                "source_name": staged.original_name,
                "source_format": plan.source_format,
                "plan": plan,
                "views": views,
                "policy": policy,
                "policies": list(ConflictPolicy),
                "summary": plan.summary(policy),
                "attention": attention,
                "flashes": flashes(msg, level),
            },
        )
    finally:
        document.close()


@router.post("/import/{token}/commit")
async def import_commit(
    request: Request,
    token: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """按勾选与策略写入。"""
    form = await request.form()
    course_id = str(form.get("course_id") or "")
    raw_policy = str(form.get("policy") or ConflictPolicy.SKIP.value)
    try:
        policy = ConflictPolicy(raw_policy)
    except ValueError:
        policy = ConflictPolicy.SKIP
    selected = [
        int(value)
        for value in form.getlist("selected")
        if isinstance(value, str) and value.isdigit()
    ]
    if not selected:
        return redirect_with(
            f"/import/{token}",
            "一道题都没勾选",
            "warn",
            course_id=course_id,
            policy=policy.value,
        )

    context = get_context(request)
    try:
        staged = staging.find(context.paths, token)
    except staging.StagingError as exc:
        return redirect_with("/import", str(exc), "error", course_id=course_id)

    plan, document = plan_file(session, course_id, staged.path)
    try:
        result = ImportService(session).commit(plan, policy=policy, selected=selected)
    finally:
        document.close()
    staging.discard(context.paths, token)

    pieces = [f"新建 {result.created}", f"更新 {result.updated}", f"跳过 {result.skipped}"]
    if result.failed:
        pieces.append(f"失败 {result.failed}")
    if result.image_errors:
        pieces.append(f"{len(result.image_errors)} 张图片有问题")
    return redirect_with(
        "/import",
        f"导入完成：{'，'.join(pieces)}。批次 {result.batch_id[:8]}，可整批撤销。",
        "ok" if not result.failed else "warn",
        course_id=course_id,
    )


@router.post("/import/{token}/discard")
def import_discard(
    request: Request,
    token: str,
    course_id: str = Form(""),
) -> RedirectResponse:
    staging.discard(get_context(request).paths, token)
    return redirect_with("/import", "已丢弃这次上传", course_id=course_id)


@router.post("/import/batches/{batch_id}/rollback")
def batch_rollback(
    batch_id: str,
    course_id: str = Form(""),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """撤销一次导入：删掉该批**新建**的题目。

    被覆盖的题不会自动恢复——它们各有修改历史，应该在题目详情页单独回滚。
    混在一起做会让「撤销导入」的后果变得不可预测。
    """
    if confirm.strip() != "撤销":
        return redirect_with("/import", "未输入「撤销」，操作已取消", "error", course_id=course_id)
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        return redirect_with("/import", "找不到这个批次", "error", course_id=course_id)
    removed = ImportService(session).rollback_batch(batch_id)
    return redirect_with("/import", f"已撤销，删除 {removed} 道本批新建的题目", course_id=course_id)
