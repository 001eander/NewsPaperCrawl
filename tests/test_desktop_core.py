"""
桌面 sidecar 协议测试
====================

用注入的 stdin/stdout、临时 Settings、stub run_stage 与 fake multiprocessing
context 覆盖:

  - JSON Lines 协议、错误响应、state/status/failed
  - run 参数校验、单操作互斥、operation_done 的四种结果
  - cancel:爬取 task.cancel;登录子进程 terminate
  - 配置读写(密码/cookie 绝不进日志)与 Bark 测试
  - serve() 的 shutdown/EOF 收尾

不触网、不启动真实浏览器、不落真实数据库。
"""

import asyncio
import io
import json
import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from crawler.adapters.config import Settings
from crawler.adapters.instance_lock import AlreadyRunningError
from crawler.desktop_core import (
    DesktopCore,
    _configure_stdio_utf8,
    _login_child_entry,
    configure_event_logging,
)
from crawler.domain.exceptions import AuthError

SECRET = "test-only-secret"
USERNAME = "reader@example.com"


# ---------------------------------------------------------------- 测试辅助


def _settings(tmp_path: Path) -> Settings:
    secrets = tmp_path / ".secrets"
    return Settings(
        data_dir=tmp_path,
        log_dir=tmp_path / "logs",
        log_interval=10,
        concurrency=8,
        secrets_dir=secrets,
        db_path=tmp_path / "crawl.db",
        paperlist_csv=tmp_path / "报纸清单.csv",
    )


async def _noop_run(stage: str, **kwargs) -> None:
    return None


def _core(tmp_path: Path, **kwargs) -> tuple[DesktopCore, io.StringIO]:
    out = io.StringIO()
    kwargs.setdefault("run_stage", _noop_run)
    core = DesktopCore(
        stdin=io.StringIO(),
        stdout=out,
        settings_factory=lambda: _settings(tmp_path),
        **kwargs,
    )
    return core, out


async def _send(core: DesktopCore, request: dict) -> None:
    await core.handle_line(json.dumps(request, ensure_ascii=False))


def _frames(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _responses(out: io.StringIO) -> list[dict]:
    return [frame for frame in _frames(out) if "id" in frame]


def _operation_done(out: io.StringIO) -> dict:
    events = [f for f in _frames(out) if f.get("type") == "operation_done"]
    assert len(events) == 1
    return events[0]


def _make_db(settings: Settings) -> None:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE papers (paperid TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE issues (
                paperid TEXT, date TEXT, status TEXT, error TEXT
            );
            CREATE TABLE boards (
                metaid TEXT, paperid TEXT, date TEXT, status TEXT, error TEXT
            );
            CREATE TABLE articles (
                metaid TEXT, paperid TEXT, date TEXT, status TEXT, error TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO papers (paperid, name) VALUES (?, ?)",
            [("n.D1", "报纸一"), ("n.D2", "报纸二")],
        )
        conn.executemany(
            "INSERT INTO issues (paperid, date, status, error) VALUES (?, ?, ?, ?)",
            [
                ("n.D1", "2026-01-02", "done", None),
                ("n.D1", "2026-01-01", "failed", "第一行\n第二行"),
                ("n.D2", "2026-01-01", "processing", None),
            ],
        )
        conn.execute(
            "INSERT INTO boards (metaid, paperid, date, status, error) "
            "VALUES ('nb.D1_20260102_A1', 'n.D1', '2026-01-02', 'done', NULL)"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 协议基础


def test_state_reports_version_and_idle(tmp_path):
    core, out = _core(tmp_path)
    asyncio.run(_send(core, {"id": "1", "command": "state"}))

    response = _responses(out)[0]
    assert response["id"] == "1"
    assert response["ok"] is True
    assert isinstance(response["result"]["version"], str)
    assert response["result"]["operation"] is None
    assert response["result"]["stage"] is None


def test_invalid_json_and_non_object(tmp_path):
    core, out = _core(tmp_path)
    asyncio.run(core.handle_line("not json"))
    asyncio.run(core.handle_line("[]"))

    assert _responses(out) == [
        {"id": None, "ok": False, "error": "无效的 JSON 请求"},
        {"id": None, "ok": False, "error": "请求必须是 JSON 对象"},
    ]


def test_unknown_command(tmp_path):
    core, out = _core(tmp_path)
    asyncio.run(_send(core, {"id": "x", "command": "nope"}))

    response = _responses(out)[0]
    assert response["ok"] is False
    assert "未知命令" in response["error"]


def test_status_and_failed(tmp_path):
    settings = _settings(tmp_path)
    _make_db(settings)
    core, out = _core(tmp_path)

    asyncio.run(_send(core, {"id": "1", "command": "status"}))
    asyncio.run(_send(core, {"id": "2", "command": "failed", "limit": 5}))

    status = _responses(out)[0]["result"]
    overview = {row["stage"]: row for row in status["overview"]}
    assert overview["issues"] == {
        "stage": "issues",
        "exists": True,
        "total": 3,
        "pending": 1,
        "done": 1,
        "failed": 1,
    }
    assert overview["boards"]["done"] == 1
    assert overview["articles"]["total"] == 0
    assert status["per_paper"] == [
        {
            "paperid": "n.D1",
            "name": "报纸一",
            "issues": "1/2",
            "boards": "1/1",
            "articles": "--",
        },
        {
            "paperid": "n.D2",
            "name": "报纸二",
            "issues": "0/1",
            "boards": "--",
            "articles": "--",
        },
    ]

    failed = _responses(out)[1]["result"]["failed"]
    assert failed == {"issues": [["n.D1", "2026-01-01", "第一行 第二行"]]}


def test_status_before_database_exists(tmp_path):
    core, out = _core(tmp_path)
    asyncio.run(_send(core, {"id": "1", "command": "status"}))

    assert _responses(out)[0]["result"] == {"overview": [], "per_paper": []}


def test_failed_limit_must_be_positive(tmp_path):
    core, out = _core(tmp_path)
    asyncio.run(_send(core, {"id": "1", "command": "failed", "limit": 0}))

    response = _responses(out)[0]
    assert response["ok"] is False
    assert "limit" in response["error"]


# ---------------------------------------------------------------- run 参数


def test_run_rejects_bad_stage_and_params(tmp_path):
    cases = [
        {"stage": "unknown", "params": {}},
        {"stage": "boards", "params": []},
        {"stage": "issues", "params": {}},
        {"stage": "boards", "params": {"nope": 1}},
        {"stage": "boards", "params": {"limit": 0}},
        {"stage": "boards", "params": {"limit": True}},
        {"stage": "articles", "params": {"concurrency": 1.5}},
        {"stage": "boards", "params": {"paperid": 3}},
    ]
    for index, payload in enumerate(cases):
        core, out = _core(tmp_path)
        request = {"id": str(index), "command": "run", **payload}
        asyncio.run(_send(core, request))
        response = _responses(out)[0]
        assert response["ok"] is False, payload
        assert response["error"], payload


def test_run_validates_issues_years(tmp_path):
    core, out = _core(tmp_path)
    asyncio.run(
        _send(
            core,
            {
                "id": "1",
                "command": "run",
                "stage": "issues",
                "params": {"start_year": 2014},
            },
        )
    )
    assert "end_year" in _responses(out)[0]["error"]


def test_run_import_omits_empty_csv(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def fake_run(stage: str, **kwargs) -> None:
        calls.append((stage, kwargs))

    async def scenario():
        core, out = _core(tmp_path, run_stage=fake_run)
        await _send(
            core,
            {"id": "1", "command": "run", "stage": "import", "params": {"csv": ""}},
        )
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    assert calls == [("import", {})]
    assert _operation_done(out)["outcome"] == "done"


# ---------------------------------------------------------------- run 生命周期


def test_run_started_then_operation_done(tmp_path):
    calls: list[tuple[str, dict]] = []

    async def fake_run(stage: str, **kwargs) -> None:
        calls.append((stage, kwargs))

    async def scenario():
        core, out = _core(tmp_path, run_stage=fake_run)
        await _send(
            core,
            {
                "id": "1",
                "command": "run",
                "stage": "boards",
                "params": {"limit": 2, "concurrency": 3},
            },
        )
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    assert _responses(out) == [
        {
            "id": "1",
            "ok": True,
            "result": {"started": True, "operation": "run", "stage": "boards"},
        }
    ]
    assert _operation_done(out) == {
        "type": "operation_done",
        "operation": "run",
        "outcome": "done",
        "message": "",
    }
    assert calls == [("boards", {"limit": 2, "concurrency": 3})]


@pytest.mark.parametrize(
    ("exc_type", "outcome"),
    [
        (AlreadyRunningError, "busy"),
        (AuthError, "auth"),
        (ValueError, "error"),
    ],
)
def test_run_outcomes(tmp_path, exc_type, outcome):
    async def failing_run(stage: str, **kwargs) -> None:
        raise exc_type("boom")

    async def scenario():
        core, out = _core(tmp_path, run_stage=failing_run)
        await _send(core, {"id": "1", "command": "run", "stage": "articles"})
        await core.wait_idle()
        return out

    event = _operation_done(asyncio.run(scenario()))
    assert event["outcome"] == outcome
    assert event["message"] == "boom"


def test_only_one_operation_and_state_reports_it(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(stage: str, **kwargs) -> None:
        started.set()
        await release.wait()

    async def scenario():
        core, out = _core(tmp_path, run_stage=slow_run)
        await _send(core, {"id": "1", "command": "run", "stage": "boards"})
        await started.wait()
        await _send(core, {"id": "2", "command": "state"})
        await _send(core, {"id": "3", "command": "run", "stage": "articles"})
        await _send(core, {"id": "4", "command": "cancel"})
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    responses = _responses(out)
    assert responses[1]["result"]["operation"] == "run"
    assert responses[1]["result"]["stage"] == "boards"
    assert responses[2]["ok"] is False
    assert "已有操作" in responses[2]["error"]
    assert responses[3]["result"] == {"stopping": True, "operation": "run"}
    assert _operation_done(out)["outcome"] == "stopped"


def test_cancel_without_operation(tmp_path):
    core, out = _core(tmp_path)
    asyncio.run(_send(core, {"id": "1", "command": "cancel"}))

    assert _responses(out)[0]["result"] == {
        "stopping": False,
        "operation": None,
    }


# ---------------------------------------------------------------- 登录


def test_login_installs_browser_then_runs(tmp_path):
    seen: dict = {}

    def fake_installed() -> bool:
        return False

    def fake_ensure(*, output, cancel_event) -> bool:
        seen["cancel_event"] = cancel_event
        output("downloading chromium")
        return True

    async def fake_login(credentials, cookies, headless, op) -> int:
        seen["login"] = (credentials, cookies, headless)
        return 0

    async def scenario():
        core, out = _core(
            tmp_path,
            browser_installed_func=fake_installed,
            ensure_browser_func=fake_ensure,
            login_runner=fake_login,
        )
        await _send(core, {"id": "1", "command": "login"})
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    assert _responses(out)[0]["result"] == {
        "started": True,
        "operation": "login",
    }
    logs = [f["message"] for f in _frames(out) if f.get("type") == "log"]
    assert any("downloading chromium" in message for message in logs)
    assert _operation_done(out)["outcome"] == "done"
    settings = _settings(tmp_path)
    assert seen["login"] == (
        settings.secrets_dir / "credentials.json",
        settings.secrets_dir / "cookies.json",
        False,
    )
    assert seen["cancel_event"] is not None


def test_login_skips_install_when_browser_present(tmp_path):
    called = {"ensure": False}

    def fake_ensure(**kwargs) -> bool:
        called["ensure"] = True
        return False

    async def fake_login(credentials, cookies, headless, op) -> int:
        return 0

    async def scenario():
        core, out = _core(
            tmp_path,
            browser_installed_func=lambda: True,
            ensure_browser_func=fake_ensure,
            login_runner=fake_login,
        )
        await _send(core, {"id": "1", "command": "login"})
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    assert called["ensure"] is False
    assert _operation_done(out)["outcome"] == "done"


def test_login_browser_install_failure(tmp_path):
    login_called = {"value": False}

    async def fake_login(credentials, cookies, headless, op) -> int:
        login_called["value"] = True
        return 0

    async def scenario():
        core, out = _core(
            tmp_path,
            browser_installed_func=lambda: False,
            ensure_browser_func=lambda **kwargs: False,
            login_runner=fake_login,
        )
        await _send(core, {"id": "1", "command": "login"})
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    event = _operation_done(out)
    assert event["outcome"] == "error"
    assert "Chromium" in event["message"]
    assert login_called["value"] is False


def test_login_nonzero_exit(tmp_path):
    async def fake_login(credentials, cookies, headless, op) -> int:
        return 2

    async def scenario():
        core, out = _core(
            tmp_path,
            browser_installed_func=lambda: True,
            login_runner=fake_login,
        )
        await _send(core, {"id": "1", "command": "login"})
        await core.wait_idle()
        return out

    event = _operation_done(asyncio.run(scenario()))
    assert event["outcome"] == "error"
    assert "退出码 2" in event["message"]


def test_login_uses_detailed_error_from_runner(tmp_path):
    async def fake_login(credentials, cookies, headless, op) -> int:
        op.error = "读者账号或密码错误"
        return 1

    async def scenario():
        core, out = _core(
            tmp_path,
            browser_installed_func=lambda: True,
            login_runner=fake_login,
        )
        await _send(core, {"id": "1", "command": "login"})
        await core.wait_idle()
        return out

    event = _operation_done(asyncio.run(scenario()))
    assert event["outcome"] == "error"
    assert event["message"] == "读者账号或密码错误"


def test_login_child_reports_system_exit_message(monkeypatch):
    def fail_login(*args, **kwargs):
        raise SystemExit("验证码错误")

    monkeypatch.setattr("crawler.desktop_core.run_login", fail_login)
    channel = _FakeConnection()
    with pytest.raises(SystemExit):
        _login_child_entry("credentials", "cookies", False, channel)
    assert channel.messages == ["验证码错误"]


def test_login_cancel_terminates_runner(tmp_path):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_login(credentials, cookies, headless, op) -> int:
        op.terminate = stopped.set
        started.set()
        await stopped.wait()
        return -15

    async def scenario():
        core, out = _core(
            tmp_path,
            browser_installed_func=lambda: True,
            login_runner=fake_login,
        )
        await _send(core, {"id": "1", "command": "login"})
        await started.wait()
        await _send(core, {"id": "2", "command": "cancel"})
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    assert _responses(out)[1]["result"] == {
        "stopping": True,
        "operation": "login",
    }
    assert _operation_done(out)["outcome"] == "stopped"


class _FakeProcess:
    """替代 multiprocessing.Process:不真正 spawn,join 等待 terminate"""

    def __init__(self, *, target, args, name, blocking: bool):
        self.target = target
        self.args = args
        self.name = name
        self.exitcode: int | None = None
        self.terminated = False
        self.done = threading.Event()
        if not blocking:
            self.exitcode = 0
            self.done.set()

    def start(self) -> None:
        return None

    def join(self) -> None:
        self.done.wait(timeout=2)

    def terminate(self) -> None:
        self.terminated = True
        self.exitcode = -15
        self.done.set()

    def kill(self) -> None:
        self.exitcode = -9
        self.done.set()


class _FakeConnection:
    def __init__(self, *, eof: bool = False):
        self.messages = []
        self.eof = eof

    def send(self, message):
        self.messages.append(message)

    def poll(self):
        return self.eof or bool(self.messages)

    def recv(self):
        if self.eof:
            raise EOFError
        return self.messages.pop(0)

    def close(self):
        return None


class _FakeMPContext:
    def __init__(self, blocking: bool = False, eof: bool = False):
        self.blocking = blocking
        self.eof = eof
        self.processes: list[_FakeProcess] = []

    def Pipe(self, *, duplex):
        return _FakeConnection(eof=self.eof), _FakeConnection()

    def Process(self, *, target, args, name):
        process = _FakeProcess(
            target=target, args=args, name=name, blocking=self.blocking
        )
        self.processes.append(process)
        return process


def test_login_uses_spawn_context(tmp_path):
    context = _FakeMPContext()

    async def scenario():
        core, out = _core(
            tmp_path,
            mp_context=context,
            browser_installed_func=lambda: True,
        )
        await _send(core, {"id": "1", "command": "login"})
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    assert _operation_done(out)["outcome"] == "done"
    assert len(context.processes) == 1
    process = context.processes[0]
    assert process.target.__name__ == "_login_child_entry"
    settings = _settings(tmp_path)
    assert process.args[:3] == (
        str(settings.secrets_dir / "credentials.json"),
        str(settings.secrets_dir / "cookies.json"),
        False,
    )
    assert isinstance(process.args[3], _FakeConnection)
    assert process.name == "newspapercrawl-login"


def test_login_success_tolerates_closed_error_channel(tmp_path):
    context = _FakeMPContext(eof=True)

    async def scenario():
        core, out = _core(
            tmp_path,
            mp_context=context,
            browser_installed_func=lambda: True,
        )
        await _send(core, {"id": "1", "command": "login"})
        await core.wait_idle()
        return out

    event = _operation_done(asyncio.run(scenario()))
    assert event["outcome"] == "done"
    assert event["message"] == "登录完成"


def test_login_cancel_terminates_real_process_path(tmp_path):
    context = _FakeMPContext(blocking=True)

    async def scenario():
        core, out = _core(
            tmp_path,
            mp_context=context,
            browser_installed_func=lambda: True,
        )
        await _send(core, {"id": "1", "command": "login"})
        for _ in range(100):
            await asyncio.sleep(0.001)
            if context.processes:
                break
        await _send(core, {"id": "2", "command": "cancel"})
        await core.wait_idle()
        return out

    out = asyncio.run(scenario())
    assert context.processes[0].terminated is True
    assert _operation_done(out)["outcome"] == "stopped"


# ---------------------------------------------------------------- 配置 / Bark


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_config_roundtrip_and_no_secret_logs(tmp_path):
    core, out = _core(tmp_path)
    handler = configure_event_logging(core.emit_log)
    try:

        async def scenario():
            await _send(
                core,
                {
                    "id": "1",
                    "command": "save_config",
                    "kind": "credentials",
                    "values": {"username": USERNAME, "password": SECRET},
                },
            )
            await _send(core, {"id": "2", "command": "load_config"})
            await _send(
                core,
                {
                    "id": "3",
                    "command": "save_config",
                    "kind": "bark",
                    "values": {"device_key": "bark-key", "bark_server": ""},
                },
            )
            await _send(core, {"id": "4", "command": "load_config"})

        asyncio.run(scenario())
    finally:
        logging.getLogger().removeHandler(handler)

    responses = {response["id"]: response for response in _responses(out)}
    assert responses["1"]["result"] == {"saved": True, "kind": "credentials"}
    assert responses["2"]["result"]["credentials"] == {
        "username": USERNAME,
        "password": SECRET,
    }
    assert responses["3"]["result"] == {"saved": True, "kind": "bark"}
    assert responses["4"]["result"]["bark"] == {
        "device_key": "bark-key",
        "bark_server": "https://api.day.app",
    }

    settings = _settings(tmp_path)
    saved_credentials = json.loads(
        (settings.secrets_dir / "credentials.json").read_text(encoding="utf-8")
    )
    assert saved_credentials["username"] == USERNAME
    assert saved_credentials["password"] == SECRET
    saved_bark = json.loads(
        (settings.secrets_dir / "bark.json").read_text(encoding="utf-8")
    )
    assert saved_bark["device_key"] == "bark-key"

    log_text = "\n".join(
        frame.get("message", "") for frame in _frames(out) if frame.get("type") == "log"
    )
    assert SECRET not in log_text
    assert "bark-key" not in log_text


def test_load_config_cleans_placeholders_and_bad_json(tmp_path):
    settings = _settings(tmp_path)
    _write(
        settings.secrets_dir / "credentials.json",
        {"username": "在此填入账号", "password": "在此填入密码"},
    )
    (settings.secrets_dir / "bark.json").write_text("{broken", encoding="utf-8")
    core, out = _core(tmp_path)

    asyncio.run(_send(core, {"id": "1", "command": "load_config"}))

    result = _responses(out)[0]["result"]
    assert result["credentials"] == {"username": "", "password": ""}
    assert result["bark"] == {
        "device_key": "",
        "bark_server": "https://api.day.app",
    }


def test_save_config_rejects_invalid_values(tmp_path):
    cases = [
        {"kind": "unknown", "values": {}},
        {"kind": "credentials", "values": []},
        {"kind": "credentials", "values": {"username": "", "password": "x"}},
        {"kind": "credentials", "values": {"username": 1, "password": "x"}},
        {"kind": "bark", "values": {"device_key": 1}},
    ]
    for index, payload in enumerate(cases):
        core, out = _core(tmp_path)
        asyncio.run(
            _send(core, {"id": str(index), "command": "save_config", **payload})
        )
        response = _responses(out)[0]
        assert response["ok"] is False, payload


class _FileNotifier:
    """按需读取 bark.json 的假 notifier,验证 test_bark 用刚保存的配置"""

    def __init__(self, path: Path):
        self.path = path
        data = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        self.is_configured = bool(data.get("device_key"))
        self.calls: list[tuple[str, str]] = []

    async def notify(self, title: str, body: str = "") -> bool:
        self.calls.append((title, body))
        return self.is_configured


def test_bark_test_uses_saved_config(tmp_path):
    notifiers: list[_FileNotifier] = []

    def factory(path: Path) -> _FileNotifier:
        notifier = _FileNotifier(path)
        notifiers.append(notifier)
        return notifier

    async def scenario():
        core, out = _core(tmp_path, notifier_factory=factory)
        await _send(
            core,
            {
                "id": "1",
                "command": "save_config",
                "kind": "bark",
                "values": {"device_key": "key-1"},
            },
        )
        await _send(core, {"id": "2", "command": "test_bark"})
        return out

    out = asyncio.run(scenario())
    assert _responses(out)[1]["result"] == {
        "configured": True,
        "sent": True,
        "message": "",
    }
    assert len(notifiers) == 1
    assert notifiers[0].path == _settings(tmp_path).secrets_dir / "bark.json"
    assert notifiers[0].calls == [("浙图爬虫测试", "Bark 配置生效")]


def test_bark_test_without_key(tmp_path):
    async def scenario():
        core, out = _core(tmp_path, notifier_factory=lambda path: _FileNotifier(path))
        await _send(core, {"id": "1", "command": "test_bark"})
        return out

    out = asyncio.run(scenario())
    assert _responses(out)[0]["result"] == {
        "configured": False,
        "sent": False,
        "message": "device key 未配置",
    }


# ---------------------------------------------------------------- 日志 / serve


def test_stdio_is_reconfigured_from_windows_gbk_to_utf8(monkeypatch):
    streams = {
        name: io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        for name in ("stdin", "stdout", "stderr")
    }
    for name, stream in streams.items():
        monkeypatch.setattr(f"sys.{name}", stream)

    _configure_stdio_utf8()

    assert all(stream.encoding == "utf-8" for stream in streams.values())
    streams["stdout"].write("sidecar 启动,版本 0.2.3\n")
    streams["stderr"].write("已保存 30 个 cookie\n")
    streams["stdout"].flush()
    streams["stderr"].flush()
    assert streams["stdout"].buffer.getvalue().decode("utf-8") == (
        "sidecar 启动,版本 0.2.3\n"
    )
    assert streams["stderr"].buffer.getvalue().decode("utf-8") == (
        "已保存 30 个 cookie\n"
    )


def test_event_log_handler_emits_log_frames(tmp_path):
    core, out = _core(tmp_path)
    handler = configure_event_logging(core.emit_log)
    try:
        logging.getLogger("test.sidecar").info("你好 sidecar")
    finally:
        logging.getLogger().removeHandler(handler)

    logs = [frame for frame in _frames(out) if frame.get("type") == "log"]
    assert any("你好 sidecar" in frame["message"] for frame in logs)


def test_serve_processes_requests_and_stops_on_shutdown(tmp_path):
    stdin = io.StringIO(
        '{"id":"1","command":"state"}\n'
        '{"id":"2","command":"shutdown"}\n'
        '{"id":"3","command":"state"}\n'
    )
    out = io.StringIO()
    core = DesktopCore(
        stdin=stdin,
        stdout=out,
        settings_factory=lambda: _settings(tmp_path),
        run_stage=_noop_run,
    )

    asyncio.run(core.serve())

    responses = _responses(out)
    assert [response["id"] for response in responses] == ["1", "2"]
    assert responses[1]["result"] == {"shutdown": True}


def test_shutdown_response_waits_for_operation_cleanup(tmp_path):
    cleaned = False

    async def slow_run(stage: str, **kwargs) -> None:
        nonlocal cleaned
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0)
            cleaned = True

    async def scenario():
        core, out = _core(tmp_path, run_stage=slow_run)
        await _send(core, {"id": "1", "command": "run", "stage": "boards"})
        await asyncio.sleep(0)
        await _send(core, {"id": "2", "command": "shutdown"})
        return out

    out = asyncio.run(scenario())
    assert cleaned is True
    frames = _frames(out)
    done_index = next(
        index
        for index, frame in enumerate(frames)
        if frame.get("type") == "operation_done"
    )
    shutdown_index = next(
        index for index, frame in enumerate(frames) if frame.get("id") == "2"
    )
    assert done_index < shutdown_index
