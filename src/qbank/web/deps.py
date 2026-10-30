"""Web 层的依赖注入。

应用上下文（引擎、会话工厂）在启动时建一次，之后每个请求从里面取会话。
``AppContext`` 持有连接池，按请求新建会在高频操作下把 SQLite 打开关闭几百次。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from qbank.app import AppContext
from qbank.core.repo.base import ConflictError, NotFoundError


def get_context(request: Request) -> AppContext:
    context = getattr(request.app.state, "bank", None)
    if not isinstance(context, AppContext):  # pragma: no cover - 误用 app 工厂才会发生
        raise RuntimeError("应用未初始化：请用 qbank.web.app.create_app 创建实例")
    return context


def get_session(context: AppContext = Depends(get_context)) -> Iterator[Session]:
    """每个请求一个事务。

    请求处理函数里不要自己 commit——正常返回即提交，抛异常即回滚，
    这样「页面渲染一半出错」不会留下半写入的数据。
    """
    with context.session() as session:
        yield session


def as_http_error(exc: Exception) -> HTTPException:
    """把领域异常翻译成 HTTP 状态码，并保留给用户看的中文信息。"""
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc
