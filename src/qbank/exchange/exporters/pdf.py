"""PDF：把自包含 HTML 交给无头 Chromium 打印。

为什么是 Chromium 而不是 WeasyPrint 一类
    试卷里有公式、合并单元格的表格、代码块。这些东西的排版规则浏览器实现得
    最全，而 PDF 要的正是「和浏览器里看到的一样」。公式走 MathML，Chromium
    原生支持，不需要 KaTeX 的 JS 与字体文件，也就不存在「打印时字体还没加载完」
    这种时好时坏的问题。WeasyPrint 不支持 MathML，公式会整片变成乱码。

为什么要在自带内核之外再找系统浏览器
    Playwright 自带的 Chromium 要单独下载约 130MB，而且**它不是所有平台都有**：
    在 macOS 12 上 ``playwright install chromium`` 直接报
    「does not support chromium on mac12」，一个字节也下不下来（本机实测）。
    而同一台机器上装着 Google Chrome。

    所以这里按「自带内核 → 系统 Chrome → 系统 Edge」依次尝试。多数人机器上
    本来就有 Chrome，于是既省掉那 130MB，也让下不动内核的平台能正常出 PDF。
    启动用的是全新的临时 profile，不碰用户自己的浏览器数据。

为什么探测要真的启动一次
    「可执行文件存在」不等于「能跑起来」——内核目录残缺、系统版本不支持、
    缺共享库，都是文件在而起不来。探测一次约半秒，结果缓存起来，换来的是
    ``qbank doctor`` 说「可用」时它就真的可用。

页码为什么不在 CSS 里
    Chromium 不支持 ``@page { @bottom-center { content: counter(page) } }``，
    写了也不会报错，只是永远不出现。页码必须走 ``page.pdf()`` 的
    ``footer_template``。页眉页脚一旦开启，页边距默认变成 0，必须显式给足，
    否则页眉会和正文重叠。
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # playwright 是可选依赖，只在类型检查时需要这个 TypedDict
    from playwright.sync_api import PdfMargins

#: 页脚留白。开了页眉页脚后 Chromium 的默认边距是 0，不给足会和正文重叠。
DEFAULT_MARGIN: PdfMargins = {
    "top": "18mm",
    "bottom": "18mm",
    "left": "16mm",
    "right": "16mm",
}

#: Chromium 的页眉页脚模板只认内联样式，且默认字号极小（约 8px），必须显式设置
_FOOTER = (
    '<div style="width:100%;font-size:9pt;color:#444;padding:0 16mm;'
    'font-family:sans-serif;display:flex;justify-content:space-between">'
    "<span>{left}</span>"
    '<span>第 <span class="pageNumber"></span> 页 / 共 <span class="totalPages"></span> 页</span>'
    "</div>"
)

_EMPTY = '<div style="display:none"></div>'


@dataclass(frozen=True, slots=True)
class Engine:
    """用哪个浏览器打印。``channel`` 为空表示 Playwright 自带的内核。"""

    channel: str | None
    label: str

    @property
    def launch_kwargs(self) -> dict[str, Any]:
        return {"channel": self.channel} if self.channel else {}


#: 依次尝试的打印引擎。自带内核优先——版本可控、行为最稳定
CANDIDATES: tuple[Engine, ...] = (
    Engine(None, "Playwright 自带的 Chromium"),
    Engine("chrome", "系统安装的 Google Chrome"),
    Engine("msedge", "系统安装的 Microsoft Edge"),
)


class PdfUnavailableError(RuntimeError):
    """没法生成 PDF。错误信息必须给出可操作的下一步。"""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"{detail}\n"
            "可以装 Playwright 自带内核：playwright install chromium（约 130MB），"
            "或者装一个 Google Chrome —— 两者有其一即可。\n"
            "在此之前可以先导出 HTML，用浏览器的「打印为 PDF」得到同样的版面。"
        )


@dataclass(frozen=True, slots=True)
class Capability:
    available: bool
    detail: str = ""
    engine: Engine | None = None


_cached: Capability | None = None


def capability(*, refresh: bool = False) -> Capability:
    """探测能否出 PDF，并记下用的是哪个引擎。

    真的启动一次浏览器再关掉——文件在不等于能跑起来。结果缓存，重复调用不花钱。
    """
    global _cached
    if _cached is not None and not refresh:
        return _cached
    _cached = _probe()
    return _cached


def _probe() -> Capability:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return Capability(False, "未安装 playwright")

    reasons: list[str] = []
    try:
        with sync_playwright() as pw:
            for engine in CANDIDATES:
                try:
                    browser = pw.chromium.launch(**engine.launch_kwargs)
                except Exception as exc:  # 每个候选失败的原因都不一样，逐个记下
                    reasons.append(f"{engine.label}：{_short(exc)}")
                    continue
                browser.close()
                return Capability(True, engine.label, engine)
    except Exception as exc:  # playwright 本身起不来
        return Capability(False, f"playwright 不可用：{_short(exc)}")
    return Capability(False, "没有可用的浏览器内核。" + "；".join(reasons))


def _short(exc: Exception) -> str:
    """异常信息只取第一行。Playwright 的报错动辄几十行，塞进界面没法看。"""
    first = str(exc).strip().splitlines()
    return first[0] if first else exc.__class__.__name__


def is_available() -> bool:
    return capability().available


@dataclass(frozen=True, slots=True)
class PdfOptions:
    """打印参数。页面尺寸交给 HTML 里的 ``@page``，这里只管页脚与边距。"""

    footer_left: str = ""
    page_numbers: bool = True
    margin: PdfMargins | None = None
    #: 打印背景色与底纹。试卷的密封线、表头底色都靠它
    print_background: bool = True


def html_to_pdf(html: str, output: Path, *, options: PdfOptions | None = None) -> Path:
    """渲染一段 HTML 为 PDF。

    HTML 必须是**自包含**的：图片内联、样式内联。这里用 ``set_content`` 而不是
    加载文件，页面没有 base URL，任何外部引用都取不到——而取不到时 Chromium
    不会报错，只会打印出一个缺图少样式的版面。
    """
    found = capability()
    if not found.available or found.engine is None:
        raise PdfUnavailableError(found.detail)
    from playwright.sync_api import sync_playwright

    opts = options or PdfOptions()
    output.parent.mkdir(parents=True, exist_ok=True)
    footer = _FOOTER.format(left=escape(opts.footer_left)) if opts.page_numbers else _EMPTY
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**found.engine.launch_kwargs)
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.pdf(
                path=str(output),
                print_background=opts.print_background,
                # 让 HTML 里的 @page size 说了算，模板才能自己决定 A4 还是 B5
                prefer_css_page_size=True,
                display_header_footer=opts.page_numbers,
                header_template=_EMPTY,
                footer_template=footer,
                margin=opts.margin or DEFAULT_MARGIN,
            )
        finally:
            browser.close()
    return output
