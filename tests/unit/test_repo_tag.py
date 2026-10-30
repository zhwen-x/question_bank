"""标签仓储测试。重点是合并——用久了必然出现同义标签。"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from qbank.core.enums import QuestionType, TagCategory
from qbank.core.repo.base import ConflictError
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.question import QuestionService


@pytest.fixture
def tags(session: Session) -> TagRepo:
    return TagRepo(session)


def _draft(stem: str, tag_names: tuple[str, ...]) -> QuestionDraft:
    return QuestionDraft.model_validate(
        {
            "type": QuestionType.TRUE_FALSE,
            "stem": stem,
            "payload": {"type": "true_false", "correct": True},
            "tags": tag_names,
        }
    )


def test_get_or_create_is_idempotent(tags: TagRepo) -> None:
    first = tags.get_or_create("循环")
    assert tags.get_or_create("循环").id == first.id
    assert len(tags.list_all()) == 1


def test_duplicate_name_rejected(tags: TagRepo) -> None:
    tags.create("循环")
    with pytest.raises(ConflictError, match="已存在"):
        tags.create("循环")


def test_blank_name_rejected(tags: TagRepo) -> None:
    with pytest.raises(ValueError, match="不能为空"):
        tags.create("  ")


def test_resolve_many_dedups_and_keeps_order(tags: TagRepo) -> None:
    resolved = tags.resolve_many(["循环", " 栈 ", "循环", "", "栈"])
    assert [tag.name for tag in resolved] == ["循环", "栈"]


def test_list_by_category(tags: TagRepo) -> None:
    tags.create("循环", category=TagCategory.KNOWLEDGE)
    tags.create("2025 期末", category=TagCategory.SOURCE)
    assert [t.name for t in tags.list_all(category=TagCategory.SOURCE)] == ["2025 期末"]


def test_usage_counts(session: Session, tags: TagRepo) -> None:
    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    service.create(course.id, _draft("甲", ("循环", "栈")))
    service.create(course.id, _draft("乙", ("循环",)))
    counts = tags.usage_counts()
    by_name = {tag.name: counts.get(tag.id, 0) for tag in tags.list_all()}
    assert by_name == {"循环": 2, "栈": 1}


def test_merge_moves_links(session: Session, tags: TagRepo) -> None:
    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    service.create(course.id, _draft("甲", ("循环结构",)))
    service.create(course.id, _draft("乙", ("循环结构",)))
    source = tags.find_by_name("循环结构")
    target = tags.get_or_create("循环")
    assert source is not None
    result = tags.merge(source.id, target.id)
    assert (result.moved, result.deduplicated, result.affected) == (2, 0, 2)
    assert tags.find_by_name("循环结构") is None
    counts = tags.usage_counts()
    assert counts[target.id] == 2


def test_merge_deduplicates_when_question_has_both(session: Session, tags: TagRepo) -> None:
    """一道题同时带两个标签时，关联是被删除而不是转移。

    只统计转移数会给出「已合并，0 道题受影响」——明明改了东西却说没改。
    """
    course = CourseRepo(session).create("数据结构")
    QuestionService(session).create(course.id, _draft("甲", ("图", "最短路")))
    source = tags.find_by_name("最短路")
    target = tags.find_by_name("图")
    assert source is not None and target is not None
    result = tags.merge(source.id, target.id)
    assert (result.moved, result.deduplicated, result.affected) == (0, 1, 1)
    assert tags.usage_counts()[target.id] == 1


def test_merge_into_self_is_noop(tags: TagRepo) -> None:
    tag = tags.create("循环")
    result = tags.merge(tag.id, tag.id)
    assert result.affected == 0
    assert tags.find_by_name("循环") is not None


def test_delete_unused(session: Session, tags: TagRepo) -> None:
    course = CourseRepo(session).create("数据结构")
    QuestionService(session).create(course.id, _draft("甲", ("循环",)))
    tags.create("没人用的标签")
    assert tags.delete_unused() == 1
    assert [t.name for t in tags.list_all()] == ["循环"]


def test_delete_tag_keeps_questions(session: Session, tags: TagRepo) -> None:
    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    question = service.create(course.id, _draft("甲", ("循环",)))
    tag = tags.find_by_name("循环")
    assert tag is not None
    tags.delete(tag.id)
    session.expire_all()
    assert service.questions.get(question.id).tags == []
