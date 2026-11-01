"""访问控制中间件：登录、只读身份、同源检查。

这组测试里最要紧的一个是 :func:`test_every_write_route_is_blocked_for_a_viewer`：
它**枚举应用里所有 POST 路由**，逐条确认只读身份拿到 403。界面上藏按钮只是不让
人白点（一条 CSS 规则，见 base.html），真正的拦截在中间件——这个「真正」必须是
可枚举、可断言的，否则「只读」就只是个说法。
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route

from qbank.app import AppContext
from qbank.web.app import create_app
from qbank.web.auth import OpenAccess, SharedPassword, TrustedHeader

EDITOR_PASSWORD = "write-me"
VIEWER_PASSWORD = "look-only"

#: TestClient 默认不发 Origin，而中间件只在 Origin 存在且不同源时拒绝。
#: 写请求的测试显式带上同源的 Origin，这样走到的是和浏览器一样的那条路径。
SAME_ORIGIN = {"Origin": "http://testserver", "Host": "testserver"}


@pytest.fixture
def password_app(web_context: AppContext) -> FastAPI:
    return create_app(web_context, SharedPassword(EDITOR_PASSWORD, VIEWER_PASSWORD))


@pytest.fixture
def anonymous(password_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(password_app, follow_redirects=False) as client:
        yield client


def _logged_in(app: FastAPI, password: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    response = client.post(
        "/login", data={"password": password, "next": "/questions"}, headers=SAME_ORIGIN
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def editor(password_app: FastAPI) -> Iterator[TestClient]:
    with _logged_in(password_app, EDITOR_PASSWORD) as client:
        yield client


@pytest.fixture
def viewer(password_app: FastAPI) -> Iterator[TestClient]:
    with _logged_in(password_app, VIEWER_PASSWORD) as client:
        yield client


# ============================================================ 未登录
def test_anonymous_read_is_redirected_to_login(anonymous: TestClient) -> None:
    response = anonymous.get("/questions?course_id=x")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fquestions"


def test_anonymous_write_gets_403_not_a_redirect(anonymous: TestClient) -> None:
    """写请求不跳登录页：303 之后浏览器会改用 GET 重发，刚填的表单就没了。"""
    response = anonymous.post("/courses", data={"name": "数据结构"}, headers=SAME_ORIGIN)
    assert response.status_code == 403
    assert "重新登录" in response.text


def test_login_page_is_reachable_without_touching_the_bank(anonymous: TestClient) -> None:
    """登录页不继承 base.html，所以不该查出任何课程名。"""
    response = anonymous.get("/login")
    assert response.status_code == 200
    assert "口令" in response.text
    assert "sidebar" not in response.text


def test_static_files_are_public(anonymous: TestClient) -> None:
    """登录页也要用样式表，静态资源必须在鉴权之前放行。"""
    assert anonymous.get("/static/app.css").status_code == 200


def test_wrong_password_says_nothing_useful(anonymous: TestClient) -> None:
    response = anonymous.post("/login", data={"password": "guess"}, headers=SAME_ORIGIN)
    assert response.status_code == 401
    assert "口令不对" in response.text
    # 不回填刚输的那一串
    assert "guess" not in response.text


def test_login_rejects_an_off_site_next(anonymous: TestClient) -> None:
    """``/login?next=https://钓鱼站`` 是个开放重定向，用户看到的是题库的域名。"""
    response = anonymous.post(
        "/login",
        data={"password": EDITOR_PASSWORD, "next": "https://evil.example/x"},
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# ============================================================ 已登录
def test_editor_can_read_and_write(editor: TestClient) -> None:
    assert editor.get("/questions").status_code == 200
    response = editor.post("/courses", data={"name": "数据结构"}, headers=SAME_ORIGIN)
    assert response.status_code == 303, response.text


def test_logout_ends_the_session(editor: TestClient) -> None:
    assert editor.post("/logout", headers=SAME_ORIGIN).status_code == 303
    assert editor.get("/questions").status_code == 303


def test_viewer_can_read(viewer: TestClient) -> None:
    response = viewer.get("/questions")
    assert response.status_code == 200
    assert "只读" in response.text


def test_viewer_page_hides_the_write_controls(viewer: TestClient) -> None:
    """藏按钮的那条 CSS 规则要真的渲染出来，否则页面上全是按不动的按钮。"""
    response = viewer.get("/tags")
    assert 'form[method="post"] { display: none !important; }' in response.text


def test_editor_page_has_no_such_rule(editor: TestClient) -> None:
    assert "display: none !important" not in editor.get("/tags").text


def _write_paths(app: FastAPI) -> list[str]:
    """应用里所有 POST 路由，路径参数填一个占位值。

    中间件在路由之前就拦下来了，所以占位值指向什么都不重要——重要的是这份清单
    是从路由表现算的：以后新加一条写路由，它自动进入这个测试。

    要递归：这个版本的 FastAPI 把 ``include_router`` 进来的路由包在一层
    ``_IncludedRouter`` 里（真正的路由表挂在它的 ``original_router`` 上），
    不摊平到 ``app.routes``。不递归的话一条都找不到——而「一条都没找到」会让这个
    测试恰好全绿，所以下面有条数下限兜着。
    """
    paths: list[str] = []

    def children(container: object) -> tuple[object, ...]:
        inner = getattr(container, "original_router", None)
        return tuple(getattr(inner if inner is not None else container, "routes", ()))

    def walk(container: object, prefix: str = "") -> None:
        for route in children(container):
            methods = getattr(route, "methods", None)
            if isinstance(route, Route) and methods and "POST" in methods:
                paths.append(prefix + re.sub(r"\{[^}]*\}", "x", route.path))
            else:
                walk(route, prefix + str(getattr(route, "prefix", "")))

    walk(app)
    assert len(paths) > 20, f"只找到 {len(paths)} 条写路由，枚举逻辑可能失效了"
    return paths


def test_every_write_route_is_blocked_for_a_viewer(
    viewer: TestClient, password_app: FastAPI
) -> None:
    allowed = {"/logout", "/login"}
    leaked = []
    for path in _write_paths(password_app):
        if path in allowed:
            continue
        response = viewer.post(path, headers=SAME_ORIGIN)
        if response.status_code != 403:
            leaked.append(f"{path} → {response.status_code}")
    assert not leaked, "以下写路由没有挡住只读身份：\n  " + "\n  ".join(leaked)


# ============================================================ 同源（CSRF）
def test_cross_site_write_is_rejected_even_when_logged_in(editor: TestClient) -> None:
    response = editor.post(
        "/courses",
        data={"name": "来自别处"},
        headers={"Origin": "https://evil.example", "Host": "testserver"},
    )
    assert response.status_code == 403
    assert "不是从本站页面发出的" in response.text


def test_cross_site_write_is_rejected_in_open_mode_too(web_context: AppContext) -> None:
    """``qbank serve`` 绑在 127.0.0.1 上也挡不住跨站表单提交——那里同样没有鉴权。"""
    with TestClient(create_app(web_context, OpenAccess()), follow_redirects=False) as client:
        response = client.post(
            "/courses", data={"name": "x"}, headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403


def test_cross_site_login_is_rejected(anonymous: TestClient) -> None:
    """登录本身也不该能被别的站点替用户发起。"""
    response = anonymous.post(
        "/login",
        data={"password": EDITOR_PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_cross_site_read_is_fine(editor: TestClient) -> None:
    """只拦写操作。GET 不改状态，拦它只会把正常的外链访问也拦掉。"""
    response = editor.get("/questions", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


# ============================================================ 代理身份头
def test_header_mode_without_the_header_is_anonymous(web_context: AppContext) -> None:
    policy = TrustedHeader("X-Forwarded-User", frozenset({"zhang"}))
    with TestClient(create_app(web_context, policy), follow_redirects=False) as client:
        assert client.get("/questions").status_code == 303

        named = client.get("/questions", headers={"X-Forwarded-User": "zhang"})
        assert named.status_code == 200
        assert "只读" not in named.text

        other = client.get("/questions", headers={"X-Forwarded-User": "wang"})
        assert other.status_code == 200
        assert "只读" in other.text

        denied = client.post(
            "/courses",
            data={"name": "x"},
            headers={"X-Forwarded-User": "wang", **SAME_ORIGIN},
        )
        assert denied.status_code == 403
        assert "只读" in denied.text


def test_header_mode_needs_no_session_cookie(web_context: AppContext) -> None:
    """代理模式不该装会话中间件——身份每个请求都从头里重新读。"""
    policy = TrustedHeader("X-Forwarded-User")
    with TestClient(create_app(web_context, policy)) as client:
        response = client.get("/questions", headers={"X-Forwarded-User": "zhang"})
        assert "set-cookie" not in response.headers
