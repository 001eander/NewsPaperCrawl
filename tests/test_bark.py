import asyncio
import json
import ssl

import aiohttp

from crawler.adapters.bark import BarkNotifier


class _Response:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def get(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.response


def _notifier(tmp_path):
    path = tmp_path / "bark.json"
    path.write_text(
        json.dumps({"device_key": "test-key", "bark_server": "https://example.test"}),
        encoding="utf-8",
    )
    return BarkNotifier(path)


def test_notify_uses_bundled_ca_and_returns_success(tmp_path, monkeypatch):
    captured = {}
    session = _Session(response=_Response(200))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(
        aiohttp,
        "TCPConnector",
        lambda **kwargs: captured.setdefault("connector", kwargs),
    )

    sent = asyncio.run(_notifier(tmp_path).notify("title", "body"))

    assert sent is True
    context = captured["connector"]["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs()


def test_notify_returns_false_for_http_error(tmp_path, monkeypatch):
    session = _Session(response=_Response(500))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    assert asyncio.run(_notifier(tmp_path).notify("title")) is False


def test_notify_returns_false_for_network_error(tmp_path, monkeypatch):
    session = _Session(error=OSError("offline"))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: session)
    assert asyncio.run(_notifier(tmp_path).notify("title")) is False
