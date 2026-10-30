"""应用上下文：引擎、会话与 schema 版本管理。

CLI、Web 与桌面外壳都从这里取会话，所以「库在哪、schema 是否最新」只有一处
判断逻辑。

**schema 的升级策略**：库不存在时直接按模型建表并打上 Alembic 的最新版本戳
（跳过逐条迁移，快且等价——已有测试断言两条路径产出的 schema 完全一致）；
库已存在但版本落后时跑 ``alembic upgrade head``。这样用户第一次启动不必知道
Alembic 的存在，老用户升级也不会被漏掉。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from qbank.config import Paths, get_paths
from qbank.core.db import init_schema, make_engine, make_session_factory, session_scope

#: alembic.ini 与 migrations/ 所在的目录（源码树根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SchemaOutdatedError(RuntimeError):
    """库的 schema 版本低于代码期望，且自动升级失败。"""


@dataclass
class AppContext:
    """一个打开的题库。"""

    paths: Paths
    engine: Engine
    session_factory: sessionmaker[Session]

    @contextmanager
    def session(self) -> Iterator[Session]:
        """事务边界。写操作必须包在这里。"""
        with session_scope(self.session_factory) as active:
            yield active

    def close(self) -> None:
        self.engine.dispose()


def _head_revision() -> str | None:
    """代码里最新的迁移版本号。取不到时返回 ``None``（例如打包后没带 migrations）。"""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # alembic 不可用或 migrations 目录缺失时不该阻断启动
        return None


def _current_revision(engine: Engine) -> str | None:
    if "alembic_version" not in inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _stamp(engine: Engine, revision: str) -> None:
    """给新建的库打上版本戳，避免下次启动又去跑一遍迁移。"""
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version(version_num) VALUES (:rev)"), {"rev": revision}
        )


def open_bank(
    db_path: Path | None = None, *, auto_upgrade: bool = True, echo: bool = False
) -> AppContext:
    """打开（必要时创建）题库。

    ``auto_upgrade=False`` 时不做任何 schema 变更，版本落后会抛
    :class:`SchemaOutdatedError`——用于「只读打开别人发来的库」这类场景。
    """
    paths = get_paths().ensure()
    path = db_path or paths.db_file
    is_new = not path.exists()
    engine = make_engine(path, echo=echo)
    head = _head_revision()

    if is_new:
        init_schema(engine)
        if head:
            _stamp(engine, head)
    else:
        # FTS 虚表不在 Base.metadata 里；旧库可能缺（例如手工删过），补上是幂等的
        init_schema(engine)
        current = _current_revision(engine)
        if head and current != head:
            if not auto_upgrade:
                raise SchemaOutdatedError(
                    f"题库 schema 版本为 {current or '未标记'}，代码期望 {head}。"
                    "请运行 `alembic upgrade head` 或以可升级方式打开。"
                )
            _upgrade(paths)

    return AppContext(paths=paths, engine=engine, session_factory=make_session_factory(engine))


def _upgrade(_paths: Paths) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(config, "head")
