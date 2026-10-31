"""路由层的共用小工具。"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

from fastapi.responses import RedirectResponse


def flashes(msg: str, level: str = "ok") -> list[dict[str, str]]:
    """把 URL 上的提示参数变成页面提示。

    刻意不走 session：本地客户端没有会话中间件，而「操作完跳转并带一条提示」
    用查询参数就够了。代价是提示会留在地址栏里，对工具型应用可以接受。
    """
    if not msg:
        return []
    safe_level = level if level in {"ok", "error", "warn"} else "ok"
    return [{"text": msg, "level": safe_level}]


def redirect_with(path: str, msg: str = "", level: str = "ok", **params: str) -> RedirectResponse:
    """跳转并带上提示。

    用 303 而不是 302：POST 之后必须让浏览器改用 GET 重新请求，
    否则用户刷新页面会把表单再提交一次（重复建题、重复导入）。

    ``path`` 自带查询串时**合并**而不是再接一个 ``?``。挑题时「加完回到刚才那页
    列表」传进来的就是一个带 ``?course_id=…&add_to=…`` 的地址；早先直接拼
    ``?msg=…`` 会产出 ``…&add_to=X?msg=Y``——提示丢了，而且 ``add_to`` 的值被
    污染成了一个不存在的 id，于是「挑题模式」在加完第一批题后悄悄退出。
    """
    head, _, existing = path.partition("?")
    query = parse_qsl(existing, keep_blank_values=False)
    query.extend((key, value) for key, value in params.items() if value)
    if msg:
        query.append(("msg", msg))
        if level != "ok":
            query.append(("level", level))
    target = f"{head}?{urlencode(query)}" if query else head
    return RedirectResponse(target, status_code=303)
