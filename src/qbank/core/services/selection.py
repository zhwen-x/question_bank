"""自动组卷：按蓝图抽题，抽不出来时说清楚为什么抽不出来。

为什么不是「分层抽样 + 贪心」
    一道题只能用一次，而各节的候选池是**重叠的**（第一节要「难度 3 的单选」、
    第三节要「第 2 章的单选」，同一道题两边都够格）。于是这不是几次独立抽样，
    而是一个「谁占用谁」的分配问题。贪心能解开绝大多数情况，但解不开的时候
    它分不清两件事：

    * 库里确实没这么多题（蓝图要改）
    * 库里够，只是先抽的那几道正好把后面需要的题占掉了（抽法要改）

    把后者报成「题库不够」，和静默降级一样糟——老师会以为自己得去出新题，
    而实际上只要换个抽法就有。

    所以这里把它当二分图匹配精确求解（增广路，Kuhn 算法）：把「第 N 节难度 D
    要 3 道」摊成 3 个空位，空位与够格的题连边，求最大匹配。匹配满了就是真能
    组出来，匹配不满就是**真的**凑不齐，不存在「算法没找到」这种中间状态。

缺额诊断是精确的，不是猜的
    某个空位增广失败时，那次深搜访问过的题目集合恰好就是它所触及的全部可用
    题（``N(S)``），而访问过的抽题单元集合就是 ``S``。按 Hall 定理，
    ``|N(S)| < S 需要的空位数`` 必然成立——即这组单元合起来真的凑不齐。于是
    报告可以直接说「第 1 节难度 3 与第 3 节难度 3 合计需要 8 道，但两边够格
    的题加起来只有 5 道」，而不是含糊地说一句「组卷失败」。

可复现是硬要求
    同一个库 + 同一份蓝图 + 同一个 seed 必须给出同一份卷子。这要求候选池的
    顺序本身是确定的：SQL 不保证没有 ``ORDER BY`` 时的行序，所以候选池一律按
    id 排序后再用种子打乱（见 :meth:`QuestionRepo.ids`）。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy.orm import Session

from qbank.core.blueprint import Blueprint, SectionSpec
from qbank.core.enums import Difficulty
from qbank.core.models import Question
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.repo.tag import TagRepo

#: 随机种子的取值范围。够大以免撞车，又短到能让人抄在纸上复现一份卷子。
SEED_MAX = 10**9


class BlueprintError(ValueError):
    """蓝图本身有问题：章节或标签在本课程里不存在。

    这类问题**必须报错而不是抽不到题**。章节名写错时，筛选条件会悄悄变成
    「没有任何题符合」，表现出来是「题库不够」——老师会去翻题库，而真正的
    问题是蓝图里多打了一个字。
    """


@dataclass(frozen=True, slots=True)
class Stratum:
    """一个抽题单元：某一节里某一难度档要抽几道。

    难度配比就是靠拆成多个单元实现的：「10 道，易 2 中 5 难 3」会变成三个
    单元，彼此共享同一张候选表但各有配额。
    """

    section_index: int
    section_title: str
    difficulty: Difficulty | None
    needed: int
    #: 候选题 id，已按 id 排序后用种子打乱
    pool: tuple[str, ...]
    #: 产生这个池子的筛选条件，缺额时用来做「放宽哪一条能多出几道」的探测
    criteria: QuestionFilter | None = None

    @property
    def label(self) -> str:
        """诊断报告里对这个单元的称呼。"""
        if self.difficulty is None:
            return self.section_title
        return f"{self.section_title}·难度{self.difficulty.value}（{self.difficulty.label}）"


@dataclass(frozen=True, slots=True)
class Shortage:
    """一个没凑齐的抽题单元，以及它为什么没凑齐。

    **一个缺额对应一个单元**，不是对应一组。早先的写法是按 Hall 违反集合报告，
    并把「被别的集合包含」的那些丢掉——结果出现过这种报告：概览说「第二节
    3/4 道」，底下的原因里却只提第一节。缺了一道却没人解释为什么，比报得啰嗦
    糟糕得多。

    ``competing`` 非空时说明它单看够、但和别的单元**抢同一批题**：这是最容易
    让人误判的情形（去翻题库会发现「明明有」），所以必须点名。
    """

    label: str
    needed: int
    #: 实际排进去几道
    filled: int
    #: 本单元（连同竞争者）总共能动用的题数
    available: int
    #: 和它抢同一批题的其他单元
    competing: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()

    @property
    def missing(self) -> int:
        return self.needed - self.filled

    def describe(self) -> str:
        """一句话说清缺口。界面与命令行共用这一句，避免两处措辞不一致。"""
        head = f"{self.label}需要 {self.needed} 道，只排进 {self.filled} 道"
        if not self.competing:
            return f"{head}——库内符合条件的只有 {self.available} 道"
        joined = "、".join(f"「{label}」" for label in self.competing)
        return (
            f"{head}——它的候选和{joined}是同一批题，"
            f"而这批题总共只有 {self.available} 道，不够几节分"
        )


@dataclass(frozen=True, slots=True)
class SelectedSection:
    """抽好的一个大题。"""

    index: int
    spec: SectionSpec
    questions: tuple[Question, ...]
    #: 每道题来自哪一难度档，与 ``questions`` 一一对应
    strata: tuple[Difficulty | None, ...] = ()

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def filled(self) -> int:
        return len(self.questions)

    def score_of(self, question: Question) -> float:
        """本节里这道题值多少分。

        ``score_each`` 优先；没写就用题目自带的分值，而综合题自带分值常常是 0
        （分都记在小问上），这时用小问之和。
        """
        if self.spec.score_each is not None:
            return self.spec.score_each
        if question.score:
            return question.score
        return sum(sub.score for sub in question.sub_questions)


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """一次组卷尝试的结果。

    **``ok`` 为假时不要据此建试卷**：调用方要么把缺额报给用户去改蓝图，要么
    明确选择「就按缺额生成」。悄悄按少几道题生成，正是这个设计要避免的事。
    """

    sections: tuple[SelectedSection, ...]
    shortages: tuple[Shortage, ...]
    seed: int
    strata: tuple[Stratum, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        return not self.shortages

    @property
    def question_count(self) -> int:
        return sum(section.filled for section in self.sections)

    @property
    def total_score(self) -> float:
        return sum(
            section.score_of(question)
            for section in self.sections
            for question in section.questions
        )

    @property
    def zero_score_count(self) -> int:
        """配分为 0 的题数。

        不算错误（蓝图可以有意让题目自带分值），但一份有 0 分题的卷子几乎总是
        哪里漏填了，所以要让界面能提醒。
        """
        return sum(
            1
            for section in self.sections
            for question in section.questions
            if section.score_of(question) == 0
        )

    def report(self) -> str:
        """多行诊断报告，命令行与界面共用。"""
        lines = [
            f"共 {self.question_count} 道、{self.total_score:g} 分，随机种子 {self.seed}",
        ]
        for section in self.sections:
            want = section.spec.count
            mark = "" if section.filled == want else f"（缺 {want - section.filled} 道）"
            lines.append(f"  {section.title}：{section.filled}/{want} 道{mark}")
        if self.shortages:
            lines.append("")
            lines.append("组不齐，原因如下：")
            for shortage in self.shortages:
                lines.append(f"  · {shortage.describe()}")
                for hint in shortage.hints:
                    lines.append(f"      {hint}")
        return "\n".join(lines)


def solve(strata: Sequence[Stratum]) -> tuple[tuple[tuple[str, ...], ...], tuple[Shortage, ...]]:
    """把各单元的配额分配到具体题目上，并精确算出凑不齐的部分。

    纯函数：候选池的顺序由调用方决定（已打乱），这里不碰随机数，因此同样的
    输入必然给出同样的输出，可以脱离数据库单独测试。

    返回 ``(每个单元分到的题 id, 缺额)``。

    >>> a = Stratum(0, "一、单选", None, 2, ("q1", "q2", "q3"))
    >>> solve([a])
    ((('q1', 'q2'),), ())

    两个单元抢同一批题时，缺额要点明是谁在抢，而不是让人去翻题库：

    >>> b = Stratum(0, "一、单选", None, 2, ("q1", "q2"))
    >>> c = Stratum(1, "二、多选", None, 1, ("q1", "q2"))
    >>> _, shortages = solve([b, c])
    >>> shortages[0].label, shortages[0].competing, shortages[0].available
    ('二、多选', ('一、单选',), 2)
    """
    stratum_of_slot: list[int] = []
    for index, stratum in enumerate(strata):
        stratum_of_slot.extend([index] * stratum.needed)

    #: 题目 -> 占用它的空位。增广过程中会被改写（换一个空位占用它）
    owner: dict[str, int] = {}

    def augment(slot: int, seen_questions: set[str], seen_strata: set[int]) -> bool:
        """为 ``slot`` 找一道题，必要时让已占位的空位换一道。"""
        stratum_index = stratum_of_slot[slot]
        seen_strata.add(stratum_index)
        for question_id in strata[stratum_index].pool:
            if question_id in seen_questions:
                continue
            seen_questions.add(question_id)
            holder = owner.get(question_id)
            if holder is None or augment(holder, seen_questions, seen_strata):
                owner[question_id] = slot
                return True
        return False

    #: 单元 -> 导致它凑不齐的那组单元（含它自己）
    blocked_by: dict[int, frozenset[int]] = {}
    for slot, stratum_index in enumerate(stratum_of_slot):
        if stratum_index in blocked_by:
            # 同一单元的空位邻域完全相同：既然前一个增广失败了，这个也必然失败。
            # （Kuhn 算法的标准性质：某一刻配不上的点，之后也配不上。）
            continue
        seen_questions: set[str] = set()
        seen_strata: set[int] = set()
        if not augment(slot, seen_questions, seen_strata):
            blocked_by[stratum_index] = frozenset(seen_strata)

    picked: list[list[str]] = [[] for _ in strata]
    for question_id, slot in owner.items():
        picked[stratum_of_slot[slot]].append(question_id)
    # 按候选池顺序排，而不是按 dict 的插入顺序——后者取决于增广路径的走法，
    # 换一个无关的筛选条件就可能让已经抽中的题换位置，看起来像是「结果不稳定」
    ordered: list[tuple[str, ...]] = []
    for index, ids in enumerate(picked):
        rank = {question_id: order for order, question_id in enumerate(strata[index].pool)}
        ordered.append(tuple(sorted(ids, key=lambda qid: rank[qid])))
    return tuple(ordered), _shortages(strata, ordered, blocked_by)


def _shortages(
    strata: Sequence[Stratum],
    picked: Sequence[tuple[str, ...]],
    blocked_by: dict[int, frozenset[int]],
) -> tuple[Shortage, ...]:
    """为每个没凑齐的单元生成一条缺额说明。

    **每个缺了的单元都必须出现一条**：概览里写着「第二节 3/4 道」而底下的原因
    只提第一节，读报告的人会以为是自己看漏了。
    """
    shortages: list[Shortage] = []
    for index, stratum in enumerate(strata):
        filled = len(picked[index])
        if filled >= stratum.needed:
            continue
        group = blocked_by.get(index, frozenset({index}))
        members = [strata[member] for member in sorted(group)]
        available = len({qid for member in members for qid in member.pool})
        # Hall 定理保证这里必然成立；不成立说明匹配或集合收集有 bug，
        # 宁可当场炸掉，也不要报一个「需要 3 道、有 5 道、缺 -2 道」的荒谬结论
        needed_by_group = sum(member.needed for member in members)
        assert needed_by_group > available, (
            f"缺额推导有误：{stratum.label} 所在的竞争组需要 {needed_by_group} 道、"
            f"可用 {available} 道，却报了缺额"
        )
        shortages.append(
            Shortage(
                label=stratum.label,
                needed=stratum.needed,
                filled=filled,
                available=available,
                competing=tuple(member.label for member in members if member is not stratum),
            )
        )
    return tuple(shortages)


class SelectionService:
    """按蓝图抽题。不写库——交给 :class:`~qbank.core.services.paper.PaperService`。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.questions = QuestionRepo(session)
        self.courses = CourseRepo(session)
        self.chapters = ChapterRepo(session)
        self.tags = TagRepo(session)

    def select(
        self,
        course_id: str,
        blueprint: Blueprint,
        *,
        seed: int | None = None,
        exclude_ids: Sequence[str] = (),
    ) -> SelectionResult:
        """抽题。``seed`` 显式传入时覆盖蓝图里的，用于「同蓝图换个种子出 B 卷」。

        ``exclude_ids`` 用于避开另一份卷子已用的题（A/B 卷不重题）。
        """
        self.courses.get(course_id)
        effective_seed = seed if seed is not None else blueprint.seed
        if effective_seed is None:
            effective_seed = random.randrange(SEED_MAX)
        rng = random.Random(effective_seed)

        strata = self._build_strata(course_id, blueprint, rng, exclude_ids)
        picked, shortages = solve(strata)
        shortages = tuple(self._with_hints(shortage, strata) for shortage in shortages)

        loaded = self.questions.get_many([question_id for group in picked for question_id in group])
        sections: list[SelectedSection] = []
        for index, spec in enumerate(blueprint.sections):
            chosen: list[tuple[Difficulty | None, Question]] = []
            for stratum, ids in zip(strata, picked, strict=True):
                if stratum.section_index != index:
                    continue
                chosen.extend((stratum.difficulty, loaded[qid]) for qid in ids if qid in loaded)
            if blueprint.shuffle_items:
                rng.shuffle(chosen)
            sections.append(
                SelectedSection(
                    index=index,
                    spec=spec,
                    questions=tuple(question for _, question in chosen),
                    strata=tuple(difficulty for difficulty, _ in chosen),
                )
            )
        return SelectionResult(
            sections=tuple(sections),
            shortages=shortages,
            seed=effective_seed,
            strata=tuple(strata),
        )

    # ------------------------------------------------------- 候选池
    def _build_strata(
        self,
        course_id: str,
        blueprint: Blueprint,
        rng: random.Random,
        exclude_ids: Sequence[str],
    ) -> list[Stratum]:
        strata: list[Stratum] = []
        for index, spec in enumerate(blueprint.sections):
            base = self._criteria(course_id, blueprint, spec, exclude_ids)
            for difficulty, needed in spec.quotas():
                criteria = base if difficulty is None else replace(base, difficulties=(difficulty,))
                pool = list(self.questions.ids(criteria))
                rng.shuffle(pool)
                strata.append(
                    Stratum(
                        section_index=index,
                        section_title=spec.title,
                        difficulty=difficulty,
                        needed=needed,
                        pool=tuple(pool),
                        criteria=criteria,
                    )
                )
        return strata

    def _criteria(
        self,
        course_id: str,
        blueprint: Blueprint,
        spec: SectionSpec,
        exclude_ids: Sequence[str],
    ) -> QuestionFilter:
        return QuestionFilter(
            course_id=course_id,
            chapter_ids=self._resolve_chapters(course_id, spec.chapters),
            include_unchaptered=spec.include_unchaptered,
            types=spec.types,
            difficulties=spec.difficulties,
            statuses=blueprint.statuses,
            all_tag_ids=self._resolve_tags(spec.tags_all),
            any_tag_ids=self._resolve_tags(spec.tags_any),
            exclude_ids=tuple(exclude_ids),
            source_contains=spec.source_contains,
            require_answer=spec.require_answer,
            exclude_used_within_days=blueprint.avoid_used_within_days,
        )

    def _resolve_chapters(self, course_id: str, paths: Sequence[str]) -> tuple[str, ...]:
        """章节路径 → 章节 id（含子章节）。写错一个字就报错，不默默抽不到题。"""
        if not paths:
            return ()
        resolved: list[str] = []
        for path in paths:
            chapter = self.chapters.find_by_path(course_id, path)
            if chapter is None:
                available = (
                    "、".join(c.path for c in self.chapters.list_tree(course_id))
                    or "（还没有章节）"
                )
                raise BlueprintError(
                    f"蓝图里的章节「{path}」在本课程里不存在。现有章节：{available}"
                )
            resolved.append(chapter.id)
        return tuple(self.chapters.subtree_ids(resolved))

    def _resolve_tags(self, names: Sequence[str]) -> tuple[str, ...]:
        if not names:
            return ()
        resolved: list[str] = []
        for name in names:
            tag = self.tags.find_by_name(name)
            if tag is None:
                available = "、".join(t.name for t in self.tags.list_all()) or "（还没有标签）"
                raise BlueprintError(f"蓝图里的标签「{name}」不存在。现有标签：{available}")
            resolved.append(tag.id)
        return tuple(resolved)

    # ------------------------------------------------------- 缺额诊断
    def _with_hints(self, shortage: Shortage, strata: Sequence[Stratum]) -> Shortage:
        """给缺额补上「放宽哪一条能多出几道」。

        「符合条件的只有 1 道」这句话本身没法行动——老师想知道的是**哪一条**
        把题挡住了。所以逐条把筛选条件摘掉再数一遍，差额大的那条就是真凶。
        """
        owner = next((stratum for stratum in strata if stratum.label == shortage.label), None)
        if owner is None:  # pragma: no cover - 缺额的 label 总来自 strata
            return shortage
        return replace(shortage, hints=tuple(self._relaxations(owner)[:5]))

    def _relaxations(self, stratum: Stratum) -> list[str]:
        criteria = stratum.criteria
        if criteria is None:
            return []
        current = len(stratum.pool)
        probes: list[tuple[str, QuestionFilter]] = []
        if criteria.difficulties:
            label = "、".join(level.label for level in criteria.difficulties)
            probes.append((f"不限难度（现在限「{label}」）", replace(criteria, difficulties=())))
        if criteria.chapter_ids:
            probes.append(
                ("不限章节", replace(criteria, chapter_ids=(), include_unchaptered=False))
            )
        if criteria.all_tag_ids or criteria.any_tag_ids:
            probes.append(("不限标签", replace(criteria, all_tag_ids=(), any_tag_ids=())))
        # 刻意不探测题型：对一个标题就叫「综合应用题」的大题说「放宽题型可多出
        # 15 道」毫无可操作性，只会把真正有用的那几条提示挤下去
        if criteria.statuses:
            label = "、".join(state.label for state in criteria.statuses)
            probes.append(
                (f"把草稿题也算上（现在只抽「{label}」）", replace(criteria, statuses=()))
            )
        if criteria.exclude_used_within_days is not None:
            days = criteria.exclude_used_within_days
            probes.append(
                (f"不避开近 {days} 天用过的题", replace(criteria, exclude_used_within_days=None))
            )
        if criteria.require_answer:
            probes.append(("把没有参考答案的题也算上", replace(criteria, require_answer=False)))
        if criteria.source_contains:
            probes.append(
                (
                    f"不限来源（现在限含「{criteria.source_contains}」）",
                    replace(criteria, source_contains=""),
                )
            )

        hints: list[str] = []
        for label, relaxed in probes:
            gained = self.questions.count(relaxed) - current
            if gained > 0:
                hints.append(f"{stratum.label}：{label}可多出 {gained} 道")
        return hints
