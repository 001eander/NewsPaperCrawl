"""
Playwright 运行时测试
====================

不启动真实浏览器:
  - driver 定位:Playwright 自身结果 / 冻结布局回退 / 找不到报错
  - browser_installed 依据 executable_path 是否存在
  - ensure_browser 运行 `install chromium`,透传 PLAYWRIGHT_BROWSERS_PATH,
    流式回调安装输出,并支持 cancel_event 终止安装进程
"""

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from crawler.adapters import playwright_runtime as runtime


class _FakeChromium:
    def __init__(self, executable: Path):
        self.executable_path = str(executable)


class _FakePlaywright:
    def __init__(self, executable: Path):
        self.chromium = _FakeChromium(executable)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeStdout:
    def __init__(self, lines: list[str], on_iter=None):
        self._lines = lines
        self._on_iter = on_iter
        self.closed = False

    def __iter__(self):
        if self._on_iter is not None:
            self._on_iter()
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


def _popen_factory(lines: list[str], exitcode: int, on_iter=None):
    instances: list = []

    class _FakePopen:
        def __init__(self, command, **kwargs):
            self.command = command
            self.kwargs = kwargs
            self.stdout = _FakeStdout(lines, on_iter)
            self.exitcode = exitcode
            self.terminated = False
            self.killed = False
            self.wait_timeout = None
            instances.append(self)

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None):
            self.wait_timeout = timeout
            return self.exitcode

    return _FakePopen, instances


def _sequence(*values):
    iterator = iter(values)

    def next_value():
        return next(iterator)

    return next_value


# ---------------------------------------------------------------- driver 定位


def test_driver_command_uses_playwright_result(monkeypatch, tmp_path):
    node = tmp_path / "node"
    cli = tmp_path / "cli.js"
    node.write_text("", encoding="utf-8")
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        runtime, "_compute_driver_executable", lambda: (str(node), str(cli))
    )

    assert runtime.driver_command() == (str(node), str(cli))


def test_driver_command_falls_back_to_frozen_layout(monkeypatch, tmp_path):
    driver = tmp_path / "playwright" / "driver"
    (driver / "package").mkdir(parents=True)
    node_name = "node.exe" if sys.platform == "win32" else "node"
    node = driver / node_name
    cli = driver / "package" / "cli.js"
    node.write_text("", encoding="utf-8")
    cli.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    def broken():
        raise RuntimeError("not bundled as expected")

    monkeypatch.setattr(runtime, "_compute_driver_executable", broken)
    monkeypatch.delenv("PLAYWRIGHT_NODEJS_PATH", raising=False)

    assert runtime.driver_command() == (str(node), str(cli))


def test_driver_command_missing_raises(monkeypatch, tmp_path):
    def broken():
        raise RuntimeError("no driver")

    monkeypatch.setattr(runtime, "_compute_driver_executable", broken)
    monkeypatch.setattr(
        runtime, "_driver_candidates", lambda: [tmp_path / "missing-driver"]
    )
    monkeypatch.delenv("PLAYWRIGHT_NODEJS_PATH", raising=False)

    with pytest.raises(runtime.DriverNotFoundError):
        runtime.driver_command()


# ---------------------------------------------------------------- 浏览器检测


def test_browser_installed_true(monkeypatch, tmp_path):
    executable = tmp_path / "Chromium"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime, "sync_playwright", lambda: _FakePlaywright(executable))

    assert runtime.browser_installed() is True


def test_browser_installed_false_when_executable_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime,
        "sync_playwright",
        lambda: _FakePlaywright(tmp_path / "not-installed"),
    )

    assert runtime.browser_installed() is False


def test_browser_installed_false_on_driver_error(monkeypatch):
    def broken():
        raise RuntimeError("driver failed")

    monkeypatch.setattr(runtime, "sync_playwright", broken)

    assert runtime.browser_installed() is False


# ---------------------------------------------------------------- 安装


def test_ensure_browser_skips_when_installed(monkeypatch):
    monkeypatch.setattr(runtime, "browser_installed", lambda: True)

    def unexpected(*args, **kwargs):
        pytest.fail("已安装时不应启动安装进程")

    monkeypatch.setattr(runtime.subprocess, "Popen", unexpected)

    assert runtime.ensure_browser() is True


def test_ensure_browser_installs_and_streams_output(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "browser_installed", _sequence(False, True))
    monkeypatch.setattr(runtime, "driver_command", lambda: ("/node", "/cli.js"))
    browsers_path = tmp_path / "browsers"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path))
    fake_popen, instances = _popen_factory(["downloading", "done"], 0)
    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)

    lines: list[str] = []
    assert runtime.ensure_browser(lines.append) is True

    assert lines == ["downloading", "done"]
    assert len(instances) == 1
    process = instances[0]
    assert process.command == ["/node", "/cli.js", "install", "chromium"]
    assert process.kwargs["stdout"] is subprocess.PIPE
    assert process.kwargs["stderr"] is subprocess.STDOUT
    assert process.kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers_path)
    assert process.stdout.closed is True


def test_ensure_browser_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(runtime, "browser_installed", _sequence(False))
    monkeypatch.setattr(runtime, "driver_command", lambda: ("/node", "/cli.js"))
    fake_popen, _ = _popen_factory(["boom"], 1)
    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)

    with pytest.raises(runtime.BrowserInstallError, match="退出码 1"):
        runtime.ensure_browser()


def test_ensure_browser_missing_after_install_raises(monkeypatch):
    monkeypatch.setattr(runtime, "browser_installed", _sequence(False, False))
    monkeypatch.setattr(runtime, "driver_command", lambda: ("/node", "/cli.js"))
    fake_popen, _ = _popen_factory(["done"], 0)
    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)

    with pytest.raises(runtime.BrowserInstallError, match="仍未检测到"):
        runtime.ensure_browser()


def test_ensure_browser_cancel_before_start(monkeypatch):
    event = threading.Event()
    event.set()

    def unexpected():
        pytest.fail("已取消时不应检测浏览器")

    monkeypatch.setattr(runtime, "browser_installed", unexpected)

    assert runtime.ensure_browser(cancel_event=event) is False


def test_ensure_browser_cancel_during_install_terminates(monkeypatch):
    event = threading.Event()
    monkeypatch.setattr(runtime, "browser_installed", _sequence(False))
    monkeypatch.setattr(runtime, "driver_command", lambda: ("/node", "/cli.js"))

    def set_cancel():
        event.set()

    fake_popen, instances = _popen_factory(["installing"], -15, on_iter=set_cancel)
    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)

    assert runtime.ensure_browser(cancel_event=event) is False

    process = instances[0]
    assert process.terminated is True
    assert process.wait_timeout == 30
    assert process.stdout.closed is True


def test_ensure_browser_popen_failure_raises(monkeypatch):
    monkeypatch.setattr(runtime, "browser_installed", lambda: False)
    monkeypatch.setattr(runtime, "driver_command", lambda: ("/node", "/cli.js"))

    def broken_popen(*args, **kwargs):
        raise OSError("cannot execute")

    monkeypatch.setattr(runtime.subprocess, "Popen", broken_popen)

    with pytest.raises(runtime.BrowserInstallError, match="无法启动"):
        runtime.ensure_browser()
