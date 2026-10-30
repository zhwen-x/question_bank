from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from qbank.cli import app
from qbank.config import Paths
from qbank.doctor import Check, Level, Status, run_checks

runner = CliRunner()


def test_all_required_checks_pass(isolate_data_dir: Paths) -> None:
    """开发环境里所有必需项都应通过；失败说明本地环境装得不全。"""
    blocking = [c.name for c in run_checks() if c.is_blocking]
    assert not blocking, f"必需能力缺失：{blocking}"


def test_every_non_ok_check_offers_a_remedy() -> None:
    """降级或缺失时必须给出下一步动作，不许只报错不给路。"""
    for check in run_checks():
        if check.status is not Status.OK:
            assert check.remedy, f"{check.name} 状态为 {check.status} 但没给修复提示"


def test_doctor_exit_code_zero_when_only_optional_missing(isolate_data_dir: Paths) -> None:
    """可选依赖缺失不该让 CI 变红。"""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "数据目录" in result.output


def test_doctor_reports_unwritable_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据目录建不出来时必须是阻断性错误，退出码非 0。

    这里把数据目录指到一个**普通文件下面**的路径——mkdir 在三平台上都会抛
    ``NotADirectoryError``，比依赖 ``/proc`` 或文件权限位更可移植。
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("QBANK_DATA_DIR", str(blocker / "qbank"))
    from qbank import config

    config.get_paths.cache_clear()
    try:
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1, result.output
        assert "阻断性问题" in result.output
    finally:
        config.get_paths.cache_clear()


def test_blocking_only_counts_required_level() -> None:
    optional_missing = Check("x", Level.OPTIONAL, Status.MISSING, "d", "r")
    required_missing = Check("y", Level.REQUIRED, Status.MISSING, "d", "r")
    assert not optional_missing.is_blocking
    assert required_missing.is_blocking


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "qbank" in result.output
