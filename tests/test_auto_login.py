"""
自动登录与认证刷新测试
======================
覆盖四件事,全部不触网、不启动浏览器:
  - AuthManager.relogin 的并发去重(同一时刻只真正登录一次)、代数去重、
    成功后热加载 cookie、冷却期内不重复登录
  - HttpClient 撞上登录页后自动重新登录再重试;登录失败/冷却拦截时
    通知并抛 AuthError
  - 凭据缺失/未填写时以 LoginError 报错,不进入 Playwright
  - 原有及动态代理域的落地页识别,代理登录中转页不算落地
"""

import asyncio
import json

import pytest

from crawler.adapters.apabi_http import HttpClient
from crawler.adapters.cookie_auth import AuthManager
from crawler.adapters.sso_login import (
    SsoLogin,
    _is_fangzheng_url,
    _is_proxy_login,
    _wait_for_fangzheng,
)
from crawler.domain.exceptions import AuthError, LoginError

COOKIES = [
    {"name": "vpn358_sid", "value": "old", "domain": ".zjlib.cn"},
]
DYNAMIC_HOST = "apabi--com-s-553.elib.zyproxy.zjlib.cn"
NEXT_DYNAMIC_HOST = "apabi--com-s-554.elib.zyproxy.zjlib.cn"


def _write_cookies(path, value: str = "old", apabi_host: str | None = None) -> None:
    payload: object = [{"name": "vpn358_sid", "value": value, "domain": ".zjlib.cn"}]
    if apabi_host:
        payload = {"cookies": payload, "apabi_host": apabi_host}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class _FakeLogin:
    """把新 cookie 写回文件,模拟真实登录的落盘行为"""

    def __init__(
        self,
        cookies_file,
        calls: list | None = None,
        apabi_host: str | None = None,
    ):
        self.cookies_file = cookies_file
        self.calls = calls if calls is not None else []
        self.apabi_host = apabi_host

    def can_login(self) -> bool:
        return True

    async def login(self) -> None:
        self.calls.append("login")
        await asyncio.sleep(0)  # 让并发调用真正交错
        _write_cookies(self.cookies_file, "new", self.apabi_host)


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


def test_legacy_dict_cookies_have_no_saved_proxy_host(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text(
        json.dumps({"cookies": {"vpn358_sid": "old"}}), encoding="utf-8"
    )

    auth = AuthManager(cookies_file)

    assert auth.apabi_host is None
    assert auth.cookie_header() == "vpn358_sid=old"


def test_login_saves_dynamic_proxy_host_with_cookies(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    login = SsoLogin(tmp_path / "credentials.json", cookies_file)

    class _Context:
        def cookies(self):
            return COOKIES

    result = login._save_cookies(
        _Context(), f"https://{DYNAMIC_HOST}/zjlib/?pid=newspaper.index"
    )

    assert result == COOKIES
    saved = json.loads(cookies_file.read_text(encoding="utf-8"))
    assert saved == {"cookies": COOKIES, "apabi_host": DYNAMIC_HOST}
    assert AuthManager(cookies_file).apabi_host == DYNAMIC_HOST


def test_relogin_reloads_dynamic_proxy_host(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    _write_cookies(cookies_file, apabi_host=DYNAMIC_HOST)
    auth = AuthManager(
        cookies_file, _FakeLogin(cookies_file, apabi_host=NEXT_DYNAMIC_HOST)
    )

    async def scenario():
        assert await auth.relogin(auth.generation) is True

    asyncio.run(scenario())
    assert auth.apabi_host == NEXT_DYNAMIC_HOST


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
        self.urls: list[str] = []

    def get(self, url: str, **kwargs) -> _RespCM:
        self.urls.append(url)
        if self.auth.valid:
            return _RespCM(_Resp("正文内容", login=False))
        return _RespCM(_Resp("", login=True))


class _FakeAuth:
    def __init__(
        self,
        relogin_result: bool = True,
        apabi_host: str | None = None,
        relogin_host: str | None = None,
    ):
        self.generation = 0
        self.valid = False
        self.login_calls = 0
        self.relogin_result = relogin_result
        self.apabi_host = apabi_host
        self.relogin_host = relogin_host

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
        if self.relogin_host:
            self.apabi_host = self.relogin_host
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


def test_get_uses_saved_dynamic_proxy_host():
    auth = _FakeAuth(apabi_host=DYNAMIC_HOST)
    auth.valid = True
    client = _client(auth, _NoNotify())
    url = "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/?pid=newspaper.issue"

    assert asyncio.run(client.get(url)) == "正文内容"
    assert client._session.urls == [
        f"https://{DYNAMIC_HOST}/zjlib/?pid=newspaper.issue"
    ]


def test_relogin_switches_current_retry_to_new_proxy_host():
    auth = _FakeAuth(
        apabi_host=DYNAMIC_HOST,
        relogin_host=NEXT_DYNAMIC_HOST,
    )
    client = _client(auth, _NoNotify())
    url = "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/?pid=newspaper.issue"

    assert asyncio.run(client.get(url)) == "正文内容"
    assert client._session.urls == [
        f"https://{DYNAMIC_HOST}/zjlib/?pid=newspaper.issue",
        f"https://{NEXT_DYNAMIC_HOST}/zjlib/?pid=newspaper.issue",
    ]


def test_download_uses_saved_dynamic_proxy_host(tmp_path):
    class _BinaryResp:
        status = 200

        async def read(self):
            return b"image"

    class _DownloadSession:
        def __init__(self):
            self.urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            return _RespCM(_BinaryResp())

    auth = _FakeAuth(apabi_host=DYNAMIC_HOST)
    client = HttpClient(auth, _NoNotify())
    session = _DownloadSession()
    client._session = session
    save_path = tmp_path / "image.jpg"
    url = (
        "https://http-img--enews--apabi--com.elib.zyproxy.zjlib.cn/"
        "D330100hzrb/cnml.files/a.resbrief.1.jpg"
    )

    async def scenario():
        assert await client.download(url, save_path) is True

    asyncio.run(scenario())
    assert session.urls == [
        (
            "https://http-img--enews--apabi--com-s-553.elib.zyproxy.zjlib.cn/"
            "D330100hzrb/cnml.files/a.resbrief.1.jpg"
        )
    ]
    assert save_path.read_bytes() == b"image"


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


# ---------------- 落地页识别 ----------------


class _LandedPage:
    url = (
        "https://apabi--com-s-553.elib.zyproxy.zjlib.cn/zjlib/"
        "?pid=newspaper.index&cult=CN"
    )

    def title(self):
        return "中华数字书苑-中华数字文明"


def test_wait_for_fangzheng_accepts_dynamic_proxy_host():
    page = _LandedPage()

    class _Context:
        def __init__(self):
            self.pages = [page]

    landed, error = _wait_for_fangzheng(_Context(), timeout=0.01)
    assert landed is page
    assert error is None


@pytest.mark.parametrize(
    "host",
    [
        "apabi--com.elib.zyproxy.zjlib.cn",
        "apabi--com-s-553.elib.zyproxy.zjlib.cn",
        "apabi--com-s-554.elib.zyproxy.zjlib.cn",
    ],
)
def test_fangzheng_url_accepts_apabi_proxy_hosts(host):
    url = f"https://{host}/zjlib/?pid=newspaper.index"
    assert _is_fangzheng_url(url) is True
    assert _is_proxy_login(url) is False


@pytest.mark.parametrize(
    "host",
    [
        "apabi--com.elib.zyproxy.zjlib.cn",
        "apabi--com-s-553.elib.zyproxy.zjlib.cn",
    ],
)
def test_proxy_login_is_not_fangzheng(host):
    url = f"https://{host}/zjlib/pub.mvc?pid=login&needIpAutoLogin=1"
    assert _is_proxy_login(url) is True
    assert _is_fangzheng_url(url) is False


def test_fangzheng_url_rejects_unrelated_host():
    url = (
        "https://example.com/apabi--com-s-553.elib.zyproxy.zjlib.cn/"
        "?pid=newspaper.index"
    )
    assert _is_fangzheng_url(url) is False
    assert _is_proxy_login(url) is False


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
