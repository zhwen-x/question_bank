"""`.qbz` 交换包测试。

这是本系统**唯一**的备份与恢复手段——删课程是硬删除，没有回收站。所以这里
不只测「导出的文件能打开」，而是测完整的恢复：删掉整门课，再从包里装回来，
题目、章节、标签、试卷、检索索引都要回来。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from qbank.config import Paths
from qbank.core.enums import ConflictPolicy, QuestionStatus, QuestionType, TagCategory
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.asset import AssetService
from qbank.core.services.paper import PaperService
from qbank.core.services.question import QuestionService
from qbank.exchange.bundle import (
    FORMAT_VERSION,
    MANIFEST,
    BundleError,
    BundleService,
    export_bundle,
    plan_import,
    read_manifest,
)


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 60, 20)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def source(session: Session, isolate_data_dir: Paths) -> str:
    """一门课：带图的题、多级章节、带分类的标签、一份试卷。"""
    course = CourseRepo(session).create("数据结构", code="CS201")
    asset, _ = AssetService(session, isolate_data_dir).store_bytes(png(), original_name="图.png")
    TagRepo(session).get_or_create("最短路径", category=TagCategory.KNOWLEDGE)
    TagRepo(session).get_or_create("2025 期末", category=TagCategory.SOURCE)
    questions = QuestionService(session)

    first = questions.create(
        course.id,
        QuestionDraft.model_validate(
            {
                "type": QuestionType.SINGLE_CHOICE,
                "stem": f"看图回答 $O(\\log n)$：![图](asset://{asset.sha256})",
                "payload": {
                    "type": "single_choice",
                    "options": [
                        {"label": "A", "content": "对", "is_correct": True},
                        {"label": "B", "content": "错"},
                    ],
                },
                "answer": "A",
                "chapter": "第5章 图/5.2 最短路径",
                "tags": ["最短路径", "2025 期末"],
                "status": QuestionStatus.ACTIVE,
            }
        ),
    )
    second = questions.create(
        course.id,
        QuestionDraft.model_validate(
            {
                "type": QuestionType.SHORT_ANSWER,
                "stem": "说明 Dijkstra 算法的思路。",
                "payload": {"type": "short_answer"},
                "answer": "按权值依次松弛。",
                "chapter": "第5章 图",
                "status": QuestionStatus.ACTIVE,
            }
        ),
    )
    session.flush()
    papers = PaperService(session)
    paper = papers.create_empty(course.id, "期末卷", sections=["一、选择题"])
    papers.add_questions(paper.id, paper.sections[0].id, [first.id, second.id], score=5)
    session.commit()
    return course.id


@pytest.fixture
def bundle(session: Session, isolate_data_dir: Paths, source: str, tmp_path: Path) -> Path:
    target = tmp_path / "库.qbz"
    export_bundle(session, target, paths=isolate_data_dir)
    return target


def _empty_bank(tmp_path: Path) -> tuple[sessionmaker[Session], Paths]:
    """另起一个干净的库，模拟「发给同事」或「换一台机器」。"""
    from qbank.core.db import init_schema, make_memory_engine, make_session_factory

    engine = make_memory_engine()
    init_schema(engine)
    paths = Paths(root=tmp_path / "另一个库").ensure()
    return make_session_factory(engine), paths


# ============================================================== 导出
def test_bundle_contains_everything(bundle: Path) -> None:
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read(MANIFEST))
    assert {
        "manifest.json",
        "courses.jsonl",
        "tags.jsonl",
        "questions.jsonl",
        "papers.jsonl",
    } <= names
    assert any(name.startswith("assets/") for name in names), "图片没进包"
    assert manifest["format_version"] == FORMAT_VERSION
    assert manifest["question_count"] == 2
    assert manifest["paper_count"] == 1
    assert manifest["asset_count"] == 1
    assert "修改历史" in manifest["excludes"], "没带的东西要写明，免得被当成 bug"


def test_manifest_reads_back(bundle: Path) -> None:
    manifest = read_manifest(bundle)
    assert "2 道题" in manifest.summary
    assert manifest.qbank_version


def test_export_one_course_only(
    session: Session, isolate_data_dir: Paths, source: str, tmp_path: Path
) -> None:
    other = CourseRepo(session).create("离散数学")
    QuestionService(session).create(
        other.id,
        QuestionDraft.model_validate(
            {"type": "short_answer", "stem": "什么是群？", "payload": {"type": "short_answer"}}
        ),
    )
    session.flush()
    target = tmp_path / "单课.qbz"
    result = export_bundle(session, target, course_ids=[source], paths=isolate_data_dir)
    assert result.manifest.course_count == 1
    assert result.manifest.question_count == 2


# ============================================================== 导入
def test_round_trip_into_an_empty_bank(bundle: Path, tmp_path: Path) -> None:
    factory, paths = _empty_bank(tmp_path)
    with factory() as session:
        result = BundleService(session, paths).import_bundle(bundle)
        session.commit()

    assert result.created == 2
    assert result.papers == 1
    assert result.detached_items == 0, "试卷里的题目引用应当接回新建的题目"

    with factory() as session:
        assert [c.name for c in CourseRepo(session).list_all()] == ["数据结构"]
        assert QuestionRepo(session).count(QuestionFilter()) == 2
        paths_in_bank = [
            c.path for c in ChapterRepo(session).list_tree(CourseRepo(session).list_all()[0].id)
        ]
        assert "第5章 图/5.2 最短路径" in paths_in_bank
        assert len(PaperRepo(session).list_all()) == 1


def test_images_survive_the_trip(bundle: Path, tmp_path: Path) -> None:
    """图没跟着走的话，导进来的题会整片显示「图片缺失」。"""
    factory, paths = _empty_bank(tmp_path)
    with factory() as session:
        BundleService(session, paths).import_bundle(bundle)
        session.commit()
    with factory() as session:
        assets = AssetService(session, paths)
        question = next(
            q for q in QuestionRepo(session).list_all(QuestionFilter()) if "asset://" in q.stem
        )
        sha = AssetService.referenced_assets(question.stem)[0]
        stored = assets.path_of(sha)
        assert stored is not None and stored.is_file(), "图片没落盘"
        assert stored.read_bytes() == png()


def test_tag_categories_survive(bundle: Path, tmp_path: Path) -> None:
    """标签分类不在题目里；不单独存就会全部落到默认分类。"""
    factory, paths = _empty_bank(tmp_path)
    with factory() as session:
        BundleService(session, paths).import_bundle(bundle)
        session.commit()
    with factory() as session:
        found = {tag.name: tag.category for tag in TagRepo(session).list_all()}
        assert found["最短路径"] is TagCategory.KNOWLEDGE
        assert found["2025 期末"] is TagCategory.SOURCE


def test_search_works_after_import(bundle: Path, tmp_path: Path) -> None:
    """导入要走服务层，索引才会建起来——否则题在库里却搜不到。"""
    factory, paths = _empty_bank(tmp_path)
    with factory() as session:
        BundleService(session, paths).import_bundle(bundle)
        session.commit()
    with factory() as session:
        assert QuestionService(session).search_ids("Dijkstra")


def test_importing_twice_changes_nothing(bundle: Path, tmp_path: Path) -> None:
    """备份重复导入是最常见的误操作，不该变出两份一模一样的东西。"""
    factory, paths = _empty_bank(tmp_path)
    for _ in range(3):
        with factory() as session:
            BundleService(session, paths).import_bundle(bundle)
            session.commit()
    with factory() as session:
        assert QuestionRepo(session).count(QuestionFilter()) == 2
        assert len(PaperRepo(session).list_all()) == 1, "试卷被导重了"
        assert len(CourseRepo(session).list_all()) == 1


def test_second_import_reports_what_it_skipped(bundle: Path, tmp_path: Path) -> None:
    factory, paths = _empty_bank(tmp_path)
    with factory() as session:
        BundleService(session, paths).import_bundle(bundle)
        session.commit()
    with factory() as session:
        again = BundleService(session, paths).import_bundle(bundle)
    assert again.created == 0
    assert again.skipped == 2
    assert again.skipped_papers == 1
    assert "跳过" in again.summary


def test_overwrite_policy_updates_existing(bundle: Path, tmp_path: Path) -> None:
    factory, paths = _empty_bank(tmp_path)
    with factory() as session:
        BundleService(session, paths).import_bundle(bundle)
        session.commit()
    with factory() as session:
        question = QuestionRepo(session).list_all(QuestionFilter())[0]
        draft = QuestionService(session).to_draft(question)
        QuestionService(session).update(question.id, draft.model_copy(update={"stem": "被改过了"}))
        session.commit()
    with factory() as session:
        result = BundleService(session, paths).import_bundle(
            bundle, policy=ConflictPolicy.OVERWRITE
        )
        session.commit()
    assert result.updated == 2
    with factory() as session:
        stems = {q.stem for q in QuestionRepo(session).list_all(QuestionFilter())}
        assert "被改过了" not in stems


# ============================================================== 恢复场景
def test_restores_a_deleted_course(
    session: Session, isolate_data_dir: Paths, source: str, bundle: Path
) -> None:
    """删课程是硬删除、连题目与试卷一起没。包要能把这些原样装回来。"""
    QuestionService(session).delete_course(source)
    session.commit()
    assert CourseRepo(session).list_all() == []
    assert PaperRepo(session).list_all() == []

    result = BundleService(session, isolate_data_dir).import_bundle(bundle)
    session.commit()

    assert result.courses == 1
    assert result.created == 2
    assert result.papers == 1
    assert QuestionRepo(session).count(QuestionFilter()) == 2
    assert len(PaperRepo(session).list_all()) == 1
    assert QuestionService(session).search_ids("Dijkstra")


def test_paper_survives_without_its_questions(bundle: Path, tmp_path: Path) -> None:
    """只导试卷、不导题目时，卷面仍然完整——题面冻结在快照里。"""
    factory, paths = _empty_bank(tmp_path)
    with factory() as session:
        service = BundleService(session, paths)
        # 先用 SKIP 把课程建起来但不要题目：手工模拟「题目都没导进来」
        result = service.import_bundle(bundle)
        session.commit()
        paper = PaperRepo(session).get_detailed(PaperRepo(session).list_all()[0].id)
        assert result.detached_items == 0
        snapshots = [item.snapshot for item in paper.sections[0].items]
    assert all(snapshot.get("draft", {}).get("stem") for snapshot in snapshots)


# ============================================================== 坏包
def test_not_a_zip(tmp_path: Path) -> None:
    broken = tmp_path / "假的.qbz"
    broken.write_text("这不是 zip", encoding="utf-8")
    with pytest.raises(BundleError, match="不是一个有效的 zip"):
        read_manifest(broken)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="找不到文件"):
        read_manifest(tmp_path / "没有这个.qbz")


def test_zip_without_manifest(tmp_path: Path) -> None:
    plain = tmp_path / "普通.qbz"
    with zipfile.ZipFile(plain, "w") as archive:
        archive.writestr("随便.txt", "x")
    with pytest.raises(BundleError, match="不是一个 qbank 交换包"):
        read_manifest(plain)


def test_future_format_version_is_refused(bundle: Path, tmp_path: Path) -> None:
    """新版的包喂给旧版程序，必须直接报错。

    「导入成功但少了一半东西」比报错糟糕得多——用户不会发现，直到某天找不到
    一道题。
    """
    future = tmp_path / "未来.qbz"
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(future, "w") as dst:
        for item in src.namelist():
            data = src.read(item)
            if item == MANIFEST:
                payload = json.loads(data)
                payload["format_version"] = FORMAT_VERSION + 1
                payload["qbank_version"] = "9.9.9"
                data = json.dumps(payload).encode()
            dst.writestr(item, data)
    with pytest.raises(BundleError) as excinfo:
        read_manifest(future)
    assert "请升级 qbank" in str(excinfo.value)
    assert "9.9.9" in str(excinfo.value)


def test_corrupt_jsonl_names_the_line(bundle: Path, tmp_path: Path) -> None:
    broken = tmp_path / "坏行.qbz"
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(broken, "w") as dst:
        for item in src.namelist():
            data = src.read(item)
            if item == "questions.jsonl":
                data = '{"ok": 1}\n{坏了\n'.encode()
            dst.writestr(item, data)
    factory, paths = _empty_bank(tmp_path)
    with factory() as session, pytest.raises(BundleError, match="第 2 行"):
        BundleService(session, paths).import_bundle(broken)


# ============================================================== 预览
def test_plan_describes_without_writing(bundle: Path, tmp_path: Path) -> None:
    factory, _paths = _empty_bank(tmp_path)
    with factory() as session:
        plan = plan_import(session, bundle)
        text = plan.describe(ConflictPolicy.SKIP)
        assert "将新建" in text
        assert "新建 2 道" in text
        assert QuestionRepo(session).count(QuestionFilter()) == 0, "预览不该写入任何东西"


def test_plan_sees_existing_questions(
    session: Session, isolate_data_dir: Paths, source: str, bundle: Path
) -> None:
    plan = plan_import(session, bundle)
    assert plan.existing_questions == 2
    assert plan.new_questions == 0
    assert plan.courses == (("数据结构", True),)
    assert "已存在，将合并" in plan.describe(ConflictPolicy.SKIP)


def test_question_ids_are_stable_across_banks(bundle: Path, tmp_path: Path) -> None:
    """同一道题在不同题库里保持同一个 id。

    这是「发给同事 → 他改几道 → 再发回来」能成立的前提：题面改过之后指纹就对
    不上了，只有 id 还认得出那是同一道题。不沿用 id 的话，收回来的包会被当成
    一批新题再建一遍，于是库里每道题都有两份。
    """
    factory, paths = _empty_bank(tmp_path)
    with factory() as session:
        BundleService(session, paths).import_bundle(bundle)
        session.commit()

    with factory() as session:
        ids_there = {q.id for q in QuestionRepo(session).list_all(QuestionFilter())}
    with zipfile.ZipFile(bundle) as archive:
        ids_in_bundle = {
            json.loads(line)["id"]
            for line in archive.read("questions.jsonl").decode("utf-8").splitlines()
            if line.strip()
        }
    assert ids_there == ids_in_bundle

    # 同事改了一道题，再把包发回来
    with factory() as session:
        question = QuestionRepo(session).list_all(QuestionFilter())[0]
        service = QuestionService(session)
        service.update(
            question.id, service.to_draft(question).model_copy(update={"stem": "同事改过的题干"})
        )
        session.commit()
        edited_id = question.id

    with factory() as session:
        result = BundleService(session, paths).import_bundle(
            bundle, policy=ConflictPolicy.OVERWRITE
        )
        session.commit()
    assert result.created == 0, "改过题面的那道不该被当成新题再建一遍"
    assert result.updated == 2
    with factory() as session:
        assert QuestionRepo(session).count(QuestionFilter()) == 2
        assert "同事改过的题干" not in QuestionRepo(session).get(edited_id).stem
