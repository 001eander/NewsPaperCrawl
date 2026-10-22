"""
HTTP 连接池容量测试
===================
验证 TCPConnector 的全局上限覆盖 HTML 与图片两套信号量:
图片高峰期占满自己的槽位时,HTML 仍能拿到连接,img_concurrency 才真正生效。
"""

import asyncio

from crawler.adapters.apabi_http import HttpClient


class _Auth:
    def cookie_header(self, url: str = "") -> str:
        return ""

    def is_auth_fail(self, text: str) -> bool:
        return False


class _Notifier:
    async def notify(self, title: str, body: str) -> None:
        pass


def test_connector_pool_covers_html_and_image_limits():
    async def scenario():
        async with HttpClient(
            _Auth(), _Notifier(), concurrency=16, img_concurrency=6
        ) as http:
            connector = http._session.connector
            assert connector.limit == 22
            assert connector.limit_per_host == 22

    asyncio.run(scenario())


def test_default_concurrency_is_16():
    assert HttpClient(_Auth(), _Notifier()).concurrency == 16
