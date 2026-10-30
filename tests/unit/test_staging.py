"""导入暂存区测试。

暂存区的作用是让上传的文件在「预览」和「确认」两次请求之间存活。
安全上最要紧的一条是 token 校验——它直接参与拼路径。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from qbank.config import Paths
from qbank.web import staging


def test_stage_and_find(isolate_data_dir: Paths) -> None:
    staged = staging.stage(isolate_data_dir, b"content", "题库.md")
    assert staged.path.read_bytes() == b"content"
    assert staging.find(isolate_data_dir, staged.token).path == staged.path


def test_original_name_is_recoverable(isolate_data_dir: Paths) -> None:
    staged = staging.stage(isolate_data_dir, b"x", "历年真题.docx")
    assert staged.original_name == "历年真题.docx"


def test_suffix_is_preserved(isolate_data_dir: Paths) -> None:
    """后缀决定走哪条解析路径（docx 要过 pandoc，md 直接读），丢了就没法判断格式。"""
    staged = staging.stage(isolate_data_dir, b"x", "a/b/c/题库.DOCX")
    assert staged.path.suffix == ".docx"


def test_unsafe_filename_is_cleaned(isolate_data_dir: Paths) -> None:
    staged = staging.stage(isolate_data_dir, b"x", "../../etc/passwd.md")
    assert ".." not in staged.path.name
    assert staged.path.parent == staging.staging_dir(isolate_data_dir)


def test_empty_file_rejected(isolate_data_dir: Paths) -> None:
    with pytest.raises(staging.StagingError, match="空的"):
        staging.stage(isolate_data_dir, b"", "a.md")


def test_missing_suffix_rejected(isolate_data_dir: Paths) -> None:
    with pytest.raises(staging.StagingError, match="扩展名"):
        staging.stage(isolate_data_dir, b"x", "没有后缀")


def test_oversized_file_rejected(isolate_data_dir: Paths) -> None:
    oversized = b"x" * (staging.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(staging.StagingError, match="超过"):
        staging.stage(isolate_data_dir, oversized, "a.md")


@pytest.mark.parametrize(
    "token",
    ["", "../../../etc/passwd", "not-hex", "abc", "0" * 31, "0" * 33, "g" * 32],
)
def test_invalid_token_rejected(isolate_data_dir: Paths, token: str) -> None:
    """token 直接参与拼路径，放任何非十六进制字符进来都是目录穿越漏洞。"""
    with pytest.raises(staging.StagingError):
        staging.find(isolate_data_dir, token)


def test_unknown_token_gives_actionable_message(isolate_data_dir: Paths) -> None:
    with pytest.raises(staging.StagingError, match="重新上传"):
        staging.find(isolate_data_dir, "0" * 32)


def test_discard_removes_file(isolate_data_dir: Paths) -> None:
    staged = staging.stage(isolate_data_dir, b"x", "a.md")
    staging.discard(isolate_data_dir, staged.token)
    assert not staged.path.exists()
    with pytest.raises(staging.StagingError):
        staging.find(isolate_data_dir, staged.token)


def test_discard_is_idempotent(isolate_data_dir: Paths) -> None:
    staging.discard(isolate_data_dir, "0" * 32)  # 不存在也不该抛


def test_sweep_removes_stale_only(isolate_data_dir: Paths) -> None:
    """用户中途关掉浏览器是常态，不能指望他点「丢弃」。"""
    import os
    import time

    old = staging.stage(isolate_data_dir, b"old", "old.md")
    fresh = staging.stage(isolate_data_dir, b"fresh", "fresh.md")
    stale_time = time.time() - timedelta(days=2).total_seconds()
    os.utime(old.path, (stale_time, stale_time))

    assert staging.sweep(isolate_data_dir) == 1
    assert not old.path.exists()
    assert fresh.path.exists()


def test_tokens_are_unique(isolate_data_dir: Paths) -> None:
    """同一个人开两个标签页各传一个文件，不能互相覆盖。"""
    first = staging.stage(isolate_data_dir, b"a", "same.md")
    second = staging.stage(isolate_data_dir, b"b", "same.md")
    assert first.token != second.token
    assert first.path.read_bytes() == b"a"
    assert second.path.read_bytes() == b"b"
