"""FastAPI 应用。

本地客户端与将来的 Web 端是**同一套代码**，区别只在部署方式：
桌面模式用 pywebview 套一个窗口，Web 端直接跑 uvicorn。所以这里不做任何
「只有本机才成立」的假设，鉴权之类的扩展点留在中间件层。

页面是服务端渲染的 Jinja2 模板，没有前端框架。公式由服务端转成 MathML，
浏览器原生渲染——不需要 KaTeX 的 JS 与字体文件，整个界面离线可用。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from qbank.app import AppContext, open_bank
from qbank.web.templating import render

STATIC_DIR = Path(__file__).parent / "static"


def create_app(context: AppContext | None = None) -> FastAPI:
    """创建应用。

    ``context`` 留出注入点，测试可以传一个内存库，不碰用户真实题库。
    """
    application = FastAPI(title="qbank", docs_url=None, redoc_url=None)
    application.state.bank = context or open_bank()
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from qbank.web.routers import (
        assets,
        chapters,
        courses,
        health,
        imports,
        questions,
        tags,
    )

    for router in (
        courses.router,
        chapters.router,
        questions.router,
        tags.router,
        assets.router,
        health.router,
        imports.router,
    ):
        application.include_router(router)

    @application.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/questions", status_code=302)

    @application.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        """错误页也套同一套布局——弹一个纯文本 404 很让人不知所措。"""
        from qbank.web.nav import Nav

        return render(
            request,
            "error.html",
            {
                "nav": Nav(),
                "page_title": f"{exc.status_code}",
                "status_code": exc.status_code,
                "detail": exc.detail or "",
            },
            status_code=exc.status_code,
        )

    return application


def serve(host: str = "127.0.0.1", port: int = 8765, *, reload: bool = False) -> None:
    """启动开发/本地服务器。

    默认只绑 ``127.0.0.1``：这是本地客户端，不该一启动就暴露在局域网里。
    真要给同事访问，显式传 ``--host 0.0.0.0``。
    """
    import uvicorn

    if reload:
        uvicorn.run("qbank.web.app:create_app", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(), host=host, port=port)


#: 供 ``uvicorn qbank.web.app:app`` 直接使用
def app() -> FastAPI:  # pragma: no cover - 仅作为 ASGI 工厂入口
    return create_app()


__all__: Sequence[str] = ("app", "create_app", "serve")
