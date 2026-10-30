"""图片资源测试。

内容寻址是这一层的核心价值：同一张图被多道题引用只存一份，而题目正文里写的是
``asset://<sha256>`` 而不是路径，所以题库搬目录、打包发给同事都不会断链。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from qbank.config import Paths
from qbank.core.services.asset import (
    AssetService,
    UnsupportedAssetError,
    sha256_of,
)


def png_bytes(
    size: tuple[int, int] = (120, 80), color: tuple[int, int, int] = (200, 30, 30)
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def assets(session: Session, isolate_data_dir: Paths) -> AssetService:
    return AssetService(session, isolate_data_dir)


@pytest.fixture
def question_ids(session: Session) -> list[str]:
    """两道真实题目。

    用真 id 而不是 ``"q1"`` 这种占位串——``question_asset`` 上有外键，
    占位串会被数据库直接拒掉（这也顺带证明 PRAGMA foreign_keys 确实生效了）。
    """
    from qbank.core.enums import QuestionType
    from qbank.core.repo.course import CourseRepo
    from qbank.core.schemas import QuestionDraft
    from qbank.core.services.question import QuestionService

    course = CourseRepo(session).create("测试课")
    service = QuestionService(session)
    return [
        service.create(
            course.id,
            QuestionDraft.model_validate(
                {
                    "type": QuestionType.SHORT_ANSWER,
                    "stem": stem,
                    "payload": {"type": "short_answer"},
                }
            ),
        ).id
        for stem in ("第一道题", "第二道题")
    ]


@pytest.fixture
def media(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    (directory / "fig1.png").write_bytes(png_bytes())
    (directory / "fig2.png").write_bytes(png_bytes(color=(30, 200, 30)))
    (directory / "copy.png").write_bytes(png_bytes())  # 与 fig1 内容相同
    (directory / "notes.txt").write_text("不是图片", encoding="utf-8")
    return directory


# ------------------------------------------------------------------ 存储
def test_store_detects_type_and_size(assets: AssetService, media: Path) -> None:
    asset, created = assets.store_file(media / "fig1.png")
    assert created
    assert asset.mime == "image/png"
    assert (asset.width, asset.height) == (120, 80)
    assert asset.byte_size > 0
    assert asset.sha256 == sha256_of((media / "fig1.png").read_bytes())


def test_identical_content_is_stored_once(assets: AssetService, media: Path) -> None:
    """同一张图在历年卷子里被引用几十次是常态。"""
    first, created_first = assets.store_file(media / "fig1.png")
    second, created_second = assets.store_file(media / "copy.png")
    assert created_first and not created_second
    assert first.id == second.id


def test_file_lands_in_bucketed_path(assets: AssetService, media: Path) -> None:
    asset, _ = assets.store_file(media / "fig1.png")
    path = assets.path_of(asset.sha256)
    assert path is not None and path.is_file()
    assert path.parent.name == asset.sha256[:2], "按哈希前两位分桶，避免单目录文件过多"


def test_type_detection_ignores_wrong_extension(assets: AssetService, tmp_path: Path) -> None:
    """``.jpg`` 里装着 PNG 很常见，pandoc 从 docx 抽出的媒体文件尤其如此。"""
    lying = tmp_path / "actually_png.jpg"
    lying.write_bytes(png_bytes())
    asset, _ = assets.store_file(lying)
    assert asset.mime == "image/png"
    assert asset.suffix == ".png"


def test_non_image_is_rejected(assets: AssetService, media: Path) -> None:
    with pytest.raises(UnsupportedAssetError, match="不是受支持的图片类型"):
        assets.store_file(media / "notes.txt")


def test_empty_content_is_rejected(assets: AssetService) -> None:
    with pytest.raises(UnsupportedAssetError, match="为空"):
        assets.store_bytes(b"")


def test_missing_file_is_restored_from_content(assets: AssetService, media: Path) -> None:
    """库里有记录但文件没了（清过目录、从别处拷了 db）时要能补回来。"""
    asset, _ = assets.store_file(media / "fig1.png")
    path = assets.path_of(asset.sha256)
    assert path is not None
    path.unlink()
    assert assets.path_of(asset.sha256) is None
    assets.store_file(media / "fig1.png")
    assert assets.path_of(asset.sha256) is not None


def test_large_image_is_warned_not_rejected(assets: AssetService, tmp_path: Path) -> None:
    """老师贴手机照片很常见。提示就够了，不该拦住导入，也不该擅自压缩。"""
    big = tmp_path / "big.png"
    big.write_bytes(png_bytes(size=(3000, 2400)))
    asset, _ = assets.store_file(big)
    warnings = assets.warnings_for(asset.sha256)
    assert warnings and "偏大" in warnings[0].message


# -------------------------------------------------------------- 引用改写
def test_absorb_markdown_images(assets: AssetService, media: Path) -> None:
    markdown = "见图 ![流程](fig1.png) 与 ![对比](fig2.png)"
    rewritten, digests, errors = assets.absorb_local_images(markdown, media)
    assert not errors
    assert len(digests) == 2
    assert "asset://" in rewritten
    assert "fig1.png" not in rewritten


def test_absorb_html_images(assets: AssetService, media: Path) -> None:
    """从 Word 导入时带尺寸的图片是 HTML ``<img>``，不是 Markdown 语法。"""
    markdown = '<img src="fig1.png" style="width:1.5in" />'
    rewritten, digests, errors = assets.absorb_local_images(markdown, media)
    assert not errors and len(digests) == 1
    assert 'src="asset://' in rewritten
    assert 'style="width:1.5in"' in rewritten, "尺寸信息应保留"


def test_absorb_handles_absolute_paths(assets: AssetService, media: Path) -> None:
    """pandoc 抽出的媒体路径是绝对路径。"""
    markdown = f"![图]({(media / 'fig1.png').as_posix()})"
    _, digests, errors = assets.absorb_local_images(markdown, media)
    assert not errors and len(digests) == 1


def test_absorb_leaves_external_urls_alone(assets: AssetService, media: Path) -> None:
    markdown = "![外链](https://example.com/a.png) ![内联](data:image/png;base64,AAA)"
    rewritten, digests, errors = assets.absorb_local_images(markdown, media)
    assert rewritten == markdown
    assert not digests and not errors


def test_missing_image_does_not_fail_the_question(assets: AssetService, media: Path) -> None:
    """丢一张图远好过丢一整道题；报告会告诉用户丢了哪张。"""
    markdown = "![有的](fig1.png) ![没的](gone.png)"
    rewritten, digests, errors = assets.absorb_local_images(markdown, media)
    assert len(digests) == 1
    assert len(errors) == 1 and "gone.png" in errors[0]
    assert "gone.png" in rewritten, "找不到的图保留原引用，便于用户自己补"


def test_already_absorbed_references_are_counted_not_rewritten(
    assets: AssetService, media: Path
) -> None:
    asset, _ = assets.store_file(media / "fig1.png")
    markdown = f"![图](asset://{asset.sha256})"
    rewritten, digests, errors = assets.absorb_local_images(markdown, media)
    assert rewritten == markdown
    assert digests == [asset.sha256]
    assert not errors


def test_referenced_assets_finds_both_syntaxes(assets: AssetService) -> None:
    markdown = '![a](asset://aaaa) <img src="asset://bbbb" /> ![a](asset://aaaa)'
    assert assets.referenced_assets(markdown) == ["aaaa", "bbbb"]


# ------------------------------------------------------------ 引用计数
def test_link_sets_reference_counts(
    assets: AssetService, media: Path, question_ids: list[str]
) -> None:
    first, _ = assets.store_file(media / "fig1.png")
    second, _ = assets.store_file(media / "fig2.png")
    assets.link(question_ids[0], [first.sha256, second.sha256])
    assets.link(question_ids[1], [first.sha256])
    assert assets.get(first.sha256).ref_count == 2
    assert assets.get(second.sha256).ref_count == 1


def test_link_replaces_rather_than_accumulates(
    assets: AssetService, media: Path, question_ids: list[str]
) -> None:
    """题目正文被编辑后，哪些图还在用只能由正文决定，增量维护一定会漏。"""
    first, _ = assets.store_file(media / "fig1.png")
    second, _ = assets.store_file(media / "fig2.png")
    assets.link(question_ids[0], [first.sha256, second.sha256])
    assets.link(question_ids[0], [second.sha256])
    assert assets.get(first.sha256).ref_count == 0
    assert assets.get(second.sha256).ref_count == 1


def test_orphans_and_purge(assets: AssetService, media: Path, question_ids: list[str]) -> None:
    used, _ = assets.store_file(media / "fig1.png")
    orphan, _ = assets.store_file(media / "fig2.png")
    assets.link(question_ids[0], [used.sha256])

    assert [a.sha256 for a in assets.orphans()] == [orphan.sha256]
    removed, freed = assets.purge_orphans()
    assert removed == 1 and freed > 0
    assert assets.orphans() == []
    assert assets.path_of(used.sha256) is not None, "还在用的图不能被删"
    assert assets.get(orphan.sha256) is None
