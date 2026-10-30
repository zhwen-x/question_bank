"""数据库引擎、会话与 schema 初始化。

**SQLite 的两个坑，这里统一处理掉**：

1. ``PRAGMA foreign_keys`` 默认是 **OFF**。不显式打开的话，模型上写的
   ``ondelete="CASCADE"`` 全部不生效，删课程不会带走章节和题目，库里会留下
   一堆悬挂引用。本模块用连接事件保证每条连接都打开它。
2. 默认的 ``journal_mode=DELETE`` 下读写互斥。改成 WAL 后「一边浏览列表、
   一边后台导入」才不会互相阻塞——这对后续 Web 端尤其重要。

全文索引不走 ORM：索引的是字符 n-gram 切分后的文本，切分要跑 Python，SQL 触发器
做不到。因此由 ``SearchService`` 在写题时同步维护（见 :mod:`qbank.core.services.search`）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from qbank.config import get_paths
from qbank.core.models import Base

#: 全文检索的两张表。
#:
#: ``question_search`` 只是一个 ``question_id`` ↔ 整型 rowid 的映射：FTS5 表按
#: rowid 做主键最快，而题目主键是 UUID 文本，所以需要这一层。
#: ``question_fts`` 存切分后的 token 串，用 ``unicode61``（原因见 ``content.segment``）。
FTS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS question_search (
        id          INTEGER PRIMARY KEY,
        question_id TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS question_fts USING fts5(
        stem,
        answer,
        tags,
        tokenize = 'unicode61'
    )
    """,
)

#: 删库重建时要一起清掉的表（Base.metadata 管不到虚表）
FTS_TABLES: tuple[str, ...] = ("question_fts", "question_search")


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """每条 SQLite 连接都套上必要的 PRAGMA。"""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return  # 将来换 Postgres 时自动跳过
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        # 等锁而不是立刻报 "database is locked"
        cursor.execute("PRAGMA busy_timeout = 5000")
    finally:
        cursor.close()


def make_engine(db_path: Path | None = None, *, echo: bool = False) -> Engine:
    """建引擎。``db_path`` 为 ``None`` 时用配置里的默认库。

    传 ``Path(":memory:")`` 不会得到内存库——内存库请用 :func:`make_memory_engine`。
    """
    path = db_path or get_paths().db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite+pysqlite:///{path}",
        echo=echo,
        future=True,
        # Web 端会在不同线程里用同一个引擎；连接池自身是线程安全的
        connect_args={"check_same_thread": False},
    )


def make_memory_engine(*, echo: bool = False) -> Engine:
    """建一个进程内共享的内存库，测试用。

    用 ``StaticPool`` 是必须的：默认连接池每取一次连接就是一个**新的**空内存库，
    建完表的那条连接一还回去，表就没了。
    """
    from sqlalchemy.pool import StaticPool

    return create_engine(
        "sqlite+pysqlite:///:memory:",
        echo=echo,
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def init_schema(engine: Engine) -> None:
    """建表 + 建全文索引（幂等）。"""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for statement in FTS_DDL:
            conn.execute(text(statement))


def drop_schema(engine: Engine) -> None:
    """删除全部表，含 FTS 虚表。仅供测试与「重置题库」使用。"""
    with engine.begin() as conn:
        for table in FTS_TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    Base.metadata.drop_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """会话工厂。

    ``expire_on_commit=False`` 让提交后仍能读对象属性——否则服务层每次 commit
    之后把对象返回给调用方，调用方一访问属性就触发一次额外查询，甚至在会话
    已关闭时直接报错。
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """事务边界：正常提交，异常回滚。

    所有写操作都应包在这里，别手写 ``try/except/rollback``。
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
