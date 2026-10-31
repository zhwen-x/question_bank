"""CLI 的端到端测试。

M1 的验收标准是「CLI 能完成增删改查与中文检索」，这个文件就是它的验收。
同时 CLI 也是 M3 图形界面上线前唯一的全链路驱动入口。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from qbank.cli import app

runner = CliRunner()

#: 覆盖三种典型题型：带代码与公式的选择题、判断题、含子题的综合题
SAMPLE_QUESTIONS = [
    {
        "type": "single_choice",
        "stem": "下列代码执行后 $n$ 的值是？\n\n```python\nn = 1\nfor i in range(10):\n    n *= 2\n```",
        "payload": {
            "type": "single_choice",
            "options": [
                {"label": "A", "content": "1024", "is_correct": True},
                {"label": "B", "content": "1023"},
                {"label": "C", "content": "$2^{11}$"},
            ],
        },
        "answer": "A",
        "explanation": "循环执行 10 次，$n = 2^{10}$",
        "chapter": "第3章 控制结构/3.2 循环结构",
        "difficulty": 2,
        "score": 2,
        "tags": ["循环", "位运算"],
        "source": "2025 期末",
        "status": "active",
        "external_id": "2025-final-01",
    },
    {
        "type": "true_false",
        "stem": "栈是一种先进先出的线性表。",
        "payload": {"type": "true_false", "correct": False},
        "answer": "错误",
        "chapter": "第4章 栈与队列",
        "difficulty": 1,
        "score": 1,
        "tags": ["栈", "队列"],
        "status": "active",
    },
    {
        "type": "comprehensive",
        "stem": "阅读下列有向图的邻接表，回答问题。",
        "payload": {"type": "comprehensive", "material_label": "阅读材料"},
        "chapter": "第7章 图",
        "difficulty": 4,
        "tags": ["图", "最短路"],
        "status": "active",
        "sub_questions": [
            {
                "type": "short_answer",
                "stem": "写出该图的邻接矩阵。",
                "payload": {"type": "short_answer"},
                "answer": "略",
                "score": 5,
            },
            {
                "type": "short_answer",
                "stem": "用 Dijkstra 算法求顶点 A 到各点的最短路径。",
                "payload": {"type": "short_answer"},
                "answer": "A→C→B，长度 7",
                "score": 10,
            },
        ],
    },
]


@pytest.fixture
def drafts_file(tmp_path: Path) -> Path:
    path = tmp_path / "drafts.yaml"
    path.write_text(
        yaml.safe_dump(SAMPLE_QUESTIONS, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def run(*args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, f"`qbank {' '.join(args)}` 失败：\n{result.output}"
    return result.output


@pytest.fixture
def seeded(drafts_file: Path) -> Path:
    run("course", "add", "数据结构", "--code", "CS201")
    run("question", "add", "数据结构", str(drafts_file))
    return drafts_file


# --------------------------------------------------------------- 基本流程
def test_course_and_question_lifecycle(drafts_file: Path) -> None:
    assert "已创建课程" in run("course", "add", "数据结构")
    assert "数据结构" in run("course", "list")
    output = run("question", "add", "数据结构", str(drafts_file))
    assert "新建 3" in output
    assert "3 道" in run("info")


def test_chapters_are_created_from_paths(seeded: Path) -> None:
    """导入文件里写的是人类可读路径，章节树应自动建出来并带缩进。"""
    output = run("chapter", "list", "数据结构")
    assert "第3章 控制结构" in output
    assert "  3.2 循环结构" in output, "子章节应缩进显示"


def test_tags_are_created_with_counts(seeded: Path) -> None:
    output = run("tag", "list")
    for name in ("循环", "位运算", "栈", "图"):
        assert name in output


# --------------------------------------------------------------- 中文检索
@pytest.mark.parametrize(
    "term",
    ["循环", "位运算", "栈", "图", "最短路径", "最短", "邻接矩阵", "Dijkstra", "range"],
)
def test_chinese_and_code_search(seeded: Path, term: str) -> None:
    """单字、双字、长词、英文标识符、代码内容都要能搜到。"""
    output = run("question", "list", "--course", "数据结构", "-s", term)
    assert "没有匹配的题目" not in output, f"{term!r} 搜不到"


def test_search_hit_in_sub_question_shows_parent(seeded: Path) -> None:
    """命中子题时列表要显示它的父题，否则表现为「什么都没搜到」。"""
    output = run("question", "list", "--course", "数据结构", "-s", "Dijkstra")
    assert "阅读下列有向图的邻接表" in output


def test_search_miss_returns_nothing_not_everything(seeded: Path) -> None:
    """搜不存在的词必须返回零条，不能退化成「不加条件」返回全部。"""
    output = run("question", "list", "--course", "数据结构", "-s", "哈希冲突")
    assert "没有匹配的题目" in output


# --------------------------------------------------------------- 筛选
def test_filter_by_type_difficulty_and_tag(seeded: Path) -> None:
    assert "栈是一种先进先出" in run(
        "question", "list", "--course", "数据结构", "--type", "true_false"
    )
    assert "阅读下列有向图" in run("question", "list", "--course", "数据结构", "--difficulty", "4")
    assert "栈是一种先进先出" in run("question", "list", "--course", "数据结构", "--tag", "栈")


def test_unknown_tag_is_reported(seeded: Path) -> None:
    result = runner.invoke(app, ["question", "list", "--tag", "不存在的标签"])
    assert result.exit_code == 1
    assert "找不到标签" in result.output


def test_unknown_course_lists_available(drafts_file: Path) -> None:
    run("course", "add", "数据结构")
    result = runner.invoke(app, ["question", "list", "--course", "离散数学"])
    assert result.exit_code == 1
    assert "找不到课程" in result.output
    assert "数据结构" in result.output, "报错时应列出现有课程，否则用户只能猜"


# --------------------------------------------------------------- 详情与往返
def test_show_renders_options_and_answer(seeded: Path) -> None:
    listing = run("question", "list", "--course", "数据结构", "--type", "single_choice")
    question_id = listing.split()[0]
    output = run("question", "show", question_id)
    assert "✓ A. 1024" in output, "正确选项要有标记"
    assert "B. 1023" in output
    assert "答案：A" in output
    assert "第3章 控制结构/3.2 循环结构" in output


def test_show_yaml_is_reimportable(seeded: Path, tmp_path: Path) -> None:
    """``show --yaml`` 导出的内容必须能原样再导回去。"""
    listing = run("question", "list", "--course", "数据结构", "--type", "true_false")
    question_id = listing.split()[0]
    exported = run("question", "show", question_id, "--yaml")
    roundtrip = tmp_path / "roundtrip.yaml"
    roundtrip.write_text(exported, encoding="utf-8")

    run("course", "add", "另一门课")
    output = run("question", "add", "另一门课", str(roundtrip))
    assert "新建 1" in output


def test_show_accepts_id_prefix(seeded: Path) -> None:
    listing = run("question", "list", "--course", "数据结构", "--type", "true_false")
    assert "判断题" in run("question", "show", listing.split()[0][:8])


# --------------------------------------------------------------- 重复导入
def test_reimport_skips_duplicates(seeded: Path) -> None:
    output = run("question", "add", "数据结构", str(seeded))
    assert "新建 0" in output and "跳过 3" in output


def test_reimport_with_overwrite_updates(seeded: Path) -> None:
    output = run("question", "add", "数据结构", str(seeded), "--on-conflict", "overwrite")
    assert "更新 3" in output


def test_duplicates_command_reports_exact_matches(seeded: Path) -> None:
    output = run("question", "duplicates", "数据结构", str(seeded))
    assert output.count("完全相同") == 3


def test_duplicates_command_makes_no_changes(seeded: Path) -> None:
    before = run("info")
    run("question", "duplicates", "数据结构", str(seeded))
    assert run("info") == before


# --------------------------------------------------------------- 回收站
def test_delete_restore_cycle(seeded: Path) -> None:
    listing = run("question", "list", "--course", "数据结构", "--type", "true_false")
    question_id = listing.split()[0]

    run("question", "rm", question_id)
    assert "没有匹配的题目" in run(
        "question", "list", "--course", "数据结构", "--type", "true_false"
    )
    assert question_id[:8] in run("question", "trash")

    run("question", "restore", question_id)
    assert "回收站是空的" in run("question", "trash")
    assert "栈是一种先进先出" in run("question", "list", "--course", "数据结构", "-s", "栈")


# --------------------------------------------------------------- 历史与回滚
def test_history_and_rollback(seeded: Path, tmp_path: Path) -> None:
    listing = run("question", "list", "--course", "数据结构", "--type", "single_choice")
    question_id = listing.split()[0]

    modified = [dict(SAMPLE_QUESTIONS[0], score=9)]
    changed = tmp_path / "changed.yaml"
    changed.write_text(yaml.safe_dump(modified, allow_unicode=True), encoding="utf-8")
    run("question", "add", "数据结构", str(changed), "--on-conflict", "overwrite")

    assert "9.0分" in run("question", "list", "--course", "数据结构", "--type", "single_choice")
    assert "第 1 版" in run("question", "history", question_id)

    run("question", "history", question_id, "--rollback-to", "1")
    assert "2.0分" in run("question", "list", "--course", "数据结构", "--type", "single_choice")


def test_rollback_to_missing_revision_fails_clearly(seeded: Path) -> None:
    listing = run("question", "list", "--course", "数据结构", "--type", "true_false")
    result = runner.invoke(app, ["question", "history", listing.split()[0], "--rollback-to", "99"])
    assert result.exit_code == 1
    assert "没有第 99 版" in result.output


# --------------------------------------------------------------- 标签维护
def test_tag_merge_reports_dedup_separately(seeded: Path) -> None:
    """那道综合题同时带「图」和「最短路」，合并时是去重而非转移。"""
    output = run("tag", "merge", "最短路", "图")
    assert "已去重" in output, "只报转移数会给出「0 道题受影响」，与事实不符"


def test_purge_requires_confirmation_without_yes(seeded: Path) -> None:
    """清空回收站不可撤销，默认必须二次确认。"""
    listing = run("question", "list", "--course", "数据结构", "--type", "true_false")
    run("question", "rm", listing.split()[0])
    result = runner.invoke(app, ["question", "trash", "--purge", "--course", "数据结构"])
    assert result.exit_code != 0, "没有 --yes 时应中止而不是直接删除"
    assert question_still_in_trash()


def question_still_in_trash() -> bool:
    return "回收站是空的" not in run("question", "trash")


def test_tag_clean_removes_unused(seeded: Path) -> None:
    listing = run("question", "list", "--course", "数据结构", "--type", "true_false")
    run("question", "rm", listing.split()[0])
    run("question", "trash", "--purge", "--yes", "--course", "数据结构")
    assert "已清理" in run("tag", "clean")


# --------------------------------------------------------------- 维护命令
def test_reindex_rebuilds(seeded: Path) -> None:
    assert "已重建 5 条索引" in run("reindex"), "3 道顶层题 + 2 道子题"


def test_info_reports_paths_and_counts(seeded: Path) -> None:
    output = run("info")
    assert "题库目录" in output and "qbank.db" in output
    assert "1 门" in output


# --------------------------------------------------------------- 模板导入
EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"


def test_import_markdown_example() -> None:
    run("course", "add", "数据结构")
    output = run("import", "数据结构", str(EXAMPLES / "导入模板示例.md"), "--yes")
    assert "新建 7" in output
    assert "7 道" in run("info")


def test_import_dry_run_writes_nothing() -> None:
    """``--dry-run`` 是用户敢于尝试导入的前提，必须真的什么都不写。"""
    run("course", "add", "数据结构")
    output = run("import", "数据结构", str(EXAMPLES / "导入模板示例.md"), "--dry-run")
    assert "未写入任何内容" in output
    assert "0 道" in run("info")


def test_import_requires_confirmation_without_yes() -> None:
    run("course", "add", "数据结构")
    result = runner.invoke(app, ["import", "数据结构", str(EXAMPLES / "导入模板示例.md")])
    assert result.exit_code != 0, "没有 --yes 时应停下来确认"
    assert "0 道" in run("info")


def test_import_only_selected_indices() -> None:
    run("course", "add", "数据结构")
    output = run(
        "import", "数据结构", str(EXAMPLES / "导入模板示例.md"), "--only", "1,3-4", "--yes"
    )
    assert "新建 3" in output


def test_import_reports_duplicates_on_second_run() -> None:
    run("course", "add", "数据结构")
    run("import", "数据结构", str(EXAMPLES / "导入模板示例.md"), "--yes")
    output = run("import", "数据结构", str(EXAMPLES / "导入模板示例.md"), "--yes")
    assert "跳过 7" in output
    assert "完全相同" in output


def test_import_overwrite_updates() -> None:
    run("course", "add", "数据结构")
    run("import", "数据结构", str(EXAMPLES / "导入模板示例.md"), "--yes")
    output = run(
        "import",
        "数据结构",
        str(EXAMPLES / "导入模板示例.md"),
        "--on-conflict",
        "overwrite",
        "--yes",
    )
    assert "更新 7" in output


def test_batch_list_and_rollback() -> None:
    run("course", "add", "数据结构")
    run("import", "数据结构", str(EXAMPLES / "导入模板示例.md"), "--yes")
    listing = run("batch", "list")
    assert "导入模板示例.md" in listing
    batch_id = listing.split()[0]
    assert "已撤销，删除 7 道题" in run("batch", "rollback", batch_id, "--yes")
    assert "0 道" in run("info")


def test_import_unsupported_format_is_rejected(tmp_path: Path) -> None:
    run("course", "add", "数据结构")
    bogus = tmp_path / "题库.pdf"
    bogus.write_text("不是支持的格式", encoding="utf-8")
    result = runner.invoke(app, ["import", "数据结构", str(bogus), "--yes"])
    assert result.exit_code == 1
    assert "不支持的文件类型" in result.output


def test_import_missing_file_is_reported(tmp_path: Path) -> None:
    run("course", "add", "数据结构")
    result = runner.invoke(app, ["import", "数据结构", str(tmp_path / "不存在.md"), "--yes"])
    assert result.exit_code == 1
    assert "找不到文件" in result.output


def test_formats_command_lists_dependency_state() -> None:
    output = run("formats")
    assert ".md" in output and ".docx" in output
    assert "pandoc" in output


def test_asset_stats_and_clean() -> None:
    run("course", "add", "数据结构")
    run("import", "数据结构", str(EXAMPLES / "导入模板示例.md"), "--yes")
    assert "没有孤儿图片" in run("asset", "stats")
    assert "没有孤儿图片" in run("asset", "clean")


# ===================================================================== 组卷
BULK_QUESTIONS = [
    {
        "type": "single_choice",
        "stem": f"第{index}题：下列关于栈的说法正确的是？",
        "payload": {
            "type": "single_choice",
            "options": [
                {"label": "A", "content": "后进先出", "is_correct": True},
                {"label": "B", "content": "先进先出"},
                {"label": "C", "content": "随机存取"},
                {"label": "D", "content": "以上都不对"},
            ],
        },
        "answer": "A",
        "difficulty": 2 + index % 3,
        "chapter": "第1章 绪论",
        "status": "active",
    }
    for index in range(20)
]


@pytest.fixture
def paper_bank(tmp_path: Path) -> Path:
    """一门课 + 20 道启用状态的单选题，够组几份卷。"""
    path = tmp_path / "bulk.yaml"
    path.write_text(
        yaml.safe_dump(BULK_QUESTIONS, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    run("course", "add", "数据结构")
    run("question", "add", "数据结构", str(path))
    blueprint = tmp_path / "blueprint.yaml"
    blueprint.write_text(
        yaml.safe_dump(
            {
                "title": "期末考试",
                "total_score": 100,
                "sections": [
                    {
                        "title": "一、单项选择题",
                        "types": ["single_choice"],
                        "count": 10,
                        "score_each": 10,
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return blueprint


def test_assemble_dry_run_writes_nothing(paper_bank: Path) -> None:
    output = run("paper", "assemble", "数据结构", str(paper_bank), "--dry-run", "--seed", "7")
    assert "10/10 道" in output
    assert "没有生成试卷" in output
    assert "还没有试卷" in run("paper", "list")


def test_assemble_then_show(paper_bank: Path) -> None:
    output = run("paper", "assemble", "数据结构", str(paper_bank), "--seed", "7")
    assert "已生成" in output
    assert "种子 7" in output

    listed = run("paper", "list")
    assert "100 分" in listed
    assert "seed=7" in listed

    paper_id = listed.split()[0]
    shown = run("paper", "show", paper_id, "--answers")
    assert "期末考试" in shown
    assert "实际 10 道、100 分" in shown
    assert "答案：A" in shown


def test_same_seed_draws_the_same_questions(paper_bank: Path, tmp_path: Path) -> None:
    """题库状态没变时，两次 --dry-run 必须抽到同一批题。"""
    first = run("paper", "assemble", "数据结构", str(paper_bank), "--dry-run", "--seed", "99")
    second = run("paper", "assemble", "数据结构", str(paper_bank), "--dry-run", "--seed", "99")
    assert first == second


def test_shortage_is_diagnosed_and_blocks_generation(paper_bank: Path, tmp_path: Path) -> None:
    """抽不齐时报清楚缺什么，并且**不**生成缺斤短两的卷子。"""
    greedy = tmp_path / "greedy.yaml"
    greedy.write_text(
        yaml.safe_dump(
            {
                "title": "不可能的卷",
                "sections": [
                    {"title": "一、单选", "types": ["single_choice"], "count": 50, "score_each": 2}
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["paper", "assemble", "数据结构", str(greedy)])
    assert result.exit_code == 1
    assert "缺 30 道" in result.output
    assert "没有生成试卷" in result.output
    assert "还没有试卷" in run("paper", "list")


def test_blueprint_errors_are_listed_not_stack_traced(paper_bank: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "title: x\nsections:\n  - title: 一\n    count: 2\n    scores_each: 2\n", "utf-8"
    )
    result = runner.invoke(app, ["paper", "assemble", "数据结构", str(bad), "--dry-run"])
    assert result.exit_code == 1
    assert "有 1 处问题" in result.output
    assert "scores_each" in result.output
    assert "Traceback" not in result.output


def test_variant_avoids_the_original(paper_bank: Path) -> None:
    run("paper", "assemble", "数据结构", str(paper_bank), "--seed", "7")
    paper_id = run("paper", "list").split()[0]
    output = run("paper", "variant", paper_id, "--title", "B 卷", "--seed", "8")
    assert "已生成《B 卷》" in output

    first = {line for line in run("paper", "show", paper_id).splitlines() if "：下列关于栈" in line}
    second_id = next(
        line.split()[0] for line in run("paper", "list").splitlines() if "B 卷" in line
    )
    second = {
        line for line in run("paper", "show", second_id).splitlines() if "：下列关于栈" in line
    }
    assert first and second and not first & second


def test_manual_paper_add_and_remove(paper_bank: Path) -> None:
    run("paper", "new", "数据结构", "随堂测验", "--section", "一、选择题")
    paper_id = run("paper", "list").split()[0]
    question_id = run("question", "list", "--course", "数据结构").split()[0]

    assert "加入 1 道" in run("paper", "add", paper_id, question_id, "--score", "5")
    assert "加入 0 道" in run("paper", "add", paper_id, question_id)

    assert "已移入回收站" in run("paper", "rm", paper_id)
    assert "还没有试卷" in run("paper", "list")
