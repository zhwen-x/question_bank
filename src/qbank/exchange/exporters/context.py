"""试卷 → 渲染上下文。四种导出格式共用同一份数据。

    Paper ─→ PaperContext ─┬→ md 模板 ──→ Markdown ─┬→ .md
                           │                        └→ pandoc ──→ .docx
                           └→ html 模板 → HTML ─────┬→ .html（自包含）
                                                    └→ Chromium → .pdf

**上下文里存的一律是 Markdown 原文，不是渲染好的 HTML。** md 与 docx 直接用，
html 与 pdf 在模板里过一个 ``md`` 过滤器。这样「题号怎么排、分值印在哪、答案
放哪一段」只在模板里定义一次——否则迟早出现「Word 版和 PDF 版题号不一样」这种
谁也说不清是哪边错了的问题。

**渲染的是试卷快照，不是题库里的当前版本。** 去年的卷子导出来就该是去年那张。

**刻意不写「导出时间」。** 同一份试卷导出两次应当字节一致：这让「改了模板之后
重新导出，diff 一下看改了什么」成为可能，也让导出能进测试。要时间戳的话，
试卷自己的创建时间比导出时间更有意义。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from qbank.core.content.render import AssetResolver, rewrite_asset_links
from qbank.core.enums import QuestionType
from qbank.core.models import Paper, PaperItem
from qbank.core.schemas import QuestionDraft
from qbank.core.services.paper import display_answer, display_options, thaw

#: 填空题答案的序号圈字符。超过这个数量就退回 ``(N)``。
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


class Layout(str, Enum):
    """导出什么。

    三种都是真实需求，而且不是「一个开关」能表达的：发给学生的卷子不能有答案，
    老师手里的那份要题目和答案挨着看，而贴在考场外的答案卷只要答案、不要题干。
    """

    #: 只有题目。发给学生的那一份
    PAPER = "paper"
    #: 题目 + 每题的答案与解析。教师用
    FULL = "full"
    #: 只有答案与评分标准。客观题排成紧凑表格
    ANSWERS = "answers"

    @property
    def label(self) -> str:
        return {"paper": "试卷", "full": "试卷（含答案）", "answers": "答案与评分标准"}[self.value]

    @property
    def shows_answers(self) -> bool:
        return self is not Layout.PAPER

    @property
    def shows_questions(self) -> bool:
        return self is not Layout.ANSWERS


@dataclass(frozen=True, slots=True)
class OptionContext:
    label: str
    content: str
    is_correct: bool


@dataclass(frozen=True, slots=True)
class SubQuestionContext:
    index: int
    type_label: str
    score: float
    stem: str
    answer: str
    explanation: str
    rubric: str

    @property
    def label(self) -> str:
        """小问编号。用「(1)」而不是「1.」，和大题的题号区分开。"""
        return f"({self.index})"


@dataclass(frozen=True, slots=True)
class ProgrammingContext:
    language: str
    function_signature: str
    starter_code: str
    #: 只含非隐藏用例——隐藏用例不随试卷发给学生
    test_cases: tuple[Mapping[str, str], ...]


@dataclass(frozen=True, slots=True)
class ItemContext:
    """试卷上的一道题。"""

    #: 全卷连续题号。中文试卷通常连续编号，分节只用「一、二、三」分组
    number: int
    #: 本节内的序号，从 1 开始。模板想按节编号时用这个
    index: int
    score: float
    type_label: str
    is_objective: bool
    difficulty: int
    stem: str
    options: tuple[OptionContext, ...] = ()
    answer: str = ""
    explanation: str = ""
    rubric: str = ""
    sub_questions: tuple[SubQuestionContext, ...] = ()
    programming: ProgrammingContext | None = None
    #: 成卷时打乱过选项顺序（标号已重排，答案已跟着换）
    shuffled: bool = False

    @property
    def compact_answer(self) -> str:
        """客观题的一行式答案，用于答案卷的紧凑表格。

        主观题返回空串——它们的答案是一段话，塞进表格里没法看，模板应当转去
        逐题列出。
        """
        return self.answer if self.is_objective else ""

    @property
    def has_answer(self) -> bool:
        return bool(self.answer or self.rubric or any(s.answer for s in self.sub_questions))


@dataclass(frozen=True, slots=True)
class SectionContext:
    """试卷的一个大题。"""

    number: int
    title: str
    instructions: str
    items: tuple[ItemContext, ...]

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def total_score(self) -> float:
        return sum(item.score for item in self.items)

    @property
    def objective_items(self) -> tuple[ItemContext, ...]:
        return tuple(item for item in self.items if item.compact_answer)

    @property
    def subjective_items(self) -> tuple[ItemContext, ...]:
        return tuple(item for item in self.items if not item.compact_answer)


@dataclass(frozen=True, slots=True)
class PaperContext:
    """一份试卷的全部可印内容。模板只能看到这个对象。"""

    title: str
    layout: Layout
    sections: tuple[SectionContext, ...]
    meta: Mapping[str, str] = field(default_factory=dict)
    created_at: datetime | None = None
    seed: int | None = None
    #: 缺图的数量。模板用它在卷头提醒，免得印出来才发现
    missing_assets: int = 0

    @property
    def total_score(self) -> float:
        return sum(section.total_score for section in self.sections)

    @property
    def question_count(self) -> int:
        return sum(section.count for section in self.sections)

    @property
    def show_answers(self) -> bool:
        return self.layout.shows_answers

    @property
    def show_questions(self) -> bool:
        return self.layout.shows_questions

    @property
    def zero_score_items(self) -> int:
        return sum(1 for s in self.sections for item in s.items if not item.score)


#: 大题序号的中文数字。超过这些就用阿拉伯数字。
_CHINESE_NUMERALS = "一二三四五六七八九十"


def chinese_number(value: int) -> str:
    """1 → 一，11 → 11。

    >>> chinese_number(3), chinese_number(10), chinese_number(11)
    ('三', '十', '11')
    """
    if 1 <= value <= len(_CHINESE_NUMERALS):
        return _CHINESE_NUMERALS[value - 1]
    return str(value)


def _blank_answers(payload: Any) -> str:
    """填空题的紧凑答案：``①后进先出 ②O(n)``。

    每空只取第一个可接受答案——答案卷上列出全部同义写法会把表格撑爆，而评分
    时真正要看的是标准答案；其余写法在「含答案」版面里完整列出。
    """
    blanks = sorted(getattr(payload, "blanks", ()), key=lambda b: b.index)
    if len(blanks) == 1:
        # 只有一个空就不编号了：「①后进先出」里那个圈号不传达任何信息
        return str(blanks[0].answers[0])
    parts: list[str] = []
    for position, blank in enumerate(blanks):
        marker = _CIRCLED[position] if position < len(_CIRCLED) else f"({position + 1})"
        parts.append(f"{marker}{blank.answers[0]}")
    return " ".join(parts)


def _objective_answer(draft: QuestionDraft, option_order: Sequence[str]) -> str:
    """客观题的标准答案，已考虑选项乱序。"""
    if draft.type in {QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE}:
        shown = display_options(draft, option_order)
        labels = "".join(option.label for option in shown if option.is_correct)
        # 选项结构缺失时退回答案原文，总比印一个空白强
        return labels or display_answer(draft, option_order)
    if draft.type is QuestionType.TRUE_FALSE:
        payload: Any = draft.payload
        return str(payload.true_label if payload.correct else payload.false_label)
    if draft.type is QuestionType.FILL_BLANK:
        return _blank_answers(draft.payload) or draft.answer
    return draft.answer


def _programming(payload: Any) -> ProgrammingContext | None:
    if not hasattr(payload, "language"):
        return None
    return ProgrammingContext(
        language=payload.language,
        function_signature=payload.function_signature,
        starter_code=payload.starter_code,
        test_cases=tuple(
            {"name": case.name, "input": case.input, "expected": case.expected}
            for case in payload.test_cases
            if not case.hidden
        ),
    )


class _Rewriter:
    """按目标形式重写 ``asset://``，并数出有多少张图找不到。"""

    def __init__(self, resolver: AssetResolver, missing: str) -> None:
        self.resolver = resolver
        self.missing = missing
        self.misses = 0

    def __call__(self, text: str) -> str:
        if not text:
            return text

        def resolve(sha: str) -> str | None:
            target = self.resolver(sha)
            if target is None:
                self.misses += 1
            return target

        return rewrite_asset_links(text, resolve, missing=self.missing)


def build_context(
    paper: Paper,
    *,
    layout: Layout = Layout.PAPER,
    asset_resolver: AssetResolver,
    missing_asset_text: str = "［图片缺失］",
) -> PaperContext:
    """试卷 → 渲染上下文。

    ``asset_resolver`` 决定图片引用被换成什么：md 导出给相对路径、docx 给绝对
    路径（pandoc 按路径找图，找不到时**不报错**，只静默输出一个没有图的文档）、
    自包含 HTML 给 ``data:`` 内联。
    """
    rewrite = _Rewriter(asset_resolver, missing_asset_text)
    sections: list[SectionContext] = []
    number = 0
    for section_index, section in enumerate(paper.sections, start=1):
        items: list[ItemContext] = []
        for index, item in enumerate(section.items, start=1):
            number += 1
            items.append(_build_item(item, number=number, index=index, rewrite=rewrite))
        sections.append(
            SectionContext(
                number=section_index,
                title=section.title,
                instructions=section.instructions,
                items=tuple(items),
            )
        )
    return PaperContext(
        title=paper.title,
        layout=layout,
        sections=tuple(sections),
        meta=dict(paper.meta),
        created_at=paper.created_at,
        seed=paper.seed,
        missing_assets=rewrite.misses,
    )


def _build_item(item: PaperItem, *, number: int, index: int, rewrite: _Rewriter) -> ItemContext:
    draft = thaw(item.snapshot)
    order = item.option_order
    return ItemContext(
        number=number,
        index=index,
        score=item.score,
        type_label=draft.type.label,
        is_objective=draft.type.is_objective,
        difficulty=int(draft.difficulty),
        stem=rewrite(draft.stem),
        options=tuple(
            OptionContext(
                label=option.label,
                content=rewrite(option.content),
                is_correct=option.is_correct,
            )
            for option in display_options(draft, order)
        ),
        answer=rewrite(_objective_answer(draft, order)),
        explanation=rewrite(draft.explanation),
        rubric=rewrite(draft.scoring_rubric),
        sub_questions=tuple(
            SubQuestionContext(
                index=position,
                type_label=sub.type.label,
                score=sub.score,
                stem=rewrite(sub.stem),
                answer=rewrite(sub.answer),
                explanation=rewrite(sub.explanation),
                rubric=rewrite(sub.scoring_rubric),
            )
            for position, sub in enumerate(draft.sub_questions, start=1)
        ),
        programming=_programming(draft.payload),
        shuffled=bool(order),
    )
