"""
自动登录与认证刷新测试
======================
覆盖三件事,全部不触网、不启动浏览器:
  - AuthManager.relogin 的并发去重(同一时刻只真正登录一次)、代数去重、
    成功后热加载 cookie、冷却期内不重复登录
  - HttpClient 撞上登录页后自动重新登录再重试;登录失败/冷却拦截时
    通知并抛 AuthError
  - 凭据缺失/未填写时以 LoginError 报错,不进入 Playwright
"""

import asyncio
import json

import pytest

from crawler.adapters.apabi_http import HttpClient
from crawler.adapters.cookie_auth import AuthManager
from crawler.adapters.sso_login import SsoLogin
from crawler.domain.exceptions import AuthError, LoginError

COOKIES = [
    {"name": "vpn358_sid", "value": "old", "domain": ".zjlib.cn"},
]


def _write_cookies(path, value: str = "old") -> None:
    path.write_text(
        json.dumps(
            [{"name": "vpn358_sid", "value": value, "domain": ".zjlib.cn"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class _FakeLogin:
    """把新 cookie 写回文件,模拟真实登录的落盘行为"""

    def __init__(self, cookies_file, calls: list | None = None):
        self.cookies_file = cookies_file
        self.calls = calls if calls is not None else []

    def can_login(self) -> bool:
        return True

    async def login(self) -> None:
        self.calls.append("login")
        await asyncio.sleep(0)  # 让并发调用真正交错
        _write_cookies(self.cookies_file, "new")


class _NoNotify:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    @property
    def is_configured(self) -> bool:
        return True

    async def notify(self, title: str, body: str = "") -> None:
        self.messages.append((title, body))


# ---------------- AuthManager ----------------


def test_relogin_reloads_cookies(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    _write_cookies(cookies_file)
    calls: list = []
    auth = AuthManager(cookies_file, _FakeLogin(cookies_file, calls))

    async def scenario():
        assert await auth.relogin(auth.generation) is True

    asyncio.run(scenario())
    assert calls == ["login"]
    assert auth.generation == 1
    assert "vpn358_sid=new" in auth.cookie_header(
        "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/"
    )


def test_concurrent_relogin_logs_in_once(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    _write_cookies(cookies_file)
    calls: list = []
    auth = AuthManager(cookies_file, _FakeLogin(cookies_file, calls))

    async def scenario():
        results = await asyncio.gather(*(auth.relogin(0) for _ in range(5)))
        assert results == [True] * 5

    asyncio.run(scenario())
    assert calls == ["login"]
    assert auth.generation == 1


def test_relogin_skips_when_generation_already_moved(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    _write_cookies(cookies_file)
    calls: list = []
    auth = AuthManager(cookies_file, _FakeLogin(cookies_file, calls))

    async def scenario():
        assert await auth.relogin(99) is True

    asyncio.run(scenario())
    assert calls == []
    assert auth.generation == 0


def test_relogin_respects_cooldown(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    _write_cookies(cookies_file)
    calls: list = []
    auth = AuthManager(cookies_file, _FakeLogin(cookies_file, calls))

    async def scenario():
        assert await auth.relogin(0) is True
        # 登录刚成功站点仍判失效:冷却期内不再拉起浏览器
        assert await auth.relogin(auth.generation) is False

    asyncio.run(scenario())
    assert calls == ["login"]


def test_relogin_without_credentials_raises_login_error(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    _write_cookies(cookies_file)
    auth = AuthManager(cookies_file)

    async def scenario():
        with pytest.raises(LoginError):
            await auth.relogin(0)

    asyncio.run(scenario())


# ---------------- HttpClient ----------------


class _Resp:
    def __init__(self, body: str, login: bool):
        self.status = 200
        self.history: list = []
        self.url = (
            "https://login.elib.zyproxy.zjlib.cn/index.php?pre=x"
            if login
            else "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/x"
        )
        self._body = body

    async def text(self) -> str:
        return self._body


class _RespCM:
    def __init__(self, resp: _Resp):
        self._resp = resp

    async def __aenter__(self) -> _Resp:
        return self._resp

    async def __aexit__(self, *args) -> bool:
        return False


class _FakeSession:
    """站点行为:会话有效返回正文,失效则重定向到登录页"""

    def __init__(self, auth: "_FakeAuth"):
        self.auth = auth

    def get(self, url: str, **kwargs) -> _RespCM:
        if self.auth.valid:
            return _RespCM(_Resp("正文内容", login=False))
        return _RespCM(_Resp("", login=True))


class _FakeAuth:
    def __init__(self, relogin_result: bool = True):
        self.generation = 0
        self.valid = False
        self.login_calls = 0
        self.relogin_result = relogin_result

    def cookie_header(self, url: str = "") -> str:
        return "vpn358_sid=ok" if self.valid else "vpn358_sid=expired"

    def is_auth_fail(self, text: str) -> bool:
        return False

    async def relogin(self, seen_generation: int | None = None) -> bool:
        self.login_calls += 1
        if seen_generation is not None and self.generation != seen_generation:
            return True
        if not self.relogin_result:
            return False
        self.valid = True
        self.generation += 1
        return True


class _ExplodingAuth(_FakeAuth):
    async def relogin(self, seen_generation: int | None = None) -> bool:
        raise LoginError("凭据文件未填写账号或密码")


def _client(auth, notifier) -> HttpClient:
    # auth_retries=1:第一次命中登录页就触发自动登录,测试无需等待退避
    http = HttpClient(auth, notifier, auth_retries=1)
    http._session = _FakeSession(auth)
    return http


def test_get_relogins_then_succeeds():
    auth = _FakeAuth()
    notifier = _NoNotify()

    async def scenario():
        return await _client(auth, notifier).get("https://example.com/page")

    assert asyncio.run(scenario()) == "正文内容"
    assert auth.login_calls == 1
    assert notifier.messages == []


def test_get_raises_when_relogin_blocked():
    auth = _FakeAuth(relogin_result=False)
    notifier = _NoNotify()

    async def scenario():
        with pytest.raises(AuthError):
            await _client(auth, notifier).get("https://example.com/page")

    asyncio.run(scenario())
    assert auth.login_calls == 1
    assert notifier.messages and notifier.messages[0][0] == "浙图爬虫:认证失效"


def test_get_reports_login_error():
    auth = _ExplodingAuth()
    notifier = _NoNotify()

    async def scenario():
        with pytest.raises(LoginError):
            await _client(auth, notifier).get("https://example.com/page")

    asyncio.run(scenario())
    assert notifier.messages and notifier.messages[0][0] == "浙图爬虫:自动登录失败"


# ---------------- 凭据校验 ----------------


def test_login_requires_credentials_file(tmp_path):
    login = SsoLogin(tmp_path / "missing.json", tmp_path / "cookies.json")
    assert login.can_login() is False
    with pytest.raises(LoginError):
        login.login_sync()


def test_login_rejects_placeholder_credentials(tmp_path):
    cred = tmp_path / "credentials.json"
    cred.write_text(
        json.dumps({"username": "在此填入登录账号", "password": "x"}), encoding="utf-8"
    )
    login = SsoLogin(cred, tmp_path / "cookies.json")
    assert login.can_login() is True
    with pytest.raises(LoginError):
        login.login_sync()
