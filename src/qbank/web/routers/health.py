"""状态与自检页。

把 ``qbank doctor`` 的结果搬到界面上。界面用户不会去开终端，但「为什么导不了
Word」「为什么没法导出 PDF」正是他们最常遇到的问题——答案就在这一页。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qbank.core.models import Asset, Question
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.services.asset import AssetService
from qbank.core.services.question import QuestionService
from qbank.doctor import run_checks
from qbank.web.deps import get_context, get_session
from qbank.web.nav import build_nav
from qbank.web.routers.common import flashes, redirect_with
from qbank.web.templating import render

router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
def health_page(
    request: Request,
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    context = get_context(request)
    assets = AssetService(session, context.paths)
    repo = QuestionRepo(session)
    service = QuestionService(session)

    all_assets = list(session.scalars(select(Asset)).all())
    missing_files = [asset for asset in all_assets if assets.path_of(asset.sha256) is None]
    orphans = list(assets.orphans())
    indexed = service.search.indexed_count()
    live = int(
        session.scalar(select(func.count(Question.id)).where(Question.deleted_at.is_(None))) or 0
    )

    return render(
        request,
        "health.html",
        {
            "nav": build_nav(session),
            "page_title": "状态与自检",
            "active": "health",
            "checks": list(run_checks()),
            "paths": context.paths,
            "stats": {
                "courses": len(CourseRepo(session).list_all()),
                "questions": repo.count(QuestionFilter()),
                "questions_all_levels": live,
                "indexed": indexed,
                "index_stale": indexed != live,
                "trash": len(repo.list_trash()),
                "assets": len(all_assets),
                "asset_bytes": sum(asset.byte_size for asset in all_assets),
                "orphan_assets": len(orphans),
                "orphan_bytes": sum(asset.byte_size for asset in orphans),
                "missing_files": len(missing_files),
            },
            "flashes": flashes(msg, level),
        },
    )


@router.post("/health/reindex")
def reindex(session: Session = Depends(get_session)) -> RedirectResponse:
    count = QuestionService(session).reindex_all()
    return redirect_with("/health", f"已重建 {count} 条索引")


@router.post("/health/clean-assets")
def clean_assets(
    request: Request,
    confirm: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if confirm.strip() != "清理":
        return redirect_with("/health", "未输入「清理」，操作已取消", "error")
    context = get_context(request)
    removed, freed = AssetService(session, context.paths).purge_orphans()
    return redirect_with(
        "/health", f"已删除 {removed} 张无人引用的图片，释放 {freed / 1024 / 1024:.1f} MB"
    )
