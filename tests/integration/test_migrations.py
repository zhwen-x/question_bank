"""迁移与模型必须始终一致。

这组测试守住两件容易出事的地方：

1. ``alembic upgrade head`` 建出来的库，要和 ``init_schema()``（``create_all``
   快路径，测试与首次启动用）建出来的完全一样。两者一旦漂移，就会出现
   「测试全绿、用户升级后炸」。
2. FTS5 虚表的 DDL 在迁移脚本里是**内联**的（迁移必须是不可变快照），和
   ``qbank.core.db.FTS_DDL`` 存在刻意的重复。第 1 条顺带把这份重复也锁住了。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from qbank.config import sqlite_url
from qbank.core.db import init_schema, make_engine

#: alembic 自己的版本表不参与比较
IGNORED_TABLES = frozenset({"alembic_version"})

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _schema_fingerprint(db_path: Path) -> dict[str, object]:
    """把库的结构读成一个可比较的字典。

    比较 PRAGMA 读出的结构化信息而不是 ``sqlite_master.sql`` 原文——两条路径
    生成的 SQL 文本在空白、列序、约束命名上本来就有差异，比文本会一直假报警。
    """
    con = sqlite3.connect(db_path)
    try:
        tables = sorted(
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            if name not in IGNORED_TABLES
        )
        schema: dict[str, object] = {"tables": tables}
        for table in tables:
            columns = [
                (row[1], row[2].upper(), bool(row[3]), bool(row[5]))  # name, type, notnull, pk
                for row in con.execute(f'PRAGMA table_info("{table}")')
            ]
            indexes = sorted(
                (
                    row[1],
                    bool(row[2]),
                    tuple(r[2] for r in con.execute(f'PRAGMA index_info("{row[1]}")')),
                )
                for row in con.execute(f'PRAGMA index_list("{table}")')
                if not row[1].startswith("sqlite_autoindex")
            )
            schema[f"cols:{table}"] = sorted(columns)
            schema[f"idx:{table}"] = indexes
        return schema
    finally:
        con.close()


def _run_alembic(args: list[str], data_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={"QBANK_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.slow
def test_migration_matches_create_all(tmp_path: Path) -> None:
    migrated_dir = tmp_path / "migrated"
    result = _run_alembic(["upgrade", "head"], migrated_dir)
    assert result.returncode == 0, f"alembic upgrade 失败：\n{result.stderr}"

    created_dir = tmp_path / "created"
    created_dir.mkdir()
    engine = make_engine(sqlite_url(created_dir / "qbank.db"))
    init_schema(engine)
    engine.dispose()

    migrated = _schema_fingerprint(migrated_dir / "qbank.db")
    created = _schema_fingerprint(created_dir / "qbank.db")

    assert migrated["tables"] == created["tables"], (
        "迁移与 create_all 的表集合不一致：\n"
        f"  仅迁移有：{set(migrated['tables']) - set(created['tables'])}\n"  # type: ignore[arg-type]
        f"  仅 create_all 有：{set(created['tables']) - set(migrated['tables'])}"  # type: ignore[arg-type]
    )
    differences = [key for key in migrated if migrated[key] != created.get(key)]
    assert not differences, "以下结构在迁移与 create_all 之间不一致：\n  " + "\n  ".join(
        f"{key}\n    迁移     : {migrated[key]}\n    create_all: {created.get(key)}"
        for key in differences
    )


@pytest.mark.slow
def test_downgrade_removes_everything_including_fts(tmp_path: Path) -> None:
    """回滚必须连 FTS 虚表一起清掉，否则重新 upgrade 会撞上残留表。"""
    data_dir = tmp_path / "roundtrip"
    assert _run_alembic(["upgrade", "head"], data_dir).returncode == 0
    result = _run_alembic(["downgrade", "base"], data_dir)
    assert result.returncode == 0, result.stderr

    con = sqlite3.connect(data_dir / "qbank.db")
    try:
        left = sorted(
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
    finally:
        con.close()
    assert left == ["alembic_version"], f"回滚后仍有残留：{left}"

    # 残留会让重新 upgrade 失败，所以再跑一遍确认幂等
    assert _run_alembic(["upgrade", "head"], data_dir).returncode == 0


@pytest.mark.slow
def test_no_pending_schema_drift(tmp_path: Path) -> None:
    """模型改了却忘记生成迁移时，这个测试会失败。"""
    data_dir = tmp_path / "drift"
    assert _run_alembic(["upgrade", "head"], data_dir).returncode == 0
    result = _run_alembic(["check"], data_dir)
    assert result.returncode == 0, (
        "检测到模型与迁移不一致，请运行：\n"
        "  alembic revision --autogenerate -m '<改了什么>'\n\n"
        f"{result.stdout}\n{result.stderr}"
    )
