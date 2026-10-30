"""导入编排：解析 → 校验 → 查重 → 预览 → 事务写入。

**绝不静默导入**。整个流程分成两步，中间有一个用户必须看到的预览：

``plan()``
    解析、校验、查重，但**不写任何东西**。产出一份逐条的计划，每条标明
    「将要新建 / 将要更新 / 将被跳过」以及发现的重复候选。

``commit()``
    按用户确认的策略写入，图片同时收进题库，并记录一条
    :class:`ImportBatch`——出了问题可以照着它回滚。

为什么要这么麻烦：导入的来源是几十份历年 Word 文件，一次导入几百道题。
静默导入一旦出错（格式误判、重复覆盖），用户发现时已经不知道改了什么，
而题库是他们唯一的一份资料。

**一道题失败不影响其余题目**：一份文件里 50 道题有 1 道格式不对，不该让
另外 49 道也进不来。错误逐条收集进报告，带源文件行号。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from qbank.core.enums import ConflictPolicy
from qbank.core.models import ImportBatch, utcnow
from qbank.core.schemas import QuestionDraft
from qbank.core.services.asset import AssetService
from qbank.core.services.dedup import DuplicateHint
from qbank.core.services.question import QuestionService
from qbank.exchange.importers.loader import SourceDocument, open_source
from qbank.exchange.importers.template import BlockError, parse_document


@dataclass(slots=True)
class PlannedQuestion:
    """计划里的一条。"""

    index: int
    draft: QuestionDraft
    #: 查重结果。``exact`` 的那条决定了默认动作
    duplicates: Sequence[DuplicateHint] = ()
    #: 正文里引用的本地图片（尚未入库）
    image_count: int = 0
    #: 图片相关的问题（找不到、类型不支持）
    image_errors: Sequence[str] = ()

    @property
    def exact_duplicate(self) -> DuplicateHint | None:
        return next((hint for hint in self.duplicates if hint.exact), None)

    @property
    def similar(self) -> Sequence[DuplicateHint]:
        return [hint for hint in self.duplicates if not hint.exact]

    def action_under(self, policy: ConflictPolicy) -> str:
        """在给定策略下这条会发生什么。预览页显示的就是它。"""
        if self.exact_duplicate is None:
            return "新建"
        return {
            ConflictPolicy.SKIP: "跳过（已存在）",
            ConflictPolicy.OVERWRITE: "覆盖已有",
            ConflictPolicy.NEW_REVISION: "更新为新版本",
            ConflictPolicy.CREATE_COPY: "另存为新题",
        }[policy]

    @property
    def headline(self) -> str:
        text = self.draft.stem.replace("\n", " ").strip()
        return text[:48] + ("…" if len(text) > 48 else "")


@dataclass(slots=True)
class ImportPlan:
    """一次导入的计划。没有任何副作用，可以反复生成。"""

    course_id: str
    source_name: str
    source_format: str
    questions: list[PlannedQuestion] = field(default_factory=list)
    errors: list[BlockError] = field(default_factory=list)
    #: 源文档要留到 commit——pandoc 抽出的图片还在它的临时目录里
    document: SourceDocument | None = None

    @property
    def total_parsed(self) -> int:
        return len(self.questions)

    @property
    def exact_duplicate_count(self) -> int:
        return sum(1 for item in self.questions if item.exact_duplicate is not None)

    @property
    def similar_count(self) -> int:
        return sum(1 for item in self.questions if item.similar)

    @property
    def image_count(self) -> int:
        return sum(item.image_count for item in self.questions)

    def summary(self, policy: ConflictPolicy) -> str:
        new_count = sum(1 for item in self.questions if item.exact_duplicate is None)
        duplicate_count = self.exact_duplicate_count
        parts = [f"解析出 {self.total_parsed} 道题", f"其中 {new_count} 道是新题"]
        if duplicate_count:
            parts.append(f"{duplicate_count} 道已存在（将{policy.label}）")
        if self.similar_count:
            parts.append(f"{self.similar_count} 道有相似题待人工确认")
        if self.image_count:
            parts.append(f"涉及 {self.image_count} 张图片")
        if self.errors:
            parts.append(f"{len(self.errors)} 道解析失败")
        return "；".join(parts)


@dataclass(slots=True)
class ImportResult:
    """commit 的结果。"""

    batch_id: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    created_ids: list[str] = field(default_factory=list)
    image_errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=utcnow)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped + self.failed

    def summary(self) -> str:
        return f"新建 {self.created}，更新 {self.updated}，跳过 {self.skipped}，失败 {self.failed}"


class ImportService:
    def __init__(self, session: Session, assets: AssetService | None = None) -> None:
        self.session = session
        self.questions = QuestionService(session)
        self.assets = assets or AssetService(session)

    # ------------------------------------------------------------ 计划
    def plan_document(self, course_id: str, document: SourceDocument) -> ImportPlan:
        """从已读好的源文档产出计划。**不写任何东西。**"""
        report = parse_document(document.markdown)
        plan = ImportPlan(
            course_id=course_id,
            source_name=document.source_name,
            source_format=document.source_format,
            errors=list(report.errors),
            document=document,
        )
        # 候选集建一次，整份文件共用。逐题重建的话，一份 300 道题的文件导入
        # 2000 道题的库要跑 843 秒（实测）——见 DuplicateIndex 的说明。
        duplicate_index = self.questions.build_duplicate_index(
            course_id, sorted({draft.type for draft in report.drafts}, key=lambda t: t.value)
        )
        for index, draft in enumerate(report.drafts, start=1):
            plan.questions.append(
                PlannedQuestion(
                    index=index,
                    draft=draft,
                    duplicates=self.questions.check_duplicates(
                        course_id, draft, index=duplicate_index
                    ),
                    image_count=self._count_local_images(draft),
                )
            )
        return plan

    @staticmethod
    def _count_local_images(draft: QuestionDraft) -> int:
        """数一下这道题引用了多少张图。

        只是数数，不做任何存取——预览阶段不应该有副作用。
        """
        from qbank.core.services.asset import _HTML_IMAGE_SRC, _IMAGE_REF

        texts = [draft.stem, draft.answer, draft.explanation]
        options = getattr(draft.payload, "options", None)
        if options:
            texts.extend(option.content for option in options)
        texts.extend(sub.stem for sub in draft.sub_questions)
        count = 0
        for text in texts:
            count += len(_IMAGE_REF.findall(text)) + len(_HTML_IMAGE_SRC.findall(text))
        return count

    # ------------------------------------------------------------ 提交
    def commit(
        self,
        plan: ImportPlan,
        *,
        policy: ConflictPolicy = ConflictPolicy.SKIP,
        selected: Sequence[int] | None = None,
    ) -> ImportResult:
        """按策略写入。

        ``selected`` 是用户在预览页勾选的条目序号（1 起）。``None`` 表示全选。
        逐条勾选是必要的——老师常常只想导入一份文件里的某几道题。
        """
        if plan.document is None:
            raise ValueError("计划缺少源文档，无法解析图片；请重新 plan")

        wanted = set(selected) if selected is not None else None
        batch = ImportBatch(
            course_id=plan.course_id,
            source_name=plan.source_name,
            source_format=plan.source_format,
        )
        self.session.add(batch)
        self.session.flush()

        result = ImportResult(batch_id=batch.id, failed=len(plan.errors))
        per_item: list[dict[str, object]] = []

        for item in plan.questions:
            if wanted is not None and item.index not in wanted:
                result.skipped += 1
                per_item.append({"index": item.index, "action": "未勾选"})
                continue
            draft, image_errors = self._absorb_images(item.draft, plan.document.base_dir)
            result.image_errors.extend(image_errors)
            try:
                written = self.questions.upsert(plan.course_id, draft, policy=policy)
            except ValueError as exc:
                result.failed += 1
                per_item.append({"index": item.index, "action": "失败", "detail": str(exc)})
                continue
            if written.created:
                result.created += 1
                if written.question is not None:
                    result.created_ids.append(written.question.id)
            elif written.updated:
                result.updated += 1
            else:
                result.skipped += 1
            if written.question is not None:
                self.assets.link(written.question.id, self.assets.referenced_assets(draft.stem))
            per_item.append(
                {
                    "index": item.index,
                    "action": written.action,
                    "question_id": written.question.id if written.question else None,
                }
            )

        batch.finished_at = utcnow()
        batch.succeeded = result.failed == 0
        batch.created_count = result.created
        batch.updated_count = result.updated
        batch.skipped_count = result.skipped
        batch.failed_count = result.failed
        batch.created_ids = list(result.created_ids)
        batch.report = {
            "items": per_item,
            "parse_errors": [str(error) for error in plan.errors],
            "image_errors": result.image_errors,
        }
        self.session.flush()
        return result

    def _absorb_images(
        self, draft: QuestionDraft, base_dir: Path
    ) -> tuple[QuestionDraft, list[str]]:
        """把草稿里引用的本地图片收进题库，并改写成 ``asset://``。

        必须在这一步做：pandoc 抽出的图片在临时目录里，源文档一关就没了。
        """
        errors: list[str] = []

        def absorb(text: str) -> str:
            if not text:
                return text
            rewritten, _, problems = self.assets.absorb_local_images(text, base_dir)
            errors.extend(problems)
            return rewritten

        updates: dict[str, object] = {
            "stem": absorb(draft.stem),
            "answer": absorb(draft.answer),
            "explanation": absorb(draft.explanation),
        }
        options = getattr(draft.payload, "options", None)
        if options:
            updates["payload"] = draft.payload.model_copy(
                update={
                    "options": tuple(
                        option.model_copy(update={"content": absorb(option.content)})
                        for option in options
                    )
                }
            )
        if draft.sub_questions:
            rebuilt = []
            for sub in draft.sub_questions:
                sub_draft, sub_errors = self._absorb_images(sub, base_dir)
                errors.extend(sub_errors)
                rebuilt.append(sub_draft)
            updates["sub_questions"] = tuple(rebuilt)
        return draft.model_copy(update=updates), errors

    # ------------------------------------------------------------ 回滚
    def rollback_batch(self, batch_id: str) -> int:
        """撤销一次导入：删掉本批**新建**的题目。

        只删新建的，不恢复被覆盖的——被覆盖的题有修改历史，应该用
        ``question history --rollback-to`` 单独回滚。混在一起做会让「撤销导入」
        这个动作的后果变得不可预测。
        """
        batch = self.session.get(ImportBatch, batch_id)
        if batch is None:
            raise ValueError(f"找不到导入批次：{batch_id}")
        removed = 0
        for question_id in batch.created_ids:
            try:
                self.questions.purge(question_id)
                removed += 1
            except LookupError:
                continue  # 用户已经手工删了，不算错
        batch.report = {**batch.report, "rolled_back_at": utcnow().isoformat()}
        batch.created_ids = []
        self.session.flush()
        return removed


def plan_file(session: Session, course_id: str, path: Path) -> tuple[ImportPlan, SourceDocument]:
    """便捷入口：读文件并产出计划。

    返回的 :class:`SourceDocument` **必须**由调用方在 commit 之后 close——
    它持有 pandoc 抽出图片的临时目录。用 ``open_source`` 而不是上下文管理器
    版本，正是因为文档要跨越 plan / commit 两步存活。
    """
    document = open_source(path)
    try:
        plan = ImportService(session).plan_document(course_id, document)
    except Exception:
        document.close()
        raise
    return plan, document
