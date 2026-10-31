"""批量操作测试。

批量操作恰恰是最该能撤销的：一次点错影响的是几十道题，而「改错了，一道道改
回去」根本不现实。所以这里重点守住两件事——**逐题留修改历史**，以及
**没变化的题不要产生无意义的历史记录**。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.question import QuestionService


@pytest.fixture
def bank(session: Session) -> tuple[str, list[str], QuestionService]:
    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    ids = []
    for index in range(4):
        question = service.create(
            course.id,
            QuestionDraft.model_validate(
                {
                    "type": QuestionType.SHORT_ANSWER,
                    "stem": f"第{index}题：说明栈的特点。",
                    "payload": {"type": "short_answer"},
                    "answer": "后进先出",
                    "chapter": "第1章 绪论",
                    "tags": ["基础"],
                    "difficulty": Difficulty.MEDIUM,
                    "status": QuestionStatus.DRAFT,
                }
            ),
        )
        ids.append(question.id)
    session.flush()
    return course.id, ids, service


def test_add_and_remove_tags(bank: tuple[str, list[str], QuestionService]) -> None:
    _, ids, service = bank
    result = service.bulk_edit(ids, add_tags=["期末重点", "栈"], remove_tags=["基础"])
    assert result.changed == 4
    for question_id in ids:
        names = {tag.name for tag in service.questions.get_detailed(question_id).tags}
        assert names == {"期末重点", "栈"}


def test_tag_changes_reach_the_search_index(
    bank: tuple[str, list[str], QuestionService], session: Session
) -> None:
    """标签进全文索引。不重建的话，刚打上的标签搜不到。"""
    _, ids, service = bank
    service.bulk_edit(ids[:2], add_tags=["期末重点"])
    session.flush()
    assert len(service.search_ids("期末重点")) == 2


def test_move_to_another_chapter_creates_it(
    bank: tuple[str, list[str], QuestionService], session: Session
) -> None:
    course_id, ids, service = bank
    service.bulk_edit(ids, chapter="第3章 栈和队列/3.1 栈")
    session.flush()
    moved = ChapterRepo(session).find_by_path(course_id, "第3章 栈和队列/3.1 栈")
    assert moved is not None
    assert QuestionRepo(session).count(QuestionFilter(chapter_ids=[moved.id])) == 4


def test_change_difficulty_and_status(bank: tuple[str, list[str], QuestionService]) -> None:
    _, ids, service = bank
    service.bulk_edit(ids, difficulty=Difficulty.VERY_HARD, status=QuestionStatus.ACTIVE)
    for question_id in ids:
        question = service.questions.get(question_id)
        assert question.difficulty is Difficulty.VERY_HARD
        assert question.status is QuestionStatus.ACTIVE


def test_every_question_gets_a_rollback_point(bank: tuple[str, list[str], QuestionService]) -> None:
    """一次点错影响几十道题，撤销能力比省下几条快照重要得多。"""
    _, ids, service = bank
    service.bulk_edit(ids, status=QuestionStatus.ACTIVE)
    for question_id in ids:
        question = service.questions.get_detailed(question_id)
        assert question.revision == 2
        assert len(question.revisions) == 1
        # 历史里记的是**改动之前**的状态
        assert question.revisions[0].snapshot["status"] == "draft"


def test_rollback_after_bulk_edit(
    bank: tuple[str, list[str], QuestionService], session: Session
) -> None:
    _, ids, service = bank
    service.bulk_edit(ids, difficulty=Difficulty.VERY_HARD)
    session.flush()
    service.rollback(ids[0], 1)
    assert service.questions.get(ids[0]).difficulty is Difficulty.MEDIUM


def test_no_op_edits_produce_no_history(bank: tuple[str, list[str], QuestionService]) -> None:
    """批量把 50 道题都设成它们本来就是的值，不该产生 50 条无意义的历史。"""
    _, ids, service = bank
    result = service.bulk_edit(ids, difficulty=Difficulty.MEDIUM, status=QuestionStatus.DRAFT)
    assert result.changed == 0
    assert result.skipped == 4
    assert result.summary == "没有题目需要改动"
    assert service.questions.get_detailed(ids[0]).revisions == []


def test_adding_a_tag_the_question_already_has_is_a_no_op(
    bank: tuple[str, list[str], QuestionService],
) -> None:
    _, ids, service = bank
    assert service.bulk_edit(ids, add_tags=["基础"]).changed == 0


def test_missing_ids_are_skipped_not_fatal(bank: tuple[str, list[str], QuestionService]) -> None:
    """界面上勾选的题可能在另一个标签页里刚被删掉。为此整批失败太脆弱。"""
    _, ids, service = bank
    result = service.bulk_edit([*ids[:2], "不存在的id"], status=QuestionStatus.ACTIVE)
    assert result.changed == 2
    assert result.skipped == 1


def test_bulk_delete_goes_to_trash(
    bank: tuple[str, list[str], QuestionService], session: Session
) -> None:
    _, ids, service = bank
    result = service.bulk_delete(ids[:3])
    assert result.changed == 3
    repo = QuestionRepo(session)
    assert repo.count(QuestionFilter()) == 1
    assert len(repo.list_trash()) == 3
    service.restore(ids[0])
    assert repo.count(QuestionFilter()) == 2


def test_deleted_questions_leave_the_search_index(
    bank: tuple[str, list[str], QuestionService], session: Session
) -> None:
    _, ids, service = bank
    before = len(service.search_ids("栈的特点"))
    service.bulk_delete(ids[:2])
    session.flush()
    assert len(service.search_ids("栈的特点")) == before - 2


def test_summary_mentions_both_counts(bank: tuple[str, list[str], QuestionService]) -> None:
    _, ids, service = bank
    service.bulk_edit(ids[:2], status=QuestionStatus.ACTIVE)
    result = service.bulk_edit(ids, status=QuestionStatus.ACTIVE)
    assert result.summary == "已修改 2 道，2 道无需改动"
