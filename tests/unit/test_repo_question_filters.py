"""题目筛选测试。

筛选条件被题目列表、检索与自动组卷共用，错一个条件会同时影响三处。最容易
出错的一条是 ``restrict_ids=[]``：它必须表示「检索无结果」而真的返回零条，
不能被当成「不限」——混淆这两种语义会让搜索框看起来完全失效。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.core.models import Paper, QuestionUsage, utcnow
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.question import QuestionService


@pytest.fixture
def seeded(session: Session) -> tuple[str, QuestionRepo, QuestionService]:
    course = CourseRepo(session).create("数据结构")
    service = QuestionService(session)
    specs = [
        (
            "单选易",
            QuestionType.SINGLE_CHOICE,
            Difficulty.EASY,
            "第1章 绪论",
            ("基础",),
            QuestionStatus.ACTIVE,
            "A",
        ),
        (
            "单选难",
            QuestionType.SINGLE_CHOICE,
            Difficulty.HARD,
            "第2章 线性表/2.1 顺序表",
            ("线性表",),
            QuestionStatus.ACTIVE,
            "B",
        ),
        (
            "判断中",
            QuestionType.TRUE_FALSE,
            Difficulty.MEDIUM,
            "第2章 线性表/2.2 链表",
            ("线性表", "基础"),
            QuestionStatus.DRAFT,
            "",
        ),
        (
            "简答难",
            QuestionType.SHORT_ANSWER,
            Difficulty.VERY_HARD,
            "",
            ("综合",),
            QuestionStatus.ACTIVE,
            "略",
        ),
    ]
    for stem, kind, difficulty, chapter, tag_names, status, answer in specs:
        payload: dict[str, object] = {"type": kind.value}
        if kind is QuestionType.SINGLE_CHOICE:
            payload["options"] = [
                {"label": "A", "content": "甲", "is_correct": True},
                {"label": "B", "content": "乙"},
            ]
        elif kind is QuestionType.TRUE_FALSE:
            payload["correct"] = True
        service.create(
            course.id,
            QuestionDraft.model_validate(
                {
                    "type": kind,
                    "stem": stem,
                    "payload": payload,
                    "difficulty": difficulty,
                    "chapter": chapter,
                    "tags": tag_names,
                    "status": status,
                    "answer": answer,
                    "source": "2025 期末" if stem.startswith("单选") else "",
                }
            ),
        )
    return course.id, QuestionRepo(session), service


def _stems(repo: QuestionRepo, criteria: QuestionFilter) -> set[str]:
    return {q.stem for q in repo.list_all(criteria)}


def test_no_filter_returns_all_top_level(seeded: tuple[str, QuestionRepo, QuestionService]) -> None:
    course_id, repo, _ = seeded
    assert len(repo.list_all(QuestionFilter(course_id=course_id))) == 4


def test_filter_by_type(seeded: tuple[str, QuestionRepo, QuestionService]) -> None:
    course_id, repo, _ = seeded
    got = _stems(repo, QuestionFilter(course_id=course_id, types=[QuestionType.SINGLE_CHOICE]))
    assert got == {"单选易", "单选难"}


def test_filter_by_difficulty(seeded: tuple[str, QuestionRepo, QuestionService]) -> None:
    course_id, repo, _ = seeded
    got = _stems(
        repo,
        QuestionFilter(course_id=course_id, difficulties=[Difficulty.HARD, Difficulty.VERY_HARD]),
    )
    assert got == {"单选难", "简答难"}


def test_filter_by_status_excludes_drafts(
    seeded: tuple[str, QuestionRepo, QuestionService],
) -> None:
    """自动组卷只应抽 ACTIVE 的题，草稿不能混进考卷。"""
    course_id, repo, _ = seeded
    got = _stems(repo, QuestionFilter(course_id=course_id, statuses=[QuestionStatus.ACTIVE]))
    assert "判断中" not in got
    assert len(got) == 3


def test_filter_by_chapter_subtree(
    seeded: tuple[str, QuestionRepo, QuestionService], session: Session
) -> None:
    """选「第2章」应包含其下所有小节的题。"""
    course_id, repo, _ = seeded
    chapters = ChapterRepo(session)
    chapter2 = chapters.find_by_path(course_id, "第2章 线性表")
    assert chapter2 is not None
    ids = chapters.subtree_ids([chapter2.id])
    got = _stems(repo, QuestionFilter(course_id=course_id, chapter_ids=ids))
    assert got == {"单选难", "判断中"}


def test_filter_unchaptered(seeded: tuple[str, QuestionRepo, QuestionService]) -> None:
    """跨章节的题不归属任何章节，要能单独筛出来。"""
    course_id, repo, _ = seeded
    got = _stems(repo, QuestionFilter(course_id=course_id, include_unchaptered=True))
    assert got == {"简答难"}


def test_filter_all_tags_is_conjunctive(
    seeded: tuple[str, QuestionRepo, QuestionService], session: Session
) -> None:
    course_id, repo, _ = seeded
    tags = TagRepo(session)
    basic = tags.find_by_name("基础")
    linear = tags.find_by_name("线性表")
    assert basic is not None and linear is not None
    got = _stems(repo, QuestionFilter(course_id=course_id, all_tag_ids=[basic.id, linear.id]))
    assert got == {"判断中"}


def test_filter_any_tags_is_disjunctive(
    seeded: tuple[str, QuestionRepo, QuestionService], session: Session
) -> None:
    course_id, repo, _ = seeded
    tags = TagRepo(session)
    basic = tags.find_by_name("基础")
    comprehensive = tags.find_by_name("综合")
    assert basic is not None and comprehensive is not None
    got = _stems(
        repo,
        QuestionFilter(course_id=course_id, any_tag_ids=[basic.id, comprehensive.id]),
    )
    assert got == {"单选易", "判断中", "简答难"}


def test_require_answer(seeded: tuple[str, QuestionRepo, QuestionService]) -> None:
    """导出答案卷时需要只取有答案的题。"""
    course_id, repo, _ = seeded
    got = _stems(repo, QuestionFilter(course_id=course_id, require_answer=True))
    assert "判断中" not in got
    assert len(got) == 3


def test_filter_by_source(seeded: tuple[str, QuestionRepo, QuestionService]) -> None:
    course_id, repo, _ = seeded
    got = _stems(repo, QuestionFilter(course_id=course_id, source_contains="2025"))
    assert got == {"单选易", "单选难"}


def test_empty_restrict_ids_means_no_results_not_unfiltered(
    seeded: tuple[str, QuestionRepo, QuestionService],
) -> None:
    """检索无结果时必须真的返回零条。

    把空列表当成「不限」会让搜索框彻底失效：搜一个不存在的词，却返回全部题目。
    """
    course_id, repo, _ = seeded
    assert repo.list_all(QuestionFilter(course_id=course_id, restrict_ids=[])) == []
    # None 才表示「不加这个条件」
    assert len(repo.list_all(QuestionFilter(course_id=course_id, restrict_ids=None))) == 4


def test_exclude_ids(seeded: tuple[str, QuestionRepo, QuestionService]) -> None:
    course_id, repo, _ = seeded
    everything = repo.list_all(QuestionFilter(course_id=course_id))
    excluded = everything[0].id
    got = repo.list_all(QuestionFilter(course_id=course_id, exclude_ids=[excluded]))
    assert excluded not in {q.id for q in got}
    assert len(got) == 3


def test_deleted_questions_hidden_by_default(
    seeded: tuple[str, QuestionRepo, QuestionService],
) -> None:
    """默认过滤掉已删除的题——漏了这层，回收站里的题会出现在组卷结果里。"""
    course_id, repo, service = seeded
    victim = repo.list_all(QuestionFilter(course_id=course_id))[0]
    service.delete(victim.id)
    assert len(repo.list_all(QuestionFilter(course_id=course_id))) == 3
    assert len(repo.list_all(QuestionFilter(course_id=course_id, include_deleted=True))) == 4


def test_exclude_recently_used(
    seeded: tuple[str, QuestionRepo, QuestionService], session: Session
) -> None:
    """避免同一道题在短期内反复出现在不同卷子上。"""
    course_id, repo, _ = seeded
    questions = repo.list_all(QuestionFilter(course_id=course_id))
    recent, old = questions[0], questions[1]
    paper = Paper(course_id=course_id, title="2025 期末")
    session.add(paper)
    session.flush()
    session.add(QuestionUsage(question_id=recent.id, paper_id=paper.id))
    session.add(
        QuestionUsage(question_id=old.id, paper_id=paper.id, used_at=utcnow() - timedelta(days=400))
    )
    session.flush()

    got = {
        q.id
        for q in repo.list_all(QuestionFilter(course_id=course_id, exclude_used_within_days=180))
    }
    assert recent.id not in got, "近期用过的题应被排除"
    assert old.id in got, "一年前用过的题可以再用"


def test_pagination_reports_total_and_has_more(
    seeded: tuple[str, QuestionRepo, QuestionService],
) -> None:
    course_id, repo, _ = seeded
    page = repo.list_page(QuestionFilter(course_id=course_id), offset=0, limit=2)
    assert len(page.items) == 2
    assert page.total == 4
    assert page.has_more
    last = repo.list_page(QuestionFilter(course_id=course_id), offset=2, limit=2)
    assert not last.has_more


def test_order_by_ids_preserves_relevance_order(
    seeded: tuple[str, QuestionRepo, QuestionService],
) -> None:
    """SQL 表达不了「按给定 id 顺序排」，所以要在 Python 侧重排。"""
    course_id, repo, _ = seeded
    everything = repo.list_all(QuestionFilter(course_id=course_id))
    wanted = [everything[2].id, everything[0].id, everything[3].id]
    page = repo.list_page(
        QuestionFilter(course_id=course_id, restrict_ids=wanted),
        limit=10,
        order_by_ids=wanted,
    )
    assert [q.id for q in page.items] == wanted


def test_usage_recording_is_idempotent(
    seeded: tuple[str, QuestionRepo, QuestionService], session: Session
) -> None:
    course_id, repo, _ = seeded
    question = repo.list_all(QuestionFilter(course_id=course_id))[0]
    paper = Paper(course_id=course_id, title="卷")
    session.add(paper)
    session.flush()
    repo.record_usage(question.id, paper.id)
    repo.record_usage(question.id, paper.id)
    assert repo.usage_count(question.id) == 1
