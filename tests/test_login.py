"""登录脚本的纯函数测试(不触网)"""

from scripts.login import extract_js_redirect, is_apabi_url


def test_extract_js_redirect_decodes_json_escapes():
    html = (
        '<script>window.location.href = "https:\\/\\/login.elib.zyproxy.zjlib.cn'
        '\\/index.php?r=site\\/enclogin\\u0026enc=abc";</script>'
    )
    assert extract_js_redirect(html) == (
        "https://login.elib.zyproxy.zjlib.cn/index.php?r=site/enclogin&enc=abc"
    )


def test_extract_js_redirect_supports_plain_assignment():
    html = "<script>window.location = 'https://example.com/x';</script>"
    assert extract_js_redirect(html) == "https://example.com/x"


def test_extract_js_redirect_returns_none_without_redirect():
    assert extract_js_redirect("<html><body>no redirect</body></html>") is None


def test_is_apabi_url_accepts_landing_page():
    assert is_apabi_url(
        "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/?pid=newspaper.index"
    )


def test_is_apabi_url_rejects_proxy_login_page():
    assert not is_apabi_url(
        "https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/pub.mvc"
        "?pid=login&needIpAutoLogin=1"
    )
