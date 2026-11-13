"""
Cookie 认证适配器
=================
- cookies.json: 保存 apabi 站点的登录 cookie(私有,gitignore)
- 功能:
    * 加载/保存 cookie
    * 构造 Cookie 请求头(按域名匹配)
    * 判定认证是否失效(委托领域规则)
    * 失效时调用注入的 LoginRunner 重新登录并热加载新 cookie
- 通知与抛错由 HTTP 适配器负责,本适配器只管凭据本身
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from ..domain.exceptions import AuthError, LoginError
from ..domain.rules import response_is_auth_fail
from ..ports.auth import LoginRunner
from .apabi_proxy import normalize_apabi_host

logger = logging.getLogger(__name__)

# 自动登录成功后,同一会话短时间内不再重复登录:若站点仍判定失效(账号权限等),
# 反复拉起浏览器只会拖垮整轮爬取,交给人工处理更合适
MIN_RELOGIN_INTERVAL = 120.0


class AuthManager:
    """实现 AuthProvider:加载登录 cookie、构造 Cookie 请求头、失效时重新登录"""

    def __init__(self, cookies_file: Path, login: LoginRunner | None = None):
        self.cookies_file = Path(cookies_file)
        self._login = login
        self.cookies: dict[str, str] = {}
        self._cookie_by_domain: dict[str, dict[str, str]] = {}
        self.apabi_host: str | None = None
        # 每成功登录一次加一;并发调用靠它判断"别人已经刷新过了"
        self._generation = 0
        self._lock = asyncio.Lock()
        self._last_login_at = float("-inf")
        self._load()

    @property
    def generation(self) -> int:
        return self._generation

    def _load(self):
        """从 cookies.json 加载 cookie,兼容三种格式:
        - 旧格式: {"cookies": {"name": "value"}}
        - Playwright 列表: [{"name": "...", "value": "...", "domain": ".zjlib.cn"}, ...]
        - 会话格式: {"cookies": [...], "apabi_host": "apabi--com-s-553..."}
        """
        if not self.cookies_file.exists():
            raise AuthError(
                f"缺少认证文件 {self.cookies_file}\n"
                "请参考 .secrets/cookies.json.example 创建,或配置 .secrets/credentials.json "
                "让爬虫自动登录"
            )
        try:
            data = json.loads(self.cookies_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise AuthError(f"cookies.json 解析失败: {e}") from e

        cookies: dict[str, str] = {}
        by_domain: dict[str, dict[str, str]] = {}
        apabi_host = (
            normalize_apabi_host(data.get("apabi_host"))
            if isinstance(data, dict)
            else None
        )

        if isinstance(data, list):
            # Playwright 直接导出的 cookie 列表
            for item in data:
                name = item.get("name")
                value = item.get("value")
                if not name or value is None:
                    continue
                cookies[name] = value
                domain = item.get("domain", "")
                if domain:
                    by_domain.setdefault(domain, {})[name] = value
        elif isinstance(data, dict):
            raw = data.get("cookies", data)  # 兼容 {"cookies": {...}} 或直接 {...}
            if isinstance(raw, dict):
                cookies = {k: str(v) for k, v in raw.items() if v is not None}
                # 旧格式无 domain 信息,全部归入空 domain
                by_domain.setdefault("", cookies)
            elif isinstance(raw, list):
                for item in raw:
                    name = item.get("name")
                    value = item.get("value")
                    if not name or value is None:
                        continue
                    cookies[name] = value
                    domain = item.get("domain", "")
                    if domain:
                        by_domain.setdefault(domain, {})[name] = value
        else:
            raise AuthError("cookies.json 格式无法识别")

        if not cookies:
            raise AuthError("cookies.json 为空,请填入登录 cookie")

        self.cookies = cookies
        self._cookie_by_domain = by_domain
        self.apabi_host = apabi_host

    def save(self):
        """保存 cookie(认证更新后调用)"""
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"cookies": self.cookies}
        if self.apabi_host:
            payload["apabi_host"] = self.apabi_host
        self.cookies_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("cookies 已保存到 %s", self.cookies_file)

    async def relogin(self, seen_generation: int | None = None) -> bool:
        """重新登录的唯一入口:并发调用只真正登录一次

        返回 True 表示 cookie 已刷新(本次登录或别人刚登录过);
        冷却期内重复触发返回 False,登录失败抛 LoginError。
        """
        if self._login is None:
            raise LoginError(
                "未配置自动登录;请在 .secrets/credentials.json 填写账号密码,"
                "或手动执行 uv run scripts/login.py"
            )
        async with self._lock:
            if seen_generation is not None and self._generation != seen_generation:
                # 排队期间别的请求已经刷新过 cookie,直接用新的重试
                return True
            if time.monotonic() - self._last_login_at < MIN_RELOGIN_INTERVAL:
                logger.warning(
                    "自动登录后 %.0fs 内再次检测到认证失效,不再重复登录",
                    MIN_RELOGIN_INTERVAL,
                )
                return False
            logger.info("检测到认证失效,开始自动登录")
            await self._login.login()
            self._load()
            self._generation += 1
            self._last_login_at = time.monotonic()
            logger.info("自动登录完成,cookie 已刷新(第 %s 代)", self._generation)
            return True

    def cookie_header(self, url: str = "") -> str:
        """
        构造 Cookie 请求头。
        若提供了 url,则按域名匹配(apabi 域带 vpn358_sid,其余带 zjlib.cn 的)。
        否则返回全部 cookie(兼容无 domain 的旧格式)。
        """
        if not url:
            cookies = self.cookies
        else:
            host = url.split("//")[1].split("/")[0].lower() if "//" in url else url
            cookies = {}
            for domain, domain_cookies in self._cookie_by_domain.items():
                # 域匹配: 请求 host 以 domain 结尾 或 domain 为空(通用 cookie)
                if not domain or host.endswith(domain.lstrip(".")):
                    cookies.update(domain_cookies)
            # 若没有域匹配(比如 url 是 img.enews.apabi.com,不含 elib 域),退回全部
            if not cookies:
                cookies = self.cookies
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    def is_auth_fail(self, text: str) -> bool:
        """判断响应文本是否表示认证失效(领域规则)"""
        return response_is_auth_fail(text)
