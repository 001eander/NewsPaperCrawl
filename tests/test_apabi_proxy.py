"""APABI 旧代理域与动态编号代理域的识别和改写测试。"""

import pytest

from crawler.adapters.apabi_proxy import (
    apabi_host_from_url,
    is_apabi_url,
    normalize_apabi_host,
    rewrite_proxy_url,
)

STATIC_HOST = "apabi--com.elib.zyproxy.zjlib.cn"
DYNAMIC_HOST = "apabi--com-s-553.elib.zyproxy.zjlib.cn"


@pytest.mark.parametrize("host", [STATIC_HOST, DYNAMIC_HOST])
def test_normalize_apabi_host(host):
    assert normalize_apabi_host(host) == host
    assert is_apabi_url(f"https://{host}/zjlib/?pid=newspaper.index") is True
    assert apabi_host_from_url(f"https://{host}/zjlib/") == host


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "example.com",
        "apabi--com-s-553.elib.zyproxy.zjlib.cn.example.com",
        "http-img--enews--apabi--com-s-553.elib.zyproxy.zjlib.cn",
    ],
)
def test_normalize_apabi_host_rejects_other_hosts(host):
    assert normalize_apabi_host(host) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"https://{STATIC_HOST}/zjlib/?pid=newspaper.issue&year=2026",
            f"https://{DYNAMIC_HOST}/zjlib/?pid=newspaper.issue&year=2026",
        ),
        (
            "https://apabi--com-s-554.elib.zyproxy.zjlib.cn/zjlib/?cult=CN",
            f"https://{DYNAMIC_HOST}/zjlib/?cult=CN",
        ),
        (
            (
                "https://http-img--enews--apabi--com.elib.zyproxy.zjlib.cn/"
                "D330100hzrb/2026-01/10/cnml.files/a.resbrief.1.jpg"
            ),
            (
                "https://http-img--enews--apabi--com-s-553.elib.zyproxy.zjlib.cn/"
                "D330100hzrb/2026-01/10/cnml.files/a.resbrief.1.jpg"
            ),
        ),
        (
            (
                "https://http-img--enews--apabi--com-s-554.elib.zyproxy.zjlib.cn/"
                "D330100hzrb/2026-01/10/cnml.files/a.resbrief.1.jpg"
            ),
            (
                "https://http-img--enews--apabi--com-s-553.elib.zyproxy.zjlib.cn/"
                "D330100hzrb/2026-01/10/cnml.files/a.resbrief.1.jpg"
            ),
        ),
    ],
)
def test_rewrite_proxy_url_uses_current_shard(url, expected):
    assert rewrite_proxy_url(url, DYNAMIC_HOST) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://img.enews.apabi.com/D330100hzrb/a.jpg",
        "https://example.com/apabi--com-s-554.elib.zyproxy.zjlib.cn/",
        "https://login.elib.zyproxy.zjlib.cn/index.php?pre=x",
    ],
)
def test_rewrite_proxy_url_leaves_unrelated_urls_unchanged(url):
    assert rewrite_proxy_url(url, DYNAMIC_HOST) == url


def test_rewrite_proxy_url_without_saved_host_keeps_legacy_behavior():
    url = f"https://{STATIC_HOST}/zjlib/?pid=newspaper.issue"
    assert rewrite_proxy_url(url, None) == url
    assert rewrite_proxy_url(url, "example.com") == url
