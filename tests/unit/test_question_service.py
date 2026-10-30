"""题目服务测试。

服务层的职责是「一次写入要同时做对六件事」：落库、答案表、章节解析、标签解析、
指纹、全文索引。每一件漏掉都会产生很隐蔽的不一致，所以这里逐项断言。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from qbank.core.enums import ConflictPolicy, Difficulty, QuestionStatus, QuestionType
from qbank.core.repo.base import NotFoundError
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.question import QuestionService


@pytest.fixture
def course_id(session: Session) -> str:
    return CourseRepo(session).create("Python 程序设计").id


@pytest.fixture
def service(session: Session) -> QuestionService:
    return QuestionService(session)


def make_choice_draft(**overrides: object) -> QuestionDraft:
    base: dict[str, object] = {
        "type": QuestionType.SINGLE_CHOICE,
        "stem": "下列代码执行后 $n$ 的值是？",
        "payload": {
            "type": "single_choice",
            "options": [
                {"label": "A", "content": "1024", "is_correct": True},
                {"label": "B", "content": "1023"},
            ],
        },
        "answer": "A",
        "explanation": r"循环执行 10 次，$n = 2^{10}$",
        "chapter": "第3章 控制结构/3.2 循环结构",
        "difficulty": Difficulty.EASY,
        "score": 2.0,
        "tags": ("循环", "位运算"),
        "source": "2025 期末",
        "status": QuestionStatus.ACTIVE,
        "external_id": "2025-final-01",
    }
    base.update(overrides)
    return QuestionDraft.model_validate(base)


# ------------------------------------------------------- 一次写入的六件事
def test_create_does_everything_at_once(
    service: QuestionService, course_id: str, session: Session
) -> None:
    question = service.create(course_id, make_choice_draft())

    assert question.stem.startswith("下列代码")
    assert question.answer is not None and question.answer.answer == "A"
    # 章节按人类可读路径自动创建
    assert question.chapter is not None
    assert question.chapter.path == "第3章 控制结构/3.2 循环结构"
    assert {c.path for c in ChapterRepo(session).list_tree(course_id)} == {
        "第3章 控制结构",
        "第3章 控制结构/3.2 循环结构",
    }
    assert {tag.name for tag in question.tags} == {"循环", "位运算"}
    assert question.fingerprint, "指纹为空会让查重失效"
    assert service.search.matching_ids("循环") == [question.id], "索引没建，题在库里但搜不到"


def test_fingerprint_covers_options_and_answer(service: QuestionService, course_id: str) -> None:
    """同题干不同选项、同题干不同答案，都是不同的题。"""
    base = make_choice_draft(external_id="")
    other_options = make_choice_draft(
        external_id="",
        payload={
            "type": "single_choice",
            "options": [
                {"label": "A", "content": "2048", "is_correct": True},
                {"label": "B", "content": "1023"},
            ],
        },
    )
    assert service.fingerprint_parts(base) != service.fingerprint_parts(other_options)


def test_payload_is_validated_before_write(service: QuestionService, course_id: str) -> None:
    """坏 payload 不该进库——校验发生在 QuestionDraft 构造时。"""
    with pytest.raises(ValueError, match="只能有 1 个正确选项"):
        make_choice_draft(
            payload={
                "type": "single_choice",
                "options": [
                    {"label": "A", "content": "x", "is_correct": True},
                    {"label": "B", "content": "y", "is_correct": True},
                ],
            }
        )


def test_create_on_missing_course_fails_clearly(service: QuestionService) -> None:
    with pytest.raises(NotFoundError, match="找不到课程"):
        service.create("no-such-course", make_choice_draft())


# ------------------------------------------------------------ 综合题与子题
def comprehensive_draft() -> QuestionDraft:
    return QuestionDraft.model_validate(
        {
            "type": QuestionType.COMPREHENSIVE,
            "stem": "阅读下列关于图的材料，回答问题。",
            "payload": {"type": "comprehensive", "material_label": "阅读材料"},
            "chapter": "第7章 图",
            "sub_questions": [
                {
                    "type": QuestionType.SHORT_ANSWER,
                    "stem": "写出该图的邻接矩阵。",
                    "payload": {"type": "short_answer"},
                    "answer": "略",
                    "score": 5.0,
                },
                {
                    "type": QuestionType.SHORT_ANSWER,
                    "stem": "用 Dijkstra 求最短路径。",
                    "payload": {"type": "short_answer"},
                    "answer": "略",
                    "score": 10.0,
                },
            ],
        }
    )


def test_sub_questions_are_stored_and_ordered(service: QuestionService, course_id: str) -> None:
    question = service.create(course_id, comprehensive_draft())
    assert [s.sort_order for s in question.sub_questions] == [0, 1]
    assert [s.score for s in question.sub_questions] == [5.0, 10.0]
    assert all(s.parent_id == question.id for s in question.sub_questions)


def test_sub_questions_are_individually_searchable(
    service: QuestionService, course_id: str
) -> None:
    """搜「最短路径」应命中那道大题里的第 2 小问，而不是搜不到。"""
    question = service.create(course_id, comprehensive_draft())
    hits = service.search.matching_ids("最短路径")
    assert hits, "子题没进索引"
    assert hits[0] in {s.id for s in question.sub_questions}


def test_sub_questions_excluded_from_list_but_present_in_search(
    service: QuestionService, course_id: str, session: Session
) -> None:
    """列表里一道综合题算一行；检索时子题要能命中。两种默认值不同。"""
    service.create(course_id, comprehensive_draft())
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 1
    assert (
        QuestionRepo(session).count(QuestionFilter(course_id=course_id, top_level_only=False)) == 3
    )


def test_update_replaces_sub_questions_wholesale(
    service: QuestionService, course_id: str, session: Session
) -> None:
    """插入一个小问时，逐条对齐会全部错位，所以采用整体替换。"""
    question = service.create(course_id, comprehensive_draft())
    draft = comprehensive_draft()
    inserted = list(draft.sub_questions)
    inserted.insert(
        0,
        QuestionDraft.model_validate(
            {
                "type": QuestionType.SHORT_ANSWER,
                "stem": "先写出顶点数。",
                "payload": {"type": "short_answer"},
                "score": 2.0,
            }
        ),
    )
    updated = service.update(
        question.id, draft.model_copy(update={"sub_questions": tuple(inserted)})
    )
    assert [s.stem for s in updated.sub_questions] == [
        "先写出顶点数。",
        "写出该图的邻接矩阵。",
        "用 Dijkstra 求最短路径。",
    ]
    assert [s.sort_order for s in updated.sub_questions] == [0, 1, 2]
    # 被替换掉的旧子题不能留在索引里
    assert (
        QuestionRepo(session).count(QuestionFilter(course_id=course_id, top_level_only=False)) == 4
    )


# ------------------------------------------------------------ 查重与 upsert
def test_check_duplicates_flags_exact_match(service: QuestionService, course_id: str) -> None:
    draft = make_choice_draft()
    created = service.create(course_id, draft)
    hints = service.check_duplicates(course_id, draft)
    assert [(h.question_id, h.exact, h.label) for h in hints] == [(created.id, True, "完全相同")]


def test_similarity_excludes_the_answer(service: QuestionService, course_id: str) -> None:
    """相似度**不能**把答案算进去——那会把最危险的一类压到阈值以下。

    实测：「栈是一种**先**进先出的线性表／答案：错误」与「栈是一种**后**进先出
    的线性表／答案：正确」，题意被一字反转，答案也跟着反转。只比题干相似度
    0.538，把答案算进去就掉到 0.412，低于 0.45 的阈值——这道最需要人工复核的题
    反而被静默漏掉。

    答案仍然要进**指纹**：同题干不同答案是两道题，不能自动跳过。
    """
    original = QuestionDraft.model_validate(
        {
            "type": QuestionType.TRUE_FALSE,
            "stem": "栈是一种先进先出的线性表。",
            "payload": {"type": "true_false", "correct": False},
            "answer": "错误",
        }
    )
    reversed_meaning = QuestionDraft.model_validate(
        {
            "type": QuestionType.TRUE_FALSE,
            "stem": "栈是一种后进先出的线性表。",
            "payload": {"type": "true_false", "correct": True},
            "answer": "正确",
        }
    )
    service.create(course_id, original)

    hints = service.check_duplicates(course_id, reversed_meaning)
    assert hints, "题意被反转的题必须被列为相似候选，交给人确认"
    assert not hints[0].exact, "它不是同一道题，不能按完全相同处理"

    # 答案仍然参与指纹：两者指纹必须不同
    assert service.fingerprint_parts(original) != service.fingerprint_parts(reversed_meaning)
    assert "错误" in service.fingerprint_parts(original)
    assert "错误" not in service.similarity_parts(original)


def test_check_duplicates_only_compares_same_type(service: QuestionService, course_id: str) -> None:
    """候选池按「同课程 + 同题型」收窄，判断题不会跟选择题比。"""
    service.create(course_id, make_choice_draft())
    other = QuestionDraft.model_validate(
        {
            "type": QuestionType.TRUE_FALSE,
            "stem": "下列代码执行后 $n$ 的值是？",
            "payload": {"type": "true_false", "correct": True},
        }
    )
    assert service.check_duplicates(course_id, other) == []


@pytest.mark.parametrize(
    ("policy", "expected_action", "expected_total"),
    [
        (ConflictPolicy.SKIP, "skipped", 1),
        (ConflictPolicy.OVERWRITE, "updated", 1),
        (ConflictPolicy.NEW_REVISION, "updated", 1),
        (ConflictPolicy.CREATE_COPY, "created", 2),
    ],
)
def test_upsert_honours_conflict_policy(
    service: QuestionService,
    course_id: str,
    session: Session,
    policy: ConflictPolicy,
    expected_action: str,
    expected_total: int,
) -> None:
    draft = make_choice_draft()
    service.create(course_id, draft)
    result = service.upsert(course_id, draft.model_copy(update={"score": 5.0}), policy=policy)
    assert result.action == expected_action
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == expected_total


def test_upsert_matches_by_external_id_even_after_edits(
    service: QuestionService, course_id: str, session: Session
) -> None:
    """同一个文件改了几处后重新导入，应识别为同一道题而不是新建。"""
    service.create(course_id, make_choice_draft())
    edited = make_choice_draft(stem="题干被大幅改写了，和原来几乎没有共同之处。")
    result = service.upsert(course_id, edited, policy=ConflictPolicy.OVERWRITE)
    assert result.updated
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 1


def test_upsert_without_external_id_falls_back_to_fingerprint(
    service: QuestionService, course_id: str
) -> None:
    draft = make_choice_draft(external_id="")
    service.create(course_id, draft)
    assert service.upsert(course_id, draft, policy=ConflictPolicy.SKIP).skipped


# ------------------------------------------------------------ 历史与回滚
def test_update_records_snapshot_visible_in_same_session(
    service: QuestionService, course_id: str
) -> None:
    """改完马上看历史必须能看到——这里曾因用 session.add 而非追加关系集合出过 bug。"""
    question = service.create(course_id, make_choice_draft())
    assert list(service.revisions(question.id)) == []
    service.update(question.id, make_choice_draft(score=3.0), note="改分值")
    assert [(r.revision, r.note) for r in service.revisions(question.id)] == [(1, "改分值")]


def test_rollback_restores_content_and_keeps_history(
    service: QuestionService, course_id: str
) -> None:
    question = service.create(course_id, make_choice_draft())
    service.update(question.id, make_choice_draft(score=3.0, stem="改过的题干"), note="改动")
    rolled = service.rollback(question.id, 1)
    assert rolled.score == 2.0
    assert rolled.stem.startswith("下列代码")
    # 回滚本身也要留痕，否则「回滚错了」无路可退
    assert [r.note for r in service.revisions(question.id)] == ["改动", "回滚到第 1 版"]


def test_rollback_to_missing_revision_lists_available(
    service: QuestionService, course_id: str
) -> None:
    question = service.create(course_id, make_choice_draft())
    with pytest.raises(ValueError, match=r"没有第 9 版"):
        service.rollback(question.id, 9)


# ------------------------------------------------------------ 删除与恢复
def test_delete_removes_from_index_and_list(
    service: QuestionService, course_id: str, session: Session
) -> None:
    question = service.create(course_id, make_choice_draft())
    service.delete(question.id)
    assert service.search.matching_ids("循环") == [], "回收站里的题不该还能被搜到"
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 0
    assert len(QuestionRepo(session).list_trash(course_id)) == 1


def test_delete_cascades_to_sub_questions(
    service: QuestionService, course_id: str, session: Session
) -> None:
    """父题删了子题还留着，就会出现无主的子题。"""
    service.create(course_id, comprehensive_draft())
    trash = QuestionRepo(session).list_trash(course_id)
    assert not trash
    question = QuestionRepo(session).list_all(QuestionFilter(course_id=course_id))[0]
    service.delete(question.id)
    assert service.search.matching_ids("最短路径") == []
    assert (
        QuestionRepo(session).count(QuestionFilter(course_id=course_id, top_level_only=False)) == 0
    )


def test_restore_rebuilds_index(service: QuestionService, course_id: str) -> None:
    question = service.create(course_id, make_choice_draft())
    service.delete(question.id)
    service.restore(question.id)
    assert service.search.matching_ids("循环") == [question.id]


def test_purge_is_permanent(service: QuestionService, course_id: str, session: Session) -> None:
    question = service.create(course_id, make_choice_draft())
    service.delete(question.id)
    service.purge(question.id)
    assert QuestionRepo(session).list_trash(course_id) == []
    assert service.search.indexed_count() == 0


# ------------------------------------------------------------ 往返
def test_draft_roundtrip_preserves_everything(service: QuestionService, course_id: str) -> None:
    """ORM → draft → ORM 必须等价，否则编辑界面与 JSON 导出会丢字段。"""
    original = make_choice_draft()
    question = service.create(course_id, original)
    recovered = service.to_draft(question)
    assert recovered.stem == original.stem
    assert recovered.payload == original.payload
    assert recovered.answer == original.answer
    assert recovered.explanation == original.explanation
    assert recovered.chapter == original.chapter
    assert recovered.difficulty == original.difficulty
    assert recovered.score == original.score
    assert set(recovered.tags) == set(original.tags)
    assert recovered.source == original.source
    assert recovered.status == original.status
    assert recovered.external_id == original.external_id


def test_comprehensive_draft_roundtrip(service: QuestionService, course_id: str) -> None:
    original = comprehensive_draft()
    question = service.create(course_id, original)
    recovered = service.to_draft(question)
    assert [s.stem for s in recovered.sub_questions] == [s.stem for s in original.sub_questions]
    assert [s.score for s in recovered.sub_questions] == [s.score for s in original.sub_questions]


def test_reindex_all_rebuilds_from_scratch(service: QuestionService, course_id: str) -> None:
    service.create(course_id, make_choice_draft())
    service.create(course_id, comprehensive_draft())
    service.search.clear()
    assert service.search.matching_ids("循环") == []
    # 顶层题 2 道（子题由 reindex_all 一并处理，因为它遍历的是全部未删除的行）
    assert service.reindex_all() == 4
    assert service.search.matching_ids("循环")
    assert service.search.matching_ids("最短路径")


def test_preview_action_matches_what_upsert_will_do(
    service: QuestionService, course_id: str
) -> None:
    """``check_duplicates`` 的判定必须和 ``upsert`` 完全一致。

    不一致的后果很具体：从 docx 导入建的库、再用 md 重新导入时，pandoc 往返
    会让题面出现细微差异 → 指纹不同 → 预览说「将要新建」；但 upsert 认的是
    ``external_id``，实际会走更新。预览存在的全部意义就是让用户事先知道会
    发生什么，说一套做一套比没有预览更糟。
    """
    original = make_choice_draft(external_id="shared-01")
    service.create(course_id, original)

    # 题面被改过，但编号不变——upsert 会认出是同一道题
    edited = make_choice_draft(
        external_id="shared-01", stem="题干被整个改写了，和原来几乎没有共同之处。"
    )
    hints = service.check_duplicates(course_id, edited)
    assert hints, "编号相同却没被判为同一道题"
    assert hints[0].exact
    assert hints[0].label == "编号相同"

    result = service.upsert(course_id, edited, policy=ConflictPolicy.SKIP)
    assert result.skipped, "预览说已存在，upsert 却当成了新题"


def test_external_id_match_is_not_listed_twice(service: QuestionService, course_id: str) -> None:
    """编号与指纹同时命中同一道题时，只报一条。"""
    draft = make_choice_draft(external_id="dup-01")
    service.create(course_id, draft)
    hints = service.check_duplicates(course_id, draft)
    assert len({hint.question_id for hint in hints}) == len(hints)
    assert len(hints) == 1
