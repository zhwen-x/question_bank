"""数据库引擎、会话与 schema 初始化。

支持两种后端：默认的 SQLite 文件（本地客户端）与 Postgres（Web 部署）。
选哪个由 ``QBANK_DATABASE_URL`` 决定，业务代码一行都不用改——这是需求 7
「为后续迁移到 Web 端预留」在存储层的落点。

**两种后端的 schema 只差全文索引这一处**。其余表由同一份 ``Base.metadata``
建出，因为模型里没有任何方言专属类型（时间列用 :class:`UtcDateTime`、枚举列用
TypeDecorator，DDL 层面都是标准类型）。全文索引无法统一：SQLite 用 FTS5 虚表，
Postgres 用 ``tsvector`` + GIN。两套 DDL 见 :data:`SEARCH_DDL`，对应的查询见
:mod:`qbank.core.services.search`。

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

from sqlalchemy import Engine, create_engine, event, make_url, text
from sqlalchemy.orm import Session, sessionmaker

from qbank.config import database_url, normalize_database_url
from qbank.core.models import Base

#: SQLite 的全文检索用两张表。
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

#: Postgres 的全文检索只需一张表。
#:
#: 不需要 rowid 映射那一层——那是 FTS5 的要求，``tsvector`` 列可以直接挂在
#: 以 ``question_id`` 为主键的行上。
#:
#: ``document`` 里存的是**我们自己切好的 token**，拼成 tsvector 字面量后转型写入，
#: 完全不经过 Postgres 的分词器与词典。这不是图省事：实测本机 Postgres 16 的
#: ``default`` 解析器把中日韩字符判成 ``blank``（token 类型 12），
#: ``to_tsvector('simple', '栈 函数 cpp')`` 的结果里只剩 ``'cpp'``——中文全军覆没。
#: 绕开解析器之后，两种后端索引的是**同一批 token**，检索语义不是「大致相同」
#: 而是完全一致。
SEARCH_DDL: dict[str, tuple[str, ...]] = {
    "sqlite": FTS_DDL,
    "postgresql": (
        """
        CREATE TABLE IF NOT EXISTS question_fts (
            question_id TEXT PRIMARY KEY,
            document    tsvector NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_question_fts_document
            ON question_fts USING GIN (document)
        """,
    ),
}

#: 删库重建时要一起清掉的表（Base.metadata 管不到它们）。
#:
#: 两种后端的表名合在一起列：``DROP TABLE IF EXISTS`` 对不存在的表是空操作，
#: 分方言判断只会多一处需要同步的地方。
FTS_TABLES: tuple[str, ...] = ("question_fts", "question_search")


def search_ddl(dialect: str) -> tuple[str, ...]:
    """取某个方言的全文索引 DDL。

    未知方言**直接报错**而不是跳过：悄悄建出一个没有检索表的库，症状是搜索框
    永远返回空——而那时已经录了几百道题，排查起来要从头翻一遍。

    >>> search_ddl("sqlite")[0].strip().startswith("CREATE TABLE")
    True
    >>> search_ddl("mysql")
    Traceback (most recent call last):
        ...
    ValueError: 不支持的数据库后端 'mysql'；目前只支持 sqlite、postgresql
    """
    try:
        return SEARCH_DDL[dialect]
    except KeyError:
        supported = "、".join(SEARCH_DDL)
        raise ValueError(f"不支持的数据库后端 {dialect!r}；目前只支持 {supported}") from None


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


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """建引擎。``url`` 为 ``None`` 时用 :func:`qbank.config.database_url`。

    连接池参数按后端分开设，两处都是踩过才加上的：

    * SQLite 要 ``check_same_thread=False``——Web 端在不同线程里用同一个引擎。
    * Postgres 要 ``pool_pre_ping``。不设的话，池里的连接在空闲期被数据库或中间的
      pgbouncer / 负载均衡掐掉之后，下一个请求会拿到一条死连接，报
      ``server closed the connection unexpectedly``。症状很有特色：早上第一次
      打开页面必定 500，刷新一下就好了。
    """
    # 归一化放在这里而不是只放在 database_url()：显式传进来的连接串同样会是
    # 从别处复制的 ``postgres://`` 或 ``postgresql://``，两种都挑不中 psycopg 3，
    # 报的还是让人摸不着头脑的 ModuleNotFoundError: psycopg2
    target = normalize_database_url(url) if url else database_url()
    if make_url(target).get_backend_name() == "sqlite":
        return _make_sqlite_engine(target, echo=echo)
    return create_engine(
        target,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        # 比多数云数据库与连接池的空闲超时都短，避免拿到即将被回收的连接
        pool_recycle=1800,
    )


def _make_sqlite_engine(url: str, *, echo: bool) -> Engine:
    database = make_url(url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        url,
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
    """建表 + 建全文索引（幂等）。全文索引按方言选 DDL。"""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for statement in search_ddl(engine.dialect.name):
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


def describe_database(url: str | None = None) -> str:
    """人看的数据库描述。**口令一定抹掉**。

    ``qbank info``、状态页与 ``qbank doctor`` 都显示这一行，而这三处的输出正是
    人求助时会直接贴出来的东西。原样打印连接串等于把数据库口令贴进聊天记录。

    >>> describe_database("sqlite+pysqlite:///data/qbank.db")
    'SQLite data/qbank.db'
    >>> describe_database("postgresql+psycopg://teacher:s3cret@db.example:5432/qbank")
    'PostgreSQL db.example:5432/qbank（用户 teacher）'
    >>> describe_database("postgresql+psycopg:///qbank")
    'PostgreSQL 本机/qbank'
    """
    parsed = make_url(url or database_url())
    if parsed.get_backend_name() == "sqlite":
        return f"SQLite {parsed.database or ':memory:'}"
    host = parsed.host or "本机"
    where = f"{host}:{parsed.port}" if parsed.port else host
    label = f"PostgreSQL {where}/{parsed.database or ''}"
    return f"{label}（用户 {parsed.username}）" if parsed.username else label
