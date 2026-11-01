"""Postgres 后端：schema、中文检索、与 SQLite 的一致性。

**怎么跑**：这组测试需要一台真的 Postgres，所以默认跳过。给一个**可以随便删的**
空库再跑：

.. code-block:: bash

    createdb qbank_test
    QBANK_TEST_DATABASE_URL=postgresql://localhost/qbank_test pytest tests/integration/test_postgres.py

每个用例开始前会把库里的表**全部删掉重建**。别指向任何有数据的库。

**为什么值得有这一组**：SQLite 与 Postgres 的差别不在 ORM 那一层——模型里没有
方言专属类型，表是同一份 metadata 建出来的。差别全在全文检索上，而检索恰恰是
最容易「看起来正常、实际全空」的地方：实测 Postgres 的 ``default`` 解析器把中日韩
字符判成空白，``to_tsvector('simple', '栈 函数')`` 返回空向量。
:func:`test_both_backends_return_the_same_hits` 就是守这一条的。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from qbank.core.db import (
    drop_schema,
    init_schema,
    make_engine,
    make_memory_engine,
    make_session_factory,
)
from qbank.core.enums import QuestionType
from qbank.core.repo.course import CourseRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.question import QuestionService

ENV_URL = "QBANK_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    not os.environ.get(ENV_URL),
    reason=f"需要一台 Postgres：设 {ENV_URL} 指向一个可以随便删的空库",
)

#: 这批题覆盖了几种会出问题的检索场景：单字术语、被夹在长串里的词、
#: 只出现在标签里的词、带符号的英文标识符。
CORPUS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("栈是一种先进后出的线性表。", "正确", ("栈", "线性表")),
    ("用 Dijkstra 求最短路径时，优先队列起什么作用？", "降低取最小值的复杂度", ("图", "最短路")),
    ("二次函数的性质有哪些？", "开口方向、对称轴、顶点", ("函数",)),
    ("C++ 的引用和指针有什么区别？", "引用必须初始化", ("cpp", "指针")),
)


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    """指向测试库的引擎。每个用例都把 schema 推平重建。"""
    engine = make_engine(os.environ[ENV_URL])
    drop_schema(engine)
    init_schema(engine)
    try:
        yield engine
    finally:
        drop_schema(engine)
        engine.dispose()


@pytest.fixture
def pg_session(pg_engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(pg_engine)
    with factory() as session:
        yield session


def _fill(session: Session) -> dict[str, str]:
    """把语料写进去，返回「题干 → id」。"""
    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    ids: dict[str, str] = {}
    for stem, answer, tags in CORPUS:
        question = service.create(
            course.id,
            QuestionDraft(
                type=QuestionType.SHORT_ANSWER,
                stem=stem,
                answer=answer,
                tags=list(tags),
                payload={"type": QuestionType.SHORT_ANSWER.value},
            ),
        )
        ids[stem] = question.id
    session.commit()
    return ids


def test_schema_is_created_with_a_tsvector_index(pg_engine: Engine) -> None:
    """检索表必须是 tsvector + GIN，不是「建了个表但没有索引」。"""
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        column_type = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'question_fts' AND column_name = 'document'"
            )
        ).scalar()
        index = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_question_fts_document'")
        ).scalar()
    assert column_type == "tsvector"
    assert index is not None and "gin" in index.lower()


def test_chinese_search_works(pg_session: Session) -> None:
    """这条如果红了，说明又走回 Postgres 自己的分词器了。"""
    ids = _fill(pg_session)
    service = QuestionService(pg_session)

    assert service.search_ids("栈") == [ids["栈是一种先进后出的线性表。"]]
    assert service.search_ids("函数") == [ids["二次函数的性质有哪些？"]]
    # 「最短路径」在原文里被夹在更长的串中，查询时单独出现——上下文无关的切分
    # 才扛得住这一条
    assert service.search_ids("最短路径") == [ids["用 Dijkstra 求最短路径时，优先队列起什么作用？"]]


def test_tags_and_symbols_are_searchable(pg_session: Session) -> None:
    """``C++`` 会被归一化成 ``cpp``，标签也进索引。"""
    ids = _fill(pg_session)
    service = QuestionService(pg_session)
    assert service.search_ids("C++") == [ids["C++ 的引用和指针有什么区别？"]]
    assert ids["用 Dijkstra 求最短路径时，优先队列起什么作用？"] in service.search_ids("最短路")


def test_nonsense_query_returns_nothing(pg_session: Session) -> None:
    _fill(pg_session)
    assert QuestionService(pg_session).search_ids("量子纠缠") == []


def test_deleting_removes_it_from_the_index(pg_session: Session) -> None:
    ids = _fill(pg_session)
    service = QuestionService(pg_session)
    service.delete(ids["栈是一种先进后出的线性表。"])
    pg_session.commit()
    assert service.search_ids("栈") == []


def test_reindex_rebuilds_everything(pg_session: Session) -> None:
    _fill(pg_session)
    service = QuestionService(pg_session)
    service.search.clear()
    assert service.search_ids("栈") == []
    assert service.reindex_all() == len(CORPUS)
    pg_session.commit()
    assert len(service.search_ids("栈")) == 1


def test_both_backends_return_the_same_hits() -> None:
    """同一批题、同一批查询，两种后端的命中集合必须一模一样。

    比的是集合而不是顺序：bm25 与 ts_rank 是两套打分公式，两条同样弱的命中谁在
    前面没有保证，也不重要。**命中与否**则必须一致——那才是「搜不搜得到」。
    """
    memory = make_memory_engine()
    init_schema(memory)
    postgres = make_engine(os.environ[ENV_URL])
    drop_schema(postgres)
    init_schema(postgres)
    try:
        with make_session_factory(memory)() as sqlite_session:
            sqlite_ids = _fill(sqlite_session)
            with make_session_factory(postgres)() as pg_session:
                pg_ids = _fill(pg_session)

                differences: list[str] = []
                for query in ("栈", "函数", "最短路径", "最短路", "队列", "线性", "C++", "指针"):
                    # 两个库里的 id 不同（各自新建），所以换算回题干再比
                    left = {
                        stem
                        for stem, qid in sqlite_ids.items()
                        if qid in QuestionService(sqlite_session).search_ids(query)
                    }
                    right = {
                        stem
                        for stem, qid in pg_ids.items()
                        if qid in QuestionService(pg_session).search_ids(query)
                    }
                    if left != right:
                        differences.append(
                            f"{query!r}：sqlite={sorted(left)} postgres={sorted(right)}"
                        )
                assert not differences, "两种后端命中不一致：\n  " + "\n  ".join(differences)
    finally:
        drop_schema(postgres)
        postgres.dispose()
        memory.dispose()
