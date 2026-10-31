"""``.qbz`` 整库交换包：导出、读取与导入。

一个 ``.qbz`` 就是一个 zip：

    manifest.json      格式版本、导出时间、内容统计
    courses.jsonl      课程及其章节（章节只存路径，导入时按路径重建）
    tags.jsonl         标签及其分类（分类不在题目里，不单独存就会丢）
    questions.jsonl    题目，每行 ``{id, course, draft}``
    papers.jsonl       试卷（可选），含冻结的题面快照
    assets/<xx>/<sha256>.<ext>   图片，按内容哈希分桶

**它是这个系统唯一的备份与共享手段。** 删课程是硬删除、连带题目与试卷一起没，
在此之前没有任何恢复路径——所以这个包要能把一门课原样装回来，而不只是「大致
导出一份题目」。

为什么题目要带上 id
    同一个库导出再导入（恢复场景）时，靠 id 能认出「就是这道题」，试卷里
    ``paper_item.question_id`` 的指向也能接回去。换一个库导入（共享场景）时
    UUID 天然不冲突，照样安全。

为什么试卷总是新建、从不合并
    试卷是某一刻的产物，没有「合并两份试卷」这种语义。题面本来就冻结在
    ``snapshot`` 里，所以即使对应的题目一道都没导进来，卷子照样完整。

不包含什么
    修改历史与导入批次记录。它们是本地的操作痕迹，对「把题库搬到另一台机器」
    没有意义，而带上会让包大出几倍。这一条写在 manifest 里，免得将来有人
    以为是 bug。
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qbank import __version__
from qbank.config import Paths, get_paths, safe_filename
from qbank.core.enums import ConflictPolicy, TagCategory
from qbank.core.models import (
    Chapter,
    Course,
    Paper,
    PaperItem,
    PaperSection,
    Question,
    new_id,
    utcnow,
)
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.asset import AssetService
from qbank.core.services.question import QuestionService

#: 交换包后缀
BUNDLE_SUFFIX = ".qbz"

#: 包格式版本。读包时**必须**检查——拿新版包喂旧版程序会得到一个
#: 「导入成功但少了一半东西」的结果，那比直接报错糟糕得多。
FORMAT_VERSION = 1

MANIFEST = "manifest.json"
COURSES = "courses.jsonl"
TAGS = "tags.jsonl"
QUESTIONS = "questions.jsonl"
PAPERS = "papers.jsonl"
ASSET_PREFIX = "assets/"

#: 单个条目的大小上限，防的是损坏或恶意构造的包把内存撑爆
MAX_ENTRY_BYTES = 64 * 1024 * 1024


class BundleError(ValueError):
    """包读不了：不是 zip、缺 manifest、版本不认识、内容损坏。"""


@dataclass(frozen=True, slots=True)
class BundleManifest:
    format_version: int
    qbank_version: str
    exported_at: str
    course_count: int
    question_count: int
    paper_count: int
    asset_count: int
    #: 明确写出没带什么，免得将来被当成 bug
    excludes: tuple[str, ...] = ("修改历史", "导入批次记录")

    def to_json(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "qbank_version": self.qbank_version,
            "exported_at": self.exported_at,
            "course_count": self.course_count,
            "question_count": self.question_count,
            "paper_count": self.paper_count,
            "asset_count": self.asset_count,
            "excludes": list(self.excludes),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> BundleManifest:
        try:
            return cls(
                format_version=int(data["format_version"]),
                qbank_version=str(data.get("qbank_version", "")),
                exported_at=str(data.get("exported_at", "")),
                course_count=int(data.get("course_count", 0)),
                question_count=int(data.get("question_count", 0)),
                paper_count=int(data.get("paper_count", 0)),
                asset_count=int(data.get("asset_count", 0)),
                excludes=tuple(data.get("excludes", ())),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleError(f"manifest.json 读不懂：{exc}") from exc

    @property
    def summary(self) -> str:
        return (
            f"{self.course_count} 门课、{self.question_count} 道题、"
            f"{self.paper_count} 份试卷、{self.asset_count} 张图"
        )


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    manifest: BundleManifest

    @property
    def summary(self) -> str:
        size = self.path.stat().st_size / 1024
        return f"{self.path.name}：{self.manifest.summary}，{size:.0f} KB"


# ===================================================================== 导出
def _chapter_rows(session: Session, course_id: str) -> list[dict[str, Any]]:
    chapters = session.scalars(
        select(Chapter).where(Chapter.course_id == course_id).order_by(Chapter.path)
    ).all()
    return [
        {"path": c.path, "title": c.title, "code": c.code, "sort_order": c.sort_order}
        for c in chapters
    ]


def _paper_row(paper: Paper) -> dict[str, Any]:
    return {
        "id": paper.id,
        "course_id": paper.course_id,
        "title": paper.title,
        "meta": paper.meta,
        "template_key": paper.template_key,
        "seed": paper.seed,
        "blueprint": paper.blueprint,
        "sections": [
            {
                "title": section.title,
                "instructions": section.instructions,
                "sort_order": section.sort_order,
                "items": [
                    {
                        "question_id": item.question_id,
                        "sort_order": item.sort_order,
                        "score": item.score,
                        "snapshot": item.snapshot,
                        "option_order": item.option_order,
                    }
                    for item in section.items
                ],
            }
            for section in paper.sections
        ],
    }


def export_bundle(
    session: Session,
    output: Path,
    *,
    course_ids: Sequence[str] | None = None,
    include_papers: bool = True,
    paths: Paths | None = None,
) -> ExportResult:
    """把整个题库（或指定课程）打成一个 ``.qbz``。

    ``course_ids`` 为空表示导出全部课程。
    """
    store = paths or get_paths()
    courses_repo = CourseRepo(session)
    questions_repo = QuestionRepo(session)
    service = QuestionService(session)
    assets = AssetService(session, store)

    courses = [
        course
        for course in courses_repo.list_all()
        if course_ids is None or course.id in set(course_ids)
    ]
    wanted = {course.id for course in courses}

    output.parent.mkdir(parents=True, exist_ok=True)
    asset_hashes: set[str] = set()
    question_total = 0
    paper_total = 0

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            COURSES,
            _jsonl(
                {
                    "id": course.id,
                    "name": course.name,
                    "code": course.code,
                    "description": course.description,
                    "sort_order": course.sort_order,
                    "chapters": _chapter_rows(session, course.id),
                }
                for course in courses
            ),
        )
        archive.writestr(
            TAGS,
            _jsonl(
                {
                    "name": tag.name,
                    "category": tag.category.value,
                    "color": tag.color,
                    "description": tag.description,
                }
                for tag in TagRepo(session).list_all()
            ),
        )

        rows: list[dict[str, Any]] = []
        for course in courses:
            for question in questions_repo.list_all(QuestionFilter(course_id=course.id)):
                draft = service.to_draft(question)
                rows.append(
                    {
                        "id": question.id,
                        "course_id": course.id,
                        "draft": draft.model_dump(mode="json"),
                    }
                )
                asset_hashes.update(_assets_in(draft))
                question_total += 1
        archive.writestr(QUESTIONS, _jsonl(rows))

        if include_papers:
            paper_rows = []
            for paper in PaperRepo(session).list_all():
                if paper.course_id not in wanted:
                    continue
                loaded = PaperRepo(session).get_detailed(paper.id)
                paper_rows.append(_paper_row(loaded))
                for section in loaded.sections:
                    for item in section.items:
                        asset_hashes.update(
                            AssetService.referenced_assets(
                                json.dumps(item.snapshot, ensure_ascii=False)
                            )
                        )
                paper_total += 1
            archive.writestr(PAPERS, _jsonl(paper_rows))

        written = 0
        for sha256 in sorted(asset_hashes):
            source = assets.path_of(sha256)
            if source is None or not source.is_file():
                continue  # 图丢了在导出时不该中断，导入方会看到「图片缺失」
            archive.write(source, arcname=f"{ASSET_PREFIX}{sha256[:2]}/{source.name}")
            written += 1

        manifest = BundleManifest(
            format_version=FORMAT_VERSION,
            qbank_version=__version__,
            exported_at=utcnow().isoformat(),
            course_count=len(courses),
            question_count=question_total,
            paper_count=paper_total,
            asset_count=written,
        )
        archive.writestr(MANIFEST, json.dumps(manifest.to_json(), ensure_ascii=False, indent=2))

    return ExportResult(path=output, manifest=manifest)


def _jsonl(rows: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _assets_in(draft: QuestionDraft) -> set[str]:
    """一道题（含子题）引用到的全部图片哈希。"""
    found = set(AssetService.referenced_assets(draft.stem))
    found.update(AssetService.referenced_assets(draft.answer))
    found.update(AssetService.referenced_assets(draft.explanation))
    found.update(AssetService.referenced_assets(draft.scoring_rubric))
    options = getattr(draft.payload, "options", ())
    for option in options:
        found.update(AssetService.referenced_assets(option.content))
    for sub in draft.sub_questions:
        found.update(_assets_in(sub))
    return found


def default_export_name(courses: Sequence[Course]) -> str:
    """默认文件名。单门课用课程名，多门课用「题库」。"""
    stem = courses[0].name if len(courses) == 1 else "题库"
    return safe_filename(f"{stem} {utcnow():%Y%m%d}") + BUNDLE_SUFFIX


# ===================================================================== 读取
@dataclass(frozen=True, slots=True)
class BundlePlan:
    """导入前的预览：这个包里有什么，导进来会发生什么。"""

    manifest: BundleManifest
    #: 课程名 → 本地是否已存在
    courses: tuple[tuple[str, bool], ...]
    new_questions: int
    existing_questions: int
    papers: int
    assets: int

    @property
    def total_questions(self) -> int:
        return self.new_questions + self.existing_questions

    def describe(self, policy: ConflictPolicy) -> str:
        lines = [f"包内容：{self.manifest.summary}"]
        for name, exists in self.courses:
            lines.append(f"  课程「{name}」：{'已存在，将合并' if exists else '将新建'}")
        lines.append(f"  题目：新建 {self.new_questions} 道")
        if self.existing_questions:
            lines.append(f"      　已存在 {self.existing_questions} 道 → {policy.label}")
        if self.papers:
            lines.append(f"  试卷：新建 {self.papers} 份（试卷从不合并）")
        return "\n".join(lines)


def _read_manifest(archive: zipfile.ZipFile) -> BundleManifest:
    if MANIFEST not in archive.namelist():
        raise BundleError("这不是一个 qbank 交换包：里面没有 manifest.json")
    manifest = BundleManifest.from_json(json.loads(archive.read(MANIFEST)))
    if manifest.format_version > FORMAT_VERSION:
        raise BundleError(
            f"这个包是 qbank {manifest.qbank_version} 导出的（格式版本 "
            f"{manifest.format_version}），当前程序只认到 {FORMAT_VERSION}。请升级 qbank。"
        )
    return manifest


def _rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, Any]]:
    if name not in archive.namelist():
        return []
    info = archive.getinfo(name)
    if info.file_size > MAX_ENTRY_BYTES:
        raise BundleError(f"{name} 有 {info.file_size // 1024 // 1024} MB，超出上限，包可能已损坏")
    rows = []
    for number, line in enumerate(archive.read(name).decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BundleError(f"{name} 第 {number} 行不是合法 JSON：{exc}") from exc
    return rows


def open_bundle(path: Path) -> zipfile.ZipFile:
    if not path.is_file():
        raise BundleError(f"找不到文件：{path}")
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise BundleError(f"{path.name} 不是一个有效的 zip 文件") from exc


def read_manifest(path: Path) -> BundleManifest:
    with open_bundle(path) as archive:
        return _read_manifest(archive)


def plan_import(session: Session, path: Path) -> BundlePlan:
    """看一眼这个包导进来会发生什么。不写任何东西。"""
    repo = QuestionRepo(session)
    courses_repo = CourseRepo(session)
    with open_bundle(path) as archive:
        manifest = _read_manifest(archive)
        course_rows = _rows(archive, COURSES)
        question_rows = _rows(archive, QUESTIONS)
        paper_rows = _rows(archive, PAPERS)
        assets = sum(
            1 for n in archive.namelist() if n.startswith(ASSET_PREFIX) and not n.endswith("/")
        )

    existing = 0
    for row in question_rows:
        if _find_existing(repo, row) is not None:
            existing += 1
    return BundlePlan(
        manifest=manifest,
        courses=tuple(
            (
                str(row.get("name", "")),
                courses_repo.find_by_name(str(row.get("name", ""))) is not None,
            )
            for row in course_rows
        ),
        new_questions=len(question_rows) - existing,
        existing_questions=existing,
        papers=len(paper_rows),
        assets=assets,
    )


def _find_existing(repo: QuestionRepo, row: dict[str, Any]) -> Question | None:
    """包里的这道题在本地已经有了吗？

    先按 id——同一个库导出再导入（恢复场景）时，id 就是最可靠的身份。其次才
    交给 upsert 按 external_id 与指纹去判，那是换一个库导入（共享场景）的情形。

    **先查一次数据库再取对象**，不能直接 ``session.get``：删课程走的是数据库级联
    （``passive_deletes``），SQLAlchemy 不会把那些 Question 从身份映射里清掉，
    于是同一个会话里 ``session.get`` 仍然会把**已经不存在的行**交回来。照它说的
    去接试卷引用，插入时就是一个 FOREIGN KEY constraint failed——而「删掉课程
    再从备份恢复」正是这个包存在的理由。
    """
    bundled_id = str(row.get("id", ""))
    if not bundled_id:
        return None
    alive = repo.session.scalar(select(Question.id).where(Question.id == bundled_id))
    if alive is None:
        return None
    found = repo.session.get(Question, bundled_id)
    return found if found is not None and not found.is_deleted else None


# ===================================================================== 导入
@dataclass(slots=True)
class ImportResult:
    courses: int = 0
    chapters: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    papers: int = 0
    skipped_papers: int = 0
    assets: int = 0
    #: 试卷里指向「这次没导进来的题」的项数。它们的题面仍在快照里，只是点不回原题
    detached_items: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [f"课程 {self.courses}", f"题目新建 {self.created}"]
        if self.updated:
            parts.append(f"更新 {self.updated}")
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.papers:
            parts.append(f"试卷 {self.papers}")
        if self.skipped_papers:
            parts.append(f"试卷已存在 {self.skipped_papers} 份、跳过")
        if self.assets:
            parts.append(f"图片 {self.assets}")
        return "，".join(parts)


class BundleService:
    def __init__(self, session: Session, paths: Paths | None = None) -> None:
        self.session = session
        self.paths = paths or get_paths()
        self.questions = QuestionService(session)
        self.courses = CourseRepo(session)
        self.chapters = ChapterRepo(session)
        self.tags = TagRepo(session)
        self.assets = AssetService(session, self.paths)
        self.papers = PaperRepo(session)

    def import_bundle(
        self,
        path: Path,
        *,
        policy: ConflictPolicy = ConflictPolicy.SKIP,
        include_papers: bool = True,
    ) -> ImportResult:
        """把一个 ``.qbz`` 合并进当前题库。

        图片先导入——题目正文里的 ``asset://`` 要能指到实际存在的图，否则导进来
        的题会全部显示「图片缺失」，而用户会以为包本身是坏的。
        """
        result = ImportResult()
        with open_bundle(path) as archive:
            _read_manifest(archive)
            result.assets = self._import_assets(archive)
            self._import_tags(_rows(archive, TAGS))
            mapping = self._import_courses(_rows(archive, COURSES), result)
            remap = self._import_questions(_rows(archive, QUESTIONS), mapping, policy, result)
            if include_papers:
                self._import_papers(_rows(archive, PAPERS), mapping, remap, result)
        return result

    # ------------------------------------------------------------ 分步
    def _import_assets(self, archive: zipfile.ZipFile) -> int:
        count = 0
        for name in archive.namelist():
            if not name.startswith(ASSET_PREFIX) or name.endswith("/"):
                continue
            info = archive.getinfo(name)
            if info.file_size > MAX_ENTRY_BYTES:
                continue
            data = archive.read(name)
            try:
                # 内容寻址：同一张图重复导入是幂等的
                self.assets.store_bytes(data, original_name=Path(name).name)
                count += 1
            except ValueError:
                continue  # 包里混进了不认识的文件类型，跳过而不是中断整次导入
        return count

    def _import_tags(self, rows: Sequence[dict[str, Any]]) -> None:
        """先建标签，把分类带过来。

        题目草稿里只有标签名字，不先建的话全部会落到默认分类，而标签分类正是
        用来防止标签列表膨胀成几百个平铺词的。
        """
        for row in rows:
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            try:
                category = TagCategory(str(row.get("category", TagCategory.KNOWLEDGE.value)))
            except ValueError:
                category = TagCategory.KNOWLEDGE
            self.tags.get_or_create(name, category=category)

    def _import_courses(
        self, rows: Sequence[dict[str, Any]], result: ImportResult
    ) -> dict[str, str]:
        """课程按**名字**合并，返回「包内 id → 本地 id」。

        不按 id 合并：换一台机器导入时，同名课程几乎总是同一门课，而 id 一定
        不同。按 id 的话会凭空多出一门重名课程，侧栏里两个「数据结构」，
        而用户完全不知道哪个是哪个。
        """
        mapping: dict[str, str] = {}
        for row in rows:
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            found = self.courses.find_by_name(name)
            if found is None:
                found = self.courses.create(
                    name, code=str(row.get("code", "")), description=str(row.get("description", ""))
                )
                result.courses += 1
            mapping[str(row.get("id", ""))] = found.id
            for chapter in row.get("chapters", []):
                path = str(chapter.get("path", "")).strip()
                if path and self.chapters.find_by_path(found.id, path) is None:
                    self.chapters.ensure_path(found.id, path)
                    result.chapters += 1
        self.session.flush()
        return mapping

    def _import_questions(
        self,
        rows: Sequence[dict[str, Any]],
        courses: dict[str, str],
        policy: ConflictPolicy,
        result: ImportResult,
    ) -> dict[str, str]:
        """返回「包内题目 id → 本地题目 id」，供试卷接回引用。"""
        remap: dict[str, str] = {}
        for number, row in enumerate(rows, 1):
            course_id = courses.get(str(row.get("course_id", "")))
            if course_id is None:
                result.errors.append(f"第 {number} 道题引用了包里没有的课程，已跳过")
                continue
            try:
                draft = QuestionDraft.model_validate(row.get("draft", {}))
            except Exception as exc:  # pydantic 的异常类型不止一种
                result.errors.append(f"第 {number} 道题校验失败：{exc}")
                continue

            bundled_id = str(row.get("id", ""))
            existing = _find_existing(QuestionRepo(self.session), row)
            if existing is not None:
                remap[bundled_id] = existing.id
                if policy is ConflictPolicy.SKIP:
                    result.skipped += 1
                elif policy is ConflictPolicy.CREATE_COPY:
                    # 另存为新题：这一份必须换 id，否则就不是「另一道题」了
                    created = self.questions.create(course_id, draft)
                    remap[bundled_id] = created.id
                    result.created += 1
                else:
                    self.questions.update(existing.id, draft, note="交换包导入")
                    result.updated += 1
                continue

            # 沿用包里的 id：同一道题在不同题库里保持同一个身份，更新后的包
            # 再发回来时才认得出（见 QuestionService.create 的说明）
            outcome = self.questions.upsert(
                course_id, draft, policy=policy, question_id=bundled_id or None
            )
            if outcome.question is not None:
                remap[bundled_id] = outcome.question.id
            result.created += int(outcome.created)
            result.updated += int(outcome.updated)
            result.skipped += int(outcome.skipped)
        self.session.flush()
        return remap

    def _import_papers(
        self,
        rows: Sequence[dict[str, Any]],
        courses: dict[str, str],
        remap: dict[str, str],
        result: ImportResult,
    ) -> None:
        """试卷不合并，但**按 id 认重**。

        「不合并」是说没有「把两份试卷并成一份」这种语义；但同一个包导两次不该
        变出两份一模一样的卷子——备份重复导入是最常见的误操作，而多出来的那份
        和原件长得一模一样，发现时已经分不清哪份是哪份了。所以带着原 id 建，
        再导一次就认得出来。

        题面冻结在 ``snapshot`` 里，所以即使一道原题都没导进来，卷子照样完整；
        ``question_id`` 接不回去的就置空，并计数——那只影响「跳到原题」。
        """
        for row in rows:
            course_id = courses.get(str(row.get("course_id", "")))
            if course_id is None:
                continue
            bundled_id = str(row.get("id", "")) or new_id()
            # 同样不能用 session.get：删课程时试卷也是被数据库级联掉的，
            # 身份映射里却还留着（见 _find_existing 的说明）
            if self.session.scalar(select(Paper.id).where(Paper.id == bundled_id)) is not None:
                result.skipped_papers += 1
                continue
            paper = Paper(
                id=bundled_id,
                course_id=course_id,
                title=str(row.get("title", "未命名试卷")),
                meta=row.get("meta") or {},
                template_key=str(row.get("template_key", "standard")),
                seed=row.get("seed"),
                blueprint=row.get("blueprint") or {},
            )
            for section_row in row.get("sections", []):
                section = PaperSection(
                    title=str(section_row.get("title", "")),
                    instructions=str(section_row.get("instructions", "")),
                    sort_order=int(section_row.get("sort_order", 0)),
                )
                for item_row in section_row.get("items", []):
                    original = item_row.get("question_id")
                    linked = remap.get(str(original)) if original else None
                    if original and linked is None:
                        result.detached_items += 1
                    section.items.append(
                        PaperItem(
                            question_id=linked,
                            sort_order=int(item_row.get("sort_order", 0)),
                            score=float(item_row.get("score", 0.0)),
                            snapshot=item_row.get("snapshot") or {},
                            option_order=item_row.get("option_order") or [],
                        )
                    )
                paper.sections.append(section)
            self.session.add(paper)
            self.session.flush()
            self.papers.sync_usage(paper.id)
            result.papers += 1
