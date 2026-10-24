import pytest

from scripts.login import read_captcha_code


class _Page:
    def __init__(self, code):
        self.code = code
        self.waited = None

    def wait_for_function(self, expression, *, timeout):
        self.waited = (expression, timeout)

    def evaluate(self, expression):
        return self.code


def test_captcha_waits_for_gverify_initialization():
    page = _Page(" ab12 ")
    assert read_captcha_code(page) == "ab12"
    assert page.waited == (
        "() => window.vm?.verifyCode?.options?.code?.length > 0",
        15_000,
    )


def test_captcha_rejects_empty_code():
    page = _Page("")
    with pytest.raises(RuntimeError, match="未能从登录页读取验证码"):
        read_captcha_code(page)
