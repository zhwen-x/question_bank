"""标签管理页面。

重点是合并——用久了必然积累出「循环」「循环结构」「while循环」这类同义标签，
没有合并功能的话筛选器会越来越没法用。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from qbank.core.enums import TagCategory
from qbank.core.repo.base import ConflictError
from qbank.core.repo.tag import TagRepo
from qbank.web.deps import get_session
from qbank.web.nav import build_nav
from qbank.web.routers.common import flashes, redirect_with
from qbank.web.templating import render

router = APIRouter()


@router.get("/tags", response_class=HTMLResponse)
def tag_page(
    request: Request,
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    repo = TagRepo(session)
    counts = repo.usage_counts()
    tags = list(repo.list_all())
    # 没人用的标签排在前面：这一页的主要用途就是清理和合并
    tags.sort(key=lambda tag: (counts.get(tag.id, 0), tag.name))
    rows = [{"tag": tag, "count": counts.get(tag.id, 0)} for tag in tags]
    return render(
        request,
        "tags.html",
        {
            "nav": build_nav(session),
            "page_title": "标签",
            "rows": rows,
            "categories": list(TagCategory),
            "unused": sum(1 for row in rows if not row["count"]),
            "active": "tags",
            "flashes": flashes(msg, level),
        },
    )


@router.post("/tags")
def tag_create(
    name: str = Form(""),
    category: str = Form(TagCategory.KNOWLEDGE.value),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        TagRepo(session).create(name, category=TagCategory(category))
    except (ConflictError, ValueError) as exc:
        return redirect_with("/tags", str(exc), "error")
    return redirect_with("/tags", f"已创建标签「{name.strip()}」")


@router.post("/tags/{tag_id}/update")
def tag_update(
    tag_id: str,
    name: str = Form(""),
    category: str = Form(TagCategory.KNOWLEDGE.value),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    repo = TagRepo(session)
    tag = repo.get(tag_id)
    try:
        tag.category = TagCategory(category)
        if name.strip() and name.strip() != tag.name:
            repo.rename(tag_id, name)
        session.flush()
    except (ConflictError, ValueError) as exc:
        return redirect_with("/tags", str(exc), "error")
    return redirect_with("/tags", "已更新")


@router.post("/tags/merge")
def tag_merge(
    source_id: str = Form(...),
    target_id: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    repo = TagRepo(session)
    source = repo.get(source_id)
    target = repo.get(target_id)
    if source.id == target.id:
        return redirect_with("/tags", "源标签与目标标签相同，未执行", "warn")
    result = repo.merge(source_id, target_id)
    detail = f"{result.moved} 道改挂标签"
    if result.deduplicated:
        detail += f"，{result.deduplicated} 道原本两个标签都有、已去重"
    return redirect_with("/tags", f"已把「{source.name}」合并进「{target.name}」：{detail}")


@router.post("/tags/{tag_id}/delete")
def tag_delete(
    tag_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """删标签。题目不受影响，只是少一个标签。"""
    repo = TagRepo(session)
    name = repo.get(tag_id).name
    repo.delete(tag_id)
    return redirect_with("/tags", f"已删除标签「{name}」，题目未受影响")


@router.post("/tags/clean")
def tag_clean(session: Session = Depends(get_session)) -> RedirectResponse:
    removed = TagRepo(session).delete_unused()
    return redirect_with("/tags", f"已清理 {removed} 个未使用的标签")
