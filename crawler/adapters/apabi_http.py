"""
APABI 站点 HTTP 适配器
=====================
实现 HttpGateway:
- aiohttp 异步会话
- 并发信号量 + 随机延迟(礼貌限速)
- 自动注入 cookie,检测认证失效(通知 + 抛 AuthError)
- 证书校验关闭(对应浏览器的 --ignore-https-errors)
"""

import asyncio
import logging
import random
import ssl
from pathlib import Path

import aiohttp

from ..domain.exceptions import AuthError
from ..ports.auth import AuthProvider
from ..ports.notifier import Notifier

logger = logging.getLogger(__name__)


class HttpClient:
    def __init__(
        self,
        auth: AuthProvider,
        notifier: Notifier,
        concurrency: int = 8,
        img_concurrency: int = 6,
        min_delay: float = 0.3,
        max_delay: float = 1.0,
        retries: int = 3,
        auth_retries: int = 6,
    ):
        self.auth = auth
        self.notifier = notifier
        self.concurrency = concurrency
        self.img_concurrency = img_concurrency
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.retries = retries
        self.auth_retries = auth_retries
        self._sem = asyncio.Semaphore(concurrency)
        # 图片 CDN(http-img--enews--apabi-*)对高并发敏感:持续高压会从 ~1s 退化到 20s+。
        # 图片下载走独立低并发信号量,避免打爆 CDN 反噬整体吞吐。
        self._img_sem = asyncio.Semaphore(img_concurrency)
        self._session: aiohttp.ClientSession | None = None
        # 关闭证书校验(图书馆代理证书过期)
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            # 代理偶发个别请求挂到 50s+:20/30s 超时会让长尾请求长期占住信号量,
            # 实测正常页 p50 ~2s,10s 超时后重试比苦等整条长尾更快
            timeout=aiohttp.ClientTimeout(total=10),
            connector=aiohttp.TCPConnector(
                ssl=self._ssl, limit=self.concurrency, limit_per_host=self.concurrency
            ),
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def _throttle(self):
        """礼貌限速:随机延迟"""
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))

    async def get(self, url: str, **kwargs) -> str:
        """GET 并返回文本,带认证检测 + 重试"""
        headers = kwargs.pop("headers", {})
        headers["Cookie"] = self.auth.cookie_header(url)
        headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        )
        # 重要:图片等静态资源不需要 Cookie(避免无谓暴露),但 html 需要
        # 代理会把个别请求 302 到 pub.mvc?pid=login&needIpAutoLogin=1,几秒后
        # 用同一份 cookie 重试即可恢复;单次命中不足以判定会话失效。登录页重试
        # 不占用网络重试次数,重试耗尽才通知并抛 AuthError。
        last_err: Exception | None = None
        net_attempts = 0
        auth_hits = 0
        while True:
            await self._throttle()
            login_hit = False
            async with self._sem:
                try:
                    async with self._session.get(
                        url, headers=headers, **kwargs
                    ) as resp:
                        if resp.status == 404:
                            return ""
                        # 认证检测①: 被重定向到登录域(cookie 过期 → 302 -> login.xxx)
                        if _redirected_to_login(resp):
                            login_hit = True
                            last_err = AuthError(
                                f"认证失效于 {url}(被重定向到登录页)"
                            )
                        else:
                            text = await resp.text()
                            # 认证检测②: 响应内容呈现登录页(兜底)
                            if self.auth.is_auth_fail(text):
                                login_hit = True
                                last_err = AuthError(f"认证失效于 {url}")
                            elif resp.status >= 400:
                                raise RuntimeError(f"HTTP {resp.status} for {url}")
                            else:
                                return text
                except AuthError:
                    raise
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.debug("第 %s 次请求失败 %s: %s", net_attempts + 1, url, e)
            if login_hit:
                auth_hits += 1
                if auth_hits >= self.auth_retries:
                    await self.notifier.notify(
                        "浙图爬虫:认证失效",
                        f"访问 {url} 时检测到登录跳转,请重新登录",
                    )
                    raise last_err
                # 被弹到 auto-login 页时,复用原连接可能仍停留在失效会话上
                headers["Connection"] = "close"
                logger.debug("第 %s 次请求命中登录页,重试 %s", auth_hits, url)
                await asyncio.sleep(min(2**auth_hits, 10))
                continue
            net_attempts += 1
            if net_attempts >= self.retries:
                break
            await asyncio.sleep(2 ** (net_attempts - 1))
        raise RuntimeError(f"请求重试耗尽: {url}: {last_err}")

    async def download(self, url: str, save_path: Path) -> bool:
        """下载二进制文件(图片等),返回是否成功"""
        save_path.parent.mkdir(parents=True, exist_ok=True)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            )
        }
        for attempt in range(self.retries):
            async with self._img_sem:
                try:
                    # 图片请求用短超时:CDN 上偶发挂起/缓慢的图若按正文 30s 超时,
                    # 重试 3 次会拖到 96s+,成为整批长尾。15s 足够下载正常图片。
                    # 图片走独立低并发信号量,靠并发数保护 CDN,不再叠加随机延迟。
                    async with self._session.get(
                        url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status == 404:
                            return False
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status}")
                        data = await resp.read()
                        save_path.write_bytes(data)
                        return True
                except Exception as e:  # noqa: BLE001
                    logger.debug("下载失败 %s 第%s次: %s", url, attempt + 1, e)
                    await asyncio.sleep(2**attempt)
        return False


# 登录域特征:被重定向到这些入口即判定为认证失效
_LOGIN_HOST_MARKERS = (
    "login.",
    "sso.",
    ".sso.",
)
_LOGIN_PATH_MARKERS = (
    "/sso/login",
    "/index.php?",
    "/login.aspx",
    "/login",
)


def _redirected_to_login(resp) -> bool:
    """
    判定响应是否被重定向到了登录入口。

    cookie 过期时,服务端会 302 跳到登录域:
      history: 302 apabi--com.xxx/zjlib/?pid=... -> login.elib.zyproxy.zjlib.cn/index.php?pre=...
    aiohttp 默认跟随重定向,但 history 保留了全部 3xx;最终 URL 落在登录域即为失效。
    """
    if resp.history:
        for h in resp.history:
            loc = (h.headers.get("Location") or "").lower()
            if "login" in loc or "sso" in loc:
                return True
    final = str(resp.url).lower()
    if final.startswith("http"):
        host = final.split("//")[1].split("/")[0]
        if any(m in host for m in _LOGIN_HOST_MARKERS):
            return True
        path = "/" + final.split("//")[1].split("/", 1)[-1]
        if any(m in path for m in _LOGIN_PATH_MARKERS):
            return True
    return False
