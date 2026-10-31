"""试卷页面：列表、自动组卷（预览后确认）、试卷编辑。

自动组卷走的是和导入一样的**预览 → 确认**两步，理由也一样：一次动作影响几十
道题，出了偏差事后很难说清改了什么。区别在于组卷不需要服务端暂存——同一个
题库状态 + 同一份蓝图 + 同一个种子必然抽出同一份卷，所以预览页只要把蓝图和
种子原样带回来，重跑一次就是刚才看到的那一份。

预览页里的蓝图是一段 YAML，而不是一堆隐藏字段：它既是回传的载荷，又正好是
可以存成文件、提交进 git、明年再用一次的那个东西。
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from qbank.core.blueprint import Blueprint
from qbank.core.enums import QuestionType
from qbank.core.repo.base import NotFoundError
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.services.paper import PaperService, ShortageError
from qbank.core.services.selection import BlueprintError
from qbank.web.deps import get_session
from qbank.web.forms import (
    FormError,
    parse_blueprint_form,
    parse_blueprint_yaml,
    section_indices,
)
from qbank.web.nav import build_nav
from qbank.web.presenters import (
    build_paper_view,
    build_picked_sections,
    web_render_options,
)
from qbank.web.routers.assets import asset_url
from qbank.web.routers.common import flashes, redirect_with
from qbank.web.templating import render

router = APIRouter()


def _options() -> Any:
    return web_render_options(asset_url)


def _blueprint_yaml(blueprint: Blueprint) -> str:
    """蓝图 → 可读的 YAML。预览页回传用，也是用户可以存下来的那份文件。"""
    return yaml.safe_dump(blueprint.to_json(), allow_unicode=True, sort_keys=False)


def _course_or_404(session: Session, course_id: str) -> Any:
    courses = CourseRepo(session)
    available = list(courses.list_all())
    if not available:
        raise HTTPException(status_code=404, detail="还没有课程，先建一门课。")
    if not course_id:
        return available[0]
    found = next((course for course in available if course.id == course_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail=f"找不到课程：{course_id}")
    return found


# ===================================================================== 列表
@router.get("/papers", response_class=HTMLResponse, response_model=None)
def paper_list(
    request: Request,
    course_id: str = "",
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    course = _course_or_404(session, course_id)
    repo = PaperRepo(session)
    papers = list(repo.list_all(course.id))
    return render(
        request,
        "papers.html",
        {
            "nav": build_nav(session, course_id=course.id),
            "active": "papers",
            "page_title": f"{course.name} · 试卷",
            "course": course,
            "papers": papers,
            "summaries": repo.summaries(course.id),
            "trashed": [
                paper
                for paper in repo.list_all(course.id, include_deleted=True)
                if paper.deleted_at is not None
            ],
            "flashes": flashes(msg, level),
        },
    )


@router.post("/papers/new")
def paper_new(
    course_id: str = Form(""),
    title: str = Form(""),
    sections: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """建一份空白卷，之后从题目列表里逐道加题。"""
    course = _course_or_404(session, course_id)
    if not title.strip():
        return redirect_with("/papers", "试卷标题不能为空", "error", course_id=course.id)
    titles = [line.strip() for line in sections.splitlines() if line.strip()] or ["一、试题"]
    paper = PaperService(session).create_empty(course.id, title.strip(), sections=titles)
    return redirect_with(f"/papers/{paper.id}", f"已建《{paper.title}》，接下来从题库里挑题")


# ================================================================= 自动组卷
@router.get("/papers/assemble", response_class=HTMLResponse, response_model=None)
def assemble_page(
    request: Request,
    course_id: str = "",
    msg: str = "",
    level: str = "ok",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    course = _course_or_404(session, course_id)
    return render(
        request,
        "paper_assemble.html",
        _assemble_context(session, course, flashes(msg, level)),
    )


def _assemble_context(
    session: Session,
    course: Any,
    flash_list: list[dict[str, str]],
    *,
    form: Any = None,
    errors: FormError | None = None,
) -> dict[str, Any]:
    return {
        "nav": build_nav(session, course_id=course.id),
        "active": "papers",
        "page_title": "自动组卷",
        "course": course,
        "types": list(QuestionType),
        "chapters": list(ChapterRepo(session).list_tree(course.id)),
        "all_tags": list(TagRepo(session).list_all()),
        "form": form,
        # 校验失败时按原来的行数重新渲染：蓝图有十几个字段，
        # 因为难度写错一个字就把已填的四个大题清空是不可接受的
        "section_rows": section_indices(form) if form is not None else [0],
        "errors": errors,
        "flashes": flash_list,
    }


@router.post("/papers/assemble", response_class=HTMLResponse, response_model=None)
async def assemble_run(
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """解析蓝图 → 抽题 → 预览或生成。

    ``action=preview`` 只抽题不写库；``action=commit`` 才真的生成试卷。
    两步用的是同一份蓝图与同一个种子，所以确认生成的就是预览里看到的那份。
    """
    form = await request.form()
    course = _course_or_404(session, str(form.get("course_id") or ""))
    action = str(form.get("action") or "preview")
    source = str(form.get("source") or "form")
    raw_seed = str(form.get("seed") or "").strip()

    try:
        if source == "yaml":
            blueprint = parse_blueprint_yaml(str(form.get("blueprint_yaml") or ""))
        else:
            blueprint = parse_blueprint_form(form)
    except FormError as error:
        return render(
            request,
            "paper_assemble.html",
            _assemble_context(session, course, [], form=form, errors=error),
            status_code=400,
        )

    seed = int(raw_seed) if raw_seed.lstrip("-").isdigit() else blueprint.seed
    service = PaperService(session)
    try:
        if action == "commit":
            allow_partial = form.get("allow_partial") is not None
            paper, result = service.assemble(
                course.id, blueprint, seed=seed, allow_partial=allow_partial
            )
            note = f"已生成《{paper.title}》：{result.question_count} 道、{result.total_score:g} 分"
            if not result.ok:
                note += f"（接受了 {sum(s.missing for s in result.shortages)} 道缺额）"
            return redirect_with(f"/papers/{paper.id}", note, "ok" if result.ok else "warn")
        result = service.selection.select(course.id, blueprint, seed=seed)
    except ShortageError as exc:
        result = exc.result
    except BlueprintError as exc:
        # 章节或标签名写错了：当成表单错误回显，而不是抛 500——这是用户的笔误
        bad_blueprint = FormError()
        bad_blueprint.add(str(exc))
        return render(
            request,
            "paper_assemble.html",
            _assemble_context(session, course, [], form=form, errors=bad_blueprint),
            status_code=400,
        )

    return render(
        request,
        "paper_preview.html",
        {
            "nav": build_nav(session, course_id=course.id),
            "active": "papers",
            "page_title": f"组卷预览 · {blueprint.title}",
            "course": course,
            "blueprint": blueprint,
            "blueprint_yaml": _blueprint_yaml(blueprint),
            "result": result,
            "picked": build_picked_sections(result, _options()),
        },
    )


# ================================================================= 试卷详情
@router.get("/papers/{paper_id}", response_class=HTMLResponse, response_model=None)
def paper_detail(
    request: Request,
    paper_id: str,
    msg: str = "",
    level: str = "ok",
    answers: bool = True,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        paper = PaperRepo(session).get_detailed(paper_id, include_deleted=True)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return render(
        request,
        "paper_detail.html",
        {
            "nav": build_nav(session, course_id=paper.course_id),
            "active": "papers",
            "page_title": paper.title,
            "paper": paper,
            "view": build_paper_view(paper, _options(), with_answers=answers),
            "show_answers": answers,
            "flashes": flashes(msg, level),
        },
    )


@router.post("/papers/{paper_id}/update")
def paper_update(
    paper_id: str,
    title: str = Form(""),
    meta_duration: str = Form(""),
    meta_class: str = Form(""),
    meta_author: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    meta = {
        key: value
        for key, value in (
            ("考试时长", meta_duration.strip()),
            ("适用班级", meta_class.strip()),
            ("命题人", meta_author.strip()),
        )
        if value
    }
    try:
        PaperService(session).update_paper(paper_id, title=title, meta=meta)
    except (ValueError, NotFoundError) as exc:
        return redirect_with(f"/papers/{paper_id}", str(exc), "error")
    return redirect_with(f"/papers/{paper_id}", "已保存")


@router.post("/papers/{paper_id}/sections/add")
def section_add(
    paper_id: str,
    title: str = Form(""),
    instructions: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    if not title.strip():
        return redirect_with(f"/papers/{paper_id}", "大题标题不能为空", "error")
    PaperService(session).add_section(paper_id, title.strip(), instructions=instructions.strip())
    return redirect_with(f"/papers/{paper_id}", f"已添加大题「{title.strip()}」")


@router.post("/papers/{paper_id}/sections/{section_id}/update")
def section_update(
    paper_id: str,
    section_id: str,
    title: str = Form(""),
    instructions: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        PaperService(session).update_section(section_id, title=title, instructions=instructions)
    except (ValueError, NotFoundError) as exc:
        return redirect_with(f"/papers/{paper_id}", str(exc), "error")
    return redirect_with(f"/papers/{paper_id}", "已保存")


@router.post("/papers/{paper_id}/sections/{section_id}/remove")
def section_remove(
    paper_id: str,
    section_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        section = PaperRepo(session).get_section(section_id)
        count = len(section.items)
        PaperService(session).remove_section(section_id)
    except NotFoundError as exc:
        return redirect_with(f"/papers/{paper_id}", str(exc), "error")
    note = f"已删除大题「{section.title}」"
    if count:
        note += f"，其中 {count} 道题已释放，可重新用于组卷"
    return redirect_with(f"/papers/{paper_id}", note)


@router.post("/papers/{paper_id}/items/{item_id}/remove")
def item_remove(
    paper_id: str,
    item_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        PaperService(session).remove_item(item_id)
    except NotFoundError as exc:
        return redirect_with(f"/papers/{paper_id}", str(exc), "error")
    return redirect_with(f"/papers/{paper_id}", "已移出试卷，这道题可重新用于组卷")


@router.post("/papers/{paper_id}/items/{item_id}/move")
def item_move(
    paper_id: str,
    item_id: str,
    offset: int = Form(0),
    section_id: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    service = PaperService(session)
    try:
        if section_id:
            service.move_item_to_section(item_id, section_id)
        else:
            service.move_item(item_id, offset)
    except (ValueError, NotFoundError) as exc:
        return redirect_with(f"/papers/{paper_id}", str(exc), "error")
    return redirect_with(f"/papers/{paper_id}")


@router.post("/papers/{paper_id}/items/{item_id}/score")
def item_score(
    paper_id: str,
    item_id: str,
    score: float = Form(0.0),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        PaperService(session).set_item_score(item_id, score)
    except (ValueError, NotFoundError) as exc:
        return redirect_with(f"/papers/{paper_id}", str(exc), "error")
    return redirect_with(f"/papers/{paper_id}")


@router.post("/papers/{paper_id}/scores")
def section_scores(
    paper_id: str,
    section_id: str = Form(""),
    score: float = Form(0.0),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """把一个大题下的所有题统一改成同一个分值。

    逐题填分值在二十道选择题上是纯粹的体力活，而「每题 2 分」是这类大题的常态。
    """
    service = PaperService(session)
    try:
        section = PaperRepo(session).get_section(section_id)
        for item in section.items:
            service.set_item_score(item.id, score)
    except (ValueError, NotFoundError) as exc:
        return redirect_with(f"/papers/{paper_id}", str(exc), "error")
    return redirect_with(f"/papers/{paper_id}", f"「{section.title}」每题已设为 {score:g} 分")


@router.post("/papers/{paper_id}/variant")
def paper_variant(
    paper_id: str,
    title: str = Form(""),
    seed: str = Form(""),
    allow_overlap: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """照同一份蓝图再出一套（A/B 卷）。"""
    raw = seed.strip()
    service = PaperService(session)
    try:
        paper, result = service.variant(
            paper_id,
            title=title.strip(),
            seed=int(raw) if raw.lstrip("-").isdigit() else None,
            avoid_overlap=not allow_overlap,
        )
    except ShortageError as exc:
        missing = sum(shortage.missing for shortage in exc.result.shortages)
        return redirect_with(
            f"/papers/{paper_id}",
            f"没能出 B 卷：避开原卷用过的题之后还缺 {missing} 道。"
            "可以勾「允许与原卷重题」，或先补题。",
            "error",
        )
    except (ValueError, NotFoundError) as exc:
        return redirect_with(f"/papers/{paper_id}", str(exc), "error")
    return redirect_with(
        f"/papers/{paper.id}",
        f"已生成《{paper.title}》，与原卷不重题，种子 {result.seed}",
    )


@router.post("/papers/{paper_id}/delete")
def paper_delete(
    paper_id: str,
    purge: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        paper = PaperRepo(session).get(paper_id, include_deleted=True)
        course_id, title = paper.course_id, paper.title
        service = PaperService(session)
        if purge:
            service.purge(paper_id)
        else:
            service.delete(paper_id)
    except NotFoundError as exc:
        return redirect_with("/papers", str(exc), "error")
    if purge:
        return redirect_with("/papers", f"已彻底删除《{title}》", course_id=course_id)
    return redirect_with(
        "/papers",
        f"《{title}》已移入回收站，它用过的题目重新可用于组卷",
        course_id=course_id,
    )


@router.post("/papers/{paper_id}/restore")
def paper_restore(
    paper_id: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        PaperService(session).restore(paper_id)
    except NotFoundError as exc:
        return redirect_with("/papers", str(exc), "error")
    return redirect_with(f"/papers/{paper_id}", "已恢复")


# ============================================== 从题目列表加题（手工选题）
@router.post("/papers/{paper_id}/add")
def paper_add_questions(
    paper_id: str,
    section_id: str = Form(""),
    question_id: list[str] = Form(default=[]),
    score: str = Form(""),
    back: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """把题目列表里勾选的题加进试卷。

    加完回到原来的列表页（``back``），而不是跳到试卷——老师的动作是「一边翻
    题库一边挑」，每加一次就被弹走会让这件事变得很难做。
    """
    target = back or f"/papers/{paper_id}"
    if not question_id:
        return redirect_with(target, "一道题都没勾选", "warn")
    try:
        paper = PaperRepo(session).get_detailed(paper_id)
    except NotFoundError as exc:
        return redirect_with(target, str(exc), "error")
    if not paper.sections:
        return redirect_with(target, "这份试卷还没有大题，先去试卷页加一个", "error")
    chosen = section_id or paper.sections[0].id
    raw_score = score.strip()
    try:
        outcome = PaperService(session).add_questions(
            paper.id,
            chosen,
            question_id,
            score=float(raw_score) if raw_score else None,
        )
    except (ValueError, NotFoundError) as exc:
        return redirect_with(target, str(exc), "error")
    level = "ok" if outcome.added else "warn"
    return redirect_with(target, f"《{paper.title}》：{outcome.summary}", level)
