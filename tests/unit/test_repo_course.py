"""课程与章节仓储测试。

重点在章节树的物化路径：``path`` 是组卷「按章节范围抽题」的基础，重命名和
移动节点时必须重写整棵子树，漏了就会出现「子章节的题目搜不到/抽不到」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qbank.core.models import PATH_SEP
from qbank.core.repo.base import ConflictError, NotFoundError
from qbank.core.repo.course import ChapterRepo, CourseRepo


@pytest.fixture
def courses(session: Session) -> CourseRepo:
    return CourseRepo(session)


@pytest.fixture
def chapters(session: Session) -> ChapterRepo:
    return ChapterRepo(session)


# ------------------------------------------------------------------ 课程
def test_create_and_get(courses: CourseRepo) -> None:
    created = courses.create("数据结构", code="CS201")
    assert courses.get(created.id).name == "数据结构"
    assert courses.find_by_name("数据结构") is not None


def test_duplicate_course_name_rejected(courses: CourseRepo) -> None:
    courses.create("数据结构")
    with pytest.raises(ConflictError, match="已存在"):
        courses.create("数据结构")


def test_course_name_is_trimmed(courses: CourseRepo) -> None:
    assert courses.create("  数据结构  ").name == "数据结构"


def test_blank_course_name_rejected(courses: CourseRepo) -> None:
    with pytest.raises(ValueError, match="不能为空"):
        courses.create("   ")


def test_get_or_create_is_idempotent(courses: CourseRepo) -> None:
    first = courses.get_or_create("操作系统")
    second = courses.get_or_create("操作系统")
    assert first.id == second.id
    assert len(courses.list_all()) == 1


def test_missing_course_raises_with_readable_message(courses: CourseRepo) -> None:
    with pytest.raises(NotFoundError, match="找不到课程"):
        courses.get("no-such-id")


# ------------------------------------------------------------------ 章节树
@pytest.fixture
def tree(courses: CourseRepo, chapters: ChapterRepo) -> tuple[str, dict[str, str]]:
    """建一棵两层的章节树，返回 (course_id, {path: chapter_id})。"""
    course = courses.create("C 语言程序设计")
    ch3 = chapters.create(course.id, "第3章 控制结构")
    loop = chapters.create(course.id, "3.2 循环结构", parent_id=ch3.id)
    chapters.create(course.id, "3.2.1 while", parent_id=loop.id)
    chapters.create(course.id, "3.1 分支结构", parent_id=ch3.id)
    chapters.create(course.id, "第4章 数组")
    return course.id, {c.path: c.id for c in chapters.list_tree(course.id)}


def test_paths_are_materialized(tree: tuple[str, dict[str, str]]) -> None:
    _, paths = tree
    assert "第3章 控制结构" in paths
    assert "第3章 控制结构/3.2 循环结构" in paths
    assert "第3章 控制结构/3.2 循环结构/3.2.1 while" in paths


def test_list_tree_sorted_by_path(tree: tuple[str, dict[str, str]], chapters: ChapterRepo) -> None:
    course_id, _ = tree
    listed = [c.path for c in chapters.list_tree(course_id)]
    assert listed == sorted(listed), "按 path 排序后前端才能直接按深度缩进"


def test_subtree_paths_includes_self_and_descendants(
    tree: tuple[str, dict[str, str]], chapters: ChapterRepo
) -> None:
    _, paths = tree
    result = chapters.subtree_paths(paths["第3章 控制结构/3.2 循环结构"])
    assert set(result) == {
        "第3章 控制结构/3.2 循环结构",
        "第3章 控制结构/3.2 循环结构/3.2.1 while",
    }


def test_subtree_ids_deduplicates_overlapping_selections(
    tree: tuple[str, dict[str, str]], chapters: ChapterRepo
) -> None:
    """用户同时勾了父章节和子章节时，不能把子章节算两遍。"""
    _, paths = tree
    ids = chapters.subtree_ids([paths["第3章 控制结构"], paths["第3章 控制结构/3.2 循环结构"]])
    assert len(ids) == len(set(ids))
    assert len(ids) == 4  # 第3章 + 3.1 + 3.2 + 3.2.1


def test_rename_rewrites_descendant_paths(
    tree: tuple[str, dict[str, str]], chapters: ChapterRepo
) -> None:
    course_id, paths = tree
    chapters.rename(paths["第3章 控制结构"], "第三章 控制结构")
    after = {c.path for c in chapters.list_tree(course_id)}
    assert "第三章 控制结构/3.2 循环结构/3.2.1 while" in after
    assert not any(p.startswith("第3章") for p in after), "旧前缀残留说明子树没重写"


def test_move_rewrites_descendant_paths(
    tree: tuple[str, dict[str, str]], chapters: ChapterRepo
) -> None:
    course_id, paths = tree
    chapters.move(paths["第3章 控制结构/3.2 循环结构"], paths["第4章 数组"])
    after = {c.path for c in chapters.list_tree(course_id)}
    assert "第4章 数组/3.2 循环结构" in after
    assert "第4章 数组/3.2 循环结构/3.2.1 while" in after


def test_move_to_root(tree: tuple[str, dict[str, str]], chapters: ChapterRepo) -> None:
    course_id, paths = tree
    chapters.move(paths["第3章 控制结构/3.2 循环结构"], None)
    after = {c.path for c in chapters.list_tree(course_id)}
    assert "3.2 循环结构" in after
    assert "3.2 循环结构/3.2.1 while" in after


def test_move_into_own_descendant_rejected(
    tree: tuple[str, dict[str, str]], chapters: ChapterRepo
) -> None:
    """挂到自己的后代下会生成脱离根的环，必须拦住。"""
    _, paths = tree
    with pytest.raises(ValueError, match="子章节"):
        chapters.move(paths["第3章 控制结构"], paths["第3章 控制结构/3.2 循环结构/3.2.1 while"])


def test_move_into_self_rejected(tree: tuple[str, dict[str, str]], chapters: ChapterRepo) -> None:
    _, paths = tree
    with pytest.raises(ValueError, match="子章节"):
        chapters.move(paths["第3章 控制结构"], paths["第3章 控制结构"])


def test_cross_course_parent_rejected(
    courses: CourseRepo, chapters: ChapterRepo, tree: tuple[str, dict[str, str]]
) -> None:
    _, paths = tree
    other = courses.create("离散数学")
    with pytest.raises(ValueError, match="另一门课程"):
        chapters.create(other.id, "第1章", parent_id=paths["第3章 控制结构"])


def test_duplicate_sibling_title_rejected(
    tree: tuple[str, dict[str, str]], chapters: ChapterRepo
) -> None:
    course_id, paths = tree
    with pytest.raises(ConflictError, match="同级已有"):
        chapters.create(course_id, "3.2 循环结构", parent_id=paths["第3章 控制结构"])


def test_same_title_allowed_under_different_parents(
    tree: tuple[str, dict[str, str]], chapters: ChapterRepo
) -> None:
    """不同章下可以有同名小节（「3.1 概述」和「4.1 概述」都叫概述很常见）。"""
    course_id, paths = tree
    chapters.create(course_id, "概述", parent_id=paths["第3章 控制结构"])
    chapters.create(course_id, "概述", parent_id=paths["第4章 数组"])
    paths_after = {c.path for c in chapters.list_tree(course_id)}
    assert "第3章 控制结构/概述" in paths_after
    assert "第4章 数组/概述" in paths_after


def test_separator_in_title_is_replaced(courses: CourseRepo, chapters: ChapterRepo) -> None:
    """标题里出现路径分隔符会让物化路径被解析错，必须替换掉。"""
    course = courses.create("测试课")
    chapter = chapters.create(course.id, "输入/输出")
    assert PATH_SEP not in chapter.title
    assert chapter.path == "输入／输出"


# ------------------------------------------------------------------ ensure_path
def test_ensure_path_creates_missing_levels(courses: CourseRepo, chapters: ChapterRepo) -> None:
    """导入文件写的是人类可读路径，缺的层级要自动补出来。"""
    course = courses.create("计算机网络")
    leaf = chapters.ensure_path(course.id, "第2章 物理层/2.3 传输介质")
    assert leaf is not None
    assert leaf.path == "第2章 物理层/2.3 传输介质"
    assert {c.path for c in chapters.list_tree(course.id)} == {
        "第2章 物理层",
        "第2章 物理层/2.3 传输介质",
    }


def test_ensure_path_is_idempotent(courses: CourseRepo, chapters: ChapterRepo) -> None:
    course = courses.create("计算机网络")
    first = chapters.ensure_path(course.id, "第2章/2.3 介质")
    second = chapters.ensure_path(course.id, "第2章/2.3 介质")
    assert first is not None and second is not None
    assert first.id == second.id
    assert len(chapters.list_tree(course.id)) == 2


def test_ensure_path_empty_means_no_chapter(courses: CourseRepo, chapters: ChapterRepo) -> None:
    """跨章节的综合题不归属任何章节，这是合法状态。"""
    course = courses.create("计算机网络")
    assert chapters.ensure_path(course.id, "") is None
    assert chapters.ensure_path(course.id, "   /  ") is None


# ------------------------------------------------- 删课程（曾经一删就崩）
def test_deleting_a_course_with_questions_works(session: Session) -> None:
    """删一门**有题目**的课程。

    这条曾经必崩：``Course.questions`` 关系上没声明 delete 级联时，ORM 会去把
    ``question.course_id`` 置为 NULL，而那一列是 NOT NULL，于是抛
    IntegrityError。旧测试只删过空课程，所以一直没发现——而现实里没有人会去删
    一门空课。修法是 ``passive_deletes=True``：数据库那边本来就声明了
    ``ON DELETE CASCADE``，交给它做。
    """
    from qbank.core.enums import QuestionType
    from qbank.core.models import Question
    from qbank.core.schemas import QuestionDraft, SingleChoicePayload
    from qbank.core.services.question import QuestionService

    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    draft = QuestionDraft(
        type=QuestionType.SINGLE_CHOICE,
        stem="栈的特点是？",
        payload=SingleChoicePayload(
            options=(
                {"label": "A", "content": "后进先出", "is_correct": True},  # type: ignore[arg-type]
                {"label": "B", "content": "先进先出"},  # type: ignore[arg-type]
            )
        ),
        answer="A",
        chapter="第1章 绪论",
        tags=("基础",),
    )
    service.create(course.id, draft)
    session.flush()

    removed = service.delete_course(course.id)

    assert removed == 1
    assert CourseRepo(session).find_by_name("数据结构") is None
    assert session.scalar(select(func.count(Question.id))) == 0
    assert service.search.indexed_count() == 0, "删课程要顺手清掉它那些题的检索索引"


def test_deleting_a_course_takes_its_papers(session: Session) -> None:
    """试卷挂在课程上，必须跟着一起没——否则留下一堆指向不存在课程的卷子。"""
    from qbank.core.repo.paper import PaperRepo
    from qbank.core.services.paper import PaperService
    from qbank.core.services.question import QuestionService

    course = CourseRepo(session).create("数据结构")
    PaperService(session).create_empty(course.id, "期末卷", sections=["一、试题"])
    session.flush()
    assert len(PaperRepo(session).list_all()) == 1

    QuestionService(session).delete_course(course.id)
    session.flush()
    assert PaperRepo(session).list_all(include_deleted=True) == []
