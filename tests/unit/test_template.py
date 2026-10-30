"""模板解析测试：块 → QuestionDraft。

错误信息的质量在这里和功能同等重要——用户拿到的是「第 47 行：…，请核对源文件」
还是一串 Pydantic 堆栈，决定了他能不能自己改好文件。
"""

from __future__ import annotations

import pytest

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.exchange.importers.template import (
    parse_answer_labels,
    parse_document,
    parse_options,
)


def one(doc: str) -> object:
    report = parse_document(doc)
    assert report.ok, f"预期解析成功，实际报错：{[str(e) for e in report.errors]}"
    assert len(report.drafts) == 1
    return report.drafts[0]


def errors_of(doc: str) -> list[str]:
    return [error.message for error in parse_document(doc).errors]


# ------------------------------------------------------------------ 选项
@pytest.mark.parametrize(
    "text",
    [
        "A. 甲\nB. 乙",
        "A、甲\nB、乙",
        "A) 甲\nB) 乙",
        "(A) 甲\n(B) 乙",
        "A 甲\nB 乙",
        "- A. 甲\n- B. 乙",
        "1. A. 甲\n2. B. 乙",
    ],
)
def test_option_label_styles(text: str) -> None:
    assert [o.label for o in parse_options(text)] == ["A", "B"]


@pytest.mark.parametrize(
    "text",
    [
        "A. 甲\n*B. 乙",
        "A. 甲\n**B. 乙**",
        "- [ ] A. 甲\n- [x] B. 乙",
        "A. 甲\nB. 乙（正确）",
        "A. 甲\nB. 乙 ✓",
        r"A. 甲\n\*B. 乙",  # pandoc 转义后的 *
    ],
)
def test_correct_marker_styles(text: str) -> None:
    options = parse_options(text.replace("\\n", "\n"))
    assert [o.label for o in options if o.marked_correct] == ["B"]


def test_bullet_star_is_not_a_correct_marker() -> None:
    """``* B.``（有空格）是列表项，``*B.``（紧贴）才是正确项标记。"""
    assert not any(o.marked_correct for o in parse_options("* A. 甲\n* B. 乙"))


def test_options_squeezed_onto_one_line() -> None:
    """从 PDF 粘贴、Markdown 经 docx 往返时换行会被吃掉。"""
    options = parse_options("A. 1024 B. 1023 C. 2048 D. 无法确定")
    assert [o.label for o in options] == ["A", "B", "C", "D"]
    assert options[0].content == "1024"


def test_option_continuation_line_is_appended() -> None:
    """选项内容里有公式块或长文本时会跨行。"""
    options = parse_options("A. 第一行\n续在第二行\nB. 乙")
    assert options[0].content == "第一行\n续在第二行"
    assert len(options) == 2


# ------------------------------------------------------------------ 答案
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("B", ["B"]),
        ("ACD", ["A", "C", "D"]),
        ("A、C、D", ["A", "C", "D"]),
        ("A,C,D", ["A", "C", "D"]),
        ("A C D", ["A", "C", "D"]),
        ("见解析", []),
    ],
)
def test_parse_answer_labels(text: str, expected: list[str]) -> None:
    assert parse_answer_labels(text, ["A", "B", "C", "D"]) == expected


def test_answer_labels_ignore_explanation_lines() -> None:
    """答案段后面常跟解释文字，里面的字母不是选项。"""
    assert parse_answer_labels("B\n因为 A 项的 n 是 1", ["A", "B"]) == ["B"]


# ------------------------------------------------------------------ 各题型
def test_single_choice() -> None:
    draft = one("""【题型】单选  【章节】第3章/3.2  【难度】较易  【分值】2
【标签】循环, 边界    【来源】2025 期末   【编号】x-1
【题干】甲
【选项】
A. 一
*B. 二
【答案】B
【解析】因为二
""")
    assert draft.type is QuestionType.SINGLE_CHOICE
    assert draft.chapter == "第3章/3.2"
    assert draft.difficulty is Difficulty.EASY
    assert draft.score == 2.0
    assert set(draft.tags) == {"循环", "边界"}
    assert draft.source == "2025 期末"
    assert draft.external_id == "x-1"
    assert draft.explanation == "因为二"
    assert [o.label for o in draft.payload.options if o.is_correct] == ["B"]


def test_multiple_choice() -> None:
    draft = one("【题型】多选\n【题干】甲\n【选项】\nA. 一\nB. 二\nC. 三\n【答案】AC\n")
    assert [o.label for o in draft.payload.options if o.is_correct] == ["A", "C"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("对", True), ("正确", True), ("√", True), ("错", False), ("错误", False), ("×", False)],
)
def test_true_false_answer_styles(answer: str, expected: bool) -> None:
    draft = one(f"【题型】判断\n【题干】甲\n【答案】{answer}\n")
    assert draft.payload.correct is expected


def test_fill_blank_numbered() -> None:
    draft = one("【题型】填空\n【题干】甲____乙____\n【答案】\n1. 一\n2. 二\n")
    assert [b.answers for b in draft.payload.blanks] == [("一",), ("二",)]


def test_fill_blank_semicolon_separated() -> None:
    draft = one("【题型】填空\n【题干】甲____乙____\n【答案】一；二\n")
    assert len(draft.payload.blanks) == 2


def test_fill_blank_keeps_comma_inside_one_answer() -> None:
    """「北京、上海」很可能是一个空的完整答案，不能按顿号拆开。"""
    draft = one("【题型】填空\n【题干】列举两个城市：____\n【答案】北京、上海\n")
    assert draft.payload.blanks[0].answers == ("北京、上海",)


def test_fill_blank_alternatives() -> None:
    draft = one("【题型】填空\n【题干】甲____\n【答案】北京/首都\n")
    assert set(draft.payload.blanks[0].answers) == {"北京", "首都"}


def test_programming() -> None:
    draft = one("""【题型】编程  【分值】20  【语言】python
【题干】实现排序
【函数签名】def f(a: list[int]) -> list[int]
【初始代码】
```python
def f(a):
    pass
```
【答案】见解析
""")
    assert draft.payload.language == "python"
    assert draft.payload.function_signature == "def f(a: list[int]) -> list[int]"
    assert "def f(a):" in draft.payload.starter_code


def test_comprehensive_with_sub_questions() -> None:
    draft = one("""【题型】综合应用  【分值】15  【章节】第7章 图
【题干】材料
【子题】简答 5分
【题干】第一问
【答案】甲
【子题】编程 10分
【题干】第二问
【答案】乙
【解析】因为乙
""")
    assert len(draft.sub_questions) == 2
    assert [s.type for s in draft.sub_questions] == [
        QuestionType.SHORT_ANSWER,
        QuestionType.PROGRAMMING,
    ]
    assert [s.score for s in draft.sub_questions] == [5.0, 10.0]
    assert draft.sub_questions[1].explanation == "因为乙"


def test_status_defaults_to_draft() -> None:
    """导入的题默认是草稿，需要人工复核后才启用。

    自动组卷只抽 ACTIVE，所以这个默认值保证了「刚导进来的题不会直接上考卷」。
    """
    draft = one("【题型】简答\n【题干】甲\n")
    assert draft.status is QuestionStatus.DRAFT


def test_status_can_be_set() -> None:
    draft = one("【题型】简答\n【题干】甲\n【状态】启用\n")
    assert draft.status is QuestionStatus.ACTIVE


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1", 1), ("3", 3), ("5", 5), ("很易", 1), ("较易", 2), ("中等", 3), ("较难", 4), ("很难", 5)],
)
def test_difficulty_styles(text: str, expected: int) -> None:
    assert int(one(f"【题型】简答\n【题干】甲\n【难度】{text}\n").difficulty) == expected


def test_unknown_difficulty_falls_back_to_medium() -> None:
    """难度写得离谱不该让整道题导不进来——它不是关键字段。"""
    assert int(one("【题型】简答\n【题干】甲\n【难度】超纲\n").difficulty) == 3


# ------------------------------------------------------------------ 错误
def test_answer_and_marker_conflict_is_reported() -> None:
    """这种不一致几乎总是源文件错了（改过答案忘了改加粗），不能悄悄处理。"""
    messages = errors_of("【题型】单选\n【题干】甲\n【选项】\nA. 一\n**B. 二**\n【答案】A\n")
    assert len(messages) == 1
    assert "不一致" in messages[0]
    assert "A" in messages[0] and "B" in messages[0]


def test_one_bad_question_does_not_block_the_rest() -> None:
    """一份文件 50 道题有 1 道格式不对，不该让另外 49 道也进不来。"""
    doc = (
        "【题型】单选\n【题干】好的\n【选项】\nA. 一\n*B. 二\n【答案】B\n\n"
        "【题型】绘图题\n【题干】坏的\n\n"
        "【题型】判断\n【题干】也是好的\n【答案】对\n"
    )
    report = parse_document(doc)
    assert len(report.drafts) == 2
    assert len(report.errors) == 1


@pytest.mark.parametrize(
    ("doc", "fragment"),
    [
        ("【题型】单选\n【选项】\nA. 一\n*B. 二\n", "缺少【题干】"),
        ("【题型】绘图题\n【题干】甲\n", "无法识别的题型"),
        ("【题型】单选\n【题干】甲\n【选项】\n【答案】A\n", "没有解析出任何选项"),
        ("【题型】单选\n【题干】甲\n【选项】\nA. 一\nB. 二\n【答案】见解析\n", "无法确定正确选项"),
        ("【题型】判断\n【题干】甲\n【答案】不一定\n", "无法识别为对或错"),
        ("【题型】填空\n【题干】甲____乙____丙____\n【答案】只有一个\n", "空位标记"),
        ("【题型】综合应用\n【题干】材料\n", "至少一个【子题】"),
        ("【题型】单选\n【题干】甲\n【选项】\nA. 一\nB. 二\n【答案】AB\n", "只能有 1 个正确选项"),
    ],
)
def test_error_messages_are_actionable(doc: str, fragment: str) -> None:
    messages = errors_of(doc)
    assert messages, f"预期报错却通过了：{doc!r}"
    assert any(fragment in message for message in messages), messages


def test_each_problem_reported_once() -> None:
    """同一个根因不能报两条——「没有解析出选项」后面跟「至少需要 2 个选项」是噪声。"""
    assert len(errors_of("【题型】单选\n【题干】甲\n【选项】\n【答案】A\n")) == 1


def test_errors_carry_line_numbers_and_excerpt() -> None:
    doc = "【题型】单选\n【题干】好的\n【选项】\nA. 一\n*B. 二\n【答案】B\n\n【题型】绘图题\n【题干】有问题的那道题\n"
    error = parse_document(doc).errors[0]
    assert error.line_number == 8
    assert "有问题的那道题" in str(error)
