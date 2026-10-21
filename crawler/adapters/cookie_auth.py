"""
Cookie 认证适配器
=================
- cookies.json: 保存 apabi 站点的登录 cookie(私有,gitignore)
- 功能:
    * 加载/保存 cookie
    * 构造 Cookie 请求头(按域名匹配)
    * 判定认证是否失效(委托领域规则;失效后的通知与抛错由 HTTP 适配器负责)
"""

import json
import logging
from pathlib import Path

from ..domain.exceptions import AuthError
from ..domain.rules import response_is_auth_fail

logger = logging.getLogger(__name__)


class AuthManager:
    """实现 AuthProvider:加载登录 cookie 并构造 Cookie 请求头"""

    def __init__(self, cookies_file: Path):
        self.cookies_file = Path(cookies_file)
        self.cookies: dict[str, str] = {}
        self._load()

    def _load(self):
        """从 cookies.json 加载 cookie,兼容两种格式:
        - 旧格式: {"cookies": {"name": "value"}}
        - 新格式: [{"name": "...", "value": "...", "domain": ".zjlib.cn"}, ...]  (登录脚本导出)
        """
        if not self.cookies_file.exists():
            raise AuthError(
                f"缺少认证文件 {self.cookies_file}\n"
                "请参考 .secrets/cookies.json.example 创建,填入浏览器里的登录 cookie"
            )
        try:
            data = json.loads(self.cookies_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise AuthError(f"cookies.json 解析失败: {e}") from e

        self.cookies = {}
        self._cookie_by_domain: dict[str, dict[str, str]] = {}

        if isinstance(data, list):
            # 新格式: 登录脚本导出的 cookie 列表
            for item in data:
                name = item.get("name")
                value = item.get("value")
                if not name or value is None:
                    continue
                self.cookies[name] = value
                domain = item.get("domain", "")
                if domain:
                    self._cookie_by_domain.setdefault(domain, {})[name] = value
        elif isinstance(data, dict):
            raw = data.get("cookies", data)  # 兼容 {"cookies": {...}} 或直接 {...}
            if isinstance(raw, dict):
                self.cookies = {k: str(v) for k, v in raw.items() if v is not None}
                # 旧格式无 domain 信息,全部归入空 domain
                self._cookie_by_domain.setdefault("", self.cookies)
            elif isinstance(raw, list):
                for item in raw:
                    name = item.get("name")
                    value = item.get("value")
                    if not name or value is None:
                        continue
                    self.cookies[name] = value
                    domain = item.get("domain", "")
                    if domain:
                        self._cookie_by_domain.setdefault(domain, {})[name] = value
        else:
            raise AuthError("cookies.json 格式无法识别")

        if not self.cookies:
            raise AuthError("cookies.json 为空,请填入登录 cookie")

    def save(self):
        """保存 cookie(认证更新后调用)"""
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cookies": self.cookies}
        self.cookies_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("cookies 已保存到 %s", self.cookies_file)

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
