"""应用上下文：引擎、会话与 schema 版本管理。

CLI、Web 与桌面外壳都从这里取会话，所以「库在哪、schema 是否最新」只有一处
判断逻辑。库是数据目录里的 SQLite 文件还是一台 Postgres，由
:func:`qbank.config.database_url` 决定，本模块两种都照同一条流程走。

**schema 的升级策略**：库不存在时直接按模型建表并打上 Alembic 的最新版本戳
（跳过逐条迁移，快且等价——已有测试断言两条路径产出的 schema 完全一致）；
库已存在但版本落后时跑 ``alembic upgrade head``。这样用户第一次启动不必知道
Alembic 的存在，老用户升级也不会被漏掉。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from qbank.config import Paths, database_url, get_paths
from qbank.core.db import init_schema, make_engine, make_session_factory, session_scope


def _project_root() -> Path:
    """``alembic.ini`` 与 ``migrations/`` 在哪。

    打包成单文件后，源码不在磁盘上的源码树里了，``__file__`` 推出来的路径指向
    一个不存在的目录。PyInstaller 会把捆进去的数据解到 ``sys._MEIPASS``，
    所以冻结状态下要认那个。

    不处理的话，``_head_revision()`` 会静默返回 ``None``（它吞掉所有异常），
    于是打包版**永远不会跑数据库迁移**——升级后打开旧题库，表结构还是旧的，
    而程序一声不吭。
    """
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        return Path(frozen)
    return Path(__file__).resolve().parents[2]


#: alembic.ini 与 migrations/ 所在的目录
PROJECT_ROOT = _project_root()


#: 判断「这是不是一个新库」时探测的表。
#:
#: 用表是否存在，而不是文件是否存在：Postgres 根本没有「库文件」，而在 SQLite
#: 上「文件在、表没有」也确实会发生（崩溃留下的零字节文件、手工 touch 出来的
#: 占位文件）。按文件判断时那种库会被当成老库，接着 ``alembic upgrade`` 撞上
#: ``init_schema`` 刚建好的表，报 "table already exists"。
PROBE_TABLE = "question"


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
    url: str | None = None, *, auto_upgrade: bool = True, echo: bool = False
) -> AppContext:
    """打开（必要时创建）题库。

    ``url`` 为 ``None`` 时用 :func:`qbank.config.database_url`——默认是数据目录里的
    SQLite 文件，设了 ``QBANK_DATABASE_URL`` 就是那台数据库。

    ``auto_upgrade=False`` 时不做任何 schema 变更，版本落后会抛
    :class:`SchemaOutdatedError`——用于「只读打开别人发来的库」这类场景。
    """
    paths = get_paths().ensure()
    target = url or database_url()
    engine = make_engine(target, echo=echo)
    head = _head_revision()
    is_new = not inspect(engine).has_table(PROBE_TABLE)

    if is_new:
        init_schema(engine)
        if head:
            _stamp(engine, head)
    else:
        # 检索索引不在 Base.metadata 里；旧库可能缺（例如手工删过），补上是幂等的
        init_schema(engine)
        current = _current_revision(engine)
        if head and current != head:
            if not auto_upgrade:
                raise SchemaOutdatedError(
                    f"题库 schema 版本为 {current or '未标记'}，代码期望 {head}。"
                    "请运行 `alembic upgrade head` 或以可升级方式打开。"
                )
            _upgrade(target)

    return AppContext(paths=paths, engine=engine, session_factory=make_session_factory(engine))


def _upgrade(url: str) -> None:
    """跑 ``alembic upgrade head``。

    连接串要显式传给 alembic，不能让它自己再去读一遍环境变量：``open_bank`` 允许
    传入一个和 ``QBANK_DATABASE_URL`` 不同的库，那种情况下让 alembic 自行解析
    等于**迁移另一个库**——而且不会报错。
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
