"""GET 表单的查询参数：空串必须等同于「没传」。

这组测试来自一个真实 bug：在题目列表页什么都不改、直接点「筛选」，页面上甩出
一坨原始 JSON——

.. code-block:: text

    {"detail":[{"type":"int_parsing","loc":["query","difficulty"], ...}]}

因为「难度」下拉的「全部」是 ``<option value="">``，表单必然提交 ``difficulty=``，
而 ``int`` 参数不接受空串。**默认状态下点一次筛选就必炸**，而已有的集成测试全是
``?type=single_choice`` 这种带值的调用，从没走过浏览器真实提交的那个形状。

所以这里不逐个参数写断言，而是**枚举全部 GET 路由的非字符串参数**，逐个确认
「传空串」和「不传」结果一致。以后新加一个 int/bool 查询参数，它自动进入这个
测试——否则同一个坑会再踩一次。
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.routing import Route

from qbank.app import AppContext
from qbank.core.repo.course import CourseRepo
from qbank.web.app import create_app

#: 会被表单提交成空串、而 FastAPI 又不接受空串的那些类型
_FRAGILE = ("int", "bool", "float")


def _routes(app: FastAPI) -> Iterator[Route]:
    """递归走出所有路由。

    这个版本的 FastAPI 把 ``include_router`` 进来的路由包在 ``_IncludedRouter``
    里（真正的路由表挂在 ``original_router`` 上），不摊平到 ``app.routes``。
    """

    def children(container: object) -> tuple[object, ...]:
        inner = getattr(container, "original_router", None)
        return tuple(getattr(inner if inner is not None else container, "routes", ()))

    def walk(container: object) -> Iterator[Route]:
        for route in children(container):
            if isinstance(route, Route) and getattr(route, "methods", None):
                yield route
            else:
                yield from walk(route)

    yield from walk(app)


def _fragile_params(app: FastAPI) -> list[tuple[str, str]]:
    """``(请求路径, 参数名)``，覆盖所有非字符串的 GET 查询参数。"""
    found: list[tuple[str, str]] = []
    for route in _routes(app):
        if "GET" not in (route.methods or set()):
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for param in dependant.query_params:
            annotation = str(param.field_info.annotation)
            if any(kind in annotation for kind in _FRAGILE):
                # 路径参数填个占位值：这里关心的是参数校验，不是能不能查到东西
                found.append((re.sub(r"\{[^}]*\}", "missing", route.path), param.name))
    return found


@pytest.fixture
def app(web_context: AppContext) -> FastAPI:
    return create_app(web_context)


@pytest.fixture
def course_id(session: Session) -> str:
    return CourseRepo(session).create("数据结构", code="CS201").id


def test_blank_values_behave_like_omitting_the_parameter(app: FastAPI, client: TestClient) -> None:
    """逐个参数比对「传空串」与「不传」。

    不要求一定是 200——路径参数填的是占位值，404 很正常。要求的是**两者一致**，
    而且绝不是 422：422 意味着用户只是点了一下筛选，就撞上一堵参数校验的墙。
    """
    params = _fragile_params(app)
    assert len(params) >= 5, f"只找到 {params}，抽取逻辑可能失效了"

    broken: list[str] = []
    for path, name in params:
        baseline = client.get(path)
        blank = client.get(path, params={name: ""})
        if blank.status_code != baseline.status_code:
            broken.append(
                f"{path}?{name}= → {blank.status_code}（不传时是 {baseline.status_code}）"
            )
    assert not broken, "这些参数传空串时和不传不一样：\n  " + "\n  ".join(broken)


def test_clicking_filter_with_everything_unset_works(client: TestClient, course_id: str) -> None:
    """浏览器真实提交的那个形状——这就是用户报的那一下。

    筛选条里三个下拉的「全部」都是 ``value=""``，加上隐藏的 ``q``，点一下
    「筛选」发出去的就是这四个空串。
    """
    response = client.get(
        "/questions",
        params={"course_id": course_id, "q": "", "type": "", "difficulty": "", "status": ""},
    )
    assert response.status_code == 200, response.text
    assert "int_parsing" not in response.text


def test_a_real_difficulty_still_filters(client: TestClient, course_id: str) -> None:
    """修空串不能把筛选本身改坏。"""
    response = client.get("/questions", params={"course_id": course_id, "difficulty": "4"})
    assert response.status_code == 200


# ------------------------------------------------- 值不对时给人看的东西
def test_out_of_range_difficulty_is_a_400_not_a_500(client: TestClient, course_id: str) -> None:
    """改版前存下的书签不该换来一页 Internal Server Error。

    ``Difficulty(99)`` 直接抛 ValueError，不接住就是 500。
    """
    response = client.get("/questions", params={"course_id": course_id, "difficulty": "99"})
    assert response.status_code == 400
    assert "难度" in response.text and "99" in response.text


@pytest.mark.parametrize(
    ("name", "value", "label"),
    [("type", "不存在", "题型"), ("status", "不存在", "状态")],
)
def test_other_unknown_filter_values_are_also_400(
    client: TestClient, course_id: str, name: str, value: str, label: str
) -> None:
    response = client.get("/questions", params={"course_id": course_id, name: value})
    assert response.status_code == 400
    assert label in response.text


def test_validation_errors_render_the_app_error_page(client: TestClient, course_id: str) -> None:
    """真的没法解析时（``page=abc``）也要落到应用自己的错误页。

    FastAPI 默认吐原始 JSON，对界面用户毫无意义——既没说哪里错，也没给回去的路。
    """
    response = client.get("/questions", params={"course_id": course_id, "page": "abc"})
    assert response.status_code == 400
    assert "int_parsing" not in response.text, "还是那坨原始 JSON"
    assert "page" in response.text, "没说清是哪个参数"
    assert "回到列表页" in response.text, "没给用户下一步"
