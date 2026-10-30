"""模板渲染。

**为什么单独一个模块而不是放在 app.py 里**：``app.py`` 是组装根，它要 import
全部 router；而每个 router 都需要 ``render``。放在一起就形成 import 环——
运行时靠延迟导入能绕过去，但 mypy 会因此看不到 router 模块的属性，
而且组装根被它所组装的东西反向依赖，本身就是不该有的方向。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from qbank import __version__

TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True


def render(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """渲染模板，统一注入每页都要的东西。"""
    payload: dict[str, Any] = {
        "request": request,
        "version": __version__,
        "flashes": context.pop("flashes", []),
        "active": context.pop("active", ""),
    }
    payload.update(context)
    return templates.TemplateResponse(request, name, payload, status_code=status_code)
