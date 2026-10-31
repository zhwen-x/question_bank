"""组卷界面的端到端测试。

重点是这条链路上三个各自会独立坏掉的环节：

1. **预览页看到的，必须就是确认后生成的那一份**——预览不暂存任何东西，
   靠「同蓝图 + 同种子」重跑得到同一结果，这个前提一旦破了，预览就失去意义
2. **组不齐时不许给出「生成试卷」按钮**——默默少出几道题是本功能最该避免的事
3. **挑题状态要能带着翻页和筛选走**——挑题是「一边翻一边勾」，状态一丢就没法用
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType
from qbank.core.repo.course import CourseRepo
from qbank.core.repo.paper import PaperRepo
from qbank.core.schemas import QuestionDraft
from qbank.core.services.question import QuestionService


def text_of(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _draft(stem: str, kind: QuestionType, difficulty: Difficulty) -> QuestionDraft:
    payload: dict[str, object] = {"type": kind.value}
    if kind is QuestionType.SINGLE_CHOICE:
        payload["options"] = [
            {"label": "A", "content": "先进先出", "is_correct": True},
            {"label": "B", "content": "后进先出"},
            {"label": "C", "content": "随机存取"},
            {"label": "D", "content": "以上都不对"},
        ]
    else:
        payload["correct"] = True
    return QuestionDraft(
        type=kind,
        stem=stem,
        payload=payload,  # type: ignore[arg-type]
        answer="A" if kind is QuestionType.SINGLE_CHOICE else "正确",
        difficulty=difficulty,
        chapter="第1章 绪论",
        status=QuestionStatus.ACTIVE,
    )


@pytest.fixture
def bank(session: Session) -> str:
    """一门课：40 道单选（各难度 8 道）+ 12 道判断。

    题量刻意给够出两套不重题的卷：A/B 卷用例要的就是「库里确实够」，
    库太小的话失败原因会变成「题不够」，测不到真正想测的东西。
    """
    course = CourseRepo(session).create("数据结构", code="CS201")
    service = QuestionService(session)
    for difficulty in Difficulty:
        for index in range(8):
            service.create(
                course.id,
                _draft(
                    f"单选 D{difficulty.value}-{index}：栈的特点是？",
                    QuestionType.SINGLE_CHOICE,
                    difficulty,
                ),
            )
    for index in range(12):
        service.create(
            course.id,
            _draft(f"判断 {index}：队列先进先出。", QuestionType.TRUE_FALSE, Difficulty.EASY),
        )
    session.commit()
    return course.id


def _blueprint_form(course_id: str, **overrides: str) -> dict[str, str]:
    form = {
        "course_id": course_id,
        "source": "form",
        "action": "preview",
        "title": "2026 春季期末",
        "total_score": "100",
        "seed": "20261030",
        "avoid_days": "180",
        "section_title_0": "一、单项选择题",
        "section_type_0": "single_choice",
        "section_count_0": "10",
        "section_score_0": "8",
        "section_difficulty_0": "2:0.4 3:0.4 4:0.2",
        "section_title_1": "二、判断题",
        "section_type_1": "true_false",
        "section_count_1": "4",
        "section_score_1": "5",
    }
    form.update(overrides)
    return form


# ------------------------------------------------------------ 空状态
def test_papers_page_when_empty(client: TestClient, bank: str) -> None:
    response = client.get(f"/papers?course_id={bank}")
    assert response.status_code == 200
    body = text_of(response.text)
    assert "还没有试卷" in body
    assert "自动组卷" in body


def test_assemble_form_renders(client: TestClient, bank: str) -> None:
    response = client.get(f"/papers/assemble?course_id={bank}")
    assert response.status_code == 200
    body = text_of(response.text)
    assert "抽题预览" in body
    assert "按配比分层抽" in body, "难度输入框要说清两种写法，否则没人猜得到"


# ------------------------------------------------------- 预览 → 确认
def test_preview_then_commit_gives_the_previewed_paper(
    client: TestClient, bank: str, session: Session
) -> None:
    """预览页不暂存任何东西，靠同一个种子重跑——所以两步的结果必须一致。"""
    preview = client.post("/papers/assemble", data=_blueprint_form(bank))
    assert preview.status_code == 200
    body = text_of(preview.text)
    assert "抽齐了" in body
    assert "14 道" in body
    assert "100 分" in body

    stems_in_preview = re.findall(r"单选 D\d-\d|判断 \d", preview.text)
    assert stems_in_preview

    commit = client.post(
        "/papers/assemble",
        data=_blueprint_form(bank, action="commit"),
        follow_redirects=True,
    )
    assert commit.status_code == 200
    assert "已生成" in text_of(commit.text)

    papers = PaperRepo(session).list_all(bank)
    assert len(papers) == 1
    paper = PaperRepo(session).get_detailed(papers[0].id)
    assert paper.total_score == 100
    assert sum(len(section.items) for section in paper.sections) == 14
    assert paper.seed == 20261030
    on_paper = re.findall(r"单选 D\d-\d|判断 \d", commit.text)
    assert on_paper == stems_in_preview, "确认生成的必须就是预览里看到的那一份"


def test_preview_refuses_to_offer_commit_when_short(client: TestClient, bank: str) -> None:
    """组不齐时不能给出「生成试卷」按钮——默默少出几道题是最该避免的事。"""
    response = client.post(
        "/papers/assemble",
        data=_blueprint_form(
            bank, section_count_0="60", section_score_0="2", total_score="", section_difficulty_0=""
        ),
    )
    assert response.status_code == 200
    body = text_of(response.text)
    assert "组不齐，没有生成试卷" in body
    assert "缺 20 道" in body
    assert ">生成试卷<" not in response.text
    assert "按缺额生成" in body, "但要留一条明确表态的出口"


def test_partial_commit_requires_the_checkbox(client: TestClient, bank: str) -> None:
    """按缺额生成必须显式勾选，而且页面要说清接受了多少缺额。"""
    response = client.post(
        "/papers/assemble",
        data=_blueprint_form(
            bank,
            action="commit",
            section_count_0="60",
            section_score_0="2",
            total_score="",
            section_difficulty_0="",
            allow_partial="1",
        ),
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "接受了 20 道缺额" in text_of(response.text)


def test_preview_shows_what_to_relax(client: TestClient, bank: str) -> None:
    """「符合条件的只有 N 道」没法行动，页面要说清哪一条把题挡住了。"""
    response = client.post(
        "/papers/assemble",
        data=_blueprint_form(
            bank,
            total_score="",
            section_count_0="10",
            section_difficulty_0="5",
            section_score_0="2",
        ),
    )
    body = text_of(response.text)
    assert "不限难度" in body
    assert "可多出" in body


# ------------------------------------------------------------ 蓝图出错
def test_bad_difficulty_keeps_the_filled_form(client: TestClient, bank: str) -> None:
    """蓝图十几个字段，因为难度写错一个字就清空整页是不可接受的。"""
    response = client.post(
        "/papers/assemble", data=_blueprint_form(bank, section_difficulty_0="很难")
    )
    assert response.status_code == 400
    assert "看不懂" in text_of(response.text)
    assert 'value="2026 春季期末"' in response.text
    assert 'value="一、单项选择题"' in response.text, "已填的大题不能被清掉"


def test_total_score_mismatch_is_reported(client: TestClient, bank: str) -> None:
    response = client.post("/papers/assemble", data=_blueprint_form(bank, total_score="90"))
    assert response.status_code == 400
    body = text_of(response.text)
    assert "声明总分 90" in body
    assert "100" in body


def test_yaml_blueprint_path(client: TestClient, bank: str) -> None:
    """一个大题要混多种题型只能走 YAML，这条路必须真的通。"""
    yaml_text = (
        "title: 混合题型卷\n"
        "sections:\n"
        "  - title: 一、客观题\n"
        "    types: [single_choice, true_false]\n"
        "    count: 6\n"
        "    score_each: 5\n"
    )
    response = client.post(
        "/papers/assemble",
        data={
            "course_id": bank,
            "source": "yaml",
            "action": "preview",
            "blueprint_yaml": yaml_text,
            "seed": "7",
        },
    )
    assert response.status_code == 200
    body = text_of(response.text)
    assert "抽齐了" in body
    assert "30 分" in body


def test_yaml_syntax_error_is_explained(client: TestClient, bank: str) -> None:
    response = client.post(
        "/papers/assemble",
        data={
            "course_id": bank,
            "source": "yaml",
            "action": "preview",
            "blueprint_yaml": "title: x\nsections: [{",
        },
    )
    assert response.status_code == 400
    assert "YAML 语法错误" in text_of(response.text)


def test_unknown_chapter_in_yaml_is_a_form_error_not_a_crash(client: TestClient, bank: str) -> None:
    response = client.post(
        "/papers/assemble",
        data={
            "course_id": bank,
            "source": "yaml",
            "action": "preview",
            "blueprint_yaml": (
                "title: x\nsections:\n  - title: 一\n    count: 2\n    chapters: [第9章 不存在]\n"
            ),
        },
    )
    assert response.status_code == 400
    body = text_of(response.text)
    assert "第9章 不存在" in body
    assert "第1章 绪论" in body, "要顺手告诉用户现有章节有哪些"


# ------------------------------------------------------------ 试卷编辑
@pytest.fixture
def paper_id(client: TestClient, bank: str, session: Session) -> str:
    client.post(
        "/papers/assemble", data=_blueprint_form(bank, action="commit"), follow_redirects=True
    )
    return PaperRepo(session).list_all(bank)[0].id


def test_paper_detail_renders_frozen_content(client: TestClient, paper_id: str) -> None:
    response = client.get(f"/papers/{paper_id}")
    assert response.status_code == 200
    body = text_of(response.text)
    assert "100 分" in body
    assert "一、单项选择题" in body
    assert "种子 20261030" in body


def test_student_view_hides_answers(client: TestClient, paper_id: str) -> None:
    with_answers = client.get(f"/papers/{paper_id}?answers=1")
    student = client.get(f"/papers/{paper_id}?answers=0")
    assert "答案：" in text_of(with_answers.text)
    assert "答案：" not in text_of(student.text)


def test_change_one_item_score(client: TestClient, paper_id: str, session: Session) -> None:
    paper = PaperRepo(session).get_detailed(paper_id)
    item_id = paper.sections[0].items[0].id
    response = client.post(
        f"/papers/{paper_id}/items/{item_id}/score", data={"score": "12"}, follow_redirects=True
    )
    assert response.status_code == 200
    session.expire_all()
    assert PaperRepo(session).get_item(item_id).score == 12


def test_apply_one_score_to_a_whole_section(
    client: TestClient, paper_id: str, session: Session
) -> None:
    """逐题填分值在二十道选择题上是纯体力活，所以有「统一配分」。"""
    paper = PaperRepo(session).get_detailed(paper_id)
    section_id = paper.sections[0].id
    response = client.post(
        f"/papers/{paper_id}/scores",
        data={"section_id": section_id, "score": "3"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    session.expire_all()
    refreshed = PaperRepo(session).get_detailed(paper_id)
    assert {item.score for item in refreshed.sections[0].items} == {3.0}


def test_remove_item_releases_the_question(
    client: TestClient, paper_id: str, session: Session
) -> None:
    paper = PaperRepo(session).get_detailed(paper_id)
    item = paper.sections[0].items[0]
    question_id = item.question_id
    response = client.post(f"/papers/{paper_id}/items/{item.id}/remove", follow_redirects=True)
    assert "可重新用于组卷" in text_of(response.text)
    session.expire_all()
    assert question_id not in PaperRepo(session).used_question_ids(paper_id)


def test_move_item_within_section(client: TestClient, paper_id: str, session: Session) -> None:
    paper = PaperRepo(session).get_detailed(paper_id)
    items = paper.sections[0].items
    first, second = items[0].id, items[1].id
    client.post(f"/papers/{paper_id}/items/{first}/move", data={"offset": "1"})
    session.expire_all()
    after = PaperRepo(session).get_detailed(paper_id).sections[0].items
    assert [item.id for item in after][:2] == [second, first]


def test_add_and_remove_a_section(client: TestClient, paper_id: str, session: Session) -> None:
    client.post(f"/papers/{paper_id}/sections/add", data={"title": "三、附加题"})
    session.expire_all()
    paper = PaperRepo(session).get_detailed(paper_id)
    assert [section.title for section in paper.sections][-1] == "三、附加题"

    response = client.post(
        f"/papers/{paper_id}/sections/{paper.sections[0].id}/remove", follow_redirects=True
    )
    assert "已释放" in text_of(response.text)
    session.expire_all()
    assert len(PaperRepo(session).get_detailed(paper_id).sections) == 2


def test_variant_avoids_overlap(client: TestClient, paper_id: str, session: Session) -> None:
    response = client.post(
        f"/papers/{paper_id}/variant",
        data={"title": "2026 春季期末（B 卷）", "seed": "99"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "与原卷不重题" in text_of(response.text)
    repo = PaperRepo(session)
    papers = repo.list_all()
    assert len(papers) == 2
    first = repo.used_question_ids(papers[0].id)
    second = repo.used_question_ids(papers[1].id)
    assert first and second and not first & second


def test_variant_explains_when_the_bank_runs_out(
    client: TestClient, paper_id: str, session: Session
) -> None:
    """出到第三套就不够了，这时必须说清为什么，而不是给一份残卷。

    难度 2 的单选共 8 道，每套要 4 道：A、B 两套刚好用完，第三套一道都没有。
    """
    client.post(f"/papers/{paper_id}/variant", data={"seed": "1"}, follow_redirects=True)
    response = client.post(f"/papers/{paper_id}/variant", data={"seed": "2"}, follow_redirects=True)
    body = text_of(response.text)
    assert "没能出 B 卷" in body
    assert "还缺" in body
    assert "允许与原卷重题" in body, "要给出下一步怎么做"


def test_trash_and_restore_a_paper(client: TestClient, paper_id: str, session: Session) -> None:
    response = client.post(f"/papers/{paper_id}/delete", follow_redirects=True)
    assert "重新可用于组卷" in text_of(response.text)
    session.expire_all()
    assert PaperRepo(session).list_all() == []

    client.post(f"/papers/{paper_id}/restore", follow_redirects=True)
    session.expire_all()
    assert len(PaperRepo(session).list_all()) == 1


# ------------------------------------------------------------ 手工挑题
def test_manual_paper_and_picking_from_the_list(
    client: TestClient, bank: str, session: Session
) -> None:
    created = client.post(
        "/papers/new",
        data={"course_id": bank, "title": "随堂测验", "sections": "一、选择题\n二、判断题"},
        follow_redirects=True,
    )
    assert created.status_code == 200
    paper = PaperRepo(session).list_all(bank)[0]
    assert [section.title for section in paper.sections] == ["一、选择题", "二、判断题"]

    picking = client.get(f"/questions?course_id={bank}&add_to={paper.id}")
    assert "正在往" in text_of(picking.text)
    assert 'name="question_id"' in picking.text

    ids = re.findall(r'name="question_id" value="([^"]+)"', picking.text)[:3]
    added = client.post(
        f"/papers/{paper.id}/add",
        data={
            "section_id": paper.sections[0].id,
            "question_id": ids,
            "score": "4",
            "back": f"/questions?course_id={bank}&add_to={paper.id}",
        },
        follow_redirects=True,
    )
    assert "加入 3 道" in text_of(added.text)
    assert "正在往" in text_of(added.text), "加完要回到挑题列表，而不是弹去试卷页"
    session.expire_all()
    refreshed = PaperRepo(session).get_detailed(paper.id)
    assert len(refreshed.sections[0].items) == 3
    assert refreshed.total_score == 12


def test_already_picked_questions_are_shown_as_such(
    client: TestClient, bank: str, session: Session
) -> None:
    client.post("/papers/new", data={"course_id": bank, "title": "测验"}, follow_redirects=True)
    paper = PaperRepo(session).list_all(bank)[0]
    ids = re.findall(
        r'name="question_id" value="([^"]+)"',
        client.get(f"/questions?course_id={bank}&add_to={paper.id}").text,
    )[:2]
    client.post(
        f"/papers/{paper.id}/add", data={"section_id": paper.sections[0].id, "question_id": ids}
    )
    session.commit()

    again = client.get(f"/questions?course_id={bank}&add_to={paper.id}")
    assert "已在卷中" in text_of(again.text)
    assert again.text.count("disabled") >= 2

    repeated = client.post(
        f"/papers/{paper.id}/add",
        data={"section_id": paper.sections[0].id, "question_id": ids},
        follow_redirects=True,
    )
    assert "已在本卷中" in text_of(repeated.text)


def test_picking_state_survives_filtering(client: TestClient, bank: str, session: Session) -> None:
    """挑题是「一边筛一边勾」，筛选一次就退出挑题模式的话这件事没法做。"""
    client.post("/papers/new", data={"course_id": bank, "title": "测验"}, follow_redirects=True)
    paper = PaperRepo(session).list_all(bank)[0]
    response = client.get(
        f"/questions?course_id={bank}&add_to={paper.id}&type=true_false&difficulty=2"
    )
    assert "正在往" in text_of(response.text)
    assert f'name="add_to" value="{paper.id}"' in response.text


def test_picking_into_a_deleted_paper_degrades_gracefully(client: TestClient, bank: str) -> None:
    """卷子在别处被删了，挑题页不该 500。"""
    response = client.get(f"/questions?course_id={bank}&add_to=不存在的试卷")
    assert response.status_code == 200
    assert "正在往" not in text_of(response.text)


def test_adding_nothing_says_so(client: TestClient, bank: str, session: Session) -> None:
    client.post("/papers/new", data={"course_id": bank, "title": "测验"}, follow_redirects=True)
    paper = PaperRepo(session).list_all(bank)[0]
    response = client.post(
        f"/papers/{paper.id}/add", data={"section_id": paper.sections[0].id}, follow_redirects=True
    )
    assert "一道题都没勾选" in text_of(response.text)


def test_missing_paper_is_404(client: TestClient, bank: str) -> None:
    assert client.get("/papers/没有这份卷子").status_code == 404


def test_bad_blueprint_keeps_selects_and_checkboxes_too(
    client: TestClient, bank: str, session: Session
) -> None:
    """回填不能只管文本框。

    一个大题要填十一个字段，因为总分差了 5 分就把章节、标签的勾选清空，
    等于让人从头再来一遍——而且用户多半不会注意到勾选没了。
    """
    response = client.post(
        "/papers/assemble",
        data=_blueprint_form(
            bank,
            total_score="90",
            section_chapter_0="第1章 绪论",
            section_shuffle_0="on",
            section_require_answer_0="on",
        ),
    )
    assert response.status_code == 400
    body = response.text
    assert 'value="第1章 绪论"\n                      selected' in body or (
        'value="第1章 绪论"' in body and "selected" in body
    )
    assert body.count("checked") >= 2, "两个勾上的复选框都要还原"


def test_deleting_a_course_warns_that_papers_go_too(
    client: TestClient, paper_id: str, bank: str
) -> None:
    """试卷挂在课程上，会跟着一起没。

    只说「删除课程及其 N 条题目」会让人以为卷子还在——卷子是单独出的劳动成果，
    必须在确认文案里点名。
    """
    listing = client.get("/courses")
    assert "1 份试卷" in text_of(listing.text)
    assert "份试卷 —— 输入课程名确认" in text_of(listing.text)

    deleted = client.post(
        f"/courses/{bank}/delete", data={"confirm_name": "数据结构"}, follow_redirects=True
    )
    assert "全部题目与 1 份试卷" in text_of(deleted.text)


# ------------------------------------------------------------ 导出
def test_export_panel_lists_formats_and_templates(client: TestClient, paper_id: str) -> None:
    body = text_of(client.get(f"/papers/{paper_id}").text)
    assert "导出并下载" in body
    for label in ("Markdown", "HTML", "Word", "PDF"):
        assert label in body
    assert "standard" in body
    assert "答案与评分标准" in body, "三种版面都要能选"


def test_export_html_downloads_a_single_file(client: TestClient, paper_id: str) -> None:
    response = client.get(f"/papers/{paper_id}/export", params={"fmt": "html"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" in response.headers.get("content-disposition", "")
    assert "<math" in response.text or "单项选择题" in response.text


def test_export_markdown_without_images_is_plain_md(client: TestClient, paper_id: str) -> None:
    response = client.get(f"/papers/{paper_id}/export", params={"fmt": "md"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")


def test_export_markdown_with_images_comes_back_zipped(
    client: TestClient, bank: str, session: Session, isolate_data_dir: Any
) -> None:
    """浏览器只能下一个文件。旁挂的图片不打包的话，用户拿到的是一堆坏引用。"""
    import io
    import zipfile

    from PIL import Image

    from qbank.core.services.asset import AssetService
    from qbank.core.services.paper import PaperService
    from qbank.core.services.question import QuestionService

    buffer = io.BytesIO()
    Image.new("RGB", (30, 20), (10, 10, 10)).save(buffer, format="PNG")
    asset, _ = AssetService(session, isolate_data_dir).store_bytes(buffer.getvalue())
    question = QuestionService(session).create(
        bank,
        QuestionDraft.model_validate(
            {
                "type": "short_answer",
                "stem": f"看图：![图](asset://{asset.sha256})",
                "payload": {"type": "short_answer"},
                "status": "active",
            }
        ),
    )
    papers = PaperService(session)
    paper = papers.create_empty(bank, "带图的卷", sections=["一"])
    papers.add_questions(paper.id, paper.sections[0].id, [question.id], score=5)
    session.commit()

    response = client.get(f"/papers/{paper.id}/export", params={"fmt": "md"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
    assert any(name.endswith(".md") for name in names)
    assert any(".assets/" in name for name in names), "图片要一起打包"


def test_export_with_a_bad_format_says_so(client: TestClient, paper_id: str) -> None:
    response = client.get(
        f"/papers/{paper_id}/export", params={"fmt": "epub"}, follow_redirects=True
    )
    assert "不认识的导出格式" in text_of(response.text)


def test_export_with_an_unknown_template_says_so(client: TestClient, paper_id: str) -> None:
    response = client.get(
        f"/papers/{paper_id}/export",
        params={"fmt": "md", "template": "没这套"},
        follow_redirects=True,
    )
    assert "没这套" in text_of(response.text)
    assert "standard" in text_of(response.text)


def test_export_of_a_missing_paper_is_404(client: TestClient) -> None:
    assert client.get("/papers/不存在/export").status_code == 404
