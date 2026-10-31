"""qbank 命令行。

CLI 不只是给人用的——它同时是集成测试的驱动入口。图形界面上线之前，全链路
（建课 → 录题 → 检索 → 组卷）都靠它跑通；上线之后它依然是批处理与脚本化的
入口：组卷蓝图是文件，`qbank paper assemble` 可以直接进脚本与 CI。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError

from qbank import __version__
from qbank.app import AppContext, open_bank
from qbank.core.blueprint import Blueprint
from qbank.core.content.text import to_plain_text
from qbank.core.enums import ConflictPolicy, Difficulty, QuestionStatus, QuestionType
from qbank.core.repo.base import ConflictError, NotFoundError
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.asset import AssetService
from qbank.core.services.paper import (
    PaperService,
    ShortageError,
    display_answer,
    display_options,
    thaw,
)
from qbank.core.services.question import QuestionService
from qbank.core.services.selection import BlueprintError, SelectionResult
from qbank.doctor import Level, Status, run_checks
from qbank.exchange.import_service import ImportService, plan_file
from qbank.exchange.importers.loader import SUPPORTED_SUFFIXES, UnsupportedSourceError
from qbank.exchange.pandoc import PandocConversionError, PandocUnavailableError

app = typer.Typer(
    name="qbank",
    help="题库管理系统：多课程题目管理、批量导入、自动组卷。",
    no_args_is_help=True,
    add_completion=False,
)
course_app = typer.Typer(help="课程管理", no_args_is_help=True)
chapter_app = typer.Typer(help="章节管理", no_args_is_help=True)
tag_app = typer.Typer(help="标签管理", no_args_is_help=True)
question_app = typer.Typer(help="题目管理", no_args_is_help=True)
asset_app = typer.Typer(help="图片资源管理", no_args_is_help=True)
batch_app = typer.Typer(help="导入批次", no_args_is_help=True)
paper_app = typer.Typer(help="试卷与组卷", no_args_is_help=True)
app.add_typer(course_app, name="course")
app.add_typer(chapter_app, name="chapter")
app.add_typer(tag_app, name="tag")
app.add_typer(question_app, name="question")
app.add_typer(asset_app, name="asset")
app.add_typer(batch_app, name="batch")
app.add_typer(paper_app, name="paper")

_STATUS_MARK = {Status.OK: "✓", Status.DEGRADED: "!", Status.MISSING: "✗"}
_STATUS_COLOR = {
    Status.OK: typer.colors.GREEN,
    Status.DEGRADED: typer.colors.YELLOW,
    Status.MISSING: typer.colors.RED,
}


def _open() -> AppContext:
    """打开题库。失败时给出可读错误并退出，而不是抛栈。"""
    try:
        return open_bank()
    except Exception as exc:  # 启动期的任何问题都应变成一句人话
        typer.secho(f"无法打开题库：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _resolve_course(context: AppContext, name_or_id: str) -> str:
    """课程名或 id 都接受——命令行里让人敲 UUID 是不友好的。"""
    with context.session() as session:
        repo = CourseRepo(session)
        found = repo.find_by_name(name_or_id)
        if found is not None:
            return found.id
        try:
            return repo.get(name_or_id).id
        except NotFoundError:
            available = "、".join(c.name for c in repo.list_all()) or "（还没有课程）"
            typer.secho(
                f"找不到课程「{name_or_id}」。现有课程：{available}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None


def _load_drafts(path: Path) -> list[QuestionDraft]:
    """从 YAML/JSON 读题目草稿。支持单个对象或数组。"""
    raw_text = path.read_text(encoding="utf-8")
    data: Any = json.loads(raw_text) if path.suffix.lower() == ".json" else yaml.safe_load(raw_text)
    items = data if isinstance(data, list) else [data]
    drafts: list[QuestionDraft] = []
    for index, item in enumerate(items, start=1):
        try:
            drafts.append(QuestionDraft.model_validate(item))
        except Exception as exc:
            typer.secho(f"第 {index} 条校验失败：{exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    return drafts


# ===================================================================== 顶层
@app.command()
def version() -> None:
    """打印版本号。"""
    typer.echo(f"qbank {__version__}")


@app.command()
def doctor() -> None:
    """环境自检：存储、中文检索、pandoc、PDF、Web 各项能力。

    必需项缺失时退出码为 1；可选项缺失只提示降级路径，退出码仍为 0，
    以便 CI 与未装可选依赖的用户都能正常通过。
    """
    blocking: list[str] = []
    degraded: list[str] = []
    for check in run_checks():
        tag = "必需" if check.level is Level.REQUIRED else "可选"
        typer.secho(
            f"{_STATUS_MARK[check.status]} [{tag}] {check.name}: {check.detail}",
            fg=_STATUS_COLOR[check.status],
        )
        if check.remedy:
            typer.secho(f"    → {check.remedy}", fg=typer.colors.BRIGHT_BLACK)
        if check.is_blocking:
            blocking.append(check.name)
        elif check.status is not Status.OK:
            degraded.append(check.name)

    typer.echo("")
    if blocking:
        typer.secho(f"存在阻断性问题：{'、'.join(blocking)}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)
    if degraded:
        typer.secho(
            f"核心功能可用；以下能力处于降级状态：{'、'.join(degraded)}",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.secho("全部能力可用。", fg=typer.colors.GREEN, bold=True)


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="监听地址")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="启动后自动打开浏览器")
    ] = True,
    reload: Annotated[bool, typer.Option("--reload", help="改代码自动重启（开发用）")] = False,
) -> None:
    """启动图形界面。

    默认只监听 ``127.0.0.1``——这是本地客户端，不该一启动就暴露在局域网里。
    真要让同事访问，显式写 ``--host 0.0.0.0``，并且清楚当前还没有任何鉴权。
    """
    try:
        from qbank.web.app import serve as run_server
    except ImportError as exc:
        typer.secho('缺少 Web 依赖。安装：pip install "qbank[web]"', fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    if host == "0.0.0.0":
        typer.secho(
            "正在监听所有网卡：局域网内任何人都能访问这个题库，而且目前没有鉴权。",
            fg=typer.colors.YELLOW,
        )
    typer.secho(f"界面地址：{url}", fg=typer.colors.GREEN, bold=True)
    typer.secho("按 Ctrl+C 停止。", fg=typer.colors.BRIGHT_BLACK)
    if open_browser and not reload:
        # reload 模式下进程会重启，自动开浏览器会开好几个
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    run_server(host=host, port=port, reload=reload)


@app.command()
def info() -> None:
    """显示题库位置与规模。"""
    context = _open()
    try:
        with context.session() as session:
            courses = CourseRepo(session).list_all()
            questions = QuestionRepo(session)
            service = QuestionService(session)
            typer.echo(f"题库目录：{context.paths.root}")
            typer.echo(f"数据库：  {context.paths.db_file}")
            typer.echo(f"课程：    {len(courses)} 门")
            total = questions.count(QuestionFilter())
            typer.echo(f"题目：    {total} 道（不含子题）")
            typer.echo(f"索引：    {service.search.indexed_count()} 条")
            trash = len(questions.list_trash())
            if trash:
                typer.secho(f"回收站：  {trash} 道", fg=typer.colors.YELLOW)
            for course in courses:
                count = questions.count(QuestionFilter(course_id=course.id))
                typer.echo(f"  · {course.name}（{count} 道）")
    finally:
        context.close()


@app.command()
def reindex() -> None:
    """重建全文索引。改了分词逻辑或从备份恢复后跑一次。"""
    context = _open()
    try:
        with context.session() as session:
            count = QuestionService(session).reindex_all()
        typer.secho(f"已重建 {count} 条索引。", fg=typer.colors.GREEN)
    finally:
        context.close()


# ===================================================================== 课程
@course_app.command("add")
def course_add(
    name: Annotated[str, typer.Argument(help="课程名")],
    code: Annotated[str, typer.Option("--code", help="课程代码，如 CS201")] = "",
) -> None:
    """新建课程。"""
    context = _open()
    try:
        with context.session() as session:
            try:
                course = CourseRepo(session).create(name, code=code)
            except (ConflictError, ValueError) as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc
            typer.secho(f"已创建课程「{course.name}」", fg=typer.colors.GREEN)
    finally:
        context.close()


@course_app.command("list")
def course_list() -> None:
    """列出全部课程。"""
    context = _open()
    try:
        with context.session() as session:
            courses = CourseRepo(session).list_all()
            if not courses:
                typer.echo("还没有课程。用 `qbank course add <课程名>` 新建。")
                return
            repo = QuestionRepo(session)
            for course in courses:
                count = repo.count(QuestionFilter(course_id=course.id))
                code = f" [{course.code}]" if course.code else ""
                typer.echo(f"{course.name}{code}  —  {count} 道题")
    finally:
        context.close()


@course_app.command("rm")
def course_remove(
    name: Annotated[str, typer.Argument(help="课程名或 id")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """删除课程，**连带其全部章节、题目与试卷**。不可撤销。"""
    context = _open()
    try:
        course_id = _resolve_course(context, name)
        with context.session() as session:
            repo = CourseRepo(session)
            course = repo.get(course_id)
            count = QuestionRepo(session).count(
                QuestionFilter(course_id=course_id, top_level_only=False)
            )
            # 试卷挂在课程上，会跟着一起没。只说「题目」会让人以为卷子还在
            papers = len(PaperRepo(session).list_all(course_id, include_deleted=True))
            if not yes:
                extra = f"、{papers} 份试卷" if papers else ""
                typer.secho(
                    f"将删除课程「{course.name}」及其 {count} 道题目{extra}，不可撤销。",
                    fg=typer.colors.YELLOW,
                )
                typer.secho("建议先执行备份导出。", fg=typer.colors.YELLOW)
                typer.confirm("确定继续？", abort=True)
            QuestionService(session).delete_course(course_id)
        typer.secho(f"已删除课程「{name}」。", fg=typer.colors.GREEN)
    finally:
        context.close()


# ===================================================================== 章节
@chapter_app.command("add")
def chapter_add(
    course: Annotated[str, typer.Argument(help="课程名或 id")],
    path: Annotated[str, typer.Argument(help="章节路径，如 '第3章 控制结构/3.2 循环结构'")],
) -> None:
    """按路径创建章节，缺失的层级自动补齐。"""
    context = _open()
    try:
        course_id = _resolve_course(context, course)
        with context.session() as session:
            chapter = ChapterRepo(session).ensure_path(course_id, path)
            if chapter is None:
                typer.secho("章节路径不能为空。", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            typer.secho(f"已创建章节「{chapter.path}」", fg=typer.colors.GREEN)
    finally:
        context.close()


@chapter_app.command("list")
def chapter_list(course: Annotated[str, typer.Argument(help="课程名或 id")]) -> None:
    """按树形列出章节及每节的题目数。"""
    context = _open()
    try:
        course_id = _resolve_course(context, course)
        with context.session() as session:
            chapters = ChapterRepo(session).list_tree(course_id)
            if not chapters:
                typer.echo("该课程还没有章节。")
                return
            repo = QuestionRepo(session)
            for chapter in chapters:
                depth = chapter.path.count("/")
                count = repo.count(QuestionFilter(course_id=course_id, chapter_ids=[chapter.id]))
                typer.echo(f"{'  ' * depth}{chapter.title}  ({count})")
    finally:
        context.close()


# ===================================================================== 标签
@tag_app.command("list")
def tag_list() -> None:
    """列出标签及使用次数。"""
    context = _open()
    try:
        with context.session() as session:
            repo = TagRepo(session)
            tags = repo.list_all()
            if not tags:
                typer.echo("还没有标签。标签会在录题时自动创建。")
                return
            counts = repo.usage_counts()
            for tag in tags:
                typer.echo(f"{tag.name:20} [{tag.category.label}]  {counts.get(tag.id, 0)} 道")
    finally:
        context.close()


@tag_app.command("merge")
def tag_merge(
    source: Annotated[str, typer.Argument(help="要合并掉的标签名")],
    target: Annotated[str, typer.Argument(help="合并到的标签名")],
) -> None:
    """把两个同义标签合并。

    用久了一定会出现「循环」「循环结构」「while循环」这类同义标签。
    """
    context = _open()
    try:
        with context.session() as session:
            repo = TagRepo(session)
            left, right = repo.find_by_name(source), repo.find_by_name(target)
            if left is None or right is None:
                missing = source if left is None else target
                typer.secho(f"找不到标签「{missing}」。", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            result = repo.merge(left.id, right.id)
        detail = f"{result.moved} 道改挂标签"
        if result.deduplicated:
            detail += f"，{result.deduplicated} 道原本两个标签都有、已去重"
        typer.secho(f"已把「{source}」合并进「{target}」：{detail}。", fg=typer.colors.GREEN)
    finally:
        context.close()


@tag_app.command("clean")
def tag_clean() -> None:
    """清理没有任何题目使用的标签。"""
    context = _open()
    try:
        with context.session() as session:
            removed = TagRepo(session).delete_unused()
        typer.secho(f"已清理 {removed} 个未使用的标签。", fg=typer.colors.GREEN)
    finally:
        context.close()


# ===================================================================== 题目
@question_app.command("add")
def question_add(
    course: Annotated[str, typer.Argument(help="课程名或 id")],
    file: Annotated[Path, typer.Argument(help="题目草稿文件（YAML 或 JSON）")],
    policy: Annotated[
        ConflictPolicy, typer.Option("--on-conflict", help="遇到已存在的题怎么处理")
    ] = ConflictPolicy.SKIP,
) -> None:
    """从 YAML/JSON 文件录入题目。

    批量导入 md/docx/html 请用 `qbank import`（见导入模板规范）。本命令面向
    脚本化与测试，吃的是最贴近内部结构的格式。
    """
    context = _open()
    try:
        course_id = _resolve_course(context, course)
        drafts = _load_drafts(file)
        created = updated = skipped = 0
        with context.session() as session:
            service = QuestionService(session)
            for draft in drafts:
                result = service.upsert(course_id, draft, policy=policy)
                created += result.created
                updated += result.updated
                skipped += result.skipped
        typer.secho(
            f"完成：新建 {created}，更新 {updated}，跳过 {skipped}。", fg=typer.colors.GREEN
        )
    finally:
        context.close()


@question_app.command("list")
def question_list(
    course: Annotated[str | None, typer.Option("--course", help="课程名或 id")] = None,
    query: Annotated[str, typer.Option("--search", "-s", help="全文检索")] = "",
    kind: Annotated[QuestionType | None, typer.Option("--type", help="题型")] = None,
    difficulty: Annotated[int | None, typer.Option("--difficulty", min=1, max=5)] = None,
    status: Annotated[QuestionStatus | None, typer.Option("--status")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="标签，可重复")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 20,
) -> None:
    """列出题目，可按课程、题型、难度、标签筛选并全文检索。"""
    context = _open()
    try:
        course_id = _resolve_course(context, course) if course else None
        with context.session() as session:
            service = QuestionService(session)
            criteria = QuestionFilter(
                course_id=course_id,
                types=[kind] if kind else (),
                difficulties=[Difficulty(difficulty)] if difficulty else (),
                statuses=[status] if status else (),
            )
            if tag:
                tags = TagRepo(session)
                resolved = [tags.find_by_name(name) for name in tag]
                unknown = [name for name, found in zip(tag, resolved, strict=True) if found is None]
                if unknown:
                    typer.secho(f"找不到标签：{'、'.join(unknown)}", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=1)
                criteria.all_tag_ids = [found.id for found in resolved if found is not None]

            ordered_ids: list[str] | None = None
            if query:
                # 用 search_ids 而不是 search.matching_ids：命中子题时要显示其父题
                ordered_ids = service.search_ids(query, limit=limit * 5)
                # 空列表意味着「检索无结果」，必须真的返回零条
                criteria.restrict_ids = ordered_ids

            page = service.questions.list_page(criteria, limit=limit, order_by_ids=ordered_ids)
            if not page.items:
                typer.echo("没有匹配的题目。")
                return
            for question in page.items:
                head = question.stem.replace("\n", " ")[:46]
                tags_text = "、".join(t.name for t in question.tags)
                typer.echo(
                    f"{question.id[:8]}  {question.type.label:6}  "
                    f"难度{int(question.difficulty)}  {question.score:>5.1f}分  {head}"
                    + (f"   #{tags_text}" if tags_text else "")
                )
            typer.secho(
                f"\n共 {page.total} 道，显示 {len(page.items)} 道。",
                fg=typer.colors.BRIGHT_BLACK,
            )
    finally:
        context.close()


@question_app.command("show")
def question_show(
    question_id: Annotated[str, typer.Argument(help="题目 id（可用前 8 位）")],
    as_yaml: Annotated[bool, typer.Option("--yaml", help="输出可直接再导入的 YAML")] = False,
) -> None:
    """显示一道题的完整内容。"""
    context = _open()
    try:
        with context.session() as session:
            service = QuestionService(session)
            question = _find_question(session, question_id)
            draft = service.to_draft(question)
            if as_yaml:
                typer.echo(
                    yaml.safe_dump(
                        draft.model_dump(mode="json"), allow_unicode=True, sort_keys=False
                    )
                )
                return
            typer.secho(
                f"[{draft.type.label}] 难度{int(draft.difficulty)} {draft.score}分", bold=True
            )
            if draft.chapter:
                typer.echo(f"章节：{draft.chapter}")
            if draft.tags:
                typer.echo(f"标签：{'、'.join(draft.tags)}")
            if draft.source:
                typer.echo(f"来源：{draft.source}")
            typer.echo(f"\n{draft.stem}\n")
            options = getattr(draft.payload, "options", None)
            if options:
                for option in options:
                    mark = "✓" if option.is_correct else " "
                    typer.echo(f"  {mark} {option.label}. {option.content}")
                typer.echo("")
            if draft.answer:
                typer.secho(f"答案：{draft.answer}", fg=typer.colors.GREEN)
            if draft.explanation:
                typer.echo(f"解析：{draft.explanation}")
            for index, sub in enumerate(draft.sub_questions, start=1):
                typer.secho(f"\n({index}) [{sub.score}分] {sub.stem}", bold=True)
                if sub.answer:
                    typer.secho(f"    答案：{sub.answer}", fg=typer.colors.GREEN)
    finally:
        context.close()


def _find_question(session: Any, prefix: str) -> Any:
    """支持用 id 前缀定位题目——让人敲完整 UUID 是不友好的。"""
    repo = QuestionRepo(session)
    try:
        return repo.get_detailed(prefix, include_deleted=True)
    except NotFoundError:
        pass
    matches = [
        question
        for question in repo.list_all(QuestionFilter(top_level_only=False, include_deleted=True))
        if question.id.startswith(prefix)
    ]
    if not matches:
        typer.secho(f"找不到题目：{prefix}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if len(matches) > 1:
        typer.secho(
            f"前缀 {prefix} 匹配到 {len(matches)} 道题，请用更长的前缀。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return repo.get_detailed(matches[0].id, include_deleted=True)


@question_app.command("rm")
def question_remove(
    question_id: Annotated[str, typer.Argument(help="题目 id（可用前 8 位）")],
) -> None:
    """移入回收站（可恢复）。"""
    context = _open()
    try:
        with context.session() as session:
            question = _find_question(session, question_id)
            QuestionService(session).delete(question.id)
        typer.secho("已移入回收站。用 `qbank question restore` 可恢复。", fg=typer.colors.GREEN)
    finally:
        context.close()


@question_app.command("restore")
def question_restore(
    question_id: Annotated[str, typer.Argument(help="题目 id（可用前 8 位）")],
) -> None:
    """从回收站恢复。"""
    context = _open()
    try:
        with context.session() as session:
            question = _find_question(session, question_id)
            QuestionService(session).restore(question.id)
        typer.secho("已恢复。", fg=typer.colors.GREEN)
    finally:
        context.close()


@question_app.command("trash")
def question_trash(
    course: Annotated[str | None, typer.Option("--course")] = None,
    purge: Annotated[bool, typer.Option("--purge", help="清空回收站（不可撤销）")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认，便于脚本化")] = False,
) -> None:
    """查看或清空回收站。"""
    context = _open()
    try:
        course_id = _resolve_course(context, course) if course else None
        with context.session() as session:
            repo = QuestionRepo(session)
            items = repo.list_trash(course_id)
            if not items:
                typer.echo("回收站是空的。")
                return
            for question in items:
                head = question.stem.replace("\n", " ")[:46]
                typer.echo(f"{question.id[:8]}  {head}")
            if purge:
                if not yes:
                    typer.confirm(f"将永久删除 {len(items)} 道题，不可撤销。继续？", abort=True)
                service = QuestionService(session)
                for question in list(items):
                    service.purge(question.id)
                typer.secho(f"已永久删除 {len(items)} 道题。", fg=typer.colors.GREEN)
    finally:
        context.close()


@question_app.command("history")
def question_history(
    question_id: Annotated[str, typer.Argument(help="题目 id（可用前 8 位）")],
    rollback_to: Annotated[int | None, typer.Option("--rollback-to", help="回滚到第 N 版")] = None,
) -> None:
    """查看修改历史，或回滚到某个版本。"""
    context = _open()
    try:
        with context.session() as session:
            question = _find_question(session, question_id)
            service = QuestionService(session)
            if rollback_to is not None:
                try:
                    service.rollback(question.id, rollback_to)
                except ValueError as exc:
                    typer.secho(str(exc), fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=1) from exc
                typer.secho(f"已回滚到第 {rollback_to} 版。", fg=typer.colors.GREEN)
                return
            revisions = service.revisions(question.id)
            typer.echo(f"当前第 {question.revision} 版")
            if not revisions:
                typer.echo("还没有修改历史。")
                return
            for revision in revisions:
                stamp = revision.changed_at.strftime("%Y-%m-%d %H:%M")
                typer.echo(f"  第 {revision.revision} 版  {stamp}  {revision.note}")
            typer.secho(
                "\n提示：「第 N 版」是那次改动**之前**的状态，用 --rollback-to N 可恢复到它。",
                fg=typer.colors.BRIGHT_BLACK,
            )
    finally:
        context.close()


@question_app.command("duplicates")
def question_duplicates(
    course: Annotated[str, typer.Argument(help="课程名或 id")],
    file: Annotated[Path, typer.Argument(help="待检查的题目草稿文件")],
) -> None:
    """检查一批草稿与库内题目是否重复。

    只报告，不做任何修改。精确重复可信；相似结果仅供参考——一字之差就可能
    把题意反过来（正确↔错误），必须人工确认。
    """
    context = _open()
    try:
        course_id = _resolve_course(context, course)
        drafts = _load_drafts(file)
        with context.session() as session:
            service = QuestionService(session)
            for index, draft in enumerate(drafts, start=1):
                hints = service.check_duplicates(course_id, draft)
                head = draft.stem.replace("\n", " ")[:40]
                if not hints:
                    typer.secho(f"{index}. 无重复  {head}", fg=typer.colors.GREEN)
                    continue
                typer.secho(f"{index}. {head}", fg=typer.colors.YELLOW)
                for hint in hints:
                    existing = service.questions.get(hint.question_id)
                    color = typer.colors.RED if hint.exact else typer.colors.YELLOW
                    typer.secho(
                        f"     {hint.label}（{hint.score:.0%}） {hint.question_id[:8]}  "
                        f"{existing.stem.replace(chr(10), ' ')[:36]}",
                        fg=color,
                    )
    finally:
        context.close()


# ===================================================================== 导入
@app.command("import")
def import_file(
    course: Annotated[str, typer.Argument(help="课程名或 id")],
    file: Annotated[Path, typer.Argument(help="要导入的文件（md / docx / html 等）")],
    policy: Annotated[
        ConflictPolicy, typer.Option("--on-conflict", help="遇到库里已有的同一道题怎么处理")
    ] = ConflictPolicy.SKIP,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只预览，不写入任何内容")] = False,
    only: Annotated[str, typer.Option("--only", help="只导入指定序号，如 1,3,5-8")] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认，便于脚本化")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="逐条显示")] = False,
) -> None:
    """按模板批量导入题目。

    流程是**先预览、后确认**：先解析、校验、查重并列出每条将发生什么，
    确认后才写入。不加 ``--yes`` 时一定会停下来问一次。

    模板格式见 docs/导入模板规范.md。
    """
    context = _open()
    try:
        course_id = _resolve_course(context, course)
        selected = _parse_selection(only)
        with context.session() as session:
            try:
                plan, document = plan_file(session, course_id, file)
            except (UnsupportedSourceError, FileNotFoundError) as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc
            except PandocUnavailableError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc
            except PandocConversionError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc

            try:
                _print_plan(plan, policy, verbose=verbose, selected=selected)
                if not plan.questions:
                    typer.secho("没有可导入的题目。", fg=typer.colors.YELLOW)
                    raise typer.Exit(code=1 if plan.errors else 0)
                if dry_run:
                    typer.secho("\n--dry-run：未写入任何内容。", fg=typer.colors.BRIGHT_BLACK)
                    return
                if not yes:
                    typer.confirm("\n确认导入？", abort=True)
                result = ImportService(session).commit(plan, policy=policy, selected=selected)
            finally:
                document.close()

        typer.secho(f"\n{result.summary()}", fg=typer.colors.GREEN, bold=True)
        for message in result.image_errors:
            typer.secho(f"  图片：{message}", fg=typer.colors.YELLOW)
        typer.secho(
            f"批次 {result.batch_id[:8]}；如需撤销：qbank batch rollback {result.batch_id[:8]}",
            fg=typer.colors.BRIGHT_BLACK,
        )
    finally:
        context.close()


def _parse_selection(text: str) -> list[int] | None:
    """解析 ``1,3,5-8`` 形式的序号选择。空串表示全选。"""
    if not text.strip():
        return None
    picked: dict[int, None] = {}
    for piece in text.replace("，", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            try:
                start, end = (int(part) for part in piece.split("-", 1))
            except ValueError:
                typer.secho(f"无法解析序号区间「{piece}」", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from None
            for number in range(start, end + 1):
                picked[number] = None
        else:
            try:
                picked[int(piece)] = None
            except ValueError:
                typer.secho(f"无法解析序号「{piece}」", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from None
    return list(picked)


def _print_plan(
    plan: Any, policy: ConflictPolicy, *, verbose: bool, selected: list[int] | None
) -> None:
    typer.secho(f"源文件：{plan.source_name}（{plan.source_format}）", bold=True)
    typer.echo(plan.summary(policy))

    if plan.errors:
        typer.secho("\n解析失败的题目：", fg=typer.colors.RED, bold=True)
        for error in plan.errors:
            typer.secho(f"  {error}", fg=typer.colors.RED)

    show_all = verbose or plan.exact_duplicate_count or plan.similar_count
    if not show_all:
        return
    typer.echo("")
    for item in plan.questions:
        skipped = selected is not None and item.index not in selected
        action = "未勾选" if skipped else item.action_under(policy)
        color = (
            typer.colors.BRIGHT_BLACK
            if skipped
            else typer.colors.YELLOW
            if item.exact_duplicate or item.similar
            else typer.colors.GREEN
        )
        typer.secho(
            f"  {item.index:>3}. [{action}] {item.draft.type.label}  {item.headline}",
            fg=color,
        )
        for hint in item.duplicates:
            mark = "═" if hint.exact else "~"
            typer.secho(
                f"       {mark} {hint.label}（{hint.score:.0%}） 已有题目 {hint.question_id[:8]}",
                fg=typer.colors.BRIGHT_BLACK,
            )
        if item.similar and not item.exact_duplicate:
            typer.secho(
                "       ↑ 相似仅供参考：一字之差就可能把题意反过来（正确↔错误），请人工确认",
                fg=typer.colors.BRIGHT_BLACK,
            )


@app.command("formats")
def formats() -> None:
    """列出支持导入的文件格式及其依赖状态。"""
    from qbank.exchange import pandoc as pandoc_module

    found = pandoc_module.capability()
    typer.echo("无需额外依赖：  " + "、".join(sorted(n for n in (".md", ".markdown", ".txt"))))
    others = sorted(SUPPORTED_SUFFIXES - {".md", ".markdown", ".txt"})
    if found.available:
        typer.secho(
            f"需要 pandoc（已就绪 {found.version}）：  " + "、".join(others),
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho("需要 pandoc（当前不可用）：  " + "、".join(others), fg=typer.colors.YELLOW)
        typer.secho(f"    {found.detail}", fg=typer.colors.BRIGHT_BLACK)
        typer.secho('    安装：pip install "pypandoc_binary"', fg=typer.colors.BRIGHT_BLACK)


# ===================================================================== 批次
@batch_app.command("list")
def batch_list(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
) -> None:
    """列出导入历史。"""
    from sqlalchemy import select

    from qbank.core.models import ImportBatch

    context = _open()
    try:
        with context.session() as session:
            batches = session.scalars(
                select(ImportBatch).order_by(ImportBatch.started_at.desc()).limit(limit)
            ).all()
            if not batches:
                typer.echo("还没有导入记录。")
                return
            for batch in batches:
                stamp = batch.started_at.strftime("%Y-%m-%d %H:%M")
                state = "成功" if batch.succeeded else "有失败"
                revertible = "可撤销" if batch.created_ids else "已撤销/无新建"
                typer.echo(
                    f"{batch.id[:8]}  {stamp}  {batch.source_name:28} "
                    f"新建{batch.created_count} 更新{batch.updated_count} "
                    f"跳过{batch.skipped_count} 失败{batch.failed_count}  {state}  {revertible}"
                )
    finally:
        context.close()


@batch_app.command("rollback")
def batch_rollback(
    batch_id: Annotated[str, typer.Argument(help="批次 id（可用前 8 位）")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """撤销一次导入：删掉该批**新建**的题目。

    被覆盖的题不会自动恢复——它们有各自的修改历史，应该用
    `qbank question history <id> --rollback-to N` 单独回滚。混在一起做会让
    「撤销导入」的后果变得不可预测。
    """
    from sqlalchemy import select

    from qbank.core.models import ImportBatch

    context = _open()
    try:
        with context.session() as session:
            batch = session.scalar(select(ImportBatch).where(ImportBatch.id.startswith(batch_id)))
            if batch is None:
                typer.secho(f"找不到批次：{batch_id}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            count = len(batch.created_ids)
            if not count:
                typer.secho("该批次没有可撤销的新建题目。", fg=typer.colors.YELLOW)
                return
            if not yes:
                typer.confirm(f"将永久删除本批新建的 {count} 道题。继续？", abort=True)
            removed = ImportService(session).rollback_batch(batch.id)
        typer.secho(f"已撤销，删除 {removed} 道题。", fg=typer.colors.GREEN)
    finally:
        context.close()


# ===================================================================== 图片
@asset_app.command("stats")
def asset_stats() -> None:
    """图片统计与孤儿检查。"""
    from sqlalchemy import func, select

    from qbank.core.models import Asset

    context = _open()
    try:
        with context.session() as session:
            total = session.scalar(select(func.count(Asset.id))) or 0
            size = session.scalar(select(func.sum(Asset.byte_size))) or 0
            assets = AssetService(session, context.paths)
            orphans = assets.orphans()
            typer.echo(f"图片目录：{context.paths.assets_dir}")
            typer.echo(f"共 {total} 张，占用 {size / 1024 / 1024:.1f} MB")
            missing = [
                asset
                for asset in session.scalars(select(Asset)).all()
                if assets.path_of(asset.sha256) is None
            ]
            if missing:
                typer.secho(
                    f"有 {len(missing)} 张图片在库里有记录但文件不见了（题目会显示「图片缺失」）",
                    fg=typer.colors.RED,
                )
            if orphans:
                freed = sum(asset.byte_size for asset in orphans)
                typer.secho(
                    f"孤儿图片 {len(orphans)} 张，占用 {freed / 1024 / 1024:.1f} MB；"
                    "用 `qbank asset clean` 清理",
                    fg=typer.colors.YELLOW,
                )
            else:
                typer.secho("没有孤儿图片。", fg=typer.colors.GREEN)
    finally:
        context.close()


@asset_app.command("clean")
def asset_clean(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """删除没有任何题目引用的图片。"""
    context = _open()
    try:
        with context.session() as session:
            assets = AssetService(session, context.paths)
            orphans = list(assets.orphans())
            if not orphans:
                typer.secho("没有孤儿图片。", fg=typer.colors.GREEN)
                return
            freed = sum(asset.byte_size for asset in orphans)
            if not yes:
                message = (
                    f"将删除 {len(orphans)} 张无人引用的图片"
                    f"（{freed / 1024 / 1024:.1f} MB）。继续？"
                )
                typer.confirm(message, abort=True)
            removed, released = assets.purge_orphans()
        typer.secho(
            f"已删除 {removed} 张，释放 {released / 1024 / 1024:.1f} MB。", fg=typer.colors.GREEN
        )
    finally:
        context.close()


if __name__ == "__main__":  # pragma: no cover
    app()


# ===================================================================== 组卷
def _find_paper(session: Any, prefix: str) -> Any:
    """支持用 id 前缀定位试卷。"""
    repo = PaperRepo(session)
    try:
        return repo.get_detailed(prefix, include_deleted=True)
    except NotFoundError:
        pass
    matches = [
        paper for paper in repo.list_all(include_deleted=True) if paper.id.startswith(prefix)
    ]
    if not matches:
        typer.secho(f"找不到试卷：{prefix}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if len(matches) > 1:
        typer.secho(
            f"前缀 {prefix} 匹配到 {len(matches)} 份试卷，请用更长的前缀。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return repo.get_detailed(matches[0].id, include_deleted=True)


def _load_blueprint(path: Path) -> Blueprint:
    """从 YAML/JSON 读蓝图。校验失败时逐条列出，而不是抛一堆 pydantic 栈。"""
    if not path.exists():
        typer.secho(f"找不到蓝图文件：{path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    raw_text = path.read_text(encoding="utf-8")
    data = json.loads(raw_text) if path.suffix.lower() == ".json" else yaml.safe_load(raw_text)
    try:
        return Blueprint.model_validate(data)
    except ValidationError as exc:
        reported = _useful_errors(exc)
        typer.secho(f"蓝图 {path.name} 有 {len(reported)} 处问题：", fg=typer.colors.RED, err=True)
        for location, message in reported:
            typer.secho(f"  {location}：{message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _useful_errors(exc: ValidationError) -> list[tuple[str, str]]:
    """挑出值得显示的校验错误。

    丢掉「上层的连带报错」：某一节里写错一个字段名时，pydantic 会先报那个字段，
    再报一句「sections 至少要有 1 项」——因为那一节整体没通过校验。后者会把人
    引向错误的方向（「我明明写了一节」），所以凡是路径为另一条错误的前缀的，
    一律不显示。
    """
    errors = exc.errors()
    paths = [tuple(str(part) for part in error["loc"]) for error in errors]
    kept: list[tuple[str, str]] = []
    for index, error in enumerate(errors):
        here = paths[index]
        if any(other != here and other[: len(here)] == here for other in paths):
            continue
        kept.append((".".join(here) or "(顶层)", str(error["msg"])))
    return kept


def _print_selection(result: SelectionResult) -> None:
    """抽题结果与诊断。组不齐时用黄色，一眼能看出这份卷还不能用。"""
    color = typer.colors.GREEN if result.ok else typer.colors.YELLOW
    typer.secho(result.report(), fg=None if result.ok else color)
    if result.zero_score_count:
        typer.secho(
            f"注意：有 {result.zero_score_count} 道题配分为 0，"
            "多半是蓝图没写 score_each 而题目自身也没分值。",
            fg=typer.colors.YELLOW,
        )


@paper_app.command("assemble")
def paper_assemble(
    course: Annotated[str, typer.Argument(help="课程名或 id")],
    blueprint_file: Annotated[Path, typer.Argument(help="蓝图 YAML/JSON 文件")],
    seed: Annotated[int | None, typer.Option("--seed", help="随机种子；覆盖蓝图里的")] = None,
    title: Annotated[str, typer.Option("--title", help="试卷标题；覆盖蓝图里的")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只看抽题结果，不生成试卷")] = False,
    allow_partial: Annotated[
        bool, typer.Option("--allow-partial", help="组不齐时也按缺额生成")
    ] = False,
) -> None:
    """按蓝图自动组卷。

    **先 `--dry-run` 看一眼**：组不齐时会告诉你缺哪一类题、放宽哪个条件能补上
    几道，而不是默默少出几道。确认无误后去掉 `--dry-run`。

    同一个库 + 同一份蓝图 + 同一个 `--seed` 必然给出同一份卷子，所以
    `--dry-run` 看到的就是接下来会生成的那一份。
    """
    blueprint = _load_blueprint(blueprint_file)
    if title:
        blueprint = blueprint.model_copy(update={"title": title})
    context = _open()
    try:
        with context.session() as session:
            course_id = _resolve_course(context, course)
            service = PaperService(session)
            if dry_run:
                try:
                    result = service.selection.select(course_id, blueprint, seed=seed)
                except BlueprintError as exc:
                    typer.secho(str(exc), fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=1) from exc
                _print_selection(result)
                typer.echo("\n（--dry-run：没有生成试卷）")
                if not result.ok:
                    raise typer.Exit(code=1)
                return
            try:
                paper, result = service.assemble(
                    course_id, blueprint, seed=seed, allow_partial=allow_partial
                )
            except ShortageError as exc:
                _print_selection(exc.result)
                typer.secho(
                    "\n没有生成试卷。改蓝图后重试，或加 --allow-partial 接受缺额。",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1) from exc
            except BlueprintError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc
            paper_id, paper_title = paper.id, paper.title
            _print_selection(result)
        typer.secho(
            f"\n已生成《{paper_title}》（{paper_id[:8]}），种子 {result.seed}",
            fg=typer.colors.GREEN,
        )
        # 不要在这里教人「重跑同一条命令即可复现」——那是错的：这份卷用掉的题
        # 已记入使用记录，avoid_used_within_days 会在下一次组卷时把它们挡掉。
        # 卷子的权威副本是卷子本身（题面已冻结），不是「重跑一遍蓝图」。
        if blueprint.avoid_used_within_days:
            typer.echo(
                f"这 {result.question_count} 道题已记入使用记录，"
                f"今后 {blueprint.avoid_used_within_days} 天内的组卷会避开它们。"
            )
        typer.echo(f"查看：qbank paper show {paper_id[:8]}")
        typer.echo(f"再出一套不重题的：qbank paper variant {paper_id[:8]}")
    finally:
        context.close()


@paper_app.command("list")
def paper_list(
    course: Annotated[str, typer.Option("--course", "-c", help="只看某门课")] = "",
) -> None:
    """列出试卷。"""
    context = _open()
    try:
        with context.session() as session:
            course_id = _resolve_course(context, course) if course else None
            repo = PaperRepo(session)
            papers = repo.list_all(course_id)
            if not papers:
                typer.echo("还没有试卷。用 `qbank paper assemble` 组一份。")
                return
            summaries = repo.summaries(course_id)
            for paper in papers:
                count, score = summaries.get(paper.id, (0, 0.0))
                stamp = paper.created_at.strftime("%Y-%m-%d %H:%M")
                seed = f"seed={paper.seed}" if paper.seed is not None else "手工组卷"
                typer.echo(
                    f"{paper.id[:8]}  {stamp}  {paper.title:24} "
                    f"{count:>3} 道  {score:>6g} 分  {seed}"
                )
    finally:
        context.close()


@paper_app.command("show")
def paper_show(
    paper_id: Annotated[str, typer.Argument(help="试卷 id（可用前 8 位）")],
    answers: Annotated[bool, typer.Option("--answers", help="同时显示参考答案")] = False,
) -> None:
    """显示一份试卷。

    显示的是**成卷时冻结的题面**，不是题库里的当前版本——这正是试卷快照的用途。
    """
    context = _open()
    try:
        with context.session() as session:
            paper = _find_paper(session, paper_id)
            # 标题不加书名号：卷名本身常带《课程名》，再套一层就成了套娃
            typer.secho(paper.title, bold=True)
            details = [f"{paper.total_score:g} 分"]
            if paper.seed is not None:
                details.append(f"seed={paper.seed}")
            if paper.deleted_at is not None:
                details.append("已在回收站")
            typer.echo("　".join(details))
            for key, value in paper.meta.items():
                typer.echo(f"{key}：{value}")
            for section in paper.sections:
                typer.secho(f"\n{section.title}", bold=True)
                # 大题标题里通常已经写了「共 20 分」，所以这里报的是**实际**的
                # 题数与配分——两者对不上正是要看出来的东西
                typer.secho(
                    f"  实际 {len(section.items)} 道、{section.total_score:g} 分",
                    fg=typer.colors.BRIGHT_BLACK,
                )
                if section.instructions:
                    typer.echo(f"  {section.instructions}")
                for index, item in enumerate(section.items, start=1):
                    draft = thaw(item.snapshot)
                    typer.echo(f"\n{index}. ({item.score:g} 分) {to_plain_text(draft.stem)}")
                    for option in display_options(draft, item.option_order):
                        mark = "✓" if answers and option.is_correct else " "
                        typer.echo(f"   {mark} {option.label}. {to_plain_text(option.content)}")
                    if answers:
                        shown = display_answer(draft, item.option_order)
                        if shown:
                            typer.secho(f"   答案：{to_plain_text(shown)}", fg=typer.colors.GREEN)
    finally:
        context.close()


@paper_app.command("new")
def paper_new(
    course: Annotated[str, typer.Argument(help="课程名或 id")],
    title: Annotated[str, typer.Argument(help="试卷标题")],
    section: Annotated[
        list[str] | None, typer.Option("--section", "-s", help="大题标题，可多次指定")
    ] = None,
) -> None:
    """建一份空白卷，之后用 `qbank paper add` 逐道加题。"""
    context = _open()
    try:
        course_id = _resolve_course(context, course)
        with context.session() as session:
            paper = PaperService(session).create_empty(
                course_id, title, sections=section or ["一、试题"]
            )
            paper_id = paper.id
        typer.secho(f"已建《{title}》（{paper_id[:8]}）", fg=typer.colors.GREEN)
    finally:
        context.close()


@paper_app.command("add")
def paper_add(
    paper_id: Annotated[str, typer.Argument(help="试卷 id（可用前 8 位）")],
    question_ids: Annotated[list[str], typer.Argument(help="题目 id（可用前 8 位），可多个")],
    section: Annotated[int, typer.Option("--section", "-s", help="第几个大题，从 1 开始")] = 1,
    score: Annotated[
        float | None, typer.Option("--score", help="每题分值；留空用题目自带的")
    ] = None,
) -> None:
    """把指定题目加进试卷。"""
    context = _open()
    try:
        with context.session() as session:
            paper = _find_paper(session, paper_id)
            if not paper.sections:
                typer.secho(
                    "这份试卷还没有大题，先用 `qbank paper section add`。",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            if not 1 <= section <= len(paper.sections):
                typer.secho(
                    f"试卷只有 {len(paper.sections)} 个大题，"
                    f"--section 应在 1..{len(paper.sections)}",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            resolved = [_find_question(session, prefix).id for prefix in question_ids]
            outcome = PaperService(session).add_questions(
                paper.id, paper.sections[section - 1].id, resolved, score=score
            )
            summary = outcome.summary
        typer.secho(summary, fg=typer.colors.GREEN)
    finally:
        context.close()


@paper_app.command("variant")
def paper_variant(
    paper_id: Annotated[str, typer.Argument(help="原试卷 id（可用前 8 位）")],
    seed: Annotated[int | None, typer.Option("--seed", help="新种子；留空则现取一个")] = None,
    title: Annotated[str, typer.Option("--title", help="新试卷标题")] = "",
    overlap: Annotated[
        bool, typer.Option("--allow-overlap/--no-overlap", help="是否允许与原卷重题")
    ] = False,
) -> None:
    """照同一份蓝图再出一套（A/B 卷）。默认避开原卷用过的题。"""
    context = _open()
    try:
        with context.session() as session:
            source = _find_paper(session, paper_id)
            try:
                paper, result = PaperService(session).variant(
                    source.id, title=title, seed=seed, avoid_overlap=not overlap
                )
            except ShortageError as exc:
                _print_selection(exc.result)
                typer.secho(
                    "\n没有生成 B 卷：避开原卷用过的题之后库里不够了。"
                    "可以加 --allow-overlap 允许重题，或先补题。",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1) from exc
            except ValueError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc
            new_id, new_title = paper.id, paper.title
            _print_selection(result)
        typer.secho(f"\n已生成《{new_title}》（{new_id[:8]}）", fg=typer.colors.GREEN)
    finally:
        context.close()


@paper_app.command("rm")
def paper_remove(
    paper_id: Annotated[str, typer.Argument(help="试卷 id（可用前 8 位）")],
    purge: Annotated[bool, typer.Option("--purge", help="彻底删除而不是放进回收站")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """删除试卷。默认进回收站，可恢复。"""
    context = _open()
    try:
        with context.session() as session:
            paper = _find_paper(session, paper_id)
            title = paper.title
            if purge and not yes:
                typer.confirm(f"将彻底删除《{title}》，不可恢复。继续？", abort=True)
            service = PaperService(session)
            if purge:
                service.purge(paper.id)
            else:
                service.delete(paper.id)
        if purge:
            typer.secho(f"已彻底删除《{title}》。", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"《{title}》已移入回收站。它用过的题目重新可用于组卷。",
                fg=typer.colors.GREEN,
            )
    finally:
        context.close()
