"""导入暂存区。

导入是「先预览、后确认」两步，中间隔着一次用户决策。文档必须在两次请求之间
存活，有两种做法：

把计划存在内存里，用 token 索引
    要处理过期清理、进程重启丢失、多标签页互相覆盖。而且 pandoc 抽出的图片
    在临时目录里，内存方案还要额外保住那个目录。

**把上传的文件落到暂存目录，每次请求重新解析**（本模块的做法）
    解析一份几百道题的文件只要几十毫秒，重复做的代价微不足道。换来的是：
    预览页可以刷新、可以改冲突策略重新看、服务重启也不丢，而且没有任何
    需要过期清理的内存状态。

暂存文件放在缓存目录下，提交或丢弃后立即删除；残留的旧文件由
:func:`sweep` 清理——用户中途关掉浏览器是常态，不能指望他点「丢弃」。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from qbank.config import Paths, safe_filename
from qbank.core.models import utcnow

#: 暂存文件保留多久。超过就当作用户已经放弃。
STAGING_TTL = timedelta(hours=12)

#: 上传文件大小上限
MAX_UPLOAD_BYTES = 80 * 1024 * 1024

#: token 与文件名之间的分隔，token 本身是 32 位十六进制
_STAGED_NAME = re.compile(r"^(?P<token>[0-9a-f]{32})__(?P<name>.+)$")


class StagingError(ValueError):
    """暂存失败（文件过大、类型不支持、token 无效）。"""


@dataclass(frozen=True, slots=True)
class StagedFile:
    token: str
    path: Path

    @property
    def original_name(self) -> str:
        matched = _STAGED_NAME.match(self.path.name)
        return matched.group("name") if matched else self.path.name


def staging_dir(paths: Paths) -> Path:
    directory = paths.cache_dir / "imports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def stage(paths: Paths, data: bytes, original_name: str) -> StagedFile:
    """把上传的内容落盘，返回 token。

    文件名经过 ``safe_filename`` 清洗但**保留原始后缀**——后缀决定了用哪条
    解析路径（docx 要走 pandoc，md 直接读），丢了就没法判断格式。
    """
    if not data:
        raise StagingError("文件是空的")
    if len(data) > MAX_UPLOAD_BYTES:
        raise StagingError(f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB")

    source = Path(original_name or "未命名")
    suffix = source.suffix.lower()
    if not suffix:
        raise StagingError("文件没有扩展名，无法判断格式")
    stem = safe_filename(source.stem, fallback="导入文件", max_length=80)
    token = uuid.uuid4().hex
    target = staging_dir(paths) / f"{token}__{stem}{suffix}"
    target.write_bytes(data)
    return StagedFile(token=token, path=target)


def find(paths: Paths, token: str) -> StagedFile:
    """按 token 取回暂存文件。

    token 必须是 32 位十六进制——它直接参与拼路径，放任何其他字符进来都会
    变成目录穿越漏洞（``../../`` 之类）。
    """
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise StagingError("无效的上传标识")
    for candidate in staging_dir(paths).iterdir():
        if candidate.is_file() and candidate.name.startswith(f"{token}__"):
            return StagedFile(token=token, path=candidate)
    raise StagingError("这次上传已经过期或被清理，请重新上传文件")


def discard(paths: Paths, token: str) -> None:
    """删掉暂存文件。删不掉就算了——它在缓存目录里，sweep 迟早会清。"""
    try:
        find(paths, token).path.unlink(missing_ok=True)
    except (StagingError, OSError):
        return


def sweep(paths: Paths, *, ttl: timedelta = STAGING_TTL) -> int:
    """清理过期的暂存文件，返回清理数量。

    用户中途关掉浏览器是常态，不能指望他点「丢弃」。
    """
    cutoff = (utcnow() - ttl).timestamp()
    removed = 0
    for candidate in staging_dir(paths).iterdir():
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except OSError:
            continue
    return removed
