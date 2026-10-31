"""试卷服务测试。

重点在三件「几个月后才会被发现」的事：

1. **题面快照必须冻住**——改了题，去年的卷子不能跟着变
2. **使用记录必须跟着题目项走**——否则「避开近期用过的题」形同失效，
   或者反过来，试着组几次卷就把那批题封印了几个月
3. **组不齐时不许悄悄少几道**——一份实际 92 分的「100 分卷」比报错糟得多
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from qbank.core.blueprint import Blueprint
from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.schemas import QuestionDraft, SingleChoicePayload
from qbank.core.services.paper import (
    PaperService,
    ShortageError,
    display_answer,
    display_options,
    freeze,
    thaw,
)
from qbank.core.services.question import QuestionService


def _choice_draft(stem: str, difficulty: Difficulty = Difficulty.MEDIUM) -> QuestionDraft:
    return QuestionDraft(
        type=QuestionType.SINGLE_CHOICE,
        stem=stem,
        payload=SingleChoicePayload(
            options=(
                {"label": "A", "content": "甲", "is_correct": True},  # type: ignore[arg-type]
                {"label": "B", "content": "乙"},  # type: ignore[arg-type]
                {"label": "C", "content": "丙"},  # type: ignore[arg-type]
                {"label": "D", "content": "丁"},  # type: ignore[arg-type]
            )
        ),
        answer="A",
        difficulty=difficulty,
        status=QuestionStatus.ACTIVE,
    )


@pytest.fixture
def bank(session: Session) -> tuple[str, QuestionService]:
    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    for index in range(30):
        service.create(course.id, _choice_draft(f"第{index}题：栈与队列的区别是？"))
    return course.id, service


def _blueprint(count: int = 10, *, score_each: float = 10, **extra: object) -> Blueprint:
    data: dict[str, object] = {
        "title": "2026 春季期末",
        "total_score": count * score_each,
        "meta": {"考试时长": "120 分钟", "命题人": "张老师"},
        "sections": [
            {
                "title": "一、单项选择题",
                "instructions": "每题只有一个正确选项。",
                "types": ["single_choice"],
                "count": count,
                "score_each": score_each,
            }
        ],
    }
    data.update(extra)
    return Blueprint.model_validate(data)


# ===================================================================== 组卷
def test_assembles_a_hundred_point_paper(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """M4 的验收判据：能出一份 100 分卷。"""
    course_id, _ = bank
    paper, result = PaperService(session).assemble(course_id, _blueprint(), seed=20261030)
    assert result.ok, result.report()
    assert paper.total_score == 100
    assert len(paper.sections) == 1
    assert len(paper.sections[0].items) == 10
    assert paper.sections[0].instructions == "每题只有一个正确选项。"
    assert paper.meta["命题人"] == "张老师"


def test_same_seed_reproduces_the_same_paper(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """M4 的另一半判据：同种子可复现。"""
    course_id, _ = bank
    service = PaperService(session)
    first, _ = service.assemble(course_id, _blueprint(), seed=42)
    second, _ = service.assemble(course_id, _blueprint(), seed=42)
    assert [item.question_id for item in first.sections[0].items] == [
        item.question_id for item in second.sections[0].items
    ]
    assert first.seed == second.seed == 42


def test_seed_is_stored_so_the_paper_can_be_reproduced_later(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    paper, result = PaperService(session).assemble(course_id, _blueprint())
    assert paper.seed == result.seed
    assert paper.blueprint["sections"][0]["count"] == 10, "蓝图要留档，否则没法照原规则再出一份"


def test_shortage_refuses_to_build_a_short_paper(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    with pytest.raises(ShortageError) as excinfo:
        PaperService(session).assemble(course_id, _blueprint(count=50, score_each=2), seed=1)
    assert "缺 20 道" in str(excinfo.value)
    assert excinfo.value.result.question_count == 30
    assert PaperRepo(session).list_all(course_id) == [], "失败的组卷不该留下半份卷子"


def test_partial_paper_requires_an_explicit_opt_in(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    paper, result = PaperService(session).assemble(
        course_id, _blueprint(count=50, score_each=2), seed=1, allow_partial=True
    )
    assert not result.ok
    assert len(paper.sections[0].items) == 30


# =================================================================== 快照
def test_editing_a_question_does_not_change_an_existing_paper(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """教学场景的硬要求：去年的卷子永远是去年那张。"""
    course_id, questions = bank
    paper, _ = PaperService(session).assemble(course_id, _blueprint(), seed=9)
    item = paper.sections[0].items[0]
    assert item.question_id is not None
    original = thaw(item.snapshot).stem

    questions.update(item.question_id, _choice_draft("改过的题干：完全不一样了"))
    session.flush()
    session.expire_all()

    frozen = thaw(PaperRepo(session).get_item(item.id).snapshot)
    assert frozen.stem == original
    assert "改过的题干" not in frozen.stem


def test_paper_survives_deleting_the_original_question(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, questions = bank
    paper, _ = PaperService(session).assemble(course_id, _blueprint(), seed=9)
    item_id = paper.sections[0].items[0].id
    question_id = paper.sections[0].items[0].question_id
    assert question_id is not None

    questions.delete(question_id)
    questions.purge(question_id)
    session.flush()
    session.expire_all()

    item = PaperRepo(session).get_item(item_id)
    assert item.question_id is None, "原题真删后应断开引用"
    assert thaw(item.snapshot).stem, "但题面必须还在"


def test_thaw_accepts_a_bare_draft() -> None:
    """快照格式将来可能再变，读旧卷子永远不能报错。"""
    draft = _choice_draft("裸 draft")
    assert thaw(draft.model_dump(mode="json")).stem == "裸 draft"


def test_freeze_records_the_revision_it_froze(session: Session) -> None:
    course = CourseRepo(session).create("c")
    service = QuestionService(session)
    question = service.create(course.id, _choice_draft("题"))
    snapshot = freeze(service.to_draft(question), question)
    assert snapshot["revision"] == question.revision
    assert snapshot["question_id"] == question.id


# =============================================================== 使用记录
def test_usage_is_recorded_so_the_next_paper_avoids_these_questions(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    paper, _ = PaperService(session).assemble(course_id, _blueprint(), seed=3)
    used = {item.question_id for item in paper.sections[0].items}

    fresh = QuestionRepo(session).ids(
        QuestionFilter(course_id=course_id, exclude_used_within_days=180)
    )
    assert not set(fresh) & used
    assert len(fresh) == 20


def test_removing_an_item_frees_the_question_again(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper, _ = service.assemble(course_id, _blueprint(), seed=3)
    item = paper.sections[0].items[0]
    freed = item.question_id
    service.remove_item(item.id)

    fresh = QuestionRepo(session).ids(
        QuestionFilter(course_id=course_id, exclude_used_within_days=180)
    )
    assert freed in fresh


def test_trashing_a_paper_releases_its_questions(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """试着组几次卷再删掉，不该把那批题封印几个月。"""
    course_id, _ = bank
    service = PaperService(session)
    paper, _ = service.assemble(course_id, _blueprint(), seed=3)
    service.delete(paper.id)

    fresh = QuestionRepo(session).ids(
        QuestionFilter(course_id=course_id, exclude_used_within_days=180)
    )
    assert len(fresh) == 30


def test_removing_a_section_releases_its_questions(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper, _ = service.assemble(course_id, _blueprint(), seed=3)
    service.remove_section(paper.sections[0].id)
    assert PaperRepo(session).used_question_ids(paper.id) == set()


# =============================================================== 手工组卷
def test_manual_paper_from_selected_questions(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper = service.create_empty(course_id, "随堂测验", sections=["一、选择题"])
    ids = QuestionRepo(session).ids(QuestionFilter(course_id=course_id))[:3]

    outcome = service.add_questions(paper.id, paper.sections[0].id, ids, score=5)
    assert outcome.added == tuple(ids)
    assert paper.total_score == 15
    assert outcome.summary == "加入 3 道"


def test_adding_the_same_question_twice_is_skipped_not_an_error(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """手工选题时勾重了是常事，为此报错太烦人——但也不能真的加两遍。"""
    course_id, _ = bank
    service = PaperService(session)
    paper = service.create_empty(course_id, "测验", sections=["一"])
    ids = QuestionRepo(session).ids(QuestionFilter(course_id=course_id))[:2]
    service.add_questions(paper.id, paper.sections[0].id, ids)

    outcome = service.add_questions(paper.id, paper.sections[0].id, [*ids, "不存在的id"])
    assert outcome.added == ()
    assert outcome.duplicates == tuple(ids)
    assert outcome.missing == ("不存在的id",)
    assert len(paper.sections[0].items) == 2
    assert "已在本卷中" in outcome.summary


def test_item_score_defaults_to_the_question_score(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, questions = bank
    draft = _choice_draft("自带 7 分的题").model_copy(update={"score": 7.0})
    question = questions.create(course_id, draft)
    service = PaperService(session)
    paper = service.create_empty(course_id, "测验", sections=["一"])
    service.add_questions(paper.id, paper.sections[0].id, [question.id])
    assert paper.sections[0].items[0].score == 7


def test_moving_and_rescoring_items(bank: tuple[str, QuestionService], session: Session) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper, _ = service.assemble(course_id, _blueprint(count=3, score_each=10), seed=2)
    items = list(paper.sections[0].items)
    first, second = items[0].id, items[1].id

    service.move_item(first, 1)
    session.expire_all()
    reordered = PaperRepo(session).get_detailed(paper.id).sections[0].items
    assert [item.id for item in reordered][:2] == [second, first]

    service.set_item_score(first, 25)
    assert PaperRepo(session).get_item(first).score == 25
    with pytest.raises(ValueError, match="不能为负数"):
        service.set_item_score(first, -1)


def test_moving_an_item_to_another_section(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper = service.create_empty(course_id, "测验", sections=["一、选择", "二、判断"])
    ids = QuestionRepo(session).ids(QuestionFilter(course_id=course_id))[:2]
    service.add_questions(paper.id, paper.sections[0].id, ids)

    service.move_item_to_section(paper.sections[0].items[0].id, paper.sections[1].id)
    session.expire_all()
    fresh = PaperRepo(session).get_detailed(paper.id)
    assert len(fresh.sections[0].items) == 1
    assert len(fresh.sections[1].items) == 1
    assert fresh.sections[0].items[0].sort_order == 0, "删掉中间一项后序号要压回连续"


# ================================================================== A/B 卷
def test_variant_reuses_the_blueprint_and_avoids_overlap(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper_a, _ = service.assemble(course_id, _blueprint(), seed=1)
    paper_b, result = service.variant(paper_a.id, title="2026 春季期末（B 卷）", seed=2)

    assert result.ok, result.report()
    assert paper_b.title == "2026 春季期末（B 卷）"
    a_ids = {item.question_id for item in paper_a.sections[0].items}
    b_ids = {item.question_id for item in paper_b.sections[0].items}
    assert not a_ids & b_ids
    assert paper_b.total_score == 100


def test_variant_of_a_manual_paper_explains_why_it_cannot(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper = service.create_empty(course_id, "手工卷", sections=["一"])
    with pytest.raises(ValueError, match="没有蓝图"):
        service.variant(paper.id)


# ============================================================== 选项乱序
def test_option_shuffle_is_reproducible(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = PaperService(session)
    blueprint = _blueprint(
        sections=[
            {
                "title": "一、单项选择题",
                "types": ["single_choice"],
                "count": 5,
                "score_each": 2,
                "shuffle_options": True,
            }
        ],
        total_score=10,
    )
    first, _ = service.assemble(course_id, blueprint, seed=77)
    second, _ = service.assemble(course_id, blueprint, seed=77)
    orders = [item.option_order for item in first.sections[0].items]
    assert orders == [item.option_order for item in second.sections[0].items]
    assert any(order != ["A", "B", "C", "D"] for order in orders), "乱序没生效"
    for order in orders:
        assert sorted(order) == ["A", "B", "C", "D"], "乱序不能丢选项"


def test_shuffled_options_are_relabelled_and_the_answer_follows() -> None:
    """卷子上「C.」排在第一个位置，学生只会以为印错了；答案也必须跟着换。"""
    draft = _choice_draft("题")
    shown = display_options(draft, ["C", "A", "D", "B"])
    assert [option.label for option in shown] == ["A", "B", "C", "D"]
    assert [option.content for option in shown] == ["丙", "甲", "丁", "乙"]
    assert display_answer(draft, ["C", "A", "D", "B"]) == "B"


def test_prose_answers_are_left_alone_when_options_are_shuffled() -> None:
    """答案里还写了别的内容时保持原样——擅自改写一段话比留着人工核对更危险。"""
    draft = _choice_draft("题").model_copy(update={"answer": "A，因为栈是后进先出"})
    assert display_answer(draft, ["C", "A", "D", "B"]) == "A，因为栈是后进先出"


def test_display_options_falls_back_when_the_order_no_longer_matches() -> None:
    """次序与选项对不上时退回原序，不能印出一份少了选项的卷子。"""
    draft = _choice_draft("题")
    shown = display_options(draft, ["A", "B"])
    assert [option.content for option in shown] == ["甲", "乙", "丙", "丁"]


def test_no_shuffle_means_no_stored_order(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    paper, _ = PaperService(session).assemble(course_id, _blueprint(count=2, score_each=1), seed=4)
    assert all(item.option_order == [] for item in paper.sections[0].items)


# ================================================================ 仓储杂项
def test_summaries_avoid_loading_every_item(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper, _ = service.assemble(course_id, _blueprint(count=4, score_each=5), seed=6)
    assert PaperRepo(session).summaries(course_id) == {paper.id: (4, 20.0)}


def test_trash_and_restore(bank: tuple[str, QuestionService], session: Session) -> None:
    course_id, _ = bank
    service = PaperService(session)
    paper, _ = service.assemble(course_id, _blueprint(count=2, score_each=1), seed=6)
    service.delete(paper.id)
    assert PaperRepo(session).list_all(course_id) == []
    service.restore(paper.id)
    assert len(PaperRepo(session).list_all(course_id)) == 1
    service.purge(paper.id)
    assert PaperRepo(session).list_all(course_id, include_deleted=True) == []


# ================================================== 可复现的边界（容易误解）
def test_assembling_twice_with_avoid_used_gives_a_different_paper(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """「同种子出同一份卷」的前提是**题库状态不变**，而组一份卷本身会改变它。

    成卷会写使用记录，于是 ``avoid_used_within_days`` 会在下一次组卷时把这批题
    挡掉。所以原样重跑一次蓝图得到的是「不重题的另一套」，不是同一套——这正是
    出 B 卷想要的行为，但很容易被误当成「可复现坏了」，因此在这里钉住。

    要复现一份旧卷，看的是卷子本身（题面已冻结），而不是重跑蓝图。
    """
    course_id, _ = bank
    service = PaperService(session)
    blueprint = _blueprint(count=10, score_each=10, avoid_used_within_days=180)

    first, _ = service.assemble(course_id, blueprint, seed=42)
    second, _ = service.assemble(course_id, blueprint, seed=42)

    first_ids = {item.question_id for item in first.sections[0].items}
    second_ids = {item.question_id for item in second.sections[0].items}
    assert first_ids != second_ids
    assert not first_ids & second_ids, "设了 avoid_used_within_days 就不该重题"


def test_the_draw_is_reproducible_when_nothing_changed(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """不写使用记录的纯抽题（预览）重复执行必须一致——Web 的「预览后确认」靠它。"""
    course_id, _ = bank
    service = PaperService(session)
    blueprint = _blueprint(count=10, score_each=10, avoid_used_within_days=180)
    preview = service.selection.select(course_id, blueprint, seed=42)
    again = service.selection.select(course_id, blueprint, seed=42)
    assert [q.id for q in preview.sections[0].questions] == [
        q.id for q in again.sections[0].questions
    ]
    paper, _ = service.assemble(course_id, blueprint, seed=42)
    assert [item.question_id for item in paper.sections[0].items] == [
        q.id for q in preview.sections[0].questions
    ], "确认生成的必须就是预览里看到的那一份"
