"""题目服务：手工录入与批量导入共用的唯一写入口。

**为什么必须收敛到一个地方**：写一道题要同时做六件事——落库、写答案表、解析
或创建章节、解析或创建标签、算指纹、更新全文索引。漏掉任何一件都会产生很难
发现的不一致：

* 漏了指纹 → 查重失效，库慢慢变脏
* 漏了索引 → 题在库里但搜不到
* 漏了章节解析 → 导入的题全部落在「未归类」
* 漏了修改历史 → 改错了无法回滚

所以 CLI、导入器、Web 路由都只能经由本服务写题，不许直接操作仓储。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qbank.core.enums import ConflictPolicy, Difficulty, QuestionStatus, QuestionType
from qbank.core.models import Question, QuestionAnswer, QuestionRevision, new_id
from qbank.core.repo.base import NotFoundError
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.schemas import PAYLOAD_MODELS, QuestionDraft
from qbank.core.services import dedup
from qbank.core.services.search import SearchService


@dataclass(frozen=True, slots=True)
class DuplicateIndex:
    """预先算好 shingle 的查重候选集。

    **为什么必须有这一层**：``check_duplicates`` 每调用一次，就要为全部候选各算
    一遍 shingle，而算 shingle 要解析一遍 Markdown。导入一份 300 道题的文件到
    一个 2000 道题的库，就是 300 × 2000 次解析——实测要 **843 秒**。

    候选集在一次导入期间是不变的，预先算一次即可。实测同样的场景降到 **6.9 秒**，
    其中 Jaccard 比较本身只占 14 毫秒，其余都是一次性的加载与解析。
    """

    candidates: tuple[dedup.Candidate, ...]

    def __len__(self) -> int:
        return len(self.candidates)


@dataclass(frozen=True, slots=True)
class BulkResult:
    """一次批量操作的结果。

    ``skipped`` 既包含「找不到」也包含「本来就是这个值」——后者不该算作改动，
    否则批量把 50 道题都设成「启用」会产生 50 条无意义的历史记录。
    """

    changed: int
    skipped: int

    @property
    def summary(self) -> str:
        if not self.changed:
            return "没有题目需要改动"
        text = f"已修改 {self.changed} 道"
        if self.skipped:
            text += f"，{self.skipped} 道无需改动"
        return text


@dataclass(frozen=True, slots=True)
class WriteResult:
    """一次写入的结果。导入向导据此汇总「新建 N 条、更新 M 条、跳过 K 条」。"""

    question: Question | None
    created: bool
    updated: bool
    skipped: bool
    duplicates: Sequence[dedup.DuplicateHint] = ()

    @property
    def action(self) -> str:
        if self.created:
            return "created"
        if self.updated:
            return "updated"
        return "skipped"


class QuestionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.questions = QuestionRepo(session)
        self.courses = CourseRepo(session)
        self.chapters = ChapterRepo(session)
        self.tags = TagRepo(session)
        self.search = SearchService(session)

    # ------------------------------------------------------- 指纹与查重
    @staticmethod
    def fingerprint_parts(draft: QuestionDraft) -> list[str]:
        """参与指纹的内容。

        必须包含选项——同题干不同选项是两道不同的题。也包含答案，因为
        「说法正确的是」与「说法错误的是」常常题干几乎一样、只有答案不同。
        子题内容也要算进去，否则两道只有小问不同的综合题会被判成重复。
        """
        parts = [draft.stem]
        payload: Any = draft.payload
        options = getattr(payload, "options", None)
        if options:
            parts.extend(option.content for option in options)
        blanks = getattr(payload, "blanks", None)
        if blanks:
            parts.extend(" ".join(blank.answers) for blank in blanks)
        parts.append(draft.answer)
        for sub in draft.sub_questions:
            parts.append(sub.stem)
            parts.append(sub.answer)
        return [part for part in parts if part]

    @staticmethod
    def similarity_parts(draft: QuestionDraft) -> list[str]:
        """参与**相似度**比较的内容：题干与选项，**不含答案**。

        这和 :meth:`fingerprint_parts` 的差别是刻意的，而且很要紧。

        实测：「栈是一种**先**进先出的线性表／答案：错误」与「栈是一种**后**进
        先出的线性表／答案：正确」——题意被一字反转，答案也跟着反转。只比题干
        相似度 0.538，把答案算进去就掉到 0.412，低于 0.45 的阈值，于是这道
        **最需要人工复核**的题反而被静默漏掉了。

        根因是：题意一旦反转，答案必然跟着反转，于是答案给相似度贡献的是额外的
        **差异**，恰好压低了我们最想抓到的那一类。所以答案只进指纹（用于判定
        「完全相同」，同题干不同答案不能自动跳过），不进相似度。
        """
        parts = [draft.stem]
        payload: Any = draft.payload
        options = getattr(payload, "options", None)
        if options:
            parts.extend(option.content for option in options)
        parts.extend(sub.stem for sub in draft.sub_questions)
        return [part for part in parts if part]

    def build_duplicate_index(
        self, course_id: str, question_types: Sequence[QuestionType]
    ) -> DuplicateIndex:
        """为一批题型预先算好候选集。

        批量导入时**必须**先建一次再逐题比对，否则每道题都会重算一遍全部候选
        （见 :class:`DuplicateIndex` 的说明）。
        """
        return DuplicateIndex(
            candidates=tuple(
                dedup.Candidate(
                    question_id=existing.id,
                    fingerprint=existing.fingerprint,
                    shingle_set=dedup.shingles(self._existing_parts(existing, with_answer=False)),
                )
                for existing in self.questions.dedup_candidates(course_id, question_types)
            )
        )

    def check_duplicates(
        self,
        course_id: str,
        draft: QuestionDraft,
        *,
        limit: int = 10,
        index: DuplicateIndex | None = None,
    ) -> list[dedup.DuplicateHint]:
        """查重。精确命中可自动处理，相似命中**只作建议**（原因见 dedup 模块）。

        判定「同一道题」的规则必须和 :meth:`upsert` **完全一致**，否则预览页说
        「将要新建」而提交时却更新了——预览存在的全部意义就是让用户事先知道会
        发生什么。所以这里和 upsert 一样，先看 ``external_id`` 再看指纹。

        ``index`` 为 ``None`` 时临时建一个只含本题型的候选集——单题检查这样最
        方便；批量导入**一定要**传预先建好的 index，否则每道题都会重算一遍
        全部候选。
        """
        target_fingerprint = dedup.fingerprint(self.fingerprint_parts(draft))

        hints: list[dedup.DuplicateHint] = []
        by_external = self.questions.find_by_external_id(course_id, draft.external_id)
        if by_external is not None:
            # 编号与题面都一致时说「完全相同」更准确；只有编号一致（题面被改过）
            # 才说「编号相同」，让用户知道内容确实变了
            hints.append(
                dedup.DuplicateHint(
                    question_id=by_external.id,
                    exact=True,
                    score=1.0,
                    matched_by=(
                        dedup.MatchBy.FINGERPRINT
                        if by_external.fingerprint == target_fingerprint
                        else dedup.MatchBy.EXTERNAL_ID
                    ),
                )
            )

        target_shingles = dedup.shingles(self.similarity_parts(draft))
        pool = index or self.build_duplicate_index(course_id, [draft.type])
        for hint in dedup.rank_similar(
            target_fingerprint, target_shingles, pool.candidates, limit=limit
        ):
            # 编号已经命中的那道题不再重复列出
            if any(existing.question_id == hint.question_id for existing in hints):
                continue
            hints.append(hint)
        return hints[:limit]

    @staticmethod
    def _existing_parts(question: Question, *, with_answer: bool = True) -> list[str]:
        """库内题目对应的比较内容。

        ``with_answer`` 要和目标侧保持一致：指纹含答案，相似度不含
        （原因见 :meth:`similarity_parts`）。
        """
        parts = [question.stem]
        options = question.payload.get("options") if question.payload else None
        if isinstance(options, list):
            parts.extend(
                str(option.get("content", "")) for option in options if isinstance(option, dict)
            )
        if with_answer and question.answer is not None:
            parts.append(question.answer.answer)
        parts.extend(sub.stem for sub in question.sub_questions)
        return [part for part in parts if part]

    # ------------------------------------------------------------ 写
    def create(
        self, course_id: str, draft: QuestionDraft, *, question_id: str | None = None
    ) -> Question:
        """新建一道题（含子题）。调用方负责事务边界。

        ``question_id`` 只给 ``.qbz`` 交换包用：导入时沿用包里原来的 UUID，
        这样同一道题在不同题库里是**同一个 id**。不沿用的话，把包发给同事、
        他改了几道题、再把更新后的包发回来——这边已经认不出那是同一道题了
        （题面变了，指纹对不上），只能当新题再建一遍。UUID 天然不冲突，沿用是
        安全的。
        """
        self.courses.get(course_id)  # 课程不存在时尽早失败，错误信息更清楚
        question = self._build(course_id, draft, question_id=question_id)
        self.questions.add(question)
        self._reindex(question, draft)
        return question

    def _build(
        self,
        course_id: str,
        draft: QuestionDraft,
        *,
        parent: Question | None = None,
        question_id: str | None = None,
    ) -> Question:
        chapter = self.chapters.ensure_path(course_id, draft.chapter)
        parts = self.fingerprint_parts(draft)
        question = Question(
            id=question_id or new_id(),
            course_id=course_id,
            chapter_id=chapter.id if chapter else None,
            parent_id=parent.id if parent else None,
            type=draft.type,
            stem=draft.stem,
            payload=draft.payload.model_dump(mode="json"),
            difficulty=draft.difficulty,
            score=draft.score,
            est_minutes=draft.est_minutes,
            status=draft.status,
            source=draft.source,
            external_id=draft.external_id,
            fingerprint=dedup.fingerprint(parts),
        )
        question.answer = QuestionAnswer(
            answer=draft.answer,
            explanation=draft.explanation,
            scoring_rubric=draft.scoring_rubric,
        )
        question.tags = self.tags.resolve_many(draft.tags)
        for index, sub_draft in enumerate(draft.sub_questions):
            sub = self._build(course_id, sub_draft, parent=question)
            sub.sort_order = index
            question.sub_questions.append(sub)
        return question

    def update(self, question_id: str, draft: QuestionDraft, *, note: str = "") -> Question:
        """就地更新。**改之前先存一份快照**，这样改错了能回滚。

        子题采用「整体替换」而不是逐条对齐：子题没有稳定的业务标识，试图
        按顺序或按内容匹配只会在「插入一个小问」时全部错位。整体替换的代价
        是子题的 id 会变，但子题本身不被外部引用（试卷存的是整题快照）。
        """
        question = self.questions.get_detailed(question_id)
        self._snapshot(question, note=note)

        chapter = self.chapters.ensure_path(question.course_id, draft.chapter)
        parts = self.fingerprint_parts(draft)
        question.chapter_id = chapter.id if chapter else None
        question.type = draft.type
        question.stem = draft.stem
        question.payload = draft.payload.model_dump(mode="json")
        question.difficulty = draft.difficulty
        question.score = draft.score
        question.est_minutes = draft.est_minutes
        question.status = draft.status
        question.source = draft.source
        if draft.external_id:
            question.external_id = draft.external_id
        question.fingerprint = dedup.fingerprint(parts)
        question.revision += 1

        if question.answer is None:
            question.answer = QuestionAnswer(question_id=question.id)
        question.answer.answer = draft.answer
        question.answer.explanation = draft.explanation
        question.answer.scoring_rubric = draft.scoring_rubric
        question.tags = self.tags.resolve_many(draft.tags)

        for sub in list(question.sub_questions):
            self.search.remove(sub.id)
            question.sub_questions.remove(sub)
        self.session.flush()
        for index, sub_draft in enumerate(draft.sub_questions):
            sub = self._build(question.course_id, sub_draft, parent=question)
            sub.sort_order = index
            question.sub_questions.append(sub)

        self.session.flush()
        self._reindex(question, draft)
        return question

    def _snapshot(self, question: Question, *, note: str) -> None:
        """存一份改动前的快照。

        **要追加到 ``question.revisions`` 而不是 ``session.add``**：会话用的是
        ``expire_on_commit=False``，直接 add 的话这个已加载的集合不会刷新，
        同一会话里紧接着读 revisions 的调用方就看不到刚产生的历史——
        「改完马上看历史，列表是空的」。
        """
        question.revisions.append(
            QuestionRevision(
                question_id=question.id,
                revision=question.revision,
                snapshot=self.to_draft(question).model_dump(mode="json"),
                note=note,
            )
        )

    def _reindex(self, question: Question, draft: QuestionDraft) -> None:
        """重建题目及其子题的索引。

        子题单独入索引：老师搜「最短路」时，命中的应该是那道大题里的第 2 小问，
        而不是「材料部分恰好没提到这个词所以搜不到」。
        """
        self.search.index(
            question.id,
            stem=draft.stem,
            answer=draft.answer,
            explanation=draft.explanation,
            tags=draft.tags,
        )
        for sub, sub_draft in zip(question.sub_questions, draft.sub_questions, strict=False):
            self.search.index(
                sub.id,
                stem=sub_draft.stem,
                answer=sub_draft.answer,
                explanation=sub_draft.explanation,
                tags=sub_draft.tags,
            )

    def upsert(
        self,
        course_id: str,
        draft: QuestionDraft,
        *,
        policy: ConflictPolicy = ConflictPolicy.SKIP,
        question_id: str | None = None,
    ) -> WriteResult:
        """导入路径用：按冲突策略决定新建、更新还是跳过。

        「同一道题」的判定顺序是 ``external_id`` 优先、其次精确指纹。
        ``external_id`` 更可靠——它来自源文件，即使题面被编辑过也还认得出，
        这正是「同一个文件改了几处后重新导入」需要的行为。
        """
        existing = self.questions.find_by_external_id(
            course_id, draft.external_id
        ) or self.questions.find_by_fingerprint(
            course_id, dedup.fingerprint(self.fingerprint_parts(draft))
        )
        if existing is None:
            return WriteResult(
                self.create(course_id, draft, question_id=question_id), True, False, False
            )

        if policy is ConflictPolicy.SKIP:
            return WriteResult(existing, False, False, True)
        if policy is ConflictPolicy.CREATE_COPY:
            return WriteResult(self.create(course_id, draft), True, False, False)
        # OVERWRITE 与 NEW_REVISION 都走 update——update 本来就会存快照，
        # 两者的差别只在界面提示语，不在数据行为
        note = "导入覆盖" if policy is ConflictPolicy.OVERWRITE else "导入新版本"
        return WriteResult(self.update(existing.id, draft, note=note), False, True, False)

    # ------------------------------------------------------------ 删除与恢复
    def delete(self, question_id: str) -> Question:
        """移入回收站，并从索引中移除——否则回收站里的题还能被搜到。"""
        question = self.questions.soft_delete(question_id)
        self.search.remove(question.id)
        for sub in question.sub_questions:
            self.search.remove(sub.id)
        return question

    def restore(self, question_id: str) -> Question:
        """从回收站恢复并重建索引。"""
        question = self.questions.restore(question_id)
        draft = self.to_draft(question)
        self._reindex(question, draft)
        return question

    def purge(self, question_id: str) -> None:
        question = self.questions.get(question_id, include_deleted=True)
        for sub in question.sub_questions:
            self.search.remove(sub.id)
        self.search.remove(question.id)
        self.questions.purge(question_id)

    # ------------------------------------------------------------ 读
    def to_draft(self, question: Question) -> QuestionDraft:
        """ORM 对象 → 交换格式。编辑界面、JSON 导出、修改历史快照共用。"""
        payload_model = PAYLOAD_MODELS[question.type]
        answer = question.answer
        return QuestionDraft(
            type=question.type,
            stem=question.stem,
            payload=payload_model.model_validate(question.payload),
            answer=answer.answer if answer else "",
            explanation=answer.explanation if answer else "",
            scoring_rubric=answer.scoring_rubric if answer else "",
            chapter=question.chapter.path if question.chapter else "",
            difficulty=question.difficulty,
            score=question.score,
            est_minutes=question.est_minutes,
            tags=tuple(tag.name for tag in question.tags),
            source=question.source,
            status=question.status,
            external_id=question.external_id,
            sub_questions=tuple(self.to_draft(sub) for sub in question.sub_questions),
        )

    def search_ids(self, query: str, *, limit: int = 200, top_level: bool = True) -> list[str]:
        """全文检索，按相关度返回题目 id。

        ``top_level=True``（默认）时把命中的子题映射成其父题——列表按题展示，
        一道综合题只占一行。不映射的话「命中了某道大题的第 2 小问」会表现为
        「列表里什么都没有」，用户只会觉得搜索坏了。
        """
        hits = self.search.matching_ids(query, limit=limit)
        return self.questions.to_top_level_ids(hits) if top_level else hits

    def revisions(self, question_id: str) -> Sequence[QuestionRevision]:
        return self.questions.get(question_id, include_deleted=True).revisions

    def rollback(self, question_id: str, revision: int) -> Question:
        """回滚到某个历史版本。

        回滚本身也会产生一条新的历史记录——否则「回滚之后发现回滚错了」就无路
        可退了。
        """
        question = self.questions.get(question_id, include_deleted=True)
        target = next((r for r in question.revisions if r.revision == revision), None)
        if target is None:
            available = sorted(r.revision for r in question.revisions)
            raise ValueError(f"题目没有第 {revision} 版；现有版本：{available}")
        draft = QuestionDraft.model_validate(target.snapshot)
        return self.update(question_id, draft, note=f"回滚到第 {revision} 版")

    # ------------------------------------------------------------ 批量
    def bulk_edit(
        self,
        question_ids: Sequence[str],
        *,
        add_tags: Sequence[str] = (),
        remove_tags: Sequence[str] = (),
        chapter: str | None = None,
        difficulty: Difficulty | None = None,
        status: QuestionStatus | None = None,
        note: str = "批量修改",
    ) -> BulkResult:
        """批量改章节 / 难度 / 状态 / 标签。

        **仍然逐题存修改历史**。批量操作恰恰是最该能撤销的：一次点错影响的是
        几十道题，而「改错了，一道道改回去」根本不现实。代价是几十条快照，
        相比之下不值一提。

        指纹不重算——参与指纹的是题干、选项与答案，这里改的四样都不在其中。

        只有标签变动时才重建索引：标签进全文索引，章节、难度、状态不进。
        """
        changed = 0
        skipped = 0
        wanted_add = self.tags.resolve_many(add_tags) if add_tags else []
        drop = {name.strip() for name in remove_tags if name.strip()}

        for question_id in question_ids:
            try:
                question = self.questions.get_detailed(question_id)
            except NotFoundError:
                skipped += 1
                continue
            current = {tag.name: tag for tag in question.tags}
            for tag in wanted_add:
                current.setdefault(tag.name, tag)
            for name in drop:
                current.pop(name, None)
            tags_changed = sorted(current) != sorted(tag.name for tag in question.tags)

            target_chapter_id = question.chapter_id
            if chapter is not None:
                resolved = self.chapters.ensure_path(question.course_id, chapter)
                target_chapter_id = resolved.id if resolved else None

            if (
                not tags_changed
                and target_chapter_id == question.chapter_id
                and (difficulty is None or difficulty == question.difficulty)
                and (status is None or status == question.status)
            ):
                skipped += 1
                continue

            self._snapshot(question, note=note)
            if tags_changed:
                question.tags = list(current.values())
            question.chapter_id = target_chapter_id
            if difficulty is not None:
                question.difficulty = difficulty
            if status is not None:
                question.status = status
            question.revision += 1
            self.session.flush()

            if tags_changed:
                self._reindex(question, self.to_draft(question))
            changed += 1
        return BulkResult(changed=changed, skipped=skipped)

    def bulk_delete(self, question_ids: Sequence[str]) -> BulkResult:
        """批量移入回收站。可逐题恢复。"""
        changed = 0
        skipped = 0
        for question_id in question_ids:
            try:
                self.delete(question_id)
                changed += 1
            except NotFoundError:
                skipped += 1
        return BulkResult(changed=changed, skipped=skipped)

    def delete_course(self, course_id: str) -> int:
        """删掉一门课，连同它的章节、题目与试卷，并清掉检索索引。

        课程删除走服务层而不是直接调仓储，是因为**索引不在数据库的级联范围内**：
        ``question_fts`` 是 FTS5 虚表，没有外键。只删课程的话，索引里会留下一批
        指向已不存在题目的行——它们不会让检索出错（结果会被题目表那边过滤掉），
        但会一直占着地方，而且 ``qbank doctor`` 报的「索引是否过期」从此永远对不上。

        返回清理掉的索引条数。
        """
        question_ids = [
            row[0]
            for row in self.session.execute(
                select(Question.id).where(Question.course_id == course_id)
            )
        ]
        for question_id in question_ids:
            self.search.remove(question_id)
        self.courses.delete(course_id)
        # 题目与试卷是**数据库**级联删掉的（passive_deletes），SQLAlchemy 并不
        # 知道它们没了，身份映射里还留着一堆指向不存在行的对象。不失效的话，
        # 同一会话里后续的 session.get 会把它们当活的交回来。
        self.session.expire_all()
        return len(question_ids)

    # ------------------------------------------------------------ 维护
    def reindex_all(self) -> int:
        """重建全部索引。改了分词逻辑或从备份恢复后需要跑一次。"""
        self.search.clear()
        count = 0
        for question in self.session.query(Question).filter(Question.deleted_at.is_(None)):
            answer = question.answer
            self.search.index(
                question.id,
                stem=question.stem,
                answer=answer.answer if answer else "",
                explanation=answer.explanation if answer else "",
                tags=[tag.name for tag in question.tags],
            )
            count += 1
        self.search.optimize()
        return count
