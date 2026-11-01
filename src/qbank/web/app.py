"""FastAPI 应用。

本地客户端与 Web 端是**同一套代码**，区别只在部署方式：桌面模式用 pywebview
套一个窗口，Web 端直接跑 uvicorn（见 ``docs/Web 部署指南.md``）。所以这里不做
任何「只有本机才成立」的假设——鉴权与同源检查都在中间件层，由
:mod:`qbank.web.auth` 按环境变量决定用哪一种，路由与模板一行都不用改。

页面是服务端渲染的 Jinja2 模板，没有前端框架。公式由服务端转成 MathML，
浏览器原生渲染——不需要 KaTeX 的 JS 与字体文件，整个界面离线可用。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from qbank.app import AppContext, open_bank
from qbank.web.auth import (
    PUBLIC_PREFIXES,
    SAFE_METHODS,
    AuthPolicy,
    https_only,
    is_same_origin,
    policy_from_env,
    safe_next,
)
from qbank.web.templating import render

STATIC_DIR = Path(__file__).parent / "static"

#: 静态资源在鉴权之前就放行。目录里没有任何题库数据，而登录页本身要用它的样式表
STATIC_PREFIX = "/static/"


def create_app(context: AppContext | None = None, policy: AuthPolicy | None = None) -> FastAPI:
    """创建应用。

    ``context`` 留出注入点，测试可以传一个内存库，不碰用户真实题库。
    ``policy`` 同理——不传就按环境变量解析（见 :func:`qbank.web.auth.policy_from_env`）。
    """
    application = FastAPI(title="qbank", docs_url=None, redoc_url=None)
    application.state.bank = context or open_bank()
    application.state.auth = policy or policy_from_env()
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from qbank.web.routers import (
        assets,
        auth,
        chapters,
        courses,
        health,
        imports,
        papers,
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
        papers.router,
        auth.router,
    ):
        application.include_router(router)

    _install_access_control(application)

    @application.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/questions", status_code=302)

    @application.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        """错误页也套同一套布局——弹一个纯文本 404 很让人不知所措。"""
        return error_page(request, exc.status_code, exc.detail or "")

    return application


def error_page(request: Request, status_code: int, detail: str) -> HTMLResponse:
    """统一的错误页。

    ``Nav()`` 给的是一个空侧栏：这个函数也服务于「未登录」与「只读身份被拒」，
    那两种情况下不该为了画侧栏去查一遍题库。
    """
    from qbank.web.nav import Nav

    return render(
        request,
        "error.html",
        {
            "nav": Nav(),
            "page_title": f"{status_code}",
            "status_code": status_code,
            "detail": detail,
        },
        status_code=status_code,
    )


def _install_access_control(application: FastAPI) -> None:
    """装上会话与访问控制中间件。

    **顺序很要紧**：Starlette 里**后加的中间件在外层**，而访问控制要读
    ``request.session``，所以会话中间件必须后加。弄反了的症状是一登录就报
    ``AssertionError: SessionMiddleware must be installed``。
    """
    policy: AuthPolicy = application.state.auth

    @application.middleware("http")
    async def enforce_access(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path.startswith(STATIC_PREFIX):
            return await call_next(request)

        # 同源检查对所有写请求生效，公开路径也不例外：登录与登出本身也不该能被
        # 别的站点替用户发起
        if request.method not in SAFE_METHODS and not is_same_origin(request):
            return error_page(
                request, 403, "这个写操作不是从本站页面发出的，已拒绝。请回到页面上重新操作。"
            )

        principal = policy.identify(request)
        request.state.principal = principal

        if path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if principal is None:
            if request.method in SAFE_METHODS:
                return RedirectResponse(
                    "/login?" + urlencode({"next": safe_next(request.url.path)}), status_code=303
                )
            # 写请求不跳登录页：302 之后浏览器会改用 GET 重发，
            # 用户刚填的那一屏表单就没了，而且看不出发生了什么
            return error_page(request, 403, "登录状态已失效，请重新登录后再提交。")

        if request.method not in SAFE_METHODS and not principal.can_edit:
            return error_page(request, 403, "当前身份是只读的，不能修改题库。")

        return await call_next(request)

    if policy.needs_login:
        application.add_middleware(
            SessionMiddleware,
            secret_key=policy.session_secret(),
            # lax 而不是 strict：strict 下「从登录页跳回原页面」这一步浏览器不带
            # cookie，表现是登录成功却又被弹回登录页。lax 同时也挡住了跨站写请求
            same_site="lax",
            https_only=https_only(),
            # 一个工作日。再长没有意义，再短会在批量录题中途把人踢下线
            max_age=12 * 3600,
        )


def serve(host: str = "127.0.0.1", port: int = 8765, *, reload: bool = False) -> None:
    """启动开发/本地服务器。

    默认只绑 ``127.0.0.1``：这是本地客户端，不该一启动就暴露在局域网里。
    真要给同事访问，显式传 ``--host 0.0.0.0``，并且设好 ``QBANK_AUTH``。
    """
    import uvicorn

    if reload:
        uvicorn.run("qbank.web.app:create_app", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(), host=host, port=port)


#: 供 ``uvicorn qbank.web.app:app --factory`` 直接使用
def app() -> FastAPI:  # pragma: no cover - 仅作为 ASGI 工厂入口
    return create_app()


__all__: Sequence[str] = ("app", "create_app", "serve")
