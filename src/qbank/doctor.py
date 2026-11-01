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

from qbank.config import database_url, get_paths, is_frozen

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


def _backend() -> str:
    """配置里指向哪种数据库。连不上也不影响这个判断——它只看连接串。"""
    from sqlalchemy import make_url

    return str(make_url(database_url()).get_backend_name())


def _check_database() -> Check:
    """库在哪、连不连得上。

    这是 Web 部署的头号故障：口令错、库没建、防火墙没开、容器里 ``localhost``
    指的是容器自己。本机 SQLite 不走网络，所以只报路径，不去连——诊断命令不该
    顺手建出一个空库来。
    """
    from qbank.core.db import describe_database

    described = describe_database()
    if _backend() == "sqlite":
        path = get_paths().db_file
        if not path.exists():
            return Check(
                name="数据库",
                level=Level.REQUIRED,
                status=Status.OK,
                detail=f"{described}（还没创建，首次打开时会自动建）",
            )
        size = path.stat().st_size / 1024 / 1024
        return Check("数据库", Level.REQUIRED, Status.OK, f"{described}，{size:.1f} MB")

    from sqlalchemy import text as sql_text

    from qbank.core.db import make_engine

    engine = make_engine()
    try:
        with engine.connect() as conn:
            version = str(conn.execute(sql_text("SHOW server_version")).scalar() or "?")
    except Exception as exc:
        return Check(
            name="数据库",
            level=Level.REQUIRED,
            status=Status.MISSING,
            detail=f"{described} 连不上：{type(exc).__name__}: {exc}",
            remedy="核对 QBANK_DATABASE_URL 的主机、库名与口令；容器里的 localhost 指的是容器自己",
        )
    finally:
        engine.dispose()
    return Check("数据库", Level.REQUIRED, Status.OK, f"{described}，服务端 {version}")


def _check_sqlite_fts() -> Check:
    if _backend() != "sqlite":
        return Check(
            name="SQLite FTS5",
            level=Level.OPTIONAL,
            status=Status.OK,
            detail="当前用的是 Postgres，这项用不到",
        )
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
    """验证字符 n-gram 索引在跨上下文的情况下仍能正确命中。

    **按当前后端探，不是永远探 SQLite**。这项检查的全部意义就在于「索引端与
    查询端是否真的对得上」，而那件事恰恰是换后端时最容易崩的：实测 Postgres 的
    ``default`` 解析器把中日韩字符判成空白，``to_tsvector`` 出来的向量里一个中文
    token 都没有。如果这里永远去探一个内存 SQLite，那种部署会一路绿灯，
    而用户看到的是「搜中文永远空」。
    """
    if _backend() != "sqlite":
        return _check_cjk_search_on_postgres()
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


def _check_cjk_search_on_postgres() -> Check:
    """在真库上走一遍写入—查询，然后回滚。

    用的是线上同一个 :class:`~qbank.core.services.search_backend.TsvectorBackend`，
    探针行写进真正的 ``question_fts`` 表再回滚，所以不留痕迹。
    """
    from qbank.core.db import make_engine, make_session_factory
    from qbank.core.services.search import segment_question
    from qbank.core.services.search_backend import TsvectorBackend

    probe_id = "doctor-cjk-probe"
    engine = make_engine()
    try:
        session = make_session_factory(engine)()
        try:
            backend = TsvectorBackend(session)
            backend.upsert(probe_id, segment_question(_CJK_PROBE_TEXT))
            missing = [
                term
                for term in _CJK_PROBE_TERMS
                if term != _CJK_PROBE_ABSENT
                and probe_id not in [hit.question_id for hit in backend.query(term, limit=5)]
            ]
            false_positive = probe_id in [
                hit.question_id for hit in backend.query(_CJK_PROBE_ABSENT, limit=5)
            ]
        finally:
            session.rollback()
            session.close()
    except Exception as exc:
        return Check(
            name="中文检索",
            level=Level.REQUIRED,
            status=Status.MISSING,
            detail=f"探针跑不起来：{type(exc).__name__}: {exc}",
            remedy="先让「数据库」这一项变绿；索引表缺失时跑 `alembic upgrade head`",
        )
    finally:
        engine.dispose()

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
        detail="字符 n-gram + Postgres tsvector（绕过其解析器），单字与多字词均可检索",
    )


def _check_auth() -> Check:
    """鉴权模式。

    列为可选，但**必需的是「知道当前是哪种」**：``qbank serve --host 0.0.0.0``
    加上默认的不鉴权，等于把题库放在局域网里任人编辑，而这件事在界面上看不出来。
    """
    try:
        from qbank.web.auth import AuthError, OpenAccess, policy_from_env
    except ImportError:
        # Web 依赖是可选的；没装就谈不上鉴权。缺什么由「Web 界面」那项去报
        return Check("鉴权", Level.OPTIONAL, Status.OK, "未安装 Web 依赖，不涉及")

    try:
        policy = policy_from_env()
    except AuthError as exc:
        return Check(
            name="鉴权",
            level=Level.REQUIRED,
            status=Status.MISSING,
            detail=str(exc),
            remedy="改对 QBANK_AUTH 相关环境变量；配错时应用会拒绝启动，不会退回不鉴权",
        )
    if isinstance(policy, OpenAccess):
        return Check(
            name="鉴权",
            level=Level.OPTIONAL,
            status=Status.OK,
            detail="不鉴权（本地客户端的正确设置）",
            remedy="要让别人通过网络访问就得设 QBANK_AUTH；见 docs/Web部署指南.md",
        )
    return Check(name="鉴权", level=Level.OPTIONAL, status=Status.OK, detail=policy.label)


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
    """PDF 导出能力。

    不只看 playwright 装没装：自带内核要单独下载约 130MB，而且**不是所有平台
    都有**（macOS 12 上 ``playwright install chromium`` 直接报不支持）。所以
    实际能力由 :func:`qbank.exchange.exporters.pdf.capability` 真的启一次浏览器
    来判断，系统装的 Chrome / Edge 同样算数。
    """
    from qbank.exchange.exporters import pdf

    found = pdf.capability()
    if found.available:
        return Check(
            name="PDF 导出",
            level=Level.OPTIONAL,
            status=Status.OK,
            detail=f"可用，用的是{found.detail}",
        )
    if importlib.util.find_spec("playwright") is None:
        return Check(
            name="PDF 导出",
            level=Level.OPTIONAL,
            status=Status.MISSING,
            detail="未安装 playwright；其他三种格式不受影响",
            remedy=(
                "这个打包版没有捆 PDF 支持：请导出 HTML 后用浏览器「打印为 PDF」，版面完全一样"
                if is_frozen()
                else 'pip install "qbank[pdf]"，再装内核或用系统已有的 Chrome'
            ),
        )
    return Check(
        name="PDF 导出",
        level=Level.OPTIONAL,
        status=Status.DEGRADED,
        detail=found.detail,
        remedy="playwright install chromium（约 130MB），或装一个 Google Chrome；"
        "在此之前可导出 HTML 后用浏览器打印",
    )


def _check_desktop() -> Check:
    """桌面外壳。缺了不影响任何功能——`qbank serve` 的界面完全一样。"""
    from qbank import desktop

    found = desktop.capability()
    if found.available:
        return Check(
            name="桌面窗口",
            level=Level.OPTIONAL,
            status=Status.OK,
            detail=f"{found.detail}，可用 `qbank desktop` 在原生窗口里打开",
        )
    return Check(
        name="桌面窗口",
        level=Level.OPTIONAL,
        status=Status.MISSING,
        detail=f"{found.detail}；用 `qbank serve` 在浏览器里打开，功能完全一样",
        remedy="" if is_frozen() else 'pip install "qbank[desktop]"',
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
    _check_database,
    _check_sqlite_fts,
    _check_cjk_search,
    _check_auth,
    _check_pandoc,
    _check_pdf,
    _check_desktop,
    _check_web,
)


def run_checks() -> Iterator[Check]:
    """依次执行全部检查。"""
    for fn in _CHECKS:
        yield fn()
