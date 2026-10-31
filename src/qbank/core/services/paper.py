"""试卷服务：手工选题与自动组卷的唯一写入口。

成卷时必须同时做三件事——冻结题面快照、按节配分、同步使用记录。漏掉任何一件
都会在几个月后才被发现：

* 漏了快照 → 改一道题，去年的卷子跟着变样
* 漏了配分 → 卷子总分对不上，打印出来才看见
* 漏了使用记录 → 「避开近 180 天用过的题」形同失效，同一道题年年考

所以 CLI、Web 路由都只能经由本服务改试卷，不许直接操作仓储。
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from qbank.core.blueprint import Blueprint
from qbank.core.models import Paper, PaperItem, Question, utcnow
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.schemas import ChoiceOption, QuestionDraft
from qbank.core.services.question import QuestionService
from qbank.core.services.selection import SelectionResult, SelectionService

#: 快照格式版本。卷子要存很多年，读快照的代码必须认得出旧格式。
SNAPSHOT_VERSION = 1

#: 纯标号答案：``B``、``ACD``、``A、C 和 D`` 之类，不含别的内容
_LABELS_ONLY = re.compile(r"^[A-Za-z\s,，、;；和与]+$")


class ShortageError(ValueError):
    """蓝图在当前题库上组不齐。

    默认**不**生成少几道题的卷子：静默降级意味着老师拿到一份 92 分的「100 分
    卷」而不知道。``result`` 带着完整诊断，调用方负责把它显示出来；确实接受
    缺额时要显式传 ``allow_partial=True``。
    """

    def __init__(self, result: SelectionResult) -> None:
        self.result = result
        super().__init__(result.report())


@dataclass(frozen=True, slots=True)
class AddOutcome:
    """一次手工加题的结果。"""

    added: tuple[str, ...]
    duplicates: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def summary(self) -> str:
        parts = [f"加入 {len(self.added)} 道"]
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} 道已在本卷中，跳过")
        if self.missing:
            parts.append(f"{len(self.missing)} 道已不存在，跳过")
        return "，".join(parts)


# ===================================================================== 快照
def freeze(draft: QuestionDraft, question: Question) -> dict[str, Any]:
    """把题面冻结成快照。

    存的是完整的 :class:`QuestionDraft`，而不是「题目 id + 版本号」：卷子要能在
    原题被删掉、被改得面目全非、甚至整个课程被删之后照样原样印出来。多占的那点
    空间换来的是「去年的卷子永远是去年那张」。
    """
    return {
        "version": SNAPSHOT_VERSION,
        "question_id": question.id,
        "revision": question.revision,
        "frozen_at": utcnow().isoformat(),
        "draft": draft.model_dump(mode="json"),
    }


def thaw(snapshot: dict[str, Any]) -> QuestionDraft:
    """从快照还原题面。

    容忍没有外层信封的裸 draft——快照格式将来可能再变，而读旧卷子永远不能
    报错：一份打不开的历史试卷等于丢了。
    """
    data = snapshot.get("draft", snapshot)
    return QuestionDraft.model_validate(data)


def display_options(draft: QuestionDraft, option_order: Sequence[str]) -> list[ChoiceOption]:
    """按展示顺序重排选项并重新编号。

    ``option_order`` 记的是**原始标号**的展示次序，不是新标号：``["C","A","D","B"]``
    的意思是「第一个位置放原来的 C」。重排后必须重新编号成 A/B/C/D——卷子上
    「C. …」排在第一个位置，学生只会以为印错了。``is_correct`` 跟着选项走，
    所以正确答案自然变成新的标号。

    >>> from qbank.core.schemas import SingleChoicePayload
    >>> payload = SingleChoicePayload(options=[
    ...     {"label": "A", "content": "甲"},
    ...     {"label": "B", "content": "乙", "is_correct": True},
    ... ])
    >>> draft = QuestionDraft(type="single_choice", stem="谁？", payload=payload, answer="B")
    >>> [(o.label, o.content, o.is_correct) for o in display_options(draft, ["B", "A"])]
    [('A', '乙', True), ('B', '甲', False)]
    """
    options: Sequence[ChoiceOption] = getattr(draft.payload, "options", ())
    if not options:
        return []
    if not option_order:
        return list(options)
    by_label = {option.label: option for option in options}
    # 快照里的标号与 option_order 对不上时（人工改过题目选项），宁可退回原序，
    # 也不要凭残缺的次序印出一份少了选项的卷子
    if set(option_order) != set(by_label):
        return list(options)
    return [
        option.model_copy(update={"label": options[position].label})
        for position, option in enumerate(by_label[label] for label in option_order)
    ]


def display_answer(draft: QuestionDraft, option_order: Sequence[str]) -> str:
    """乱序之后的参考答案。

    选项一打乱，原来写着「B」的答案就错了。这里只在答案**整体只是标号**时重算
    （``B``、``ACD``、``A、C 和 D`` 都算），因为这种情况下答案的全部信息就是
    标号本身，重算是安全的。答案里还写了别的内容（「B，因为栈是后进先出」）时
    保持原样——擅自改写一段话比留着一个需要人工核对的答案更危险。
    """
    if not option_order:
        return draft.answer
    shown = display_options(draft, option_order)
    if not shown or not _LABELS_ONLY.match(draft.answer or ""):
        return draft.answer
    return "".join(option.label for option in shown if option.is_correct)


class PaperService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.papers = PaperRepo(session)
        self.courses = CourseRepo(session)
        self.questions = QuestionService(session)
        self.selection = SelectionService(session)

    # ------------------------------------------------------- 自动组卷
    def assemble(
        self,
        course_id: str,
        blueprint: Blueprint,
        *,
        seed: int | None = None,
        exclude_ids: Sequence[str] = (),
        allow_partial: bool = False,
    ) -> tuple[Paper, SelectionResult]:
        """按蓝图组一份卷。

        组不齐时抛 :class:`ShortageError`，**不**生成缺斤短两的卷子。

        ``seed`` 覆盖蓝图里的种子。同一个库 + 同一份蓝图 + 同一个种子必然给出
        同一份卷子，所以「先预览、确认后再生成」不需要在服务端暂存任何东西：
        预览页把种子带回来，重跑一次就是同一份。
        """
        result = self.selection.select(course_id, blueprint, seed=seed, exclude_ids=exclude_ids)
        if not result.ok and not allow_partial:
            raise ShortageError(result)
        paper = self.papers.create(
            course_id,
            blueprint.title,
            meta=dict(blueprint.meta),
            template_key=blueprint.template_key,
            seed=result.seed,
            blueprint=blueprint.to_json(),
        )
        for selected in result.sections:
            section = self.papers.add_section(
                paper.id, selected.spec.title, instructions=selected.spec.instructions
            )
            for question in selected.questions:
                self._add_item(
                    section.id,
                    question,
                    score=selected.score_of(question),
                    shuffle_options=selected.spec.shuffle_options,
                    seed=result.seed,
                )
        self.papers.sync_usage(paper.id)
        return paper, result

    def variant(
        self,
        paper_id: str,
        *,
        title: str = "",
        seed: int | None = None,
        avoid_overlap: bool = True,
    ) -> tuple[Paper, SelectionResult]:
        """照同一份蓝图再出一套（A/B 卷）。

        ``avoid_overlap`` 为真时避开原卷用过的题——两套卷子考同一批学生时，
        重题就等于白出。
        """
        source = self.papers.get_detailed(paper_id)
        if not source.blueprint:
            raise ValueError(
                f"试卷「{source.title}」是手工组的，没有蓝图可照着再出一份。"
                "请先用蓝图自动组卷，或手工复制。"
            )
        blueprint = Blueprint.model_validate(source.blueprint)
        if title:
            blueprint = blueprint.model_copy(update={"title": title})
        exclude = self.papers.used_question_ids(paper_id) if avoid_overlap else set()
        return self.assemble(source.course_id, blueprint, seed=seed, exclude_ids=sorted(exclude))

    # ------------------------------------------------------- 手工组卷
    def create_empty(
        self,
        course_id: str,
        title: str,
        *,
        sections: Sequence[str] = (),
        meta: dict[str, str] | None = None,
    ) -> Paper:
        """建一份空白卷，之后从题目列表里逐道加题。"""
        self.courses.get(course_id)
        paper = self.papers.create(course_id, title, meta=meta or {})
        for section_title in sections:
            self.papers.add_section(paper.id, section_title)
        return paper

    def add_questions(
        self,
        paper_id: str,
        section_id: str,
        question_ids: Sequence[str],
        *,
        score: float | None = None,
    ) -> AddOutcome:
        """把选中的题加到某一大题下。

        同一道题在一份卷子里只能出现一次——重复加不报错，只跳过并告诉调用方
        跳了几道（手工选题时勾重了是常事，为此报错太烦人）。
        """
        section = self.papers.get_section(section_id)
        if section.paper_id != paper_id:
            raise ValueError("这个大题不属于该试卷")
        present = self.papers.used_question_ids(paper_id)
        loaded = self.questions.questions.get_many(question_ids)

        added: list[str] = []
        duplicates: list[str] = []
        missing: list[str] = []
        for question_id in question_ids:
            if question_id in present:
                duplicates.append(question_id)
                continue
            question = loaded.get(question_id)
            if question is None or question.is_deleted:
                missing.append(question_id)
                continue
            self._add_item(section_id, question, score=score)
            present.add(question_id)
            added.append(question_id)
        self.papers.sync_usage(paper_id)
        return AddOutcome(tuple(added), tuple(duplicates), tuple(missing))

    def _add_item(
        self,
        section_id: str,
        question: Question,
        *,
        score: float | None = None,
        shuffle_options: bool = False,
        seed: int | None = None,
    ) -> PaperItem:
        draft = self.questions.to_draft(question)
        return self.papers.add_item(
            section_id,
            question_id=question.id,
            snapshot=freeze(draft, question),
            score=score if score is not None else draft.total_score,
            option_order=self._option_order(draft, shuffle_options, seed, question.id),
        )

    @staticmethod
    def _option_order(
        draft: QuestionDraft, shuffle: bool, seed: int | None, question_id: str
    ) -> list[str]:
        """算出选项的展示顺序。空列表表示按原序。

        随机流按「种子 + 题目 id」单独起，**不**复用抽题用的那个：抽题过程消耗
        多少随机数取决于候选池大小，一旦复用，往题库里加一道无关的题就会让整卷
        的选项顺序全变——而「同一个种子出同一份卷」是写在说明书上的承诺。
        """
        options: Sequence[ChoiceOption] = getattr(draft.payload, "options", ())
        if not shuffle or not draft.type.can_shuffle_options or len(options) < 2:
            return []
        if not getattr(draft.payload, "shuffle_ok", False):
            return []
        labels = [option.label for option in options]
        random.Random(f"{seed}:{question_id}").shuffle(labels)
        return labels

    # ------------------------------------------------------- 编辑
    def remove_item(self, item_id: str) -> Paper:
        section = self.papers.get_item(item_id).section
        paper_id = section.paper_id
        self.papers.remove_item(item_id)
        self.papers.sync_usage(paper_id)
        return self.papers.get(paper_id)

    def move_item(self, item_id: str, offset: int) -> PaperItem:
        return self.papers.move_item(item_id, offset)

    def move_item_to_section(self, item_id: str, section_id: str) -> PaperItem:
        return self.papers.move_item_to_section(item_id, section_id)

    def set_item_score(self, item_id: str, score: float) -> PaperItem:
        if score < 0:
            raise ValueError("分值不能为负数")
        item = self.papers.get_item(item_id)
        item.score = score
        self.session.flush()
        return item

    def add_section(self, paper_id: str, title: str, *, instructions: str = "") -> Paper:
        self.papers.add_section(paper_id, title, instructions=instructions)
        return self.papers.get(paper_id)

    def update_section(
        self, section_id: str, *, title: str | None = None, instructions: str | None = None
    ) -> Any:
        section = self.papers.get_section(section_id)
        if title is not None:
            if not title.strip():
                raise ValueError("大题标题不能为空")
            section.title = title.strip()
        if instructions is not None:
            section.instructions = instructions.strip()
        self.session.flush()
        return section

    def remove_section(self, section_id: str) -> Paper:
        """删掉一个大题，连带它下面的题目项。使用记录随之更新。"""
        paper_id = self.papers.get_section(section_id).paper_id
        self.papers.remove_section(section_id)
        self.papers.sync_usage(paper_id)
        return self.papers.get(paper_id)

    def update_paper(
        self,
        paper_id: str,
        *,
        title: str | None = None,
        meta: dict[str, str] | None = None,
        template_key: str | None = None,
    ) -> Paper:
        paper = self.papers.get(paper_id)
        if title is not None:
            if not title.strip():
                raise ValueError("试卷标题不能为空")
            paper.title = title.strip()
        if meta is not None:
            paper.meta = meta
        if template_key is not None:
            paper.template_key = template_key
        self.session.flush()
        return paper

    def delete(self, paper_id: str) -> Paper:
        """移入回收站。

        使用记录保持原样——``exclude_used_within_days`` 的查询本身会跳过已删除
        的卷子，所以不必在这里清理，恢复卷子时也就不用再补回来。
        """
        return self.papers.soft_delete(paper_id)

    def restore(self, paper_id: str) -> Paper:
        return self.papers.restore(paper_id)

    def purge(self, paper_id: str) -> None:
        self.papers.purge(paper_id)
