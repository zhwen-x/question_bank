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
    "correct": ["1"],  # 行序号：第 1 行就是 B
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
            correct=["0"],
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
                correct=["0", "1"],
            )
        )


def test_multiple_choice_allows_several() -> None:
    draft = parse_question_form(
        form(
            type="multiple_choice",
            stem="甲",
            option_label=["A", "B", "C"],
            option_content=["一", "二", "三"],
            correct=["0", "2"],
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
                correct=["0"],
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
                correct=["0"],
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
                correct=["0"],
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


# ============================================ 「正确选项」的编码方式
#
# 这组测试来自一个真实 bug：单选题点「创建」后报「请勾选正确选项」，而用户
# 明明选了。根因是 radio 的 value 用的是同一行里**用户可编辑的标号**——
# 点「添加选项」新增的行，JS 会自动补上标号，radio 的 value 却补不上
# （还是 <template> 里的空串），于是提交上去的是一个对不上任何行的值。
#
# 新建表单默认只给两行，而单选题通常要四个选项，所以这条路径是常态而不是边角。


def test_picking_a_row_that_was_added_later_works() -> None:
    """正确项落在「添加选项」新增出来的行上——就是用户报的那个场景。"""
    draft = parse_question_form(
        form(
            type="single_choice",
            stem="下列哪个是栈的特点？",
            option_label=["A", "B", "C", "D"],
            option_content=["先进先出", "随机访问", "后进先出", "自动排序"],
            correct=["2"],
        )
    )
    assert [o.label for o in draft.payload.options if o.is_correct] == ["C"]


def test_selection_survives_renaming_the_label() -> None:
    """用户把标号从 A/B 改成 甲/乙，选中的还是同一行。

    这是同一个根因的另一条路径：标号一改，和它绑定的 value 就失联了。
    改用行序号之后，标号怎么改都不影响。
    """
    draft = parse_question_form(
        form(
            type="single_choice",
            stem="甲",
            option_label=["甲", "乙"],
            option_content=["一", "二"],
            correct=["1"],
        )
    )
    assert [o.label for o in draft.payload.options if o.is_correct] == ["乙"]


def test_blank_rows_do_not_shift_the_selection() -> None:
    """中间留了一行空的，后面行的序号不该因为被跳过而错位。

    序号取的是**提交顺序**里的下标，不是过滤之后 rows 里的下标。
    """
    draft = parse_question_form(
        form(
            type="single_choice",
            stem="甲",
            option_label=["A", "", "C"],
            option_content=["一", "", "三"],
            correct=["2"],
        )
    )
    assert [o.label for o in draft.payload.options] == ["A", "C"]
    assert [o.label for o in draft.payload.options if o.is_correct] == ["C"]


def test_an_unreadable_selection_does_not_blame_the_user() -> None:
    """浏览器缓存了旧版表单时，提示要说「刷新」，不能说「你没选」。

    这正是原 bug 最坑人的地方：报错把责任推给用户，而用户确实选了，
    于是只能反复点那个圆点。
    """
    with pytest.raises(FormError) as caught:
        parse_question_form(
            form(
                type="single_choice",
                stem="甲",
                option_label=["A", "B"],
                option_content=["一", "二"],
                correct=[""],  # 出事那版模板提交上来的就是这个
            )
        )
    assert "请刷新" in str(caught.value)


def test_the_three_places_agree_on_what_correct_carries() -> None:
    """模板发什么、JS 维护什么、服务端怎么读——三处必须一致。

    三者分属 Jinja、JavaScript、Python，没有编译器会发现它们对不上。
    原 bug 就是模板发标号、JS 只补标号不补 value、服务端按标号匹配，
    三方各自「正确」，合起来不工作。任何一方改回去都要在这里红。
    """
    import re
    from pathlib import Path

    import qbank

    src = Path(qbank.__file__).parent
    template = (src / "web" / "templates" / "question_form.html").read_text("utf-8")
    script = (src / "web" / "static" / "app.js").read_text("utf-8")

    # 模板：渲染出来的那一行必须用序号，不能用 option.label
    rendered = re.search(r'name="correct" value="\{\{ ([^}]+) \}\}"', template)
    assert rendered, "模板里找不到 correct 的 value，抽取逻辑可能失效了"
    assert rendered.group(1).strip() == "loop.index0", (
        f"模板把 correct 的 value 写成了 {rendered.group(1)!r}；"
        "用选项标号会和用户可编辑的输入框绑死，增删行或改标号就会失联"
    )

    # JS：增删行之后必须重排这些 value
    assert "querySelectorAll('input[name=\"correct\"]')" in script, (
        "app.js 不再维护 correct 的 value 了；"
        "从 <template> 克隆出来的新行 value 是空串，提交上去对不上任何一行"
    )

    # 服务端：按序号读
    forms_source = (src / "web" / "forms.py").read_text("utf-8")
    assert "int(value) for value in submitted if value.isdigit()" in forms_source, (
        "服务端不再按行序号解读 correct 了"
    )


def test_validation_and_redisplay_read_correct_the_same_way() -> None:
    """校验用的规则和出错回填用的规则必须是同一条。

    这条是修 bug 时第二次被绊倒逼出来的：``_parse_options`` 改成按行序号之后，
    ``routers/questions.py`` 里那份拷贝还按选项标号匹配，于是症状从
    「误报没勾选」变成「报错之后四个圆点全空」——用户连自己刚选的都找不回来。

    两处现在共用 :func:`correct_positions`，这个测试守着它们不再分家。
    """
    from qbank.web.forms import correct_positions
    from qbank.web.routers.questions import _values_from_form

    submitted = form(
        type="single_choice",
        stem="甲",
        option_label=["A", "B", "C", "D"],
        option_content=["一", "二", "三", "四"],
        correct=["2"],
    )

    draft = parse_question_form(submitted)
    validated = [option.is_correct for option in draft.payload.options]
    redisplayed = [option["is_correct"] for option in _values_from_form(submitted)["options"]]
    assert validated == redisplayed == [False, False, True, False]

    positions, unreadable = correct_positions(submitted)
    assert positions == {2} and not unreadable
