"""运行期配置与跨平台数据目录。

所有落盘位置都必须经由本模块取得，不许在业务代码里拼路径字符串——这是
macOS / Linux / Windows 三平台一致性的唯一保障点。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "qbank"
APP_AUTHOR = "qbank"

#: 覆盖数据目录的环境变量（测试与便携模式用）
ENV_DATA_DIR = "QBANK_DATA_DIR"


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
        """用户自定义导出模板的存放位置。导出功能尚未实现，目录先占住。"""
        return self.root / "templates"

    @property
    def exports_dir(self) -> Path:
        """默认导出目录。导出功能尚未实现。"""
        return self.root / "exports"

    @property
    def backups_dir(self) -> Path:
        """``.qbz`` 交换包与自动备份的存放位置。两者都尚未实现。"""
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
