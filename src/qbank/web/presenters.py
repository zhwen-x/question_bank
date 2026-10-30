"""视图模型：把 ORM 对象整理成模板直接能用的形状。

放在单独一层的原因是**渲染题目内容是有成本的**（Markdown 解析 + LaTeX→MathML），
列表页一次要处理几十道题。这里集中决定「列表页渲染多少、详情页渲染多少」，
而不是让模板里散落一堆渲染调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from qbank.core.content.render import MathMode, RenderOptions, render_markdown
from qbank.core.models import Question
from qbank.core.schemas import QuestionDraft

#: 列表行里题干摘要的字符上限
EXCERPT_LIMIT = 220

#: 段落分隔（一个或多个空行）
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")


def web_render_options(asset_resolver: Any) -> RenderOptions:
    """页面渲染用的选项。

    公式走 MathML：服务端转换、浏览器原生渲染，不需要 KaTeX 的 JS 与字体文件，
    界面因此完全离线可用。
    """
    return RenderOptions(math=MathMode.MATHML, asset_resolver=asset_resolver)


def stem_excerpt(markdown: str, limit: int = EXCERPT_LIMIT) -> str:
    """取题干的开头用于列表显示。

    按**段落**截断而不是按字符数硬截：从中间切断会留下未闭合的 ``$``、未闭合的
    代码围栏，渲染出来是一团乱码。第一段在实践中既短又完整，正好够认出是哪道题。
    超长的单段（比如整段代码）才会退回按字符截断，并补上围栏闭合。
    """
    text = markdown.strip()
    if not text:
        return ""
    first = _PARAGRAPH_BREAK.split(text, maxsplit=1)[0].strip()
    if len(first) <= limit:
        return first
    clipped = first[:limit].rstrip() + "…"
    # 硬截可能把围栏或公式切开，补回闭合符号，避免渲染结果溢出到后面的内容
    if clipped.count("```") % 2:
        clipped += "\n```"
    if clipped.count("$$") % 2:
        clipped += "$$"
    elif clipped.replace("$$", "").count("$") % 2:
        clipped += "$"
    return clipped


@dataclass(slots=True)
class QuestionRow:
    """列表页的一行。"""

    question: Question
    excerpt_html: str

    @property
    def id(self) -> str:
        return self.question.id


@dataclass(slots=True)
class OptionView:
    label: str
    content_html: str
    is_correct: bool


@dataclass(slots=True)
class BlankView:
    index: int
    answers: tuple[str, ...]
    match: str


@dataclass(slots=True)
class SubQuestionView:
    index: int
    type_label: str
    score: float
    stem_html: str
    answer_html: str
    explanation_html: str


@dataclass(slots=True)
class QuestionView:
    """详情页的视图模型。"""

    question: Question
    draft: QuestionDraft
    stem_html: str
    answer_html: str = ""
    explanation_html: str = ""
    rubric_html: str = ""
    options: list[OptionView] = field(default_factory=list)
    blanks: list[BlankView] = field(default_factory=list)
    sub_questions: list[SubQuestionView] = field(default_factory=list)
    true_false: bool | None = None
    programming: dict[str, Any] | None = None
    key_points: tuple[str, ...] = ()

    @property
    def total_score(self) -> float:
        if self.sub_questions:
            return sum(sub.score for sub in self.sub_questions) or self.question.score
        return self.question.score


def build_rows(questions: list[Question], options: RenderOptions) -> list[QuestionRow]:
    return [
        QuestionRow(
            question=question,
            excerpt_html=render_markdown(stem_excerpt(question.stem), options),
        )
        for question in questions
    ]


def build_view(question: Question, draft: QuestionDraft, options: RenderOptions) -> QuestionView:
    """详情页视图模型。按题型取出各自的 payload 字段。"""

    def html(markdown: str) -> str:
        return render_markdown(markdown, options) if markdown else ""

    view = QuestionView(
        question=question,
        draft=draft,
        stem_html=html(draft.stem),
        answer_html=html(draft.answer),
        explanation_html=html(draft.explanation),
        rubric_html=html(draft.scoring_rubric),
    )

    payload: Any = draft.payload
    choices = getattr(payload, "options", None)
    if choices:
        view.options = [
            OptionView(
                label=choice.label,
                content_html=html(choice.content),
                is_correct=choice.is_correct,
            )
            for choice in choices
        ]
    blanks = getattr(payload, "blanks", None)
    if blanks:
        view.blanks = [
            BlankView(index=blank.index, answers=blank.answers, match=blank.match.value)
            for blank in blanks
        ]
    if hasattr(payload, "correct"):
        view.true_false = bool(payload.correct)
    if hasattr(payload, "language"):
        view.programming = {
            "language": payload.language,
            "signature": payload.function_signature,
            "starter_code": payload.starter_code,
            "test_cases": payload.test_cases,
            "time_limit_ms": payload.time_limit_ms,
            "memory_limit_mb": payload.memory_limit_mb,
        }
    if hasattr(payload, "key_points"):
        view.key_points = payload.key_points

    view.sub_questions = [
        SubQuestionView(
            index=index,
            type_label=sub.type.label,
            score=sub.score,
            stem_html=html(sub.stem),
            answer_html=html(sub.answer),
            explanation_html=html(sub.explanation),
        )
        for index, sub in enumerate(draft.sub_questions, start=1)
    ]
    return view


# ============================================================== 导入预览
@dataclass(slots=True)
class DuplicateView:
    """一条查重结果在界面上的样子。"""

    question_id: str
    label: str
    score: float
    exact: bool
    existing_excerpt: str
    diff: list[Any]
    change_count: int
    tiny: bool

    @property
    def needs_attention(self) -> bool:
        """差异只有几个字时要额外提醒。

        这种情况最危险：肉眼几乎看不出来，却很可能正是「正确↔错误」这类把
        题意反过来的改动——而相似度反而会很高，容易让人直接当成重复跳过。
        """
        return not self.exact and self.tiny


@dataclass(slots=True)
class PlannedView:
    """导入预览里的一行。"""

    index: int
    draft: Any
    excerpt_html: str
    action: str
    image_count: int
    duplicates: list[DuplicateView]

    @property
    def exact_duplicate(self) -> DuplicateView | None:
        return next((item for item in self.duplicates if item.exact), None)

    @property
    def similar(self) -> list[DuplicateView]:
        return [item for item in self.duplicates if not item.exact]

    @property
    def needs_attention(self) -> bool:
        return any(item.needs_attention for item in self.duplicates)

    @property
    def default_selected(self) -> bool:
        """默认是否勾选。

        完全重复的默认**不勾**——重复导入同一份文件是最常见的误操作，
        默认不勾可以让「全部确认」这个动作保持安全。相似的仍然默认勾选，
        因为相似很可能就是另一道题。
        """
        return self.exact_duplicate is None
