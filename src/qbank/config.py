"""运行期配置与跨平台数据目录。

所有落盘位置都必须经由本模块取得，不许在业务代码里拼路径字符串——这是
macOS / Linux / Windows 三平台一致性的唯一保障点。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from platformdirs import PlatformDirs


def is_frozen() -> bool:
    """跑在 PyInstaller 打出来的包里吗？

    打包版里 ``pip install`` 是无效建议——包是只读的，装了也进不去。凡是
    「装个什么就好了」的提示，都要先问一句这个，否则给出的是一条走不通的路。
    """
    return bool(getattr(sys, "frozen", False))


APP_NAME = "qbank"
APP_AUTHOR = "qbank"

#: 覆盖数据目录的环境变量（测试与便携模式用）
ENV_DATA_DIR = "QBANK_DATA_DIR"

#: 覆盖数据库连接串的环境变量。设了它就不再用数据目录里的 SQLite 文件，
#: 这是本地单机模式切到 Web/Postgres 部署的唯一开关。
ENV_DATABASE_URL = "QBANK_DATABASE_URL"


@dataclass(frozen=True, slots=True)
class Paths:
    """题库的全部落盘位置。

    默认位置（platformdirs 决定）：

    * macOS   ``~/Library/Application Support/qbank``
    * Linux   ``~/.local/share/qbank``
    * Windows ``%LOCALAPPDATA%\\qbank``
    """

    root: Path

    @property
    def db_file(self) -> Path:
        """SQLite 主库。"""
        return self.root / "qbank.db"

    @property
    def assets_dir(self) -> Path:
        """内容寻址的图片库，按 sha256 前两位分桶，避免单目录文件过多。"""
        return self.root / "assets"

    @property
    def templates_dir(self) -> Path:
        """用户自定义导出模板。这里的同名模板会覆盖内置的那一套。"""
        return self.root / "templates"

    @property
    def exports_dir(self) -> Path:
        """默认导出目录。命令行与界面导出都落在这里。"""
        return self.root / "exports"

    @property
    def backups_dir(self) -> Path:
        """``.qbz`` 交换包与自动备份的存放位置。两者都尚未实现，目录先占住。"""
        return self.root / "backups"

    @property
    def cache_dir(self) -> Path:
        """可随时删除的中间产物（pandoc 临时媒体、渲染缓存）。"""
        return self.root / "cache"

    @property
    def log_file(self) -> Path:
        return self.root / "logs" / "qbank.log"

    def asset_path(self, sha256: str, suffix: str) -> Path:
        """给定内容哈希返回图片落盘路径（分桶）。

        >>> Paths(Path("/tmp/x")).asset_path("abcdef1234", ".png").as_posix()
        '/tmp/x/assets/ab/abcdef1234.png'
        """
        if len(sha256) < 2:
            raise ValueError(f"sha256 过短，无法分桶: {sha256!r}")
        return self.assets_dir / sha256[:2] / f"{sha256}{suffix}"

    def all_dirs(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.assets_dir,
            self.templates_dir,
            self.exports_dir,
            self.backups_dir,
            self.cache_dir,
            self.log_file.parent,
        )

    def ensure(self) -> Paths:
        """创建全部目录（幂等）。"""
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)
        return self


def default_root() -> Path:
    """解析数据目录根：环境变量优先，否则按平台惯例。"""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser().resolve()
    dirs = PlatformDirs(appname=APP_NAME, appauthor=APP_AUTHOR, roaming=False)
    return Path(dirs.user_data_dir)


def sqlite_url(path: Path) -> str:
    """SQLite 文件的连接串。

    >>> sqlite_url(Path("/tmp/x/qbank.db"))
    'sqlite+pysqlite:////tmp/x/qbank.db'
    """
    return f"sqlite+pysqlite:///{path}"


#: 外部平台给出的连接串需要改写的前缀。
#:
#: ``postgres://`` 是 Heroku / Railway / Fly 这类平台在 ``DATABASE_URL`` 里的
#: 惯用写法，而 SQLAlchemy 2.0 直接拒收它（``Can't load plugin``）。
#: 裸 ``postgresql://`` 能被接受，但默认挑 psycopg2——本项目装的是 psycopg 3，
#: 于是报 ``ModuleNotFoundError: psycopg2``。两种写法都是复制粘贴来的常态，
#: 在这里一次改对，比让每个部署的人自己去查文档划算。
_URL_REWRITES: tuple[tuple[str, str], ...] = (
    ("postgres://", "postgresql+psycopg://"),
    ("postgresql://", "postgresql+psycopg://"),
)


def normalize_database_url(url: str) -> str:
    """把常见的连接串写法补成 SQLAlchemy 能用的形式。

    >>> normalize_database_url("postgres://u:p@h/qbank")
    'postgresql+psycopg://u:p@h/qbank'
    >>> normalize_database_url("postgresql://localhost/qbank")
    'postgresql+psycopg://localhost/qbank'
    >>> normalize_database_url("postgresql+psycopg2://localhost/qbank")
    'postgresql+psycopg2://localhost/qbank'
    >>> normalize_database_url("sqlite+pysqlite:///a.db")
    'sqlite+pysqlite:///a.db'
    """
    stripped = url.strip()
    for prefix, replacement in _URL_REWRITES:
        if stripped.startswith(prefix):
            return replacement + stripped[len(prefix) :]
    return stripped


def database_url() -> str:
    """题库的连接串：环境变量优先，否则是数据目录里的 SQLite 文件。"""
    override = os.environ.get(ENV_DATABASE_URL, "").strip()
    if override:
        return normalize_database_url(override)
    return sqlite_url(get_paths().db_file)


@lru_cache(maxsize=1)
def get_paths() -> Paths:
    """进程内共享的 :class:`Paths`。

    测试里改了 ``QBANK_DATA_DIR`` 之后需调用 ``get_paths.cache_clear()``。
    """
    return Paths(root=default_root())


#: Windows 保留设备名；导出文件名必须回避，否则在 Windows 上无法创建
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_ILLEGAL_CHARS = '<>:"/\\|?*'


def safe_filename(name: str, *, fallback: str = "untitled", max_length: int = 120) -> str:
    """把任意标题转成三平台都能落盘的文件名（不含扩展名）。

    处理：非法字符、控制字符、首尾空格与点、Windows 保留名、过长截断。

    >>> safe_filename('数据结构 期末A卷 2026/01')
    '数据结构 期末A卷 2026_01'
    >>> safe_filename('CON')
    'CON_'
    >>> safe_filename('   ...   ')
    'untitled'
    """
    cleaned = "".join("_" if ch in _ILLEGAL_CHARS or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.strip().rstrip(". ")
    if not cleaned:
        return fallback
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = f"{cleaned}_"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ") or fallback
    return cleaned
