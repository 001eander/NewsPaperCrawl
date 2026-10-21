#!/usr/bin/env python3
"""
浙图 SSO 登录脚本
=================
纯 HTTP 复刻浏览器登录链路,不需要浏览器:
  1. 打开 SSO 登录页建立会话
  2. POST 账号/密码,拿到 cookie 同步用的 sign
  3. POST 超星 cookie 同步接口,种下 .zjlib.cn 会话 cookie
  4. GET vpn358 中转接口,解析其中的 JS 跳转并跟随到 apabi 落地页,
     此时写入 vpn358_sid
  5. 保存全部 cookie 到 .secrets/cookies.json

登录页的验证码由 GVerify 在本地生成、本地比对,登录请求不带验证码,
所以纯 HTTP 直接跳过它。

用法:
  uv run scripts/login.py
  uv run scripts/login.py --credentials .secrets/credentials.json --cookies .secrets/cookies.json
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import aiohttp

# ---- 常量 ----
SHARE_ORIGIN = "https://share.zjlib.cn"
LOGIN_PAGE_URL = f"{SHARE_ORIGIN}/sso/login/custom/zj-library2?refer={SHARE_ORIGIN}"
LOGIN_POST_URL = f"{SHARE_ORIGIN}/sso/login/custom/zj-library-login2"
COOKIE_SYNC_URL = "https://portal.chaoxing.com/entry/sso-login/cookie/sync"
VPN358_URL = f"{SHARE_ORIGIN}/sso/api/auth/library/vpn358"
# vpn358 中转的 refer:方正数字报落地页
APABI_REFER = "https://apabi.com/zjlib/?pid=newspaper.index"
# apabi(方正数字报)代理域 URL 特征
APABI_URL_MARKERS = ("apabi--com.elib.zyproxy.zjlib.cn",)
# 登录后要确认的关键会话 cookie
VPN_COOKIE = "vpn358_sid"
# 浙江图书馆的 wfwfid(图书馆机构编号)
WFW_FID = "2120"
REQUEST_TIMEOUT = 60
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
# 中转页里形如 window.location.href = "..."; 的跳转
_JS_REDIRECT_RE = re.compile(r"""window\.location(?:\.href)?\s*=\s*["']([^"']+)["']""")

DEFAULT_CREDENTIALS = (
    Path(__file__).resolve().parent.parent / ".secrets" / "credentials.json"
)
DEFAULT_COOKIES = Path(__file__).resolve().parent.parent / ".secrets" / "cookies.json"


class LoginError(RuntimeError):
    """登录链路中可预期的失败(凭据错误、中转页异常等)"""


def load_credentials(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"找不到凭据文件: {path}\n请先创建(可参考 {path}.example)")
    data = json.loads(path.read_text(encoding="utf-8"))
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password or "在此填入" in username + password:
        sys.exit(f"凭据文件 {path} 未填写账号或密码")
    return {"username": username, "password": password}


def extract_js_redirect(html: str) -> str | None:
    """取中转页里 window.location 的跳转地址;没有则返回 None

    页面里的 URL 用 JSON 字符串转义(\\/ 和 \\u0026),按 JSON 解码还原。
    """
    m = _JS_REDIRECT_RE.search(html)
    if not m:
        return None
    raw = m.group(1)
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace("\\/", "/")


def is_apabi_url(url: str) -> bool:
    return any(m in url for m in APABI_URL_MARKERS) and "pid=login" not in url


async def _post_json(session, url: str, data: dict) -> dict:
    async with session.post(url, data=data) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise LoginError(f"请求 {url} 失败: HTTP {resp.status}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LoginError(f"接口 {url} 返回的不是 JSON: {text[:200]}") from e


async def _login_async(cred: dict, cookies_path: Path) -> None:
    jar = aiohttp.CookieJar(unsafe=True)  # 超星域会向 .zjlib.cn 种 cookie
    async with aiohttp.ClientSession(
        cookie_jar=jar,
        headers={"User-Agent": USER_AGENT},
        connector=aiohttp.TCPConnector(ssl=False),  # 站点 TLS 证书过期
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
    ) as session:
        print("[1/4] 打开 SSO 登录页...")
        async with session.get(LOGIN_PAGE_URL) as resp:
            await resp.read()
            if resp.status != 200:
                raise LoginError(f"打开登录页失败: HTTP {resp.status}")

        print("[2/4] 提交账号密码...")
        data = await _post_json(
            session,
            LOGIN_POST_URL,
            {
                "account": cred["username"],
                "password": cred["password"],
                "wfwfid": WFW_FID,
                "loginType": "2",
            },
        )
        if not data.get("success"):
            raise LoginError(f"登录失败: {data.get('message') or '账号或密码错误'}")
        sign = (data.get("data") or {}).get("encryptedCookies") or ""
        if not sign:
            raise LoginError("登录接口未返回 cookie 同步凭据")

        print("[3/4] 同步 SSO 会话 cookie...")
        async with session.post(
            COOKIE_SYNC_URL, data={"sign": sign, "url": SHARE_ORIGIN}
        ) as resp:
            await resp.read()
            if resp.status != 200:
                raise LoginError(f"同步 SSO cookie 失败: HTTP {resp.status}")
        if not any(c.key == "p_auth_token" for c in jar):
            raise LoginError("SSO cookie 同步未生效")

        print("[4/4] 经 vpn358 中转进入方正数字报...")
        async with session.get(
            VPN358_URL, params={"wfwfid": WFW_FID, "refer": APABI_REFER}
        ) as resp:
            html = await resp.text()
            if resp.status != 200:
                raise LoginError(f"vpn358 中转失败: HTTP {resp.status}")
        target = extract_js_redirect(html)
        if not target:
            raise LoginError("vpn358 中转页格式变化,未找到跳转地址")

        async with session.get(target) as resp:
            await resp.read()
            final_url = str(resp.url)
            if resp.status != 200 or not is_apabi_url(final_url):
                raise LoginError(f"未进入方正数字报,当前页面: {final_url}")

        cookies = [
            {"name": c.key, "value": c.value, "domain": c["domain"]} for c in jar
        ]
        cookies.sort(key=lambda c: (c["domain"], c["name"]))
        cookies_path.parent.mkdir(parents=True, exist_ok=True)
        cookies_path.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        names = {c["name"] for c in cookies}
        if VPN_COOKIE in names:
            print(f"已保存 {len(cookies)} 个 cookie 到 {cookies_path}(含 {VPN_COOKIE})")
        else:
            print(
                f"已保存 {len(cookies)} 个 cookie 到 {cookies_path}"
                f"(提示: 未含 {VPN_COOKIE},爬虫认证可能失效)"
            )


def run_login(
    credentials_path: Path | str | None = None,
    cookies_path: Path | str | None = None,
) -> None:
    """执行 SSO 登录并保存全部 cookie 到 cookies_path"""
    cred = load_credentials(
        Path(credentials_path) if credentials_path else DEFAULT_CREDENTIALS
    )
    cookies_path = Path(cookies_path) if cookies_path else DEFAULT_COOKIES
    try:
        asyncio.run(_login_async(cred, cookies_path))
    except LoginError as e:
        sys.exit(str(e))
    except (TimeoutError, aiohttp.ClientError) as e:
        sys.exit(f"登录请求失败: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="浙图 SSO 登录")
    ap.add_argument(
        "--credentials",
        default=str(DEFAULT_CREDENTIALS),
        help=f"凭据文件(默认 {DEFAULT_CREDENTIALS})",
    )
    ap.add_argument(
        "--cookies",
        default=str(DEFAULT_COOKIES),
        help=f"Cookie 保存路径(默认 {DEFAULT_COOKIES})",
    )
    args = ap.parse_args()
    run_login(credentials_path=args.credentials, cookies_path=args.cookies)
