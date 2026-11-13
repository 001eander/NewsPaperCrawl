"""浙江图书馆 APABI 代理域识别与动态编号改写。"""

import re
from urllib.parse import urlsplit, urlunsplit

APABI_HOST = "apabi--com.elib.zyproxy.zjlib.cn"
APABI_DYNAMIC_HOST = re.compile(r"apabi--com-s-\d+\.elib\.zyproxy\.zjlib\.cn")
APABI_BASE_URL = f"https://{APABI_HOST}/zjlib/"

_PROXY_SUFFIX = ".elib.zyproxy.zjlib.cn"
_PROXY_HOST = re.compile(
    r"(?P<prefix>(?:[a-z0-9-]+--)*apabi--com)"
    r"(?P<shard>-s-\d+)?"
    r"\.elib\.zyproxy\.zjlib\.cn"
)


def _proxy_parts(host: str) -> tuple[str, str] | None:
    match = _PROXY_HOST.fullmatch(host.lower())
    if match is None:
        return None
    return match.group("prefix"), match.group("shard") or ""


def normalize_apabi_host(host: object) -> str | None:
    """只接受 APABI 页面代理域，拒绝其他主机或相似字符串。"""
    if not isinstance(host, str):
        return None
    normalized = host.lower()
    parts = _proxy_parts(normalized)
    if parts is None or parts[0] != "apabi--com":
        return None
    return normalized


def is_apabi_url(url: str) -> bool:
    return normalize_apabi_host(urlsplit(url).hostname) is not None


def apabi_host_from_url(url: str) -> str | None:
    return normalize_apabi_host(urlsplit(url).hostname)


def rewrite_proxy_url(url: str, apabi_host: str | None) -> str:
    """把 APABI 页面及同族图片代理 URL 切换到会话当前编号。"""
    current = _proxy_parts(normalize_apabi_host(apabi_host) or "")
    if current is None:
        return url

    parts = urlsplit(url)
    target = _proxy_parts(parts.hostname or "")
    if target is None:
        return url

    host = f"{target[0]}{current[1]}{_PROXY_SUFFIX}"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit(parts._replace(netloc=host))
