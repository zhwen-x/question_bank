"""登录与登出。

只有 ``QBANK_AUTH=password`` 用得上这两个页面。其余模式下 ``/login`` 直接跳回
首页——留一个「登录页说自己不需要登录」的死胡同毫无意义。
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from qbank.web.auth import SESSION_KEY, AuthPolicy, safe_next
from qbank.web.templating import render

router = APIRouter()


def _policy(request: Request) -> AuthPolicy:
    policy = getattr(request.app.state, "auth", None)
    if not isinstance(policy, AuthPolicy):  # pragma: no cover - 误用 app 工厂才会发生
        raise RuntimeError("应用未初始化：请用 qbank.web.app.create_app 创建实例")
    return policy


#: 线上的参数名是 ``next``（惯例），Python 这边不能叫这个——会遮蔽内置函数。
_NEXT = Query("/", alias="next")
_NEXT_FORM = Form("/", alias="next")


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request, next_url: str = _NEXT) -> Response:
    policy = _policy(request)
    if not policy.needs_login:
        return RedirectResponse(safe_next(next_url), status_code=303)
    # 提示文案不从查询串取：那等于让任何人都能给登录页配一句自己写的话
    return render(request, "login.html", {"page_title": "登录", "next": safe_next(next_url)})


@router.post("/login", response_model=None)
def login(
    request: Request,
    password: str = Form(""),
    next_url: str = _NEXT_FORM,
) -> Response:
    policy = _policy(request)
    target = safe_next(next_url)
    if not policy.needs_login:
        return RedirectResponse(target, status_code=303)

    principal = policy.authenticate(password)
    if principal is None:
        # 不说「口令错误」还是「口令为空」，也不回填刚输的那一串：
        # 两者都只方便试口令的人
        return render(
            request,
            "login.html",
            {"page_title": "登录", "next": target, "error": "口令不对"},
            status_code=401,
        )
    request.session[SESSION_KEY] = "editor" if principal.can_edit else "viewer"
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    # 只有需要登录的模式才装了会话中间件；别的模式下 request.session 会直接抛
    if _policy(request).needs_login:
        request.session.pop(SESSION_KEY, None)
    return RedirectResponse("/login", status_code=303)
