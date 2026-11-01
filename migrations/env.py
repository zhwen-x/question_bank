"""Alembic 运行环境。

数据库地址不写在 alembic.ini 里，而是运行时解析——这样 ``alembic upgrade head``
和应用自身永远指向同一个库，不会出现「迁移跑在另一个文件上」这种事。

解析顺序：调用方塞进 alembic 配置的 ``sqlalchemy.url`` 优先（``qbank.app._upgrade``
走这条，因为 ``open_bank`` 允许打开一个和环境变量不同的库），否则按
:func:`qbank.config.database_url` ——即 ``QBANK_DATABASE_URL``，再退回
``QBANK_DATA_DIR`` 下的 SQLite 文件。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from qbank.config import database_url, normalize_database_url
from qbank.core.db import make_engine
from qbank.core.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    return normalize_database_url(configured) if configured else database_url()


#: 不受 Alembic 管理的表。
#:
#: 检索索引不在 Base.metadata 里，autogenerate 会把它当作「模型里没有、库里却有」
#: 而生成 drop_table——真跑下去会把全文索引整个删掉。必须在这里排除。
#: 两种后端都要排：SQLite 是 FTS5 虚表及其影子表
#: （question_fts_data/_idx/_content/...），Postgres 是一张普通的 tsvector 表。
_UNMANAGED_PREFIXES = ("question_fts", "question_search")


def _include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    if type_ == "table" and name is not None:
        return not name.startswith(_UNMANAGED_PREFIXES)
    return True


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """让自动生成的迁移脚本不引用应用代码。

    ``UtcDateTime`` 是个 TypeDecorator，它的 Python 侧时区归一化不属于 DDL 范畴；
    DDL 层面它就是 ``DateTime(timezone=True)``。不转换的话 autogenerate 会写出
    ``qbank.core.models.UtcDateTime(...)``，迁移脚本既要 import 应用模块，又会在
    将来重命名该类时失效。
    """
    from qbank.core.models import UtcDateTime

    if type_ == "type" and isinstance(obj, UtcDateTime):
        autogen_context.imports.add("import sqlalchemy as sa")  # type: ignore[attr-defined]
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=_render_item,
        # SQLite 不支持大多数 ALTER，用「建新表-拷数据-换名」的批量模式
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = make_engine(_database_url())
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            render_item=_render_item,
            include_name=_include_name,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
