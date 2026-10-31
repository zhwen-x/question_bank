"""桌面外壳：用 pywebview 给本地服务套一个原生窗口。

**它只是个窗口。** 里面跑的是和 ``qbank serve`` 完全一样的那个 FastAPI 应用，
一行业务代码都不一样——这正是 M0 定下的分层要换来的东西：桌面版和将来的 Web 端
是同一套代码，区别只在谁来开窗。

为什么服务跑在后台线程而不是子进程
    子进程要处理「窗口关了但进程还在」「端口被占」「怎么把错误传回来」一堆事。
    同一个进程里起一个 daemon 线程，窗口一关进程就退，线程跟着没，不留垃圾。
    代价是不能热重载，而桌面版本来也不需要。

为什么端口要现找一个空的
    8765 很可能已经被另一个 ``qbank serve`` 占着。硬用固定端口的后果是窗口
    打开后显示的是**另一个实例**的页面——用户看到的是自己的题库，只是改了之后
    另一个窗口不刷新，诡异到无法排查。所以每次现问系统要一个空端口。
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass

from qbank.app import AppContext, open_bank
from qbank.config import is_frozen

#: 窗口初始尺寸。比常见笔记本的可用高度略小，免得一打开就顶到 Dock
DEFAULT_SIZE = (1280, 860)

#: 最小尺寸。再窄的话侧栏加正文就挤不下了
MIN_SIZE = (960, 600)

#: 等服务起来的最长时间（秒）。超了就是真有问题，不该无限等
STARTUP_TIMEOUT = 20.0


class DesktopUnavailableError(RuntimeError):
    """装不了或起不来窗口。错误信息必须给出退路。"""

    def __init__(self, detail: str) -> None:
        # 打包版里装不了东西，给 pip 命令等于指一条走不通的路
        install = "" if is_frozen() else '安装桌面外壳：pip install "qbank[desktop,web]"。\n'
        super().__init__(
            f"{detail}\n{install}也可以用 `qbank serve`，功能完全一样，只是跑在浏览器里。"
        )


@dataclass(frozen=True, slots=True)
class Capability:
    available: bool
    detail: str = ""


def capability() -> Capability:
    """探测能不能开原生窗口。不抛异常，供 doctor 与 CLI 做降级判断。"""
    try:
        import webview  # noqa: F401 - 只为确认装没装
    except ImportError:
        return Capability(False, "未安装 pywebview")
    try:
        # pywebview 直到真的开窗才去找 GUI 后端，所以这里主动问一次
        from webview.guilib import initialize

        initialize()
    except Exception as exc:  # 缺 GTK/Qt/WebKit 时抛的异常类型不止一种
        return Capability(False, f"找不到可用的窗口后端：{_first_line(exc)}")
    # pywebview 没有 __version__ 属性，得问包元数据
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("pywebview")
    except PackageNotFoundError:  # pragma: no cover - 源码树里直接跑才会遇到
        installed = "?"
    return Capability(True, f"pywebview {installed}")


def _first_line(exc: Exception) -> str:
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else exc.__class__.__name__


def is_available() -> bool:
    return capability().available


def free_port() -> int:
    """问系统要一个当前空闲的端口。

    绑 0 号端口让内核分配，拿到号再关掉。严格说存在竞态（关掉到重新绑之间可能
    被别人抢走），但本地单机场景下可以忽略，而它换来的是「绝不会连到另一个
    实例」这个更重要的性质。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(context: AppContext, port: int) -> None:
    import uvicorn

    from qbank.web.app import create_app

    uvicorn.run(
        create_app(context),
        host="127.0.0.1",
        port=port,
        log_level="warning",  # 窗口模式下控制台刷访问日志没有意义
    )


def _wait_until_up(port: int, timeout: float = STARTUP_TIMEOUT) -> bool:
    """等到端口真的能连上。

    不等的话窗口会先于服务打开，用户看到的是一个「无法连接」的错误页，
    然后即使服务起来了也不会自动刷新。
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def run(context: AppContext | None = None, *, title: str = "qbank 题库") -> None:
    """起服务、开窗口，阻塞到窗口关闭。

    ``context`` 留出注入点，测试可以传一个内存库。
    """
    found = capability()
    if not found.available:
        raise DesktopUnavailableError(found.detail)
    import webview

    bank = context or open_bank()
    port = free_port()
    server = threading.Thread(target=_serve, args=(bank, port), daemon=True)
    server.start()

    if not _wait_until_up(port):
        raise DesktopUnavailableError(f"本地服务在 {STARTUP_TIMEOUT:.0f} 秒内没能起来")

    width, height = DEFAULT_SIZE
    min_width, min_height = MIN_SIZE
    webview.create_window(
        title,
        f"http://127.0.0.1:{port}/",
        width=width,
        height=height,
        min_size=(min_width, min_height),
        text_select=True,  # 题干要能选中复制，默认是禁止的
    )
    try:
        webview.start()
    finally:
        bank.close()
