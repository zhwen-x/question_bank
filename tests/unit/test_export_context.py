"""导出上下文测试。

上下文是四种格式的共同输入，这里错一处，四种格式一起错。重点在两件事：

1. **题号与配分**——印错题号的卷子没法用，而且事后谁都说不清是哪一步错的
2. **答案的归属**——选项乱序之后答案必须跟着走，这是整个导出里最危险的一处
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.core.models import Paper
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.paper import PaperService
from qbank.core.services.question import QuestionService
from qbank.exchange.exporters.context import Layout, build_context, chinese_number


def _keep(sha: str) -> str:
    return f"img/{sha}.png"


def _draft(kind: QuestionType, stem: str, **extra: object) -> QuestionDraft:
    payload: dict[str, object] = {"type": kind.value}
    if kind in {QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE}:
        payload["options"] = [
            {"label": "A", "content": "甲", "is_correct": True},
            {"label": "B", "content": "乙"},
            {"label": "C", "content": "丙"},
            {"label": "D", "content": "丁"},
        ]
    elif kind is QuestionType.TRUE_FALSE:
        payload["correct"] = False
    elif kind is QuestionType.FILL_BLANK:
        payload["blanks"] = [
            {"index": 1, "answers": ["后进先出", "LIFO"]},
            {"index": 2, "answers": ["O(1)"]},
        ]
    data: dict[str, object] = {
        "type": kind,
        "stem": stem,
        "payload": payload,
        "answer": "A",
        "status": QuestionStatus.ACTIVE,
        "difficulty": Difficulty.MEDIUM,
    }
    data.update(extra)
    return QuestionDraft.model_validate(data)


@pytest.fixture
def paper(session: Session) -> Paper:
    """一份两节的卷子，覆盖客观题与主观题。"""
    course = CourseRepo(session).create("数据结构")
    questions = QuestionService(session)
    papers = PaperService(session)
    made = papers.create_empty(course.id, "期末", sections=["一、选择题", "二、填空题"])

    choice = questions.create(course.id, _draft(QuestionType.SINGLE_CHOICE, "栈的特点？"))
    judge = questions.create(course.id, _draft(QuestionType.TRUE_FALSE, "队列先进先出。"))
    blank = questions.create(
        course.id,
        _draft(QuestionType.FILL_BLANK, "栈是____的，入栈复杂度____。", answer="后进先出；O(1)"),
    )
    short = questions.create(
        course.id,
        _draft(
            QuestionType.SHORT_ANSWER,
            "说明栈与队列的区别。",
            answer="栈后进先出，队列先进先出。",
            scoring_rubric="答出两点各 5 分。",
        ),
    )
    session.flush()
    papers.add_questions(made.id, made.sections[0].id, [choice.id, judge.id], score=2)
    papers.add_questions(made.id, made.sections[1].id, [blank.id, short.id], score=4)
    session.flush()
    return PaperRepo(session).get_detailed(made.id)


def test_question_numbers_run_continuously_across_sections(paper: Paper) -> None:
    """中文试卷通常全卷连续编号，分节只用「一、二、三」分组。"""
    context = build_context(paper, asset_resolver=_keep)
    numbers = [item.number for section in context.sections for item in section.items]
    assert numbers == [1, 2, 3, 4]
    assert [item.index for item in context.sections[1].items] == [1, 2]


def test_totals_come_from_the_items(paper: Paper) -> None:
    context = build_context(paper, asset_resolver=_keep)
    assert context.total_score == 12
    assert context.question_count == 4
    assert [s.total_score for s in context.sections] == [4, 8]


def test_compact_answers_per_type(paper: Paper) -> None:
    """答案卷的紧凑表格：客观题一行一个，主观题留空（它们要整段列出）。"""
    context = build_context(paper, layout=Layout.ANSWERS, asset_resolver=_keep)
    first, second = context.sections
    assert first.items[0].compact_answer == "A"
    assert first.items[1].compact_answer == "错误"
    assert second.items[0].compact_answer == "①后进先出 ②O(1)"
    assert second.items[1].compact_answer == "", "简答题塞不进紧凑表格"
    assert [item.number for item in second.objective_items] == [3]
    assert [item.number for item in second.subjective_items] == [4]


def test_single_blank_has_no_circled_marker(session: Session) -> None:
    """只有一个空就不编号——「①后进先出」里那个圈号不传达任何信息。"""
    course = CourseRepo(session).create("c")
    questions = QuestionService(session)
    papers = PaperService(session)
    made = papers.create_empty(course.id, "p", sections=["一"])
    one = questions.create(
        course.id,
        QuestionDraft.model_validate(
            {
                "type": QuestionType.FILL_BLANK,
                "stem": "栈是____的。",
                "payload": {
                    "type": "fill_blank",
                    "blanks": [{"index": 1, "answers": ["后进先出"]}],
                },
                "answer": "后进先出",
            }
        ),
    )
    session.flush()
    papers.add_questions(made.id, made.sections[0].id, [one.id])
    session.flush()
    context = build_context(PaperRepo(session).get_detailed(made.id), asset_resolver=_keep)
    assert context.sections[0].items[0].answer == "后进先出"


def test_shuffled_options_carry_the_answer_with_them(paper: Paper, session: Session) -> None:
    """选项乱序后，答案必须是**新**标号。这是导出里最危险的一处。"""
    item = paper.sections[0].items[0]
    item.option_order = ["C", "D", "A", "B"]
    session.flush()
    context = build_context(PaperRepo(session).get_detailed(paper.id), asset_resolver=_keep)
    shown = context.sections[0].items[0]
    assert [option.content for option in shown.options] == ["丙", "丁", "甲", "乙"]
    assert [option.label for option in shown.options] == ["A", "B", "C", "D"]
    assert shown.answer == "C", "原来的 A（甲）排到了第三位，答案必须跟着变成 C"
    assert shown.shuffled


def test_layout_switches(paper: Paper) -> None:
    for layout, questions, answers in (
        (Layout.PAPER, True, False),
        (Layout.FULL, True, True),
        (Layout.ANSWERS, False, True),
    ):
        context = build_context(paper, layout=layout, asset_resolver=_keep)
        assert context.show_questions is questions
        assert context.show_answers is answers


def test_assets_are_rewritten_and_misses_counted(session: Session) -> None:
    """图找不到时换成提示文字并计数——留一个坏引用只会让人以为「图坏了」。"""
    course = CourseRepo(session).create("c")
    questions = QuestionService(session)
    papers = PaperService(session)
    made = papers.create_empty(course.id, "p", sections=["一"])
    with_image = questions.create(
        course.id, _draft(QuestionType.SHORT_ANSWER, "看图回答：![图](asset://abc123)")
    )
    session.flush()
    papers.add_questions(made.id, made.sections[0].id, [with_image.id])
    session.flush()
    loaded = PaperRepo(session).get_detailed(made.id)

    found = build_context(loaded, asset_resolver=_keep)
    assert "img/abc123.png" in found.sections[0].items[0].stem
    assert found.missing_assets == 0

    lost = build_context(loaded, asset_resolver=lambda _: None)
    assert "asset://" not in lost.sections[0].items[0].stem
    assert "［图片缺失］" in lost.sections[0].items[0].stem
    assert lost.missing_assets == 1


def test_chinese_numbers() -> None:
    assert [chinese_number(n) for n in (1, 5, 10, 11)] == ["一", "五", "十", "11"]
