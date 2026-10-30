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

#: FTS5 trigram 分词器要求查询串 >= 3 字符，2 字中文词（"函数"/"性质"）查不到，
#: 因此中文检索改走 jieba 预分词 + unicode61。此处把该事实固化为一项检查。
_CJK_PROBE_TEXT = "二次函数的图像与性质"
_CJK_PROBE_TERM = "函数"


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
    """验证「jieba 分词 + unicode61」确实能命中 2 字中文词。"""
    if importlib.util.find_spec("jieba") is None:
        return Check(
            name="中文检索",
            level=Level.REQUIRED,
            status=Status.MISSING,
            detail="jieba 未安装",
            remedy="pip install jieba",
        )
    # 走真实的分词入口，而不是直接调 jieba——这样自检覆盖的就是线上同一条代码路径
    from qbank.core.content.segment import segment, to_match_query

    segmented = segment(_CJK_PROBE_TEXT)
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE probe USING fts5(x, tokenize='unicode61')")
        con.execute("INSERT INTO probe(x) VALUES (?)", (segmented,))
        hit = con.execute(
            "SELECT count(*) FROM probe WHERE probe MATCH ?",
            (to_match_query(_CJK_PROBE_TERM),),
        ).fetchone()[0]
    finally:
        con.close()
    if not hit:
        return Check(
            name="中文检索",
            level=Level.REQUIRED,
            status=Status.DEGRADED,
            detail=f"分词后仍未命中 {_CJK_PROBE_TERM!r}（分词结果：{segmented}）",
            remedy="检查 jieba 版本；或在 search_service 中启用 LIKE 兜底",
        )
    return Check(
        name="中文检索",
        level=Level.REQUIRED,
        status=Status.OK,
        detail=f"jieba 分词 + FTS5 unicode61，{_CJK_PROBE_TERM!r} 命中",
    )


def _check_pandoc() -> Check:
    if importlib.util.find_spec("pypandoc") is None:
        return Check(
            name="pandoc",
            level=Level.OPTIONAL,
            status=Status.MISSING,
            detail="pypandoc 未安装",
            remedy="pip install pypandoc_binary —— 缺失时 Word/HTML 导入导出不可用，md/html 仍可用",
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
        detail=f"pandoc {version}，支持 docx/html 双向转换与 OMML↔LaTeX",
    )


def _check_pdf() -> Check:
    if importlib.util.find_spec("playwright") is None:
        return Check(
            name="PDF 导出",
            level=Level.OPTIONAL,
            status=Status.MISSING,
            detail="playwright 未安装",
            remedy='pip install "qbank[pdf]" 后执行 playwright install chromium'
            " —— 缺失时降级为导出 HTML，由浏览器打印成 PDF",
        )
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            executable = pw.chromium.executable_path
    except Exception as exc:
        return Check(
            name="PDF 导出",
            level=Level.OPTIONAL,
            status=Status.DEGRADED,
            detail=f"playwright 已装但 Chromium 不可用：{exc}",
            remedy="playwright install chromium —— 在此之前降级为导出 HTML",
        )
    from pathlib import Path

    if not Path(executable).exists():
        return Check(
            name="PDF 导出",
            level=Level.OPTIONAL,
            status=Status.DEGRADED,
            detail="Chromium 内核尚未下载",
            remedy="playwright install chromium（约 130MB）—— 在此之前降级为导出 HTML",
        )
    return Check(
        name="PDF 导出",
        level=Level.OPTIONAL,
        status=Status.OK,
        detail="Playwright + Chromium 可用",
    )


def _check_web() -> Check:
    missing = [m for m in ("fastapi", "uvicorn") if importlib.util.find_spec(m) is None]
    if missing:
        return Check(
            name="Web 界面",
            level=Level.OPTIONAL,
            status=Status.MISSING,
            detail=f"缺少 {', '.join(missing)}",
            remedy='pip install "qbank[web]" —— 缺失时仅 CLI 可用',
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
