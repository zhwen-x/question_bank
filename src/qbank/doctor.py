"""环境自检：探测各项能力是否可用，并给出可操作的修复提示。

设计原则：**可选能力缺失不应阻断主流程**。每项检查都明确区分
``REQUIRED``（缺了跑不起来）与 ``OPTIONAL``（缺了只是降级），
并给出降级后的替代路径，而不是只报一个错。
"""

from __future__ import annotations

import importlib.util
import platform
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from qbank.config import get_paths

#: 中文检索的两个实测坑（见 content.segment）：FTS5 的 trigram 分词器对 2 字
#: 中文词返回 0 条；jieba 的切分依赖上下文，会让「题在库里却搜不到」。
#: 现方案是字符一元+二元 n-gram。这里用一条跨越上下文的探针验证它确实可用：
#: 探针文本里「最短」被夹在更长的串中，而查询词单独出现。
_CJK_PROBE_TEXT = "用 Dijkstra 求最短路径与二次函数的性质"
_CJK_PROBE_TERMS = ("函数", "性质", "最短路径", "栈")
_CJK_PROBE_ABSENT = "栈"


class Level(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class Status(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: Level
    status: Status
    detail: str
    #: 缺失/降级时给用户的下一步动作
    remedy: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.level is Level.REQUIRED and self.status is not Status.OK


def _check_python() -> Check:
    ok = sys.version_info >= (3, 10)
    return Check(
        name="Python",
        level=Level.REQUIRED,
        status=Status.OK if ok else Status.MISSING,
        detail=f"{platform.python_version()} on {platform.system()} {platform.machine()}",
        remedy="" if ok else "需要 Python >= 3.10",
    )


def _check_data_dir() -> Check:
    paths = get_paths()
    try:
        paths.ensure()
        probe = paths.cache_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            name="数据目录",
            level=Level.REQUIRED,
            status=Status.MISSING,
            detail=f"{paths.root} 不可写：{exc}",
            remedy="检查目录权限，或用 QBANK_DATA_DIR 指定别处",
        )
    return Check(
        name="数据目录",
        level=Level.REQUIRED,
        status=Status.OK,
        detail=str(paths.root),
    )


def _check_sqlite_fts() -> Check:
    try:
        con = sqlite3.connect(":memory:")
    except sqlite3.Error as exc:  # pragma: no cover - 理论上不会发生
        return Check("SQLite", Level.REQUIRED, Status.MISSING, str(exc))
    try:
        con.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
    except sqlite3.OperationalError:
        return Check(
            name="SQLite FTS5",
            level=Level.REQUIRED,
            status=Status.MISSING,
            detail=f"SQLite {sqlite3.sqlite_version} 未编译 FTS5",
            remedy="换一个带 FTS5 的 Python 发行版（官方安装包与 pyenv 默认都带）",
        )
    finally:
        con.close()
    return Check(
        name="SQLite FTS5",
        level=Level.REQUIRED,
        status=Status.OK,
        detail=f"SQLite {sqlite3.sqlite_version}，FTS5 可用",
    )


def _check_cjk_search() -> Check:
    """验证字符 n-gram 索引在跨上下文的情况下仍能正确命中。"""
    # 走真实的分词入口，这样自检覆盖的就是线上同一条代码路径
    from qbank.core.content.segment import segment, to_match_query

    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE probe USING fts5(x, tokenize='unicode61')")
        con.execute("INSERT INTO probe(x) VALUES (?)", (segment(_CJK_PROBE_TEXT),))

        def hits(term: str) -> int:
            query = to_match_query(term)
            if not query:
                return 0
            return int(
                con.execute("SELECT count(*) FROM probe WHERE probe MATCH ?", (query,)).fetchone()[
                    0
                ]
            )

        missing = [
            term for term in _CJK_PROBE_TERMS if term != _CJK_PROBE_ABSENT and not hits(term)
        ]
        false_positive = hits(_CJK_PROBE_ABSENT) > 0
    finally:
        con.close()

    if missing:
        return Check(
            name="中文检索",
            level=Level.REQUIRED,
            status=Status.DEGRADED,
            detail=f"以下词应能命中却没有：{'、'.join(missing)}",
            remedy="分词层异常，检查 qbank.core.content.segment",
        )
    if false_positive:
        return Check(
            name="中文检索",
            level=Level.REQUIRED,
            status=Status.DEGRADED,
            detail=f"未出现的词 {_CJK_PROBE_ABSENT!r} 被误命中",
            remedy="分词层异常，检查 qbank.core.content.segment",
        )
    return Check(
        name="中文检索",
        level=Level.REQUIRED,
        status=Status.OK,
        detail="字符 n-gram + FTS5 unicode61，单字与多字词均可检索",
    )


def _check_pandoc() -> Check:
    if importlib.util.find_spec("pypandoc") is None:
        return Check(
            name="pandoc",
            level=Level.OPTIONAL,
            status=Status.MISSING,
            detail="pypandoc 未安装",
            remedy="pip install pypandoc_binary —— 缺失时无法导入 Word/HTML，Markdown 仍可用",
        )
    import pypandoc

    try:
        version = pypandoc.get_pandoc_version()
    except OSError as exc:
        return Check(
            name="pandoc",
            level=Level.OPTIONAL,
            status=Status.MISSING,
            detail=f"找不到 pandoc 二进制：{exc}",
            remedy="pip install pypandoc_binary（自带二进制），或系统安装 pandoc",
        )
    return Check(
        name="pandoc",
        level=Level.OPTIONAL,
        status=Status.OK,
        detail=f"pandoc {version}，可导入 docx/html，Word 公式自动转 LaTeX",
    )


def _check_pdf() -> Check:
    if importlib.util.find_spec("playwright") is None:
        return Check(
            name="PDF 渲染依赖",
            level=Level.OPTIONAL,
            status=Status.MISSING,
            detail="playwright 未安装（导出功能本身尚未实现）",
            remedy='导出上线后需要它；可提前装：pip install "qbank[pdf]"'
            " 再执行 playwright install chromium",
        )
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            executable = pw.chromium.executable_path
    except Exception as exc:
        return Check(
            name="PDF 渲染依赖",
            level=Level.OPTIONAL,
            status=Status.DEGRADED,
            detail=f"playwright 已装但 Chromium 不可用：{exc}",
            remedy="playwright install chromium（导出功能本身尚未实现）",
        )
    from pathlib import Path

    if not Path(executable).exists():
        return Check(
            name="PDF 渲染依赖",
            level=Level.OPTIONAL,
            status=Status.DEGRADED,
            detail="Chromium 内核尚未下载（导出功能本身尚未实现）",
            remedy="导出上线后需要它；可提前执行 playwright install chromium（约 130MB）",
        )
    return Check(
        name="PDF 渲染依赖",
        level=Level.OPTIONAL,
        status=Status.OK,
        detail="Playwright + Chromium 可用（导出功能尚未实现，依赖已就位）",
    )


def _check_web() -> Check:
    missing = [m for m in ("fastapi", "uvicorn") if importlib.util.find_spec(m) is None]
    if missing:
        return Check(
            name="Web 界面",
            level=Level.OPTIONAL,
            status=Status.MISSING,
            detail=f"缺少 {', '.join(missing)}",
            remedy='pip install "qbank[web]" —— 缺失时只能用命令行',
        )
    return Check(name="Web 界面", level=Level.OPTIONAL, status=Status.OK, detail="FastAPI 可用")


_CHECKS = (
    _check_python,
    _check_data_dir,
    _check_sqlite_fts,
    _check_cjk_search,
    _check_pandoc,
    _check_pdf,
    _check_web,
)


def run_checks() -> Iterator[Check]:
    """依次执行全部检查。"""
    for fn in _CHECKS:
        yield fn()
