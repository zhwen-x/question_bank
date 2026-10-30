"""表单解析测试。

重点是**校验失败时的错误信息**：界面上的错误直接决定用户能不能自己改好。
以及各题型 payload 的组装——表单是扁平的重复字段，组装错了题目会静默变形。
"""

from __future__ import annotations

import pytest
from starlette.datastructures import FormData

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.web.forms import FormError, parse_question_form


def form(**fields: object) -> FormData:
    """构造表单。列表值会展开成同名重复字段，和浏览器提交的形状一致。"""
    items: list[tuple[str, str]] = []
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            items.extend((key, str(item)) for item in value)
        else:
            items.append((key, str(value)))
    return FormData(items)


CHOICE_BASE = {
    "type": "single_choice",
    "stem": "下列代码的输出是？",
    "option_label": ["A", "B", "C"],
    "option_content": ["1024", "1023", "2048"],
    "correct": ["B"],
    "answer": "B",
}


def test_single_choice_roundtrip() -> None:
    draft = parse_question_form(
        form(
            **CHOICE_BASE,
            chapter="第3章/3.2 循环",
            difficulty="2",
            score="2.5",
            est_minutes="3",
            tags="循环、边界条件 while",
            source="2025 期末",
            status="active",
            external_id="web-01",
            explanation="循环执行 10 次",
        )
    )
    assert draft.type is QuestionType.SINGLE_CHOICE
    assert draft.chapter == "第3章/3.2 循环"
    assert draft.difficulty is Difficulty.EASY
    assert draft.score == 2.5
    assert draft.est_minutes == 3
    assert draft.tags == ("循环", "边界条件", "while")
    assert draft.status is QuestionStatus.ACTIVE
    assert [o.label for o in draft.payload.options if o.is_correct] == ["B"]


def test_empty_option_rows_are_ignored() -> None:
    """用户点了「添加选项」又没填，不该因此报错。"""
    draft = parse_question_form(
        form(
            type="single_choice",
            stem="甲",
            option_label=["A", "B", ""],
            option_content=["一", "二", ""],
            correct=["A"],
        )
    )
    assert len(draft.payload.options) == 2


def test_missing_correct_is_reported() -> None:
    with pytest.raises(FormError) as caught:
        parse_question_form(
            form(
                type="single_choice",
                stem="甲",
                option_label=["A", "B"],
                option_content=["一", "二"],
            )
        )
    assert "请勾选正确选项" in str(caught.value)
    assert "correct" in caught.value.fields


def test_single_choice_rejects_multiple_correct() -> None:
    with pytest.raises(FormError, match="只能有一个正确选项"):
        parse_question_form(
            form(
                type="single_choice",
                stem="甲",
                option_label=["A", "B"],
                option_content=["一", "二"],
                correct=["A", "B"],
            )
        )


def test_multiple_choice_allows_several() -> None:
    draft = parse_question_form(
        form(
            type="multiple_choice",
            stem="甲",
            option_label=["A", "B", "C"],
            option_content=["一", "二", "三"],
            correct=["A", "C"],
        )
    )
    assert [o.label for o in draft.payload.options if o.is_correct] == ["A", "C"]


def test_duplicate_labels_are_reported() -> None:
    with pytest.raises(FormError, match="标号重复"):
        parse_question_form(
            form(
                type="single_choice",
                stem="甲",
                option_label=["A", "A"],
                option_content=["一", "二"],
                correct=["A"],
            )
        )


def test_too_few_options_reported() -> None:
    with pytest.raises(FormError, match="至少需要 2 个选项"):
        parse_question_form(
            form(
                type="single_choice",
                stem="甲",
                option_label=["A"],
                option_content=["一"],
                correct=["A"],
            )
        )


def test_option_without_content_reported() -> None:
    with pytest.raises(FormError) as caught:
        parse_question_form(
            form(
                type="single_choice",
                stem="甲",
                option_label=["A", "B"],
                option_content=["一", ""],
                correct=["A"],
            )
        )
    assert "没有内容" in str(caught.value)


def test_blank_stem_reported() -> None:
    with pytest.raises(FormError) as caught:
        parse_question_form(form(**{**CHOICE_BASE, "stem": "   "}))
    assert "题干不能为空" in str(caught.value)
    assert "stem" in caught.value.fields


def test_true_false() -> None:
    assert (
        parse_question_form(form(type="true_false", stem="甲", tf_correct="false")).payload.correct
        is False
    )
    assert (
        parse_question_form(form(type="true_false", stem="甲", tf_correct="true")).payload.correct
        is True
    )


def test_fill_blank_with_alternatives() -> None:
    draft = parse_question_form(
        form(
            type="fill_blank",
            stem="甲____乙____",
            blank_answer=["北京/首都", "上海"],
            blank_match=["exact", "exact"],
            blank_tolerance=["", ""],
        )
    )
    assert [b.answers for b in draft.payload.blanks] == [("北京", "首都"), ("上海",)]
    assert [b.index for b in draft.payload.blanks] == [1, 2]


def test_fill_blank_renumbers_after_gaps() -> None:
    """用户删掉中间一空后，序号必须重新连续，否则会被校验层拒掉。"""
    draft = parse_question_form(
        form(
            type="fill_blank",
            stem="甲____乙____",
            blank_answer=["一", "", "三"],
            blank_match=["exact", "exact", "exact"],
            blank_tolerance=["", "", ""],
        )
    )
    assert [b.index for b in draft.payload.blanks] == [1, 2]
    assert [b.answers for b in draft.payload.blanks] == [("一",), ("三",)]


def test_fill_blank_without_answers_reported() -> None:
    with pytest.raises(FormError, match="至少要填一个空"):
        parse_question_form(form(type="fill_blank", stem="甲____", blank_answer=[""]))


def test_numeric_tolerance_must_be_numeric() -> None:
    with pytest.raises(FormError, match="允许误差不是数字"):
        parse_question_form(
            form(
                type="fill_blank",
                stem="甲____",
                blank_answer=["3.14"],
                blank_match=["numeric"],
                blank_tolerance=["大概"],
            )
        )


def test_programming_with_test_cases() -> None:
    draft = parse_question_form(
        form(
            type="programming",
            stem="实现快排",
            language="python",
            signature="def f(a: list[int]) -> None",
            starter_code="def f(a):\n    pass",
            tc_name=["样例1", "大数据"],
            tc_input=["3 1 2", "..."],
            tc_expected=["1 2 3", "..."],
            tc_hidden=["1"],
        )
    )
    assert draft.payload.language == "python"
    assert len(draft.payload.test_cases) == 2
    assert [c.hidden for c in draft.payload.test_cases] == [False, True]


def test_comprehensive_with_sub_questions() -> None:
    draft = parse_question_form(
        form(
            type="comprehensive",
            stem="材料",
            material_label="阅读材料",
            sub_type=["short_answer", "fill_blank"],
            sub_stem=["第一问", "第二问____"],
            sub_answer=["甲", "乙"],
            sub_explanation=["", "因为乙"],
            sub_score=["5", "10"],
        )
    )
    assert [s.type for s in draft.sub_questions] == [
        QuestionType.SHORT_ANSWER,
        QuestionType.FILL_BLANK,
    ]
    assert [s.score for s in draft.sub_questions] == [5.0, 10.0]
    assert draft.sub_questions[1].explanation == "因为乙"


def test_comprehensive_without_sub_questions_reported() -> None:
    with pytest.raises(FormError, match="至少要有一个小问"):
        parse_question_form(form(type="comprehensive", stem="材料"))


def test_sub_question_cannot_be_comprehensive() -> None:
    """只支持两层——三层以上的题目结构在试卷上排不开。"""
    with pytest.raises(FormError, match="不支持"):
        parse_question_form(
            form(
                type="comprehensive",
                stem="材料",
                sub_type=["comprehensive"],
                sub_stem=["第一问"],
                sub_score=["5"],
            )
        )


def test_empty_sub_rows_are_ignored() -> None:
    draft = parse_question_form(
        form(
            type="comprehensive",
            stem="材料",
            sub_type=["short_answer", "short_answer"],
            sub_stem=["第一问", "  "],
            sub_score=["5", ""],
        )
    )
    assert len(draft.sub_questions) == 1


def test_short_answer_key_points() -> None:
    draft = parse_question_form(
        form(type="short_answer", stem="甲", key_points="要点一\n\n要点二\n")
    )
    assert draft.payload.key_points == ("要点一", "要点二")


def test_unknown_type_reported() -> None:
    with pytest.raises(FormError, match="无法识别的题型"):
        parse_question_form(form(type="绘图题", stem="甲"))


def test_out_of_range_difficulty_reported() -> None:
    with pytest.raises(FormError, match="难度必须是 1 到 5"):
        parse_question_form(form(**CHOICE_BASE, difficulty="9"))


def test_unknown_status_falls_back_to_draft() -> None:
    """状态是个边缘字段，写错了不该拦住整道题。"""
    draft = parse_question_form(form(**CHOICE_BASE, status="乱写的"))
    assert draft.status is QuestionStatus.DRAFT


def test_all_errors_collected_not_just_the_first() -> None:
    """一次把问题都说完，别让用户改一个报一个。"""
    with pytest.raises(FormError) as caught:
        parse_question_form(
            form(type="single_choice", stem="", option_label=["A"], option_content=["一"])
        )
    assert len(caught.value.messages) >= 2
