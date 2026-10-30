"""界面的端到端测试。

每个页面都要能在**空题库**和**有数据**两种状态下渲染——空状态是用户第一次
打开时看到的画面，恰恰最容易在开发中被忽略（开发机上总是有数据）。

写入路径重点验证两件事：改动确实落库了，以及校验失败时用户填的内容没被清空。
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from qbank.core.enums import ConflictPolicy, QuestionType
from qbank.core.repo.course import ChapterRepo, CourseRepo
from qbank.core.repo.question import QuestionFilter, QuestionRepo
from qbank.core.repo.tag import TagRepo
from qbank.core.services.question import QuestionService
from qbank.exchange.import_service import ImportService, plan_file

EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"

CHOICE_FORM = {
    "type": "single_choice",
    "stem": "下列代码执行后 $n$ 的值是？\n\n```python\nn = 1\n```",
    "chapter": "第3章 控制结构/3.2 循环结构",
    "difficulty": "2",
    "score": "2",
    "tags": "循环、位运算",
    "source": "2025 期末",
    "status": "active",
    "external_id": "web-01",
    "option_label": ["A", "B", "C"],
    "option_content": ["1024", "1023", "$2^{11}$"],
    "correct": ["B"],
    "answer": "B",
    "explanation": "循环执行 10 次。",
}


def text_of(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def listed_types(html: str) -> list[str]:
    """列表区域里各行的题型。

    不能直接在整页 HTML 里找题型名——筛选下拉框的 ``<option>`` 里也有全部题型，
    用整页断言会永远为真。
    """
    rows = re.findall(r'<a class="q-item".*?</a>', html, re.S)
    return [
        match.group(1)
        for row in rows
        if (match := re.search(r'<span class="badge type">([^<]+)</span>', row))
    ]


def png(size: tuple[int, int] = (80, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def course_id(session: Session) -> str:
    return CourseRepo(session).create("数据结构", code="CS201").id


@pytest.fixture
def seeded(session: Session, course_id: str) -> str:
    """导入示例题库，覆盖全部七种题型。"""
    plan, document = plan_file(session, course_id, EXAMPLES / "导入模板示例.md")
    try:
        ImportService(session).commit(plan, policy=ConflictPolicy.SKIP)
    finally:
        document.close()
    session.commit()
    return course_id


# ------------------------------------------------------------ 空状态
def test_empty_bank_guides_to_create_a_course(client: TestClient) -> None:
    """第一次打开时看到的画面——开发机上总有数据，这个状态最容易被忽略。"""
    response = client.get("/questions", follow_redirects=True)
    assert response.status_code == 200
    body = text_of(response.text)
    assert "还没有课程" in body
    assert "创建课程" in body


@pytest.mark.parametrize("path", ["/", "/questions", "/courses", "/tags", "/trash", "/health"])
def test_pages_render_when_empty(client: TestClient, path: str) -> None:
    assert client.get(path, follow_redirects=True).status_code == 200


def test_course_page_without_chapters(client: TestClient, course_id: str) -> None:
    response = client.get(f"/chapters?course_id={course_id}", follow_redirects=True)
    assert response.status_code == 200
    assert "还没有章节" in text_of(response.text)


# ------------------------------------------------------------ 列表与筛选
def test_question_list_shows_all_types(client: TestClient, seeded: str) -> None:
    response = client.get(f"/questions?course_id={seeded}")
    assert response.status_code == 200
    assert set(listed_types(response.text)) == {
        "单项选择题",
        "多项选择题",
        "判断题",
        "填空题",
        "简答题",
        "综合应用题",
        "编程题",
    }


def test_list_renders_formulas_as_mathml(client: TestClient, seeded: str) -> None:
    """列表行里的公式要真的渲染出来，而不是显示一串 LaTeX 源码。

    用 MathML 而非 KaTeX，所以界面不依赖任何 JS 与字体文件。
    """
    response = client.get(f"/questions?course_id={seeded}")
    assert "<math" in response.text
    assert "\\log" not in response.text or "<mi>" in response.text


@pytest.mark.parametrize(
    ("query", "expected"),
    [("最短路径", "综合应用题"), ("快速排序", "编程题"), ("邻接矩阵", "综合应用题")],
)
def test_search(client: TestClient, seeded: str, query: str, expected: str) -> None:
    response = client.get(f"/questions?course_id={seeded}&q={query}")
    assert expected in listed_types(response.text)


def test_search_hit_in_sub_question_shows_parent(client: TestClient, seeded: str) -> None:
    """命中小问时要显示它所属的大题，否则表现为「什么都没搜到」。"""
    response = client.get(f"/questions?course_id={seeded}&q=Dijkstra")
    assert listed_types(response.text) == ["综合应用题"]


def test_search_miss_shows_empty_state(client: TestClient, seeded: str) -> None:
    response = client.get(f"/questions?course_id={seeded}&q=哈希冲突")
    assert "没有符合条件的题目" in text_of(response.text)


def test_filter_by_type(client: TestClient, seeded: str) -> None:
    assert listed_types(client.get(f"/questions?course_id={seeded}&type=true_false").text) == [
        "判断题"
    ]


def test_filter_by_difficulty(client: TestClient, seeded: str) -> None:
    listed = listed_types(client.get(f"/questions?course_id={seeded}&difficulty=4").text)
    assert listed and set(listed) <= {"综合应用题", "编程题"}


def test_filter_by_tag(client: TestClient, seeded: str) -> None:
    assert listed_types(client.get(f"/questions?course_id={seeded}&tag=循环").text) == [
        "单项选择题"
    ]


def test_filter_preserves_search_term(client: TestClient, seeded: str) -> None:
    """先搜索再按题型收窄时不能把搜索词丢掉——筛选表单里有个隐藏的 q。"""
    response = client.get(f"/questions?course_id={seeded}&q=排序")
    assert 'name="q" value="排序"' in response.text


def test_chapter_filter_includes_sub_chapters(
    client: TestClient, seeded: str, session: Session
) -> None:
    """点「第3章」想看的是整章，不是只有直接挂在它下面的题。"""
    chapter = ChapterRepo(session).find_by_path(seeded, "第3章 控制结构")
    assert chapter is not None
    response = client.get(f"/questions?course_id={seeded}&chapter_id={chapter.id}")
    assert "单项选择题" in text_of(response.text)


def test_unknown_course_gives_friendly_404(client: TestClient, seeded: str) -> None:
    response = client.get("/questions?course_id=no-such-course")
    assert response.status_code == 404
    assert "没有找到这个页面" in text_of(response.text)


# ------------------------------------------------------------ 详情
def test_detail_pages_render_for_every_type(
    client: TestClient, seeded: str, session: Session
) -> None:
    for question in QuestionRepo(session).list_all(QuestionFilter(course_id=seeded)):
        response = client.get(f"/questions/{question.id}")
        assert response.status_code == 200, f"{question.type.label} 详情页挂了"
        assert question.type.label in text_of(response.text)


def test_comprehensive_detail_shows_sub_questions(
    client: TestClient, seeded: str, session: Session
) -> None:
    question = QuestionRepo(session).list_all(
        QuestionFilter(course_id=seeded, types=[QuestionType.COMPREHENSIVE])
    )[0]
    body = text_of(client.get(f"/questions/{question.id}").text)
    assert "邻接矩阵" in body
    assert "Dijkstra" in body
    assert "5.0 分" in body and "10.0 分" in body


def test_detail_marks_correct_option(client: TestClient, seeded: str, session: Session) -> None:
    question = QuestionRepo(session).list_all(
        QuestionFilter(course_id=seeded, types=[QuestionType.SINGLE_CHOICE])
    )[0]
    assert "✓" in client.get(f"/questions/{question.id}").text


def test_detail_title_is_plain_text(client: TestClient, seeded: str, session: Session) -> None:
    """``<title>`` 里不该出现 ``$x^2$`` 这种原始标记。"""
    question = QuestionRepo(session).list_all(
        QuestionFilter(course_id=seeded, types=[QuestionType.COMPREHENSIVE])
    )[0]
    title = re.search(r"<title>(.*?)</title>", client.get(f"/questions/{question.id}").text)
    assert title is not None
    assert "$" not in title.group(1)


# ------------------------------------------------------------ 新建与编辑
def test_create_question_through_form(client: TestClient, course_id: str, session: Session) -> None:
    response = client.post(
        f"/questions?course_id={course_id}", data=CHOICE_FORM, follow_redirects=False
    )
    assert response.status_code == 303
    question_id = response.headers["location"].split("?")[0].rsplit("/", 1)[-1]

    question = QuestionRepo(session).get_detailed(question_id)
    assert question.type is QuestionType.SINGLE_CHOICE
    assert question.score == 2.0
    assert question.chapter is not None
    assert question.chapter.path == "第3章 控制结构/3.2 循环结构"
    assert {tag.name for tag in question.tags} == {"循环", "位运算"}
    assert question.external_id == "web-01"
    assert [o["label"] for o in question.payload["options"] if o["is_correct"]] == ["B"]


def test_create_builds_missing_chapters(
    client: TestClient, course_id: str, session: Session
) -> None:
    client.post(f"/questions?course_id={course_id}", data=CHOICE_FORM)
    paths = {chapter.path for chapter in ChapterRepo(session).list_tree(course_id)}
    assert paths == {"第3章 控制结构", "第3章 控制结构/3.2 循环结构"}


def test_created_question_is_searchable(client: TestClient, course_id: str) -> None:
    client.post(f"/questions?course_id={course_id}", data=CHOICE_FORM)
    assert "单项选择题" in text_of(client.get(f"/questions?course_id={course_id}&q=循环").text)


def test_validation_error_keeps_user_input(client: TestClient, course_id: str) -> None:
    """老师写一道综合题可能花十分钟，不能因为漏填一项就把整页清空。"""
    broken = {**CHOICE_FORM, "stem": "这段题干绝不能丢"}
    broken.pop("correct")
    response = client.post(f"/questions?course_id={course_id}", data=broken)
    assert response.status_code == 422
    assert "请勾选正确选项" in text_of(response.text)
    assert "这段题干绝不能丢" in response.text
    assert "1023" in response.text, "选项内容也要回填"
    assert "web-01" in response.text, "编号等元数据同样要回填"


def test_validation_error_writes_nothing(
    client: TestClient, course_id: str, session: Session
) -> None:
    broken = {**CHOICE_FORM}
    broken.pop("correct")
    client.post(f"/questions?course_id={course_id}", data=broken)
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 0


def test_edit_records_history(client: TestClient, course_id: str, session: Session) -> None:
    created = client.post(
        f"/questions?course_id={course_id}", data=CHOICE_FORM, follow_redirects=False
    )
    question_id = created.headers["location"].split("?")[0].rsplit("/", 1)[-1]

    response = client.post(
        f"/questions/{question_id}/edit",
        data={**CHOICE_FORM, "score": "5", "note": "改了分值"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    service = QuestionService(session)
    question = service.questions.get_detailed(question_id)
    assert question.score == 5.0
    assert [(r.revision, r.note) for r in service.revisions(question_id)] == [(1, "改了分值")]
    assert "改了分值" in text_of(client.get(f"/questions/{question_id}").text)


def test_rollback_from_detail_page(client: TestClient, course_id: str, session: Session) -> None:
    created = client.post(
        f"/questions?course_id={course_id}", data=CHOICE_FORM, follow_redirects=False
    )
    question_id = created.headers["location"].split("?")[0].rsplit("/", 1)[-1]
    client.post(f"/questions/{question_id}/edit", data={**CHOICE_FORM, "score": "9"})

    client.post(f"/questions/{question_id}/rollback", data={"revision": "1"})
    assert QuestionRepo(session).get(question_id).score == 2.0


def test_comprehensive_through_form(client: TestClient, course_id: str, session: Session) -> None:
    response = client.post(
        f"/questions?course_id={course_id}",
        data={
            "type": "comprehensive",
            "stem": "阅读下列有向图，回答问题。",
            "chapter": "第7章 图",
            "difficulty": "4",
            "status": "active",
            "sub_type": ["short_answer", "short_answer"],
            "sub_stem": ["写出邻接矩阵。", "求最短路径。"],
            "sub_answer": ["略", "A→C→B"],
            "sub_explanation": ["", "按权值松弛"],
            "sub_score": ["5", "10"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    question_id = response.headers["location"].split("?")[0].rsplit("/", 1)[-1]
    question = QuestionRepo(session).get_detailed(question_id)
    assert [sub.score for sub in question.sub_questions] == [5.0, 10.0]
    assert [sub.sort_order for sub in question.sub_questions] == [0, 1]


@pytest.mark.parametrize(
    "kind",
    [
        "single_choice",
        "multiple_choice",
        "true_false",
        "fill_blank",
        "short_answer",
        "comprehensive",
        "programming",
    ],
)
def test_new_form_renders_for_every_type(client: TestClient, course_id: str, kind: str) -> None:
    response = client.get(f"/questions/new?course_id={course_id}&type={kind}")
    assert response.status_code == 200


def test_edit_form_renders_for_every_type(
    client: TestClient, seeded: str, session: Session
) -> None:
    for question in QuestionRepo(session).list_all(QuestionFilter(course_id=seeded)):
        response = client.get(f"/questions/{question.id}/edit")
        assert response.status_code == 200, f"{question.type.label} 的编辑表单挂了"


# ------------------------------------------------------------ 回收站
def test_delete_restore_cycle(client: TestClient, course_id: str, session: Session) -> None:
    created = client.post(
        f"/questions?course_id={course_id}", data=CHOICE_FORM, follow_redirects=False
    )
    question_id = created.headers["location"].split("?")[0].rsplit("/", 1)[-1]

    client.post(f"/questions/{question_id}/delete")
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 0
    assert question_id[:8] in client.get("/trash").text

    client.post(f"/questions/{question_id}/restore")
    assert QuestionRepo(session).count(QuestionFilter(course_id=course_id)) == 1


def test_purge_requires_typing_the_word(
    client: TestClient, course_id: str, session: Session
) -> None:
    """清空回收站是本应用里唯一真正不可撤销的操作。"""
    created = client.post(
        f"/questions?course_id={course_id}", data=CHOICE_FORM, follow_redirects=False
    )
    question_id = created.headers["location"].split("?")[0].rsplit("/", 1)[-1]
    client.post(f"/questions/{question_id}/delete")

    client.post("/trash/purge", data={"confirm": "是"})
    assert len(QuestionRepo(session).list_trash()) == 1, "确认词不对却执行了删除"

    client.post("/trash/purge", data={"confirm": "清空"})
    assert QuestionRepo(session).list_trash() == []


# ------------------------------------------------------------ 课程与章节
def test_create_course_through_form(client: TestClient, session: Session) -> None:
    client.post("/courses", data={"name": "离散数学", "code": "MA201"})
    assert CourseRepo(session).find_by_name("离散数学") is not None


def test_duplicate_course_shows_error(client: TestClient, course_id: str) -> None:
    response = client.post("/courses", data={"name": "数据结构"}, follow_redirects=True)
    assert "已存在" in text_of(response.text)


def test_course_delete_requires_typing_the_name(
    client: TestClient, course_id: str, session: Session
) -> None:
    """删课程会连带删掉全部题目，一个确认框挡不住误点。"""
    client.post(f"/courses/{course_id}/delete", data={"confirm_name": "数据"})
    assert CourseRepo(session).find_by_name("数据结构") is not None

    client.post(f"/courses/{course_id}/delete", data={"confirm_name": "数据结构"})
    assert CourseRepo(session).find_by_name("数据结构") is None


def test_chapter_rename_rewrites_subtree(client: TestClient, seeded: str, session: Session) -> None:
    chapters = ChapterRepo(session)
    chapter = chapters.find_by_path(seeded, "第3章 控制结构")
    assert chapter is not None
    client.post(f"/chapters/{chapter.id}/rename", data={"title": "第三章 控制结构"})
    # 请求走的是另一个会话，当前会话里的对象还是旧的，必须让它重新加载
    session.expire_all()
    paths = {node.path for node in chapters.list_tree(seeded)}
    assert "第三章 控制结构/3.2 循环结构" in paths
    assert not any(path.startswith("第3章") for path in paths)


def test_chapter_delete_keeps_questions(client: TestClient, seeded: str, session: Session) -> None:
    """删章节不该删题——它们只是失去章节归属。"""
    chapters = ChapterRepo(session)
    chapter = chapters.find_by_path(seeded, "第3章 控制结构/3.2 循环结构")
    assert chapter is not None
    before = QuestionRepo(session).count(QuestionFilter(course_id=seeded))
    client.post(f"/chapters/{chapter.id}/delete")
    session.expire_all()
    assert QuestionRepo(session).count(QuestionFilter(course_id=seeded)) == before


# ------------------------------------------------------------ 标签
def test_tag_merge_through_form(client: TestClient, seeded: str, session: Session) -> None:
    tags = TagRepo(session)
    source = tags.find_by_name("最短路")
    target = tags.find_by_name("图")
    assert source is not None and target is not None
    response = client.post(
        "/tags/merge",
        data={"source_id": source.id, "target_id": target.id},
        follow_redirects=True,
    )
    assert "已去重" in text_of(response.text)
    assert tags.find_by_name("最短路") is None


def test_tag_clean_removes_unused(client: TestClient, session: Session) -> None:
    TagRepo(session).create("没人用的")
    session.commit()
    client.post("/tags/clean")
    assert TagRepo(session).find_by_name("没人用的") is None


# ------------------------------------------------------------ 图片与预览
def test_upload_serve_and_preview(client: TestClient) -> None:
    uploaded = client.post("/assets", files={"file": ("fig.png", png(), "image/png")})
    assert uploaded.status_code == 200
    payload = uploaded.json()
    assert payload["ref"].startswith("asset://")
    assert (payload["width"], payload["height"]) == (80, 60)

    preview = client.post("/preview", data={"text": f"见图 ![流程]({payload['ref']})"})
    assert f'src="{payload["url"]}"' in preview.text

    served = client.get(payload["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    # 哈希即内容，同一个 URL 的内容永不改变，可以放心长期缓存
    assert "immutable" in served.headers["cache-control"]


def test_upload_rejects_non_image(client: TestClient) -> None:
    response = client.post("/assets", files={"file": ("notes.txt", b"not an image", "text/plain")})
    assert response.status_code == 415


def test_missing_asset_returns_404(client: TestClient) -> None:
    assert client.get("/assets/" + "0" * 64).status_code == 404


def test_preview_renders_math_and_tables(client: TestClient) -> None:
    response = client.post(
        "/preview", data={"text": "公式 $\\frac{a}{b}$\n\n| 甲 | 乙 |\n|---|---|\n| 1 | 2 |"}
    )
    assert "<mfrac>" in response.text
    assert "<table>" in response.text


def test_preview_sanitizes_untrusted_html(client: TestClient) -> None:
    """题目内容可能来自同事发来的 Word/HTML，必须当不可信内容处理。"""
    response = client.post(
        "/preview", data={"text": '<script>alert(1)</script><img src="javascript:x">正文'}
    )
    assert "script" not in response.text.lower()
    assert "javascript:" not in response.text.lower()
    assert "正文" in response.text


def test_empty_preview_shows_placeholder(client: TestClient) -> None:
    assert "placeholder" in client.post("/preview", data={"text": "   "}).text


# ------------------------------------------------------------ 状态页
def test_health_page_reports_capabilities(client: TestClient, seeded: str) -> None:
    body = text_of(client.get("/health").text)
    assert "中文检索" in body
    assert "SQLite FTS5" in body
    assert "题库规模" in body


def test_reindex_from_health_page(client: TestClient, seeded: str, session: Session) -> None:
    service = QuestionService(session)
    service.search.clear()
    session.commit()
    assert service.search.indexed_count() == 0

    client.post("/health/reindex")
    assert service.search.indexed_count() > 0
