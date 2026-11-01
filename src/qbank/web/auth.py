"""认证接入点。

**这里提供的不是一套用户系统**，而是一个口子：题库本身没有「用户」这个概念，
题目不属于谁，也没有按人分权的需求。真正需要的是「谁能打开这个地址、谁只能看」，
而这件事在不同部署里由完全不同的东西决定——一个教研室用一个共享口令就够了，
一所学校则已经有统一身份认证。所以这里把它做成三种可选策略，由环境变量选，
业务代码一行都不碰。

====================  ==========================================================
``QBANK_AUTH``        含义
====================  ==========================================================
``open``（默认）      不鉴权。本地客户端与桌面版就该是这样
``password``          一个共享口令；可另设一个只读口令
``header``            信任反向代理注入的身份头（接入已有的 SSO）
====================  ==========================================================

**三条刻意的「宁可起不来」**

1. ``password`` 模式没给口令时**拒绝启动**，不退回 ``open``。一个以为自己有鉴权、
   实际敞开的部署，比一个起不来的部署危险得多。
2. ``header`` 模式同样要求显式给出头名。
3. 未知的 ``QBANK_AUTH`` 值直接报错，不猜。写错一个字母就裸奔，代价不对等。

**``header`` 模式的前提必须说清楚**：HTTP 头是客户端可以随便写的。这个模式只有
在反向代理**覆盖**（而不是追加）该头时才成立；代理配错、或者有人绕过代理直连
应用端口，任何人都能自称任何身份。部署细节见 ``docs/Web部署指南.md``。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.requests import Request

#: 选择策略的环境变量
ENV_MODE = "QBANK_AUTH"
ENV_PASSWORD = "QBANK_AUTH_PASSWORD"
ENV_READONLY_PASSWORD = "QBANK_AUTH_READONLY_PASSWORD"
ENV_HEADER = "QBANK_AUTH_HEADER"
ENV_EDITORS = "QBANK_AUTH_EDITORS"
ENV_SECRET_KEY = "QBANK_SECRET_KEY"
ENV_HTTPS_ONLY = "QBANK_HTTPS_ONLY"

#: 会话里存身份的键
SESSION_KEY = "principal"

#: 不需要身份就能访问的路径前缀。
#:
#: 登录页自己当然要放行；静态资源放行是因为登录页也要用样式表，否则会看到一张
#: 没有样式的表单。静态目录里没有任何题库数据。
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/", "/login", "/logout")

#: 不改变任何状态的方法。其余方法都要求可写身份与同源。
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AuthError(RuntimeError):
    """认证配置有问题，应用不该起来。"""


@dataclass(frozen=True, slots=True)
class Principal:
    """当前是谁在操作。

    只有两个维度：叫什么（显示用）、能不能写。没有角色表，因为题库里没有
    「属于某人的数据」——再细的权限模型都会是无主的抽象。
    """

    name: str
    can_edit: bool

    @property
    def role_label(self) -> str:
        return "可编辑" if self.can_edit else "只读"


#: 单机模式下的身份：就是坐在这台机器前的人
OWNER = Principal(name="本机", can_edit=True)


class AuthPolicy:
    """一种认证策略。

    默认实现就是 :class:`OpenAccess` 的行为，子类按需覆盖。
    """

    #: 给 ``qbank doctor`` 与状态页显示
    label = "不鉴权"
    #: 要不要挂登录页
    needs_login = False

    def identify(self, request: Request) -> Principal | None:
        """这个请求是谁。``None`` 表示未登录。"""
        return OWNER

    def authenticate(self, password: str) -> Principal | None:
        """口令换身份。不走口令的策略永远返回 ``None``。"""
        return None

    def session_secret(self) -> str:
        """签 cookie 用的密钥。只有 ``needs_login`` 的策略会被问到。"""
        raise AuthError(f"{type(self).__name__} 不需要会话")


class OpenAccess(AuthPolicy):
    """不鉴权：任何能连到这个端口的人都是主人。

    这是本地客户端与桌面版的正确行为，``qbank serve`` 默认绑 127.0.0.1 也正是
    因为鉴权交给了「只有本机能连」这件事。
    """


class SharedPassword(AuthPolicy):
    """一个共享口令，可另设一个只读口令。

    适合一个教研室几个人共用。口令比账号体系粗，但它和真实的协作方式是对齐的：
    「这个学期的题库，组里这几个人都能改」——没有人需要知道是谁改的，
    题目修改历史已经逐条留在库里。
    """

    label = "共享口令"
    needs_login = True

    def __init__(self, editor_password: str, readonly_password: str = "") -> None:
        if not editor_password:
            raise AuthError(f"{ENV_MODE}=password 必须同时设 {ENV_PASSWORD}，否则等于没有鉴权")
        if readonly_password and secrets.compare_digest(readonly_password, editor_password):
            raise AuthError(f"{ENV_READONLY_PASSWORD} 和 {ENV_PASSWORD} 不能相同")
        self.editor_password = editor_password
        self.readonly_password = readonly_password

    def identify(self, request: Request) -> Principal | None:
        stored = request.session.get(SESSION_KEY)
        if stored == "editor":
            return Principal(name="已登录", can_edit=True)
        if stored == "viewer":
            return Principal(name="访客", can_edit=False)
        return None

    def authenticate(self, password: str) -> Principal | None:
        # compare_digest 而不是 ==：后者在第一个不同的字节就返回，
        # 泄漏的时间差足以逐字节试出口令
        if secrets.compare_digest(password, self.editor_password):
            return Principal(name="已登录", can_edit=True)
        if self.readonly_password and secrets.compare_digest(password, self.readonly_password):
            return Principal(name="访客", can_edit=False)
        return None

    def session_secret(self) -> str:
        """没显式给密钥时，从口令派生一个。

        不随机生成：多个 worker 各自随机，会话就会随着请求落到哪个进程而随机失效；
        单进程随机，则每次重启所有人都被踢下线。

        从口令派生有三个好处：不必多设一个环境变量、多进程天然一致、改口令即
        作废所有现有会话——最后这条正是改口令时想要的效果。
        """
        explicit = os.environ.get(ENV_SECRET_KEY, "").strip()
        if explicit:
            return explicit
        return hashlib.sha256(f"qbank-session:{self.editor_password}".encode()).hexdigest()


class TrustedHeader(AuthPolicy):
    """身份来自反向代理注入的请求头，用来接已有的统一认证。

    代理（nginx / Apache / oauth2-proxy / 各家网关）完成认证后把用户名写进头，
    应用只读它。这样接 SSO 不需要在题库里实现任何协议。

    ``editors`` 为空表示「能登进来的人都能改」。给了名单就只有名单里的人能写，
    其余人只读。
    """

    label = "反向代理身份头"
    needs_login = False

    def __init__(self, header: str, editors: frozenset[str] = frozenset()) -> None:
        if not header:
            raise AuthError(f"{ENV_MODE}=header 必须设 {ENV_HEADER}，指明信任哪个头")
        self.header = header
        self.editors = editors

    def identify(self, request: Request) -> Principal | None:
        name = request.headers.get(self.header, "").strip()
        if not name:
            return None
        return Principal(name=name, can_edit=not self.editors or name in self.editors)


def _split_names(raw: str) -> frozenset[str]:
    """拆用户名清单。中英文逗号、分号、空白都当分隔符——这是手写配置的常态。

    >>> sorted(_split_names("zhang, li；wang  zhao"))
    ['li', 'wang', 'zhang', 'zhao']
    >>> _split_names("")
    frozenset()
    """
    import re

    return frozenset(part for part in re.split(r"[,，;；\s]+", raw.strip()) if part)


def policy_from_env() -> AuthPolicy:
    """按环境变量造策略。未知模式与缺配置都抛 :class:`AuthError`。"""
    mode = os.environ.get(ENV_MODE, "open").strip().lower() or "open"
    if mode == "open":
        return OpenAccess()
    if mode == "password":
        return SharedPassword(
            os.environ.get(ENV_PASSWORD, "").strip(),
            os.environ.get(ENV_READONLY_PASSWORD, "").strip(),
        )
    if mode == "header":
        return TrustedHeader(
            os.environ.get(ENV_HEADER, "").strip(),
            _split_names(os.environ.get(ENV_EDITORS, "")),
        )
    raise AuthError(f"{ENV_MODE}={mode!r} 不是已知模式；可选 open、password、header")


def https_only() -> bool:
    """会话 cookie 要不要只走 HTTPS。

    默认 **False**，而不是「安全优先」的 True。理由是失败方式不对等：在明文 HTTP
    的局域网部署里把它设成 True，浏览器根本不会存这个 cookie，表现是**登录成功后
    又被弹回登录页**，而且没有任何报错——这种故障极难自查。上了 TLS 就该显式设
    ``QBANK_HTTPS_ONLY=1``，部署文档里是必选项。
    """
    return os.environ.get(ENV_HTTPS_ONLY, "").strip().lower() in {"1", "true", "yes", "on"}


# ============================================================ 同源（CSRF）
def is_same_origin(request: Request) -> bool:
    """这个请求是不是从本站页面发出来的。

    **为什么用同源判断，而不是给每个表单塞一个 CSRF token**

    CSRF 的本质是「浏览器带着用户的凭据，去执行别处页面让它执行的请求」——攻击
    一定由浏览器发起。而现代浏览器在跨站的表单提交上必然带上 ``Origin``
    （或 ``Sec-Fetch-Site``），这两个头又都是脚本改不了的。所以「头在、且不是本站」
    即可判定并拒绝。token 方案能多防住的只有「不发 Origin 的浏览器」，而那类浏览器
    已经不存在了；反过来，token 要落到四十多个表单上，漏掉一个就是一个静默的洞。

    这不是本地模式可以省掉的事：``qbank serve`` 绑在 127.0.0.1 上，任何网页都能
    向它发跨站表单请求，而它没有鉴权——「只有本机能连」挡不住这个。

    比的是 ``Origin`` 的 netloc 与 ``Host`` 头，而不是 ``request.url``：反向代理后面
    ``request.url`` 要靠 ``--proxy-headers`` 才正确，而 ``Host`` 就是浏览器实际用的
    那个名字，与代理配置无关。
    """
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if site in {"cross-site", "same-site"}:
        return False
    origin = request.headers.get("origin", "").strip()
    if not origin:
        # 没有 Origin 也没有跨站的 Sec-Fetch-Site：不可能是浏览器发出的跨站表单
        return True
    if origin.lower() == "null":
        return False
    return urlsplit(origin).netloc == request.headers.get("host", "")


def safe_next(target: str) -> str:
    """登录后要跳回哪。只接受本站路径。

    不校验的话 ``/login?next=https://钓鱼站`` 就是一个开放重定向：攻击者拿这个
    链接做跳板，用户看到的是题库的域名。

    >>> safe_next("/questions?course_id=1")
    '/questions?course_id=1'
    >>> safe_next("https://evil.example/x")
    '/'
    >>> safe_next("//evil.example/x")
    '/'
    >>> safe_next("")
    '/'
    """
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target
