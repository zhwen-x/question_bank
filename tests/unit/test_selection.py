"""自动组卷测试。

这里最要紧的不是「能抽出题」，而是三件容易悄悄出错的事：

1. **同一个种子必须出同一份卷**——不然「复现去年那份卷」是空话
2. **不能报假缺额**——库里够题却说「题库不够」，老师会白白去出新题
3. **报出来的缺额必须是真的**——用 Hall 证书独立复核一遍，不信算法的自述
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from qbank.core.blueprint import Blueprint
from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.core.repo.course import CourseRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.question import QuestionService
from qbank.core.services.selection import (
    BlueprintError,
    SelectionService,
    Stratum,
    solve,
)


# ===================================================== 纯求解器（不碰数据库）
def _stratum(section: int, title: str, needed: int, pool: tuple[str, ...]) -> Stratum:
    return Stratum(
        section_index=section,
        section_title=title,
        difficulty=None,
        needed=needed,
        pool=pool,
    )


def test_fills_each_stratum_from_its_own_pool() -> None:
    strata = [
        _stratum(0, "一、单选", 2, ("a1", "a2", "a3")),
        _stratum(1, "二、判断", 1, ("b1", "b2")),
    ]
    picked, shortages = solve(strata)
    assert shortages == ()
    assert picked == (("a1", "a2"), ("b1",))


def test_never_uses_the_same_question_twice() -> None:
    """两节都够格的题只能归一节——这是把组卷变成分配问题的根本原因。"""
    shared = ("q1", "q2", "q3")
    strata = [_stratum(0, "一", 2, shared), _stratum(1, "二", 1, shared)]
    picked, shortages = solve(strata)
    assert shortages == ()
    chosen = [qid for group in picked for qid in group]
    assert sorted(chosen) == ["q1", "q2", "q3"]


def test_backtracks_instead_of_reporting_a_false_shortage() -> None:
    """先抽的那节把唯一的题占掉时，必须让它换一道，而不是报「题库不够」。

    这是贪心算法分不清的那种情形：第一节有两道可选，第二节只有一道，而那一道
    正是第一节先拿走的。库里明明够，贪心却会说组不齐。
    """
    strata = [
        _stratum(0, "一、宽松", 1, ("q1", "q2")),
        _stratum(1, "二、只认 q1", 1, ("q1",)),
    ]
    picked, shortages = solve(strata)
    assert shortages == (), "库里够题却报了缺额"
    assert picked == (("q2",), ("q1",))


def test_backtracks_through_a_chain() -> None:
    """多级让位：第三节只认 q1，q1 被第二节占着，第二节只能换成 q2，如此递推。"""
    strata = [
        _stratum(0, "一", 1, ("q1", "q2", "q3")),
        _stratum(1, "二", 1, ("q1", "q2")),
        _stratum(2, "三", 1, ("q1",)),
    ]
    picked, shortages = solve(strata)
    assert shortages == ()
    assert picked == (("q3",), ("q2",), ("q1",))


def test_reports_a_single_stratum_shortage() -> None:
    picked, shortages = solve([_stratum(0, "一、综合", 3, ("q1",))])
    assert picked == (("q1",),)
    assert len(shortages) == 1
    assert shortages[0].needed == 3
    assert shortages[0].filled == 1
    assert shortages[0].available == 1
    assert shortages[0].missing == 2
    assert shortages[0].competing == ()
    assert shortages[0].describe() == "一、综合需要 3 道，只排进 1 道——库内符合条件的只有 1 道"


def test_names_the_section_it_is_competing_with() -> None:
    """单看每节都够、合起来不够时，必须点名是谁在抢。

    只说「第二节缺 1 道」会把人引向错误的结论——第二节的候选其实有 2 道，
    他去翻题库会发现「明明有」，真正的原因是第一节也要用它们。
    """
    shared = ("q1", "q2")
    _, shortages = solve([_stratum(0, "一、单选", 2, shared), _stratum(1, "二、多选", 1, shared)])
    assert len(shortages) == 1
    assert shortages[0].label == "二、多选"
    assert shortages[0].competing == ("一、单选",)
    assert shortages[0].available == 2
    assert "是同一批题" in shortages[0].describe()


def test_every_short_section_gets_its_own_explanation() -> None:
    """概览说「第二节 3/4 道」而原因里只提第一节，读报告的人会以为自己看漏了。

    早先按 Hall 违反集合报告、并丢掉「被别的集合包含」的那些，就出现过这种
    无人解释的缺额。
    """
    _, shortages = solve(
        [
            _stratum(0, "一、难综合", 8, ("q1", "q2")),
            _stratum(1, "二、综合", 4, ("q1", "q2", "q3", "q4", "q5")),
        ]
    )
    assert [shortage.label for shortage in shortages] == ["一、难综合", "二、综合"]
    assert shortages[0].competing == (), "自己就不够的那节不该把别人拖进来"
    assert shortages[1].competing == ("一、难综合",)


def test_sections_with_disjoint_pools_are_not_blamed_for_each_other() -> None:
    _, shortages = solve(
        [
            _stratum(0, "一、自己就不够", 3, ("q1",)),
            _stratum(1, "二、本来够", 1, ("q8", "q9")),
        ]
    )
    assert [shortage.label for shortage in shortages] == ["一、自己就不够"]


def test_empty_pool_is_reported_not_silently_skipped() -> None:
    picked, shortages = solve([_stratum(0, "一、没有这种题", 2, ())])
    assert picked == ((),)
    assert shortages[0].available == 0
    assert shortages[0].missing == 2


@pytest.mark.parametrize(
    "strata",
    [
        [_stratum(0, "一", 2, ("a", "b")), _stratum(1, "二", 2, ("b", "c"))],
        [_stratum(0, "一", 1, ("a",)), _stratum(1, "二", 1, ("a",)), _stratum(2, "三", 1, ("a",))],
        [_stratum(0, "一", 3, ("a", "b", "c", "d")), _stratum(1, "二", 2, ("c", "d"))],
    ],
)
def test_reported_shortages_are_genuinely_infeasible(strata: list[Stratum]) -> None:
    """独立复核缺额：参与竞争的各节合计需要的道数，确实多于它们候选的并集。

    这是 Hall 条件，和求解过程无关——算法说凑不齐，这里不信它的自述，
    直接从候选池重算一遍。
    """
    _, shortages = solve(strata)
    assert shortages, "这些用例本来就该凑不齐"
    for shortage in shortages:
        names = {shortage.label, *shortage.competing}
        members = [s for s in strata if s.label in names]
        union = {qid for member in members for qid in member.pool}
        assert sum(member.needed for member in members) > len(union)


# ===================================================== 服务层（带数据库）
def _draft(
    stem: str,
    kind: QuestionType,
    difficulty: Difficulty,
    *,
    chapter: str = "",
    tags: tuple[str, ...] = (),
    status: QuestionStatus = QuestionStatus.ACTIVE,
    score: float = 0,
) -> QuestionDraft:
    payload: dict[str, object] = {"type": kind.value}
    if kind in {QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE}:
        payload["options"] = [
            {"label": "A", "content": f"{stem} 甲", "is_correct": True},
            {"label": "B", "content": f"{stem} 乙"},
            {"label": "C", "content": f"{stem} 丙"},
            {"label": "D", "content": f"{stem} 丁"},
        ]
    elif kind is QuestionType.TRUE_FALSE:
        payload["correct"] = True
    return QuestionDraft(
        type=kind,
        stem=stem,
        payload=payload,  # type: ignore[arg-type]
        answer="A",
        difficulty=difficulty,
        chapter=chapter,
        tags=tags,
        status=status,
        score=score,
    )


@pytest.fixture
def bank(session: Session) -> tuple[str, QuestionService]:
    """一门课：各难度各若干道单选、判断、简答。"""
    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    for difficulty in Difficulty:
        for index in range(4):
            service.create(
                course.id,
                _draft(
                    f"单选 D{difficulty.value} 第{index}题",
                    QuestionType.SINGLE_CHOICE,
                    difficulty,
                    chapter=f"第{1 + index % 2}章",
                    tags=("基础",) if index % 2 == 0 else ("进阶",),
                ),
            )
    # 判断题给 4 道：刚好够「出 A/B 两套各 2 道且不重题」这个用例
    for index in range(4):
        service.create(
            course.id,
            _draft(f"判断 第{index}题", QuestionType.TRUE_FALSE, Difficulty.EASY, chapter="第1章"),
        )
    service.create(
        course.id,
        _draft(
            "草稿单选", QuestionType.SINGLE_CHOICE, Difficulty.MEDIUM, status=QuestionStatus.DRAFT
        ),
    )
    return course.id, service


def _blueprint(**overrides: object) -> Blueprint:
    base: dict[str, object] = {
        "title": "期末考试",
        "sections": [
            {"title": "一、单项选择题", "types": ["single_choice"], "count": 6, "score_each": 2},
            {"title": "二、判断题", "types": ["true_false"], "count": 2, "score_each": 1},
        ],
    }
    base.update(overrides)
    return Blueprint.model_validate(base)


def test_same_seed_gives_the_same_paper(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """可复现是组卷的硬要求：同库 + 同蓝图 + 同种子 = 同一份卷。"""
    course_id, _ = bank
    service = SelectionService(session)
    first = service.select(course_id, _blueprint(), seed=20261030)
    second = service.select(course_id, _blueprint(), seed=20261030)
    assert [[q.id for q in s.questions] for s in first.sections] == [
        [q.id for q in s.questions] for s in second.sections
    ]


def test_different_seed_gives_a_different_draw(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = SelectionService(session)
    first = service.select(course_id, _blueprint(), seed=1)
    second = service.select(course_id, _blueprint(), seed=999)
    assert {q.id for q in first.sections[0].questions} != {
        q.id for q in second.sections[0].questions
    }


def test_seed_is_recorded_when_not_given(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """没给种子也要把现取的那个记下来，否则这份卷再也复现不了。"""
    course_id, _ = bank
    result = SelectionService(session).select(course_id, _blueprint())
    assert result.seed is not None
    again = SelectionService(session).select(course_id, _blueprint(), seed=result.seed)
    assert [q.id for q in again.sections[0].questions] == [
        q.id for q in result.sections[0].questions
    ]


def test_difficulty_mix_is_respected(bank: tuple[str, QuestionService], session: Session) -> None:
    course_id, _ = bank
    blueprint = _blueprint(
        sections=[
            {
                "title": "一、单项选择题",
                "types": ["single_choice"],
                "count": 10,
                "score_each": 2,
                "difficulty_mix": {2: 0.4, 3: 0.4, 4: 0.2},
            }
        ]
    )
    result = SelectionService(session).select(course_id, blueprint, seed=7)
    assert result.ok, result.report()
    counts: dict[int, int] = {}
    for question in result.sections[0].questions:
        counts[question.difficulty.value] = counts.get(question.difficulty.value, 0) + 1
    assert counts == {2: 4, 3: 4, 4: 2}


def test_draft_questions_never_enter_a_paper(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    blueprint = _blueprint(
        sections=[{"title": "一、单选", "types": ["single_choice"], "count": 20, "score_each": 1}]
    )
    result = SelectionService(session).select(course_id, blueprint, seed=3)
    stems = {question.stem for question in result.sections[0].questions}
    assert "草稿单选" not in stems


def test_total_score_matches_the_blueprint(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    blueprint = _blueprint(
        total_score=100,
        sections=[
            {"title": "一、单项选择题", "types": ["single_choice"], "count": 10, "score_each": 8},
            {"title": "二、判断题", "types": ["true_false"], "count": 2, "score_each": 10},
        ],
    )
    result = SelectionService(session).select(course_id, blueprint, seed=11)
    assert result.ok, result.report()
    assert result.total_score == 100


def test_unknown_chapter_raises_instead_of_drawing_nothing(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """章节名打错一个字，必须报错——否则表现成「题库不够」，人会去翻题库。"""
    course_id, _ = bank
    blueprint = _blueprint(
        sections=[{"title": "一", "types": ["single_choice"], "count": 1, "chapters": ["第99章"]}]
    )
    with pytest.raises(BlueprintError) as excinfo:
        SelectionService(session).select(course_id, blueprint, seed=1)
    assert "第99章" in str(excinfo.value)
    assert "第1章" in str(excinfo.value), "报错时要告诉用户现有章节有哪些"


def test_unknown_tag_raises(bank: tuple[str, QuestionService], session: Session) -> None:
    course_id, _ = bank
    blueprint = _blueprint(
        sections=[{"title": "一", "types": ["single_choice"], "count": 1, "tags_any": ["没这个"]}]
    )
    with pytest.raises(BlueprintError, match="没这个"):
        SelectionService(session).select(course_id, blueprint, seed=1)


def test_shortage_hints_point_at_the_binding_constraint(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    """「符合条件的只有 N 道」没法行动，要说清是哪一条把题挡住了。"""
    course_id, _ = bank
    blueprint = _blueprint(
        sections=[
            {
                "title": "一、单选",
                "types": ["single_choice"],
                "count": 10,
                "difficulties": [5],
                "tags_all": ["基础"],
            }
        ]
    )
    result = SelectionService(session).select(course_id, blueprint, seed=1)
    assert not result.ok
    hints = " ".join(result.shortages[0].hints)
    assert "不限难度" in hints
    assert "不限标签" in hints
    assert "可多出" in hints
    assert "题型" not in hints, "对一个限定了题型的大题说「别限题型」没有可操作性"


def test_exclude_ids_keeps_two_papers_from_sharing_questions(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    service = SelectionService(session)
    first = service.select(course_id, _blueprint(), seed=5)
    used = [question.id for section in first.sections for question in section.questions]
    second = service.select(course_id, _blueprint(), seed=5, exclude_ids=used)
    assert second.ok, second.report()
    again = {question.id for section in second.sections for question in section.questions}
    assert not again & set(used)


def test_report_lists_each_section_progress(
    bank: tuple[str, QuestionService], session: Session
) -> None:
    course_id, _ = bank
    blueprint = _blueprint(
        sections=[{"title": "一、单选", "types": ["single_choice"], "count": 50, "score_each": 1}]
    )
    report = SelectionService(session).select(course_id, blueprint, seed=1).report()
    assert "20/50 道" in report
    assert "组不齐" in report
