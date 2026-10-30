"""全局测试夹具。

**关键约定**：每个测试都在自己的临时数据目录里跑，绝不碰用户真实题库
（``~/Library/Application Support/qbank`` 等）。``isolate_data_dir`` 是
autouse 的，不需要测试主动声明。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from qbank import config
from qbank.core.db import init_schema, make_memory_engine, make_session_factory


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[config.Paths]:
    """把数据目录指向 tmp_path，并清掉 ``get_paths`` 的缓存。"""
    root = tmp_path / "qbank-data"
    monkeypatch.setenv(config.ENV_DATA_DIR, str(root))
    config.get_paths.cache_clear()
    paths = config.get_paths().ensure()
    assert paths.root == root.resolve(), "数据目录隔离失效，测试可能污染真实题库"
    yield paths
    config.get_paths.cache_clear()


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    """每个测试一个独立的内存库，建好表与 FTS 索引。

    用内存库而非临时文件：快得多，且不会因为 WAL 的 ``-wal``/``-shm`` 残留
    干扰 tmp_path 的断言。
    """
    engine = make_memory_engine()
    init_schema(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """直接拿一个会话用。测试结束自动回滚，不必手工清理。"""
    with session_factory() as active:
        yield active
        active.rollback()
