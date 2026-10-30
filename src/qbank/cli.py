"""qbank 命令行。

CLI 不只是给人用的——它同时是集成测试的驱动入口。M3 的图形界面上线之前，
全链路（建课 → 录题 → 检索 → 组卷 → 导出）都靠它跑通；上线之后它依然是
批处理与脚本化的入口。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from qbank import __version__
from qbank.app import AppContext, open_bank
from qbank.core.enums import ConflictPolicy, Difficulty, QuestionStatus, QuestionType
from qbank.core.repo.base import ConflictError, NotFoundError
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.question import QuestionService
from qbank.doctor import Level, Status, run_checks

app = typer.Typer(
    name="qbank",
    help="题库管理系统：多课程题目管理、批量导入、自动组卷与多格式导出。",
    no_args_is_help=True,
    add_completion=False,
)
course_app = typer.Typer(help="课程管理", no_args_is_help=True)
chapter_app = typer.Typer(help="章节管理", no_args_is_help=True)
tag_app = typer.Typer(help="标签管理", no_args_is_help=True)
question_app = typer.Typer(help="题目管理", no_args_is_help=True)
app.add_typer(course_app, name="course")
app.add_typer(chapter_app, name="chapter")
app.add_typer(tag_app, name="tag")
app.add_typer(question_app, name="question")

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
    """删除课程，**连带其全部章节与题目**。不可撤销。"""
    context = _open()
    try:
        course_id = _resolve_course(context, name)
        with context.session() as session:
            repo = CourseRepo(session)
            course = repo.get(course_id)
            count = QuestionRepo(session).count(
                QuestionFilter(course_id=course_id, top_level_only=False)
            )
            if not yes:
                typer.secho(
                    f"将删除课程「{course.name}」及其 {count} 道题目，不可撤销。",
                    fg=typer.colors.YELLOW,
                )
                typer.secho("建议先执行备份导出。", fg=typer.colors.YELLOW)
                typer.confirm("确定继续？", abort=True)
            repo.delete(course_id)
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


if __name__ == "__main__":  # pragma: no cover
    app()
