#!/usr/bin/env python3
"""
浙图 SSO 登录脚本
=================
用 Playwright 打开浏览器,复刻手动登录链路:
  1. 打开浙江图书馆 share 资源列表页
  2. 点击 "方正数字报" -> JS 自动弹出新标签页(经 vpn358 中转)
     - 未登录:新标签页落到 SSO 登录页,自动填账号/选机构/填验证码/点登录,
       登录后站点自动回跳到方正数字报落地页
     - 已登录:新标签页直接落到 apabi
  3. 检测到方正数字报落地页后,轮询到 vpn358_sid 会话 cookie 再保存全部
     Cookies 到 .secrets/cookies.json

验证码是页面本地用 GVerify 画在 canvas 上的,校验也在本地完成(登录请求根本
不带验证码)。所以直接从 Vue 实例读取 GVerify 生成的那串字符填入即可,不需要
OCR 或第三方识别服务。

关键点:全程不强制 goto,由站点 JS 自己带过去,这样才能拿到
vpn358_sid 会话 cookie(crawler/auth.py 认证依赖)。

用法:
  uv run scripts/login.py
  uv run scripts/login.py --headless
  uv run scripts/login.py --credentials .secrets/credentials.json --cookies .secrets/cookies.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# 项目未安装为包,直接运行脚本时 sys.path[0] 是 scripts/ 而非项目根;
# 与 scripts/status.py 一样手动补上项目根,才能读取 Settings 的默认路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.adapters.config import Settings

# ---- 常量 ----
# share 资源列表页(appId/typeId 指向 "方正数字报" 所在的资源列表)
SHARE_URL = (
    "https://share.zjlib.cn/engine2/general/more"
    "?appId=1292055&typeId=4312537&currentBranch=0"
    "&wfwfid=2120&pageId=35594&websiteId=28609&mhType=1"
    "&publicId=5a4b97f974953111ed0a17754b327ca0790d"
    "&mhEnc=7918f9663ed2443f881ad8f98aed56dd"
)
# 资源列表里 "方正数字报" 的标题元素(class=name)
RESOURCE_NAME = "方正数字报"
# SSO 登录页 URL 特征
LOGIN_URL_MARKER = "sso/login"
# apabi(方正数字报)代理域 URL 特征
APABI_URL_MARKERS = ("apabi--com.elib.zyproxy.zjlib.cn",)
# 登录后要确认的关键会话 cookie
VPN_COOKIE = "vpn358_sid"
# 轮询关键会话 cookie 的超时与间隔(秒)
COOKIE_WAIT_TIMEOUT = 60
COOKIE_POLL_INTERVAL = 0.5
# 验证码错误时站点会重新生成验证码,允许自动重试的次数
LOGIN_MAX_ATTEMPTS = 3
# 提交登录后等待站点跳转到方正数字报的总超时(秒)
LOGIN_REDIRECT_TIMEOUT = 90

# 默认凭据/cookie 路径与爬虫共用 Settings(支持 NEWSPAPER_SECRETS_DIR 覆盖)。
# run_login 显式传入路径时仍以传入值为准,CLI 行为不变。
_SETTINGS = Settings.from_env()
DEFAULT_CREDENTIALS = _SETTINGS.secrets_dir / "credentials.json"
DEFAULT_COOKIES = _SETTINGS.secrets_dir / "cookies.json"


def load_credentials(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"找不到凭据文件: {path}\n请先创建(可参考 {path}.example)")
    data = json.loads(path.read_text(encoding="utf-8"))
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password or "在此填入" in username + password:
        sys.exit(f"凭据文件 {path} 未填写账号或密码")
    return {"username": username, "password": password}


def select_province_and_library(page) -> None:
    """自动选择 省本级 -> 浙江图书馆(两级下拉框)"""
    jq_input = page.locator('input[name="jqtype"]')
    jq_input.wait_for(state="visible", timeout=15_000)
    jq_input.click()
    province = page.locator(".select-list .one-title", has_text="省本级")
    province.wait_for(state="visible", timeout=10_000)
    province.click()
    lib = page.locator('li.option[data-index="2120"]', has_text="浙江图书馆")
    lib.wait_for(state="visible", timeout=10_000)
    lib.click()
    page.wait_for_function(
        """(sel) => document.querySelector(sel)?.value?.includes('浙江图书馆')""",
        arg='input[name="jqtype"]',
        timeout=10_000,
    )


def is_fangzheng_url(url: str) -> bool:
    """方正数字报落地页 URL 特征:apabi 代理域,或 share 域上的 area 落地页

    代理域的 pub.mvc?pid=login&needIpAutoLogin=1 是"还没登录成功"的中转页,
    不能算落地,否则会把无效 cookie 当成登录结果保存。
    """
    if any(m in url for m in APABI_URL_MARKERS):
        return "pid=login" not in url
    # SSO 登录成功后,落地页可能回跳到 share.zjlib.cn 的 area 页面
    return "share.zjlib.cn/entry/area/" in url


def is_proxy_login(url: str) -> bool:
    """apabi 代理的 auto-login 中转页(pub.mvc?pid=login):尚未完成授权"""
    return any(m in url for m in APABI_URL_MARKERS) and "pid=login" in url


def is_fangzheng_page(page) -> bool:
    """页面是否为"方正数字报":命中 URL 特征,或页面标题含"方正数字报"(URL 变动时兜底)"""
    url = page.url or ""
    if is_proxy_login(url):
        return False
    if is_fangzheng_url(url):
        return True
    try:
        return "方正数字报" in (page.title() or "")
    except Exception:  # noqa: BLE001  # 导航中标题不可读时按"不是"处理
        return False


def wait_for_cookie(ctx, name: str, timeout: float) -> bool:
    """轮询 context 的 cookie,直到出现指定 cookie 或超时"""
    deadline = time.time() + timeout
    while True:
        if any(c["name"] == name for c in ctx.cookies()):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(COOKIE_POLL_INTERVAL)


def _page_label(page) -> str:
    try:
        return f"{page.url} | {page.title()}"
    except Exception:  # noqa: BLE001  # 导航中不可读时回退为 "(标题不可读)"
        return f"{page.url} | (标题不可读)"


def is_login(url: str) -> bool:
    return LOGIN_URL_MARKER in url


def read_captcha_code(page) -> str:
    """读取 GVerify 本次生成的验证码

    验证码由 /sso/assets/lib/gVerify.js 在本地 canvas 上绘制,答案存在页面
    Vue 实例的 verifyCode.options.code 里;登录接口本身不接收验证码,只在
    前端比对。所以直接读内存即可,无需 OCR 或识别服务。
    """
    page.wait_for_function(
        "() => window.vm?.verifyCode?.options?.code?.length > 0",
        timeout=15_000,
    )
    code = page.evaluate("() => window.vm?.verifyCode?.options?.code || ''").strip()
    if not code:
        raise RuntimeError("未能从登录页读取验证码(GVerify 实例不可用)")
    return code


def login_error(page):
    """登录页当前显示的错误信息;没有错误时返回 None"""
    try:
        loc = page.locator(".err-txt:visible")
        if loc.count() == 0:
            return None
        text = loc.first.inner_text().strip()
        return text or None
    except Exception:  # noqa: BLE001  # 导航中页面不可读,按无错误处理
        return None


def submit_login(page) -> str:
    """读取验证码填入并点登录,返回本次填入的验证码"""
    code = read_captcha_code(page)
    page.locator(".ipt-code").fill(code)
    page.locator("button.btn-big-blue").click()
    return code


def wait_for_fangzheng(ctx, timeout: float):
    """等站点跳转到方正数字报

    返回 (落地页, 错误信息):落地成功时错误信息为 None;登录被拒时落地页为
    None 并带回页面错误文案;超时则两者都是 None。
    """
    deadline = time.time() + timeout
    last_urls: list[str] = []
    while time.time() < deadline:
        for pg in ctx.pages:
            if is_fangzheng_page(pg):
                return pg, None
            err = login_error(pg)
            if err:
                return None, err
        urls = sorted(_page_label(pg) for pg in ctx.pages)
        if urls != last_urls:
            print("  标签页:", " ; ".join(urls) if urls else "(无)")
            last_urls = urls
        time.sleep(1)
    return None, None


def run_login(
    credentials_path: Path | str | None = None,
    cookies_path: Path | str | None = None,
    headless: bool = False,
) -> None:
    """
    执行 SSO 登录并保存全部 cookie 到 cookies_path。

    供 cli.py 的 login 子命令复用;也可直接运行本脚本调用。
    """
    cred = load_credentials(
        Path(credentials_path) if credentials_path else DEFAULT_CREDENTIALS
    )
    cookies_path = Path(cookies_path) if cookies_path else DEFAULT_COOKIES

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel="chromium")
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,  # 站点 TLS 证书过期,需忽略才能正常加载
        )
        page = ctx.new_page()
        page.goto(SHARE_URL, wait_until="domcontentloaded", timeout=60_000)
        print(f"[1/6] 已打开浙江图书馆资源列表: {page.title()}")

        # 步骤2: 点击 "方正数字报",监听新标签页(JS 自动弹出)
        print("[2/6] 点击 方正数字报...")
        with ctx.expect_page(timeout=30_000) as page_info:
            page.locator("div.name", has_text=RESOURCE_NAME).first.click()
        np = page_info.value

        # 等待新标签页稳定:落到 SSO 登录页、代理 auto-login 中转页,或已登录的 apabi
        np.wait_for_url(
            lambda u: is_login(u) or is_proxy_login(u) or is_fangzheng_url(u),
            timeout=45_000,
        )

        # ---- 分情况:未登录走完整登录流程,已登录直接取 cookie ----
        if is_login(np.url):
            print("[2/6] 需要登录,SSO 登录页已弹出")
            np.locator(".ipt-code").wait_for(state="visible", timeout=15_000)

            np.locator(".ipt-tel").fill(cred["username"])
            np.locator(".ipt-pwd").fill(cred["password"])
            select_province_and_library(np)
            print("[3/6] 已填入账号并选择 省本级 -> 浙江图书馆")

            apabi_page = None
            for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
                try:
                    submit_login(np)
                except RuntimeError as e:
                    sys.exit(str(e))
                print(f"[3/6] 已提交登录(第 {attempt}/{LOGIN_MAX_ATTEMPTS} 次)")
                # 给前端校验和 AJAX 留出时间,避免把上一轮的错误文案当成结果
                time.sleep(2)
                apabi_page, err = wait_for_fangzheng(ctx, LOGIN_REDIRECT_TIMEOUT)
                if apabi_page is not None or err is None:
                    break
                print(f"  登录被拒: {err}")
                # 只有验证码错误才重试,密码错误继续重试可能触发账号锁定
                if "验证码" not in err or attempt == LOGIN_MAX_ATTEMPTS:
                    break
                print("  重新读取验证码后重试...")

            if apabi_page is None:
                print("提示: 登录后未自动进入方正数字报,当前标签页:")
                for pg in ctx.pages:
                    print("   -", _page_label(pg))
                sys.exit("登录失败,请检查账号/密码是否正确")
            apabi_page.bring_to_front()
            print(f"[6/6] 登录成功,已进入方正数字报: {apabi_page.url}")
        else:
            apabi_page = np
            apabi_page.bring_to_front()
            if is_proxy_login(apabi_page.url or ""):
                # 代理 auto-login 中转页:等它自己跳到真正的数字报页面,
                # 否则保存下来的 cookie 无法访问报纸内容
                print("[2/6] 已登录,等待代理完成 auto-login 跳转...")
                try:
                    apabi_page.wait_for_url(
                        lambda u: is_fangzheng_url(u), timeout=60_000
                    )
                except Exception:  # noqa: BLE001
                    sys.exit(
                        "代理仍停留在登录页,登录未生效\n当前页面:"
                        + _page_label(apabi_page)
                    )
            print("[2/6] 已登录,新标签页直接进入方正数字报")
            print("[6/6] 已进入方正数字报")

        # 登录成功后 vpn358_sid 才是可用凭据,轮询到它再保存:比等固定时长更快,
        # 也不会把尚未落定的会话存下来
        if wait_for_cookie(ctx, VPN_COOKIE, COOKIE_WAIT_TIMEOUT):
            print(f"已取得会话 cookie {VPN_COOKIE}")
        else:
            print(f"注意: 等待 {VPN_COOKIE} 超时(仍保存,靠保存后的 cookie 检查兜底)")

        # 已进入方正数字报落地页,保存全部 cookie(所有标签页共享 context)
        cookies = ctx.cookies()
        payload = [
            {"name": c["name"], "value": c["value"], "domain": c["domain"]}
            for c in cookies
        ]
        cookies_path.parent.mkdir(parents=True, exist_ok=True)
        cookies_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        names = {c["name"] for c in payload}
        if VPN_COOKIE in names:
            print(f"已保存 {len(payload)} 个 cookie 到 {cookies_path}(含 {VPN_COOKIE})")
        else:
            print(
                f"已保存 {len(payload)} 个 cookie 到 {cookies_path}(提示: 未含 {VPN_COOKIE},爬虫认证可能失效,建议重新登录)"
            )

        browser.close()


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
    ap.add_argument(
        "--headless",
        action="store_true",
        help="不打开浏览器窗口(适合定时任务/服务器)",
    )
    args = ap.parse_args()
    run_login(
        credentials_path=args.credentials,
        cookies_path=args.cookies,
        headless=args.headless,
    )
