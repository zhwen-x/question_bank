"""领域枚举。

这些值会落到数据库里，**改名即破坏兼容**。新增成员安全，重命名或删除成员
必须配一个 Alembic 数据迁移。
"""

from __future__ import annotations

from enum import Enum


class QuestionType(str, Enum):
    """题型。

    ``COMPREHENSIVE``（综合应用题）是唯一允许有子题的题型：子题通过
    ``Question.parent_id`` 自引用挂上去，父题的题干作为共享材料。
    """

    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    TRUE_FALSE = "true_false"
    FILL_BLANK = "fill_blank"
    SHORT_ANSWER = "short_answer"
    COMPREHENSIVE = "comprehensive"
    PROGRAMMING = "programming"

    @property
    def label(self) -> str:
        """中文名，用于界面与导出。"""
        return _TYPE_LABELS[self]

    @property
    def is_objective(self) -> bool:
        """客观题（答案唯一、可机器判分）。影响导出答案卷的排版方式。"""
        return self in _OBJECTIVE_TYPES

    @property
    def can_shuffle_options(self) -> bool:
        """是否支持选项乱序（生成 A/B 卷时用）。"""
        return self in {QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE}

    @property
    def allows_sub_questions(self) -> bool:
        return self is QuestionType.COMPREHENSIVE


_TYPE_LABELS: dict[QuestionType, str] = {
    QuestionType.SINGLE_CHOICE: "单项选择题",
    QuestionType.MULTIPLE_CHOICE: "多项选择题",
    QuestionType.TRUE_FALSE: "判断题",
    QuestionType.FILL_BLANK: "填空题",
    QuestionType.SHORT_ANSWER: "简答题",
    QuestionType.COMPREHENSIVE: "综合应用题",
    QuestionType.PROGRAMMING: "编程题",
}

_OBJECTIVE_TYPES = frozenset(
    {
        QuestionType.SINGLE_CHOICE,
        QuestionType.MULTIPLE_CHOICE,
        QuestionType.TRUE_FALSE,
        QuestionType.FILL_BLANK,
    }
)

#: 导入时接受的题型别名（老师在 Word 模板里手写的各种叫法）
TYPE_ALIASES: dict[str, QuestionType] = {
    "单选": QuestionType.SINGLE_CHOICE,
    "单选题": QuestionType.SINGLE_CHOICE,
    "单项选择": QuestionType.SINGLE_CHOICE,
    "单项选择题": QuestionType.SINGLE_CHOICE,
    "选择": QuestionType.SINGLE_CHOICE,
    "选择题": QuestionType.SINGLE_CHOICE,
    "多选": QuestionType.MULTIPLE_CHOICE,
    "多选题": QuestionType.MULTIPLE_CHOICE,
    "多项选择": QuestionType.MULTIPLE_CHOICE,
    "多项选择题": QuestionType.MULTIPLE_CHOICE,
    "不定项选择": QuestionType.MULTIPLE_CHOICE,
    "判断": QuestionType.TRUE_FALSE,
    "判断题": QuestionType.TRUE_FALSE,
    "是非题": QuestionType.TRUE_FALSE,
    "填空": QuestionType.FILL_BLANK,
    "填空题": QuestionType.FILL_BLANK,
    "简答": QuestionType.SHORT_ANSWER,
    "简答题": QuestionType.SHORT_ANSWER,
    "名词解释": QuestionType.SHORT_ANSWER,
    "问答题": QuestionType.SHORT_ANSWER,
    "论述题": QuestionType.SHORT_ANSWER,
    "计算题": QuestionType.SHORT_ANSWER,
    "综合": QuestionType.COMPREHENSIVE,
    "综合题": QuestionType.COMPREHENSIVE,
    "综合应用": QuestionType.COMPREHENSIVE,
    "综合应用题": QuestionType.COMPREHENSIVE,
    "应用题": QuestionType.COMPREHENSIVE,
    "材料题": QuestionType.COMPREHENSIVE,
    "编程": QuestionType.PROGRAMMING,
    "编程题": QuestionType.PROGRAMMING,
    "程序设计": QuestionType.PROGRAMMING,
    "程序设计题": QuestionType.PROGRAMMING,
    "程序填空": QuestionType.PROGRAMMING,
}


def parse_question_type(raw: str) -> QuestionType:
    """把用户写的题型文字解析成 :class:`QuestionType`。

    >>> parse_question_type("单选")
    <QuestionType.SINGLE_CHOICE: 'single_choice'>
    >>> parse_question_type(" 综合应用题 ")
    <QuestionType.COMPREHENSIVE: 'comprehensive'>
    >>> parse_question_type("single_choice")
    <QuestionType.SINGLE_CHOICE: 'single_choice'>
    """
    text = raw.strip()
    if not text:
        raise ValueError("题型不能为空")
    try:
        return QuestionType(text.lower())
    except ValueError:
        pass
    normalized = text.replace(" ", "").replace("　", "")
    if normalized in TYPE_ALIASES:
        return TYPE_ALIASES[normalized]
    # 只列规范中文名。别名表有四十多项，全列出来会把终端刷满，
    # 用户也不需要知道「材料题」也能写——他写错时想知道的是「该写什么」。
    known = "、".join(kind.label for kind in QuestionType)
    raise ValueError(f"无法识别的题型「{raw.strip()}」；请写成：{known}")


class QuestionStatus(str, Enum):
    """题目生命周期。

    自动组卷**只抽 ACTIVE**；DRAFT 用于导入后待复核，RETIRED 用于「不再使用
    但要留着做历史追溯」（比如教材改版后作废的题）。
    """

    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"

    @property
    def label(self) -> str:
        return {"draft": "草稿", "active": "启用", "retired": "停用"}[self.value]


class Difficulty(int, Enum):
    """难度五档。用 int 以便直接参与区间筛选与分布配平。"""

    VERY_EASY = 1
    EASY = 2
    MEDIUM = 3
    HARD = 4
    VERY_HARD = 5

    @property
    def label(self) -> str:
        return {1: "很易", 2: "较易", 3: "中等", 4: "较难", 5: "很难"}[self.value]


class TagCategory(str, Enum):
    """标签分类。

    不分类的话标签很快就会膨胀成几百个平铺的词，筛选器没法用。
    """

    KNOWLEDGE = "knowledge"
    ABILITY = "ability"
    SOURCE = "source"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            "knowledge": "知识点",
            "ability": "能力",
            "source": "来源",
            "other": "其他",
        }[self.value]


class BlankMatch(str, Enum):
    """填空题判分方式（当前只存，判分留给 Web 端）。"""

    EXACT = "exact"
    IGNORE_CASE = "ignore_case"
    REGEX = "regex"
    NUMERIC = "numeric"


class ConflictPolicy(str, Enum):
    """导入时遇到已存在题目的处理策略。"""

    SKIP = "skip"
    OVERWRITE = "overwrite"
    NEW_REVISION = "new_revision"
    CREATE_COPY = "create_copy"

    @property
    def label(self) -> str:
        return {
            "skip": "跳过",
            "overwrite": "覆盖",
            "new_revision": "建新版本",
            "create_copy": "另存为新题",
        }[self.value]
