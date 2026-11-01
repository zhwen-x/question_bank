"""认证策略的解析与两个安全判断。

这里测的都是「配错了会怎样」：一个以为自己有鉴权、实际敞开的部署，比一个起不来
的部署危险得多，所以每一种配错都必须抛异常而不是退回宽松默认。
"""

from __future__ import annotations

import pytest

from qbank.web import auth


def test_default_is_open_access() -> None:
    """没设环境变量就是不鉴权——本地客户端与桌面版的正确行为。"""
    policy = auth.policy_from_env()
    assert isinstance(policy, auth.OpenAccess)
    assert policy.needs_login is False


def test_password_mode_without_a_password_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """这是本模块最重要的一条：缺口令时**不许**退回 open。"""
    monkeypatch.setenv(auth.ENV_MODE, "password")
    with pytest.raises(auth.AuthError, match=auth.ENV_PASSWORD):
        auth.policy_from_env()


def test_readonly_password_may_not_equal_the_editor_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个口令相同的话，「只读」这档就是个幻觉——先匹配到的是可写那档。"""
    monkeypatch.setenv(auth.ENV_MODE, "password")
    monkeypatch.setenv(auth.ENV_PASSWORD, "same")
    monkeypatch.setenv(auth.ENV_READONLY_PASSWORD, "same")
    with pytest.raises(auth.AuthError):
        auth.policy_from_env()


def test_header_mode_without_a_header_name_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(auth.ENV_MODE, "header")
    with pytest.raises(auth.AuthError, match=auth.ENV_HEADER):
        auth.policy_from_env()


def test_unknown_mode_is_an_error_not_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """``QBANK_AUTH=passwrod`` 不该静悄悄地变成不鉴权。"""
    monkeypatch.setenv(auth.ENV_MODE, "passwrod")
    with pytest.raises(auth.AuthError, match="passwrod"):
        auth.policy_from_env()


def test_password_mode_distinguishes_editor_from_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.ENV_MODE, "password")
    monkeypatch.setenv(auth.ENV_PASSWORD, "write-me")
    monkeypatch.setenv(auth.ENV_READONLY_PASSWORD, "look-only")
    policy = auth.policy_from_env()

    editor = policy.authenticate("write-me")
    viewer = policy.authenticate("look-only")
    assert editor is not None and editor.can_edit
    assert viewer is not None and not viewer.can_edit
    assert policy.authenticate("nope") is None
    assert policy.authenticate("") is None


def test_session_secret_is_stable_across_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥由口令派生，所以多 worker 一致、重启也一致。

    随机生成的话，症状是「会话随请求落到哪个 worker 而随机失效」——而日志里
    什么都看不出来。
    """
    monkeypatch.setenv(auth.ENV_MODE, "password")
    monkeypatch.setenv(auth.ENV_PASSWORD, "write-me")
    first = auth.policy_from_env().session_secret()
    second = auth.policy_from_env().session_secret()
    assert first == second

    # 改口令必须让旧会话失效，这正是改口令时想要的效果
    monkeypatch.setenv(auth.ENV_PASSWORD, "write-me-2")
    assert auth.policy_from_env().session_secret() != first


def test_explicit_secret_key_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.ENV_MODE, "password")
    monkeypatch.setenv(auth.ENV_PASSWORD, "write-me")
    monkeypatch.setenv(auth.ENV_SECRET_KEY, "  chosen-by-hand  ")
    assert auth.policy_from_env().session_secret() == "chosen-by-hand"


def test_header_mode_editor_list_restricts_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.ENV_MODE, "header")
    monkeypatch.setenv(auth.ENV_HEADER, "X-Forwarded-User")
    monkeypatch.setenv(auth.ENV_EDITORS, "zhang，li")
    policy = auth.policy_from_env()
    assert isinstance(policy, auth.TrustedHeader)
    assert policy.editors == {"zhang", "li"}


def test_https_only_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 False 是刻意的，理由写在 :func:`qbank.web.auth.https_only` 的文档里。"""
    assert auth.https_only() is False
    monkeypatch.setenv(auth.ENV_HTTPS_ONLY, "1")
    assert auth.https_only() is True
    monkeypatch.setenv(auth.ENV_HTTPS_ONLY, "no")
    assert auth.https_only() is False


# ============================================================ 同源判断
class _FakeRequest:
    """只提供 ``headers``——:func:`is_same_origin` 用不到别的。"""

    def __init__(self, **headers: str) -> None:
        self.headers = {key.replace("_", "-").lower(): value for key, value in headers.items()}


def _same_origin(**headers: str) -> bool:
    return auth.is_same_origin(_FakeRequest(**headers))  # type: ignore[arg-type]


def test_no_origin_header_passes() -> None:
    """命令行工具、curl、测试客户端都不发 Origin，而那些都不是 CSRF。"""
    assert _same_origin(host="localhost:8765")


def test_matching_origin_passes() -> None:
    assert _same_origin(host="qbank.example.cn", origin="https://qbank.example.cn")


def test_foreign_origin_is_rejected() -> None:
    assert not _same_origin(host="qbank.example.cn", origin="https://evil.example")


def test_null_origin_is_rejected() -> None:
    """沙箱 iframe 与某些重定向后的提交会带 ``Origin: null``。"""
    assert not _same_origin(host="qbank.example.cn", origin="null")


def test_cross_site_fetch_metadata_is_rejected_even_without_origin() -> None:
    assert not _same_origin(host="qbank.example.cn", sec_fetch_site="cross-site")


def test_same_origin_fetch_metadata_passes() -> None:
    assert _same_origin(host="qbank.example.cn", sec_fetch_site="same-origin")


def test_port_must_match_too() -> None:
    """``localhost:8765`` 与 ``localhost:3000`` 不同源——本地开发时很容易撞上。"""
    assert not _same_origin(host="localhost:8765", origin="http://localhost:3000")
