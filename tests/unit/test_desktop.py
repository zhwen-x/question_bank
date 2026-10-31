"""桌面外壳测试。

开窗口本身没法在无头环境里测，所以这里测的是**开窗口之前**那些会出错的地方：
端口怎么选、起不来时说什么、以及打包版里不要给出走不通的建议。
"""

from __future__ import annotations

import socket

import pytest

from qbank import desktop


def test_free_port_is_actually_free() -> None:
    """不能用固定端口：8765 很可能已经被另一个 `qbank serve` 占着，
    那样窗口打开后显示的是**另一个实例**的页面，诡异到无法排查。"""
    port = desktop.free_port()
    assert 1024 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))  # 还能绑上，说明确实是空的


def test_free_port_varies() -> None:
    assert len({desktop.free_port() for _ in range(5)}) > 1


def test_capability_shape() -> None:
    found = desktop.capability()
    assert isinstance(found.available, bool)
    assert found.detail or found.available


def test_error_points_at_the_browser_fallback() -> None:
    """桌面外壳起不来不该是死路——`qbank serve` 的界面完全一样。"""
    message = str(desktop.DesktopUnavailableError("找不到窗口后端"))
    assert "找不到窗口后端" in message
    assert "qbank serve" in message


def test_error_does_not_suggest_pip_when_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    """打包版里 `pip install` 是走不通的——包是只读的。"""
    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    message = str(desktop.DesktopUnavailableError("缺后端"))
    assert "pip install" not in message
    assert "qbank serve" in message


def test_wait_until_up_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务起不来时要在限定时间内放弃，而不是无限等下去。"""
    assert desktop._wait_until_up(desktop.free_port(), timeout=0.3) is False
