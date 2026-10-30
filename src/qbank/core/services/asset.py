"""图片资源：内容寻址存储。

**为什么按内容哈希存**：同一张图在历年卷子里被引用几十次是常态。按 sha256
存，物理上只有一份；更要紧的是题目正文里写的是 ``asset://<sha256>`` 而不是
文件路径，于是整库搬家、改目录名、打包成 ``.qbz`` 发给同事，引用都不会断。

**为什么不自动压缩**：老师贴进来的常常是手机拍的大图，压缩确实能让导出小很多。
但压缩会改变字节内容，也就改变哈希——同一张原图用不同参数处理会存成两份，
去重失效；而且重新编码是有损的，万一糊了用户的电路图或几何图就麻烦了。
所以这里**原样存储**，只把尺寸过大的图在导入报告里标出来，让人自己决定。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qbank.config import Paths, get_paths
from qbank.core.content.render import ASSET_SCHEME
from qbank.core.models import Asset, QuestionAsset

#: 可接受的图片类型 → 扩展名。白名单而非黑名单：导入来源是别人发来的文件，
#: 不该把任意二进制都收进题库。
SUPPORTED_IMAGE_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
    "image/svg+xml": ".svg",
    "image/x-emf": ".emf",
    "image/x-wmf": ".wmf",
}

#: 超过这个像素宽/高就在导入报告里提示（不自动处理）
LARGE_IMAGE_PIXELS = 2200

#: 超过这个字节数同样提示
LARGE_IMAGE_BYTES = 2 * 1024 * 1024

#: Markdown 里的图片引用。``!`` 前缀区别于普通链接。
_IMAGE_REF = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: 原始 HTML 的 ``<img src="...">``。
#:
#: 为什么必须处理这一种：从 Word 导入时要保留 ``raw_html``（否则合并单元格的
#: 表格会被悄悄拍平成普通表格，而试卷里这种表很常见），而带尺寸信息的图片
#: pandoc 就会输出成 ``<img src="..." style="width:1.5in" />``，不是 Markdown 语法。
_HTML_IMAGE_SRC = re.compile(
    r"(?P<head><img\b[^>]*?\bsrc\s*=\s*(?P<quote>[\"']))(?P<src>[^\"']*)(?P<tail>(?P=quote))",
    re.IGNORECASE,
)

#: Pillow 的格式名 → MIME。Pillow 认出来的格式比文件扩展名可靠。
_PILLOW_FORMATS: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "WMF": "image/x-wmf",
    "EMF": "image/x-emf",
}


class UnsupportedAssetError(ValueError):
    """文件不是受支持的图片类型。"""


@dataclass(frozen=True, slots=True)
class AssetWarning:
    """导入报告里的提示，不阻断导入。"""

    sha256: str
    original_name: str
    message: str


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _probe(data: bytes, original_name: str) -> tuple[str, int, int]:
    """探测 MIME 与像素尺寸。

    优先用 Pillow 读实际内容——扩展名经常是错的（``.jpg`` 里装着 PNG 很常见，
    pandoc 从 docx 抽出来的媒体文件尤其如此）。
    """
    suffix = Path(original_name).suffix.lower()
    looks_like_svg = suffix == ".svg" or data.lstrip()[:5].lower() in (
        b"<svg ",
        b"<svg>",
        b"<?xml",
    )
    if looks_like_svg and b"<svg" in data[:2048].lower():
        return "image/svg+xml", 0, 0
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            mime = _PILLOW_FORMATS.get(image.format or "", "")
            if mime:
                return mime, image.width, image.height
    except Exception:  # Pillow 不认的格式（EMF/WMF 等）退回按扩展名判断
        pass
    for mime, extension in SUPPORTED_IMAGE_TYPES.items():
        if extension == suffix:
            return mime, 0, 0
    raise UnsupportedAssetError(
        f"{original_name or '(未命名)'} 不是受支持的图片类型；"
        f"支持：{'、'.join(sorted(SUPPORTED_IMAGE_TYPES.values()))}"
    )


class AssetService:
    """图片的存取与引用计数。"""

    def __init__(self, session: Session, paths: Paths | None = None) -> None:
        self.session = session
        self.paths = paths or get_paths()

    # ------------------------------------------------------------ 写
    def store_bytes(self, data: bytes, *, original_name: str = "") -> tuple[Asset, bool]:
        """存一张图，返回 ``(资源, 是否新建)``。

        内容相同即视为同一张图，直接复用已有记录，不重复落盘。
        """
        if not data:
            raise UnsupportedAssetError("图片内容为空")
        digest = sha256_of(data)
        existing = self.session.scalar(select(Asset).where(Asset.sha256 == digest))
        if existing is not None:
            self._ensure_on_disk(existing, data)
            return existing, False

        mime, width, height = _probe(data, original_name)
        suffix = SUPPORTED_IMAGE_TYPES[mime]
        asset = Asset(
            sha256=digest,
            mime=mime,
            suffix=suffix,
            byte_size=len(data),
            width=width,
            height=height,
            original_name=Path(original_name).name[:255],
        )
        self.session.add(asset)
        self.session.flush()
        self._write(asset, data)
        return asset, True

    def store_file(self, path: Path) -> tuple[Asset, bool]:
        return self.store_bytes(path.read_bytes(), original_name=path.name)

    def _write(self, asset: Asset, data: bytes) -> None:
        target = self.paths.asset_path(asset.sha256, asset.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _ensure_on_disk(self, asset: Asset, data: bytes) -> None:
        """库里有记录但文件没了（用户清过目录、或从别处拷了 db）时补回来。"""
        target = self.paths.asset_path(asset.sha256, asset.suffix)
        if not target.exists():
            self._write(asset, data)

    # ------------------------------------------------------------ 读
    def get(self, sha256: str) -> Asset | None:
        return self.session.scalar(select(Asset).where(Asset.sha256 == sha256))

    def path_of(self, sha256: str) -> Path | None:
        """图片的磁盘路径。记录或文件不存在时返回 ``None``。"""
        asset = self.get(sha256)
        if asset is None:
            return None
        path = self.paths.asset_path(asset.sha256, asset.suffix)
        return path if path.exists() else None

    def read(self, sha256: str) -> bytes | None:
        path = self.path_of(sha256)
        return path.read_bytes() if path else None

    def warnings_for(self, sha256: str) -> list[AssetWarning]:
        """尺寸或体积过大的提示。不阻断导入，只在报告里列出来。"""
        asset = self.get(sha256)
        if asset is None:
            return []
        warnings: list[AssetWarning] = []
        if max(asset.width, asset.height) > LARGE_IMAGE_PIXELS:
            warnings.append(
                AssetWarning(
                    sha256,
                    asset.original_name,
                    f"尺寸 {asset.width}×{asset.height} 偏大，导出的卷子会比较臃肿",
                )
            )
        if asset.byte_size > LARGE_IMAGE_BYTES:
            warnings.append(
                AssetWarning(
                    sha256,
                    asset.original_name,
                    f"体积 {asset.byte_size / 1024 / 1024:.1f} MB 偏大",
                )
            )
        return warnings

    # ------------------------------------------------------------ 引用关系
    def link(self, question_id: str, sha256_values: Iterable[str]) -> None:
        """重建一道题的图片引用关系，并同步引用计数。

        整体替换而非增量：题目正文被编辑后，哪些图还在用只能由正文决定，
        增量维护一定会漏。
        """
        wanted: dict[str, Asset] = {}
        for digest in dict.fromkeys(sha256_values):
            asset = self.get(digest)
            if asset is not None:
                wanted[digest] = asset

        existing_links = self.session.scalars(
            select(QuestionAsset).where(QuestionAsset.question_id == question_id)
        ).all()
        existing_by_asset = {link.asset_id: link for link in existing_links}
        wanted_ids = {asset.id for asset in wanted.values()}

        for asset_id, link in existing_by_asset.items():
            if asset_id not in wanted_ids:
                self.session.delete(link)
        for asset in wanted.values():
            if asset.id not in existing_by_asset:
                self.session.add(QuestionAsset(question_id=question_id, asset_id=asset.id))
        self.session.flush()
        self.recount(wanted_ids | set(existing_by_asset))

    def recount(self, asset_ids: Iterable[str]) -> None:
        """按关联表重算引用计数。

        不做 ``+1/-1`` 增量：任何一次漏算都会永久偏离，而孤儿清理是靠这个数
        决定删不删文件的——偏了就会删掉还在用的图，或者永远清不掉垃圾。
        """
        for asset_id in dict.fromkeys(asset_ids):
            asset = self.session.get(Asset, asset_id)
            if asset is None:
                continue
            asset.ref_count = int(
                self.session.scalar(
                    select(func.count(QuestionAsset.question_id)).where(
                        QuestionAsset.asset_id == asset_id
                    )
                )
                or 0
            )
        self.session.flush()

    def orphans(self) -> Sequence[Asset]:
        """没有任何题目引用的图片。"""
        return self.session.scalars(
            select(Asset).where(Asset.id.notin_(select(QuestionAsset.asset_id).distinct()))
        ).all()

    def purge_orphans(self) -> tuple[int, int]:
        """删除孤儿图片，返回 ``(删除数, 释放字节数)``。

        文件删不掉（权限、被占用）时**不**删数据库记录——否则下次就再也找不到
        这个文件了，只会留下一个永远清不掉的垃圾。
        """
        removed = 0
        freed = 0
        for asset in list(self.orphans()):
            path = self.paths.asset_path(asset.sha256, asset.suffix)
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                continue
            freed += asset.byte_size
            self.session.delete(asset)
            removed += 1
        self.session.flush()
        return removed, freed

    # --------------------------------------------- Markdown 里的图片引用改写
    @staticmethod
    def referenced_assets(markdown: str) -> list[str]:
        """抽出正文里引用的全部 ``asset://`` 哈希，去重并保持出现顺序。

        >>> AssetService.referenced_assets("![a](asset://aa) 和 ![b](asset://bb)")
        ['aa', 'bb']
        >>> AssetService.referenced_assets("![a](asset://aa) 又 ![a](asset://aa)")
        ['aa']
        >>> AssetService.referenced_assets("![外链](http://x/y.png)")
        []
        >>> AssetService.referenced_assets('<img src="asset://cc" style="width:1in" />')
        ['cc']
        """
        found: dict[str, None] = {}
        for pattern in (_IMAGE_REF, _HTML_IMAGE_SRC):
            for match in pattern.finditer(markdown):
                src = match.group("src")
                if src.startswith(ASSET_SCHEME):
                    found[src[len(ASSET_SCHEME) :]] = None
        return list(found)

    def absorb_local_images(
        self, markdown: str, base_dir: Path
    ) -> tuple[str, list[str], list[str]]:
        """把正文里指向本地文件的图片收进题库，并改写成 ``asset://``。

        导入链路必经的一步：pandoc 把 docx 里的内嵌图片抽成 ``media/image1.png``
        这样的相对路径，必须在写库前收进来——否则临时目录一删，图就全丢了。

        返回 ``(改写后的正文, 新引用的哈希列表, 错误信息列表)``。

        找不到或类型不支持的图片**不会**让整道题导入失败：正文里保留原始引用，
        错误进报告。丢一张图远好过丢一整道题，而且报告会告诉用户丢了哪张。
        """
        digests: list[str] = []
        errors: list[str] = []

        def resolve(src: str) -> str | None:
            """返回新的 ``asset://`` 地址；原样保留时返回 ``None``。"""
            if src.startswith(ASSET_SCHEME):
                digests.append(src[len(ASSET_SCHEME) :])
                return None
            if re.match(r"^[a-z][a-z0-9+.-]*:", src, re.IGNORECASE):
                return None  # 外链与 data: 原样保留
            # 绝对路径时 pathlib 会直接取绝对值，正好适配 pandoc 抽到临时目录的情况
            candidate = (base_dir / src).resolve()
            if not candidate.is_file():
                errors.append(f"找不到图片 {src}")
                return None
            try:
                asset, _ = self.store_file(candidate)
            except (UnsupportedAssetError, OSError) as exc:
                errors.append(f"图片 {src} 无法导入：{exc}")
                return None
            digests.append(asset.sha256)
            return f"{ASSET_SCHEME}{asset.sha256}"

        def replace_markdown(match: re.Match[str]) -> str:
            resolved = resolve(match.group("src"))
            return match.group(0) if resolved is None else f"![{match.group('alt')}]({resolved})"

        def replace_html(match: re.Match[str]) -> str:
            resolved = resolve(match.group("src"))
            if resolved is None:
                return match.group(0)
            return f"{match.group('head')}{resolved}{match.group('tail')}"

        rewritten = _IMAGE_REF.sub(replace_markdown, markdown)
        rewritten = _HTML_IMAGE_SRC.sub(replace_html, rewritten)
        return rewritten, list(dict.fromkeys(digests)), errors
