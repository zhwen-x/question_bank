"""导出时的图片处理：三种目标，三个解析器。

库里存的是 ``asset://<sha256>``，三种导出目标要的东西完全不同：

===========  ==========================================================
自包含 HTML  ``data:image/png;base64,…`` 内联。单文件发给学生，断网能看
PDF          同上——PDF 由那份 HTML 打印而来
Word         **绝对路径**。pandoc 按路径读图，找不到时**不报错**，只会静默
             输出一个没有图的文档，所以这里宁可当场失败
Markdown     旁挂目录 ``<文件名>.assets/``，正文里是相对路径。.md 要能原样
             发给别人、提交进 git，内联 base64 会让文本文件无法阅读与 diff
===========  ==========================================================

共同的约定：图找不到时解析器返回 ``None``，由上层换成「［图片缺失］」并计数。
悄悄留一个坏引用的后果是 Word 里一个红叉、HTML 里一个破图图标——用户看到的是
「这张图坏了」，而不是「这张图没带过来」，排查方向完全不同。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

from qbank.core.services.asset import AssetService

#: 内联图片的大小上限。超过的改用旁挂文件，并在报告里提醒。
#:
#: base64 会把体积放大约 1/3，一份带十几张照片的卷子能轻松超过 20MB——
#: 浏览器打得开，但邮件发不出去，而用户只会以为导出坏了。
MAX_INLINE_BYTES = 4 * 1024 * 1024


@dataclass(slots=True)
class AssetReport:
    """一次导出里图片的去向。"""

    #: 成功解析的图片哈希
    resolved: set[str] = field(default_factory=set)
    #: 找不到的图片哈希
    missing: set[str] = field(default_factory=set)
    #: 因为太大而没有内联的图片哈希
    too_large: set[str] = field(default_factory=set)
    #: 旁挂写出的文件
    written: list[Path] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.resolved)


class ExportAssets:
    """按导出目标把 ``asset://`` 解析成具体引用。"""

    def __init__(self, assets: AssetService) -> None:
        self.assets = assets
        self.report = AssetReport()

    # ---------------------------------------------------------- 自包含
    def inline(self, sha256: str) -> str | None:
        """``data:`` 内联。供自包含 HTML 与 PDF 使用。"""
        asset = self.assets.get(sha256)
        data = self.assets.read(sha256)
        if asset is None or data is None:
            self.report.missing.add(sha256)
            return None
        if len(data) > MAX_INLINE_BYTES:
            # 仍然内联：HTML 的卖点就是单文件。但要记下来，让上层能提醒
            self.report.too_large.add(sha256)
        self.report.resolved.add(sha256)
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{asset.mime};base64,{encoded}"

    # ------------------------------------------------------------ Word
    def absolute(self, sha256: str) -> str | None:
        """图片在题库里的绝对路径。pandoc 用。"""
        path = self.assets.path_of(sha256)
        if path is None or not path.is_file():
            self.report.missing.add(sha256)
            return None
        self.report.resolved.add(sha256)
        return path.as_posix()

    # -------------------------------------------------------- Markdown
    def sidecar(self, directory: Path) -> SidecarResolver:
        return SidecarResolver(self, directory)


class SidecarResolver:
    """把图片复制到旁挂目录，正文里换成相对路径。

    目录按需创建：一份没有图的卷子不该在旁边留一个空文件夹。
    """

    def __init__(self, owner: ExportAssets, directory: Path) -> None:
        self.owner = owner
        self.directory = directory

    def __call__(self, sha256: str) -> str | None:
        source = self.owner.assets.path_of(sha256)
        if source is None or not source.is_file():
            self.owner.report.missing.add(sha256)
            return None
        target = self.directory / f"{sha256}{source.suffix}"
        if not target.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            self.owner.report.written.append(target)
        self.owner.report.resolved.add(sha256)
        # 正文里用相对路径，整个目录可以原样搬走
        return f"{self.directory.name}/{target.name}"
