from __future__ import annotations

from pathlib import Path

import pytest

from qbank.config import Paths, safe_filename


def test_all_dirs_created(isolate_data_dir: Paths) -> None:
    for directory in isolate_data_dir.all_dirs():
        assert directory.is_dir(), f"{directory} 未创建"


def test_ensure_is_idempotent(isolate_data_dir: Paths) -> None:
    isolate_data_dir.ensure().ensure()
    assert isolate_data_dir.root.is_dir()


def test_asset_path_is_bucketed() -> None:
    paths = Paths(root=Path("/data"))
    got = paths.asset_path("ab12cd34ef", ".png")
    assert got == Path("/data/assets/ab/ab12cd34ef.png")


def test_asset_path_rejects_short_hash() -> None:
    with pytest.raises(ValueError, match="过短"):
        Paths(root=Path("/data")).asset_path("a", ".png")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("数据结构 期末A卷", "数据结构 期末A卷"),
        ("2026/01 卷", "2026_01 卷"),
        ('a<>:"/\\|?*b', "a_________b"),
        ("CON", "CON_"),  # Windows 保留设备名
        ("lpt3", "lpt3_"),  # 大小写不敏感
        ("  trailing.  ", "trailing"),
        ("...", "untitled"),
        ("", "untitled"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


def test_safe_filename_truncates() -> None:
    assert len(safe_filename("甲" * 500)) == 120


def test_safe_filename_strips_control_chars() -> None:
    assert safe_filename("a\x00\x1fb") == "a__b"
