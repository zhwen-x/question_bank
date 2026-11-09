"""GET 表单的查询参数类型。

**HTML 表单对「没选」的字段提交的是空串，不是不提交。**

筛选条里「难度」的「全部」就是 ``<option value="">``，所以点一下「筛选」，
浏览器发出去的一定带着 ``difficulty=``。而 FastAPI 把 ``int`` 的空串判为校验
失败，于是默认状态下点筛选直接得到一个 422，页面上甩出一坨原始 JSON：

.. code-block:: text

    {"detail":[{"type":"int_parsing","loc":["query","difficulty"], ...}]}

这不是某一个参数写错了，而是「表单驱动的 GET 路由 + 非字符串参数」这个组合
本身的坑：``str`` 参数收到空串没事，``int`` / ``bool`` 必炸。所以这里给出一个
统一的写法，而不是逐个参数打补丁——否则下一个加上去的参数还会再犯一次。

``tests/integration/test_web_query.py`` 枚举了全部 GET 路由的非字符串参数，
逐个断言「传空串」等同于「不传」。新加的参数自动进入那个测试。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator


def blank_as(default: Any) -> BeforeValidator:
    """把空串当作「这个参数没传」，回落到它自己的默认值。

    只认空串。``None`` 本来就表示没传，别的值一律原样交给正常的类型校验——
    ``page=abc`` 仍然该是一个校验错误，这里不负责把它糊过去。
    """
    return BeforeValidator(lambda value: default if value == "" else value)


#: 可留空的难度（1–5）。「全部」时为 ``None``
OptionalLevel = Annotated[int | None, blank_as(None)]

#: 页码。空串回到第 1 页
PageNumber = Annotated[int, blank_as(1)]

#: 默认关闭的开关
OffFlag = Annotated[bool, blank_as(False)]

#: 默认打开的开关
OnFlag = Annotated[bool, blank_as(True)]
