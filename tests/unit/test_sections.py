"""模板切块测试。

这一层把带标记的文本切成「题目块 → 小节」，是 md / docx / html 三条导入路径的
共同汇合点。它出错的后果很隐蔽：题目数量对不上、某个字段莫名为空，而用户看
源文件明明是对的。
"""

from __future__ import annotations

import pytest

from qbank.exchange.importers.sections import canonical_label, split_blocks

MARKER_DOC = """填写说明：每题以【题型】开头。

【题型】单选  【章节】第3章/3.2 循环  【难度】2  【分值】2
【标签】循环, 边界
【题干】下列代码的输出是？

```python
# 这里有 【假标记】、### 假标题、A. 假选项
n = 1
```
【选项】
A. 1024
*B. 1023
【答案】B
【解析】循环已退出。

【题型】判断  【难度】1
【题干】栈是先进先出的。
【答案】错
"""

FENCE_DOC = """## Q
```yaml
type: single_choice
chapter: 3.2 循环
difficulty: 2
tags: [循环]
```
### 题干
下列代码的输出是？
### 选项
- [ ] A. 1024
- [x] B. 1023
### 答案
B
"""


def test_marker_syntax_splits_into_questions() -> None:
    blocks = split_blocks(MARKER_DOC)
    assert len(blocks) == 2
    assert blocks[0].inline["type"] == "单选"
    assert blocks[1].inline["type"] == "判断"


def test_leading_prose_is_discarded() -> None:
    """模板文件开头的填写说明不该变成一道题。"""
    blocks = split_blocks(MARKER_DOC)
    assert "填写说明" not in blocks[0].get("stem")


def test_line_numbers_point_at_the_question_not_the_file_start() -> None:
    """行号是报错信息的全部价值所在——指错了用户就无从下手。"""
    blocks = split_blocks(MARKER_DOC)
    assert blocks[0].line_number == 3
    assert blocks[1].line_number == 17


def test_multiple_labels_on_one_line() -> None:
    inline = split_blocks(MARKER_DOC)[0].inline
    assert inline["chapter"] == "第3章/3.2 循环"
    assert inline["difficulty"] == "2"
    assert inline["score"] == "2"


def test_code_fence_contents_are_protected() -> None:
    """编程题题干里出现 ``【】``、``###``、``A.`` 都是正常内容。"""
    stem = split_blocks(MARKER_DOC)[0].get("stem")
    assert "【假标记】" in stem
    assert "### 假标题" in stem
    assert "A. 假选项" in stem
    # 而真正的 ``【选项】`` 仍然被识别为小节
    assert "1023" in split_blocks(MARKER_DOC)[0].get("options")


def test_fence_syntax_uses_yaml_metadata() -> None:
    blocks = split_blocks(FENCE_DOC)
    assert len(blocks) == 1
    assert blocks[0].meta["type"] == "single_choice"
    assert blocks[0].meta["difficulty"] == 2
    assert blocks[0].get("answer") == "B"


def test_both_syntaxes_can_be_mixed() -> None:
    blocks = split_blocks(FENCE_DOC + "\n" + MARKER_DOC)
    assert len(blocks) == 3


def test_sub_blocks_do_not_become_separate_questions() -> None:
    """子题的题型写在 ``【子题】`` 行上。

    如果子题里写 ``【题型】``，它会被当成新题目的开始——综合题丢掉全部子题，
    子题变成一堆独立的简答题。这正是 ``【子题】简答 5分`` 这种写法的来由。
    """
    doc = """【题型】综合应用
【题干】材料
【子题】简答 5分
【题干】第一问
【答案】甲
【子题】编程 10分
【题干】第二问
【答案】乙
"""
    blocks = split_blocks(doc)
    assert len(blocks) == 1, "子题被当成了独立题目"
    assert len(blocks[0].subs) == 2
    assert blocks[0].subs[0].header == "简答 5分"
    assert blocks[0].subs[0].get("stem") == "第一问"
    assert blocks[0].subs[1].get("answer") == "乙"


def test_horizontal_rule_separates_questions() -> None:
    doc = "【题型】单选\n【题干】甲\n\n---\n\n【题型】判断\n【题干】乙\n"
    assert len(split_blocks(doc)) == 2


def test_heading_separates_questions() -> None:
    doc = "## 第一题\n【题型】单选\n【题干】甲\n\n## 第二题\n【题型】判断\n【题干】乙\n"
    assert len(split_blocks(doc)) == 2


def test_mid_line_marker_terminates_previous_section() -> None:
    """换行被吃掉时（从 PDF 粘贴、Markdown 经 docx 往返）标记会挤在行中。

    不切开的话，``【解析】`` 会被当成答案内容的一部分。
    """
    doc = "【题型】填空\n【题干】甲____\n【答案】乙 【解析】因为乙\n"
    block = split_blocks(doc)[0]
    assert block.get("answer") == "乙"
    assert block.get("explanation") == "因为乙"


def test_trailing_multiline_label_takes_following_lines() -> None:
    """行末的 ``【答案】`` 后面没内容时，值在下面几行。"""
    doc = "【题型】填空  【题干】甲____乙____  【答案】\n1. 一\n2. 二\n"
    block = split_blocks(doc)[0]
    assert "1. 一" in block.get("answer")
    assert "2. 二" in block.get("answer")


def test_unknown_bracket_text_is_not_a_label() -> None:
    """正文里的 ``见【附录三】`` 不能被当成标记。"""
    doc = "【题型】简答\n【题干】见【附录三】的说明，回答问题。\n"
    block = split_blocks(doc)[0]
    assert "【附录三】" in block.get("stem")


def test_ascii_brackets_only_recognized_at_line_start() -> None:
    """``list[int]`` 不能被当成标记——否则函数签名会被截断。"""
    doc = "【题型】编程\n【题干】实现排序\n【函数签名】def f(a: list[int]) -> list[int]\n"
    block = split_blocks(doc)[0]
    assert block.get("signature") == "def f(a: list[int]) -> list[int]"


def test_ascii_brackets_at_line_start_are_labels() -> None:
    doc = "[题型]单选\n[题干]甲\n"
    assert split_blocks(doc)[0].inline["type"] == "单选"


def test_label_aliases() -> None:
    assert canonical_label("参考答案") == "answer"
    assert canonical_label("知识点") == "tags"
    assert canonical_label("Difficulty") == "difficulty"
    assert canonical_label("小问") == "sub"
    assert canonical_label("随便写的") is None


def test_empty_document_yields_nothing() -> None:
    assert split_blocks("") == []
    assert split_blocks("只有一段普通文字，没有任何标记。") == []


def test_crlf_is_normalized() -> None:
    doc = "【题型】单选\r\n【题干】甲\r\n【答案】A\r\n"
    assert split_blocks(doc)[0].get("answer") == "A"


# =========================================== 行首的题目序号
#
# 老师手里的历年真题是按题号排的，`1.` `2.` 是整理时的脚手架，不是题目信息。
# 不认它的话，`1.【题型】单选` 整行都不算标记行，报出来的是「缺少【题型】」
# ——而用户明明写了。


@pytest.mark.parametrize(
    "prefix",
    ["1.", "1、", "1)", "1）", "1．", "1:", "12. ", "(1)", "（1）", " 3. ", "一、", "二．"],
)
def test_serial_prefix_is_accepted_and_dropped(prefix: str) -> None:
    doc = f"{prefix}【题型】单选\n【题干】栈的特点是？\n"
    blocks = split_blocks(doc)
    assert len(blocks) == 1, f"{prefix!r} 开头的标记行没被认出来"
    assert blocks[0].inline["type"] == "单选"
    assert blocks[0].get("stem") == "栈的特点是？", "序号渗进题干了"


def test_serial_prefix_works_on_any_label_line() -> None:
    """序号出现在哪一行都一样丢掉，不只是题目起始行。"""
    doc = "1.【题型】单选\n2.【题干】栈的特点是？\n3.【答案】B\n"
    block = split_blocks(doc)[0]
    assert block.get("stem") == "栈的特点是？"
    assert block.get("answer") == "B"


def test_a_bare_number_is_not_a_serial() -> None:
    """必须带分隔符。光一个数字开头的行太容易是正文。"""
    doc = "【题型】简答\n【题干】见下表\n2024【附注】与题无关\n"
    block = split_blocks(doc)[0]
    assert "2024【附注】" in block.get("stem")


def test_serial_is_kept_when_the_label_is_unknown() -> None:
    """``3.【注】…`` 里的【注】不是已知标签，整行要原样留在正文里。

    序号只在标签被认出来时才真的被丢掉，否则就是在偷吃用户的正文。
    """
    doc = "【题型】简答\n【题干】说明两点：\n3.【注】这里的 n 是正整数\n"
    stem = split_blocks(doc)[0].get("stem")
    assert "3.【注】这里的 n 是正整数" in stem


def test_ordered_list_inside_a_stem_survives() -> None:
    """题干里的有序列表不该被当成题号。"""
    doc = "【题型】简答\n【题干】请回答：\n1. 什么是栈\n2. 什么是队列\n"
    stem = split_blocks(doc)[0].get("stem")
    assert "1. 什么是栈" in stem and "2. 什么是队列" in stem


def test_serial_inside_a_code_fence_is_untouched() -> None:
    doc = "【题型】编程\n【题干】实现\n```python\n1.【step】first\n```\n"
    stem = split_blocks(doc)[0].get("stem")
    assert "1.【step】first" in stem


def test_keeping_the_number_requires_an_explicit_label() -> None:
    """序号被丢掉；真想留在库里就写【题号】，它进 external_id。"""
    doc = "1.【题型】单选\n【题号】2024-期末-01\n【题干】甲\n"
    block = split_blocks(doc)[0]
    assert block.inline["external_id"] == "2024-期末-01"
