"""题型 payload 与导入交换格式的校验模型。

这一层是**导入的第一道防线**：md / docx / html 三条解析路径最终都产出
:class:`QuestionDraft`，由 Pydantic 统一校验。校验在写库之前完成，所以
格式错误的题目永远进不了库。

每条规则都对应一类真实会犯的错（选项缺答案、答案标了两个、填空序号跳号、
正则写错…），因此错误信息要足够具体，让人能直接定位到源文件里改。
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qbank.core.enums import BlankMatch, Difficulty, QuestionStatus, QuestionType

#: 选项标号允许的写法：A / a / (A) / A. / A、 —— 归一化后只存裸字母
_LABEL_CLEAN = re.compile(r"^[(\[（]?([A-Za-z])[)\].、．:：]?$")

MAX_STEM_LENGTH = 20_000
MAX_OPTIONS = 26


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


# --------------------------------------------------------------------- 选择题
class ChoiceOption(_Base):
    """一个选项。``content`` 是 Markdown，可含公式、图片、代码块。"""

    label: str = Field(min_length=1, max_length=4, description="选项标号，如 A")
    content: str = Field(min_length=1, description="选项正文（Markdown）")
    is_correct: bool = False

    @field_validator("label", mode="before")
    @classmethod
    def _normalize_label(cls, value: object) -> object:
        """把 ``(A)``、``A.``、``a、`` 统一成 ``A``。"""
        if not isinstance(value, str):
            return value
        matched = _LABEL_CLEAN.match(value.strip())
        return matched.group(1).upper() if matched else value.strip()


class _ChoiceBase(_Base):
    options: tuple[ChoiceOption, ...]
    shuffle_ok: bool = Field(default=True, description="生成 A/B 卷时是否允许打乱选项顺序")

    @field_validator("options")
    @classmethod
    def _check_options(cls, options: tuple[ChoiceOption, ...]) -> tuple[ChoiceOption, ...]:
        if len(options) < 2:
            raise ValueError(f"选择题至少需要 2 个选项，当前 {len(options)} 个")
        if len(options) > MAX_OPTIONS:
            raise ValueError(f"选项不得超过 {MAX_OPTIONS} 个")
        labels = [o.label.upper() for o in options]
        duplicates = {label for label in labels if labels.count(label) > 1}
        if duplicates:
            raise ValueError(f"选项标号重复：{'、'.join(sorted(duplicates))}")
        return options

    @property
    def correct_labels(self) -> tuple[str, ...]:
        return tuple(o.label for o in self.options if o.is_correct)


class SingleChoicePayload(_ChoiceBase):
    type: Literal[QuestionType.SINGLE_CHOICE] = QuestionType.SINGLE_CHOICE

    @model_validator(mode="after")
    def _exactly_one_correct(self) -> SingleChoicePayload:
        correct = self.correct_labels
        if len(correct) != 1:
            got = "、".join(correct) if correct else "无"
            raise ValueError(f"单选题必须且只能有 1 个正确选项，当前标记为正确的是：{got}")
        return self


class MultipleChoicePayload(_ChoiceBase):
    type: Literal[QuestionType.MULTIPLE_CHOICE] = QuestionType.MULTIPLE_CHOICE

    @model_validator(mode="after")
    def _at_least_one_correct(self) -> MultipleChoicePayload:
        if not self.correct_labels:
            raise ValueError("多选题至少需要 1 个正确选项，当前没有任何选项被标记为正确")
        return self


# --------------------------------------------------------------------- 判断题
class TrueFalsePayload(_Base):
    type: Literal[QuestionType.TRUE_FALSE] = QuestionType.TRUE_FALSE
    correct: bool
    true_label: str = Field(default="正确", description="导出时「对」的字样")
    false_label: str = Field(default="错误", description="导出时「错」的字样")


# --------------------------------------------------------------------- 填空题
class Blank(_Base):
    """一个空。``index`` 从 1 开始，对应题干里第 N 个 ``____``。"""

    index: int = Field(ge=1)
    answers: tuple[str, ...] = Field(min_length=1, description="可接受的答案，任一命中即算对")
    match: BlankMatch = BlankMatch.EXACT
    tolerance: float = Field(default=0.0, ge=0.0, description="NUMERIC 模式下的允许误差")

    @model_validator(mode="after")
    def _check_match_mode(self) -> Blank:
        if self.match is BlankMatch.REGEX:
            for answer in self.answers:
                try:
                    re.compile(answer)
                except re.error as exc:
                    raise ValueError(
                        f"第 {self.index} 空的正则 {answer!r} 无法编译：{exc}"
                    ) from exc
        if self.match is BlankMatch.NUMERIC:
            for answer in self.answers:
                try:
                    float(answer)
                except ValueError as exc:
                    raise ValueError(
                        f"第 {self.index} 空标为数值判分，但答案 {answer!r} 不是数字"
                    ) from exc
        elif self.tolerance:
            raise ValueError(f"第 {self.index} 空的 tolerance 只在 NUMERIC 判分方式下有意义")
        return self


class FillBlankPayload(_Base):
    type: Literal[QuestionType.FILL_BLANK] = QuestionType.FILL_BLANK
    blanks: tuple[Blank, ...] = Field(min_length=1)

    @field_validator("blanks")
    @classmethod
    def _check_indices_contiguous(cls, blanks: tuple[Blank, ...]) -> tuple[Blank, ...]:
        indices = sorted(b.index for b in blanks)
        if len(set(indices)) != len(indices):
            raise ValueError(f"填空序号重复：{indices}")
        expected = list(range(1, len(indices) + 1))
        if indices != expected:
            raise ValueError(f"填空序号必须从 1 连续编号，期望 {expected}，实际 {indices}")
        return blanks


# --------------------------------------------------------------------- 简答题
class ShortAnswerPayload(_Base):
    type: Literal[QuestionType.SHORT_ANSWER] = QuestionType.SHORT_ANSWER
    key_points: tuple[str, ...] = Field(default=(), description="得分点，用于生成评分标准；可留空")


# ----------------------------------------------------------------- 综合应用题
class ComprehensivePayload(_Base):
    """综合题自身不存子题——子题是独立的 Question 行，通过 ``parent_id`` 关联。

    这样子题才能单独被检索、单独统计使用情况，也允许将来把某个子题提成独立题目。
    """

    type: Literal[QuestionType.COMPREHENSIVE] = QuestionType.COMPREHENSIVE
    material_label: str = Field(default="", description="材料标题，如「阅读下列程序」")


# --------------------------------------------------------------------- 编程题
class TestCase(_Base):
    name: str = ""
    input: str = ""
    expected: str = ""
    hidden: bool = Field(default=False, description="隐藏用例不随试卷导出给学生")


class ProgrammingPayload(_Base):
    """只存元数据与用例，**不做判题**——判题留给后续 Web 端。"""

    type: Literal[QuestionType.PROGRAMMING] = QuestionType.PROGRAMMING
    language: str = Field(default="", description="如 python / c / java；留空表示不限")
    function_signature: str = ""
    starter_code: str = ""
    test_cases: tuple[TestCase, ...] = ()
    time_limit_ms: int = Field(default=1000, gt=0)
    memory_limit_mb: int = Field(default=256, gt=0)


QuestionPayload = Annotated[
    Union[  # noqa: UP007 - 判别联合需要显式 Union，PEP 604 写法 pydantic 支持但可读性差
        SingleChoicePayload,
        MultipleChoicePayload,
        TrueFalsePayload,
        FillBlankPayload,
        ShortAnswerPayload,
        ComprehensivePayload,
        ProgrammingPayload,
    ],
    Field(discriminator="type"),
]

#: 题型 -> payload 模型，供「按题型造空 payload」与一致性校验使用
PAYLOAD_MODELS: dict[QuestionType, type[_Base]] = {
    QuestionType.SINGLE_CHOICE: SingleChoicePayload,
    QuestionType.MULTIPLE_CHOICE: MultipleChoicePayload,
    QuestionType.TRUE_FALSE: TrueFalsePayload,
    QuestionType.FILL_BLANK: FillBlankPayload,
    QuestionType.SHORT_ANSWER: ShortAnswerPayload,
    QuestionType.COMPREHENSIVE: ComprehensivePayload,
    QuestionType.PROGRAMMING: ProgrammingPayload,
}


# ----------------------------------------------------------- 导入/导出交换格式
class QuestionDraft(BaseModel):
    """一道待入库的题目。三条导入路径的统一产物，也是 JSON 导出的单元。

    ``chapter`` 用「人写的路径」而非 id，因为导入文件里不可能知道数据库 id；
    服务层负责按路径查找或自动创建章节。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: QuestionType
    stem: str = Field(min_length=1, max_length=MAX_STEM_LENGTH, description="题干（Markdown）")
    payload: QuestionPayload
    answer: str = Field(default="", description="参考答案（Markdown）")
    explanation: str = Field(default="", description="解析（Markdown）")
    scoring_rubric: str = Field(default="", description="评分标准（Markdown）")
    chapter: str = Field(default="", description="章节路径，如 `3.2 循环结构` 或 `第3章/3.2 循环`")
    difficulty: Difficulty = Difficulty.MEDIUM
    score: float = Field(default=0.0, ge=0, le=1000, description="默认分值；0 表示由组卷时决定")
    est_minutes: int = Field(default=0, ge=0, le=600, description="预计用时，0 表示未填")
    tags: tuple[str, ...] = ()
    source: str = Field(default="", description="来源，如「2025 期末」")
    status: QuestionStatus = QuestionStatus.DRAFT
    external_id: str = Field(
        default="", description="来源文件里的稳定标识；用于判断「同一道题」以支持重复导入"
    )
    sub_questions: tuple[QuestionDraft, ...] = ()

    @model_validator(mode="after")
    def _payload_matches_type(self) -> QuestionDraft:
        if self.payload.type != self.type:
            raise ValueError(f"题型为 {self.type.value}，但 payload 是 {self.payload.type.value}")
        return self

    @model_validator(mode="after")
    def _sub_questions_only_for_comprehensive(self) -> QuestionDraft:
        if self.sub_questions and not self.type.allows_sub_questions:
            raise ValueError(f"{self.type.label}不支持子题，只有综合应用题可以有子题")
        if self.type.allows_sub_questions and not self.sub_questions:
            raise ValueError("综合应用题必须至少有 1 个子题")
        for sub in self.sub_questions:
            if sub.sub_questions:
                raise ValueError("子题不能再嵌套子题（只支持两层）")
        return self

    @model_validator(mode="after")
    def _blank_count_matches_stem(self) -> QuestionDraft:
        """填空题的空数要和题干里的空位标记对得上。

        认 ``____``（≥2 个下划线）与 ``（ ）``/``( )`` 两种写法；题干里一个都
        没有时跳过检查（有人习惯把空写在答案里）。
        """
        if not isinstance(self.payload, FillBlankPayload):
            return self
        markers = len(re.findall(r"_{2,}|[（(]\s*[)）]", self.stem))
        if markers and markers != len(self.payload.blanks):
            raise ValueError(
                f"题干里有 {markers} 个空位标记，但提供了 {len(self.payload.blanks)} 个答案"
            )
        return self

    @property
    def total_score(self) -> float:
        """综合题的分值为子题之和（若自身未显式给分）。"""
        if self.sub_questions and not self.score:
            return sum(sub.score for sub in self.sub_questions)
        return self.score


QuestionDraft.model_rebuild()
