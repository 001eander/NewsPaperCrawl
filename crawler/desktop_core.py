"""
桌面 sidecar
============

Tauri 启动的常驻 Python 进程,通过 stdin/stdout 的 JSON Lines 与前端通信。
本模块只做协议与操作编排,爬取仍复用 crawler.bootstrap.run_stage。

协议
----
请求::

    {"id": "1", "command": "run", "stage": "boards", "params": {"limit": 10}}

响应::

    {"id": "1", "ok": true, "result": {...}}
    {"id": "1", "ok": false, "error": "..."}

非响应事件::

    {"type": "log", "message": "..."}
    {"type": "operation_done", "operation": "run", "outcome": "done",
     "message": "..."}

outcome 为 done / stopped / auth / error / busy。run/login 在启动后立即
响应 started,结束时发 operation_done;status/failed/config 等直接返回结果。
同一时间只允许一个 run/login。

stdout 只写协议帧:main() 会把 sys.stdout 重定向到 stderr,所有第三方 print
落到 stderr;协议帧使用重定向前保存的原始 stdout。日志另由 EventLogHandler
转成 log 事件,不经过 stdout。
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import sys
import threading
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, TextIO

# PyInstaller 以脚本方式运行本文件(不是 `python -m crawler.desktop_core`),
# 此时 __package__ 为空,先把项目根加入 sys.path 并改用绝对导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crawler
from crawler.adapters.bark import BarkNotifier
from crawler.adapters.config import Settings
from crawler.adapters.instance_lock import AlreadyRunningError
from crawler.adapters.playwright_runtime import (
    BrowserInstallError,
    browser_installed,
    ensure_browser,
)
from crawler.adapters.status_queries import StatusQueries
from crawler.bootstrap import run_stage as default_run_stage
from crawler.domain.exceptions import AuthError
from scripts.login import run_login

logger = logging.getLogger(__name__)

RUN_STAGES = ("import", "issues", "boards", "articles")
_LOG_FORMAT = "%(asctime)s %(levelname)s: %(message)s"
_LOG_DATE_FORMAT = "%H:%M:%S"

_RUN_PARAMS: dict[str, frozenset[str]] = {
    "import": frozenset({"csv"}),
    "issues": frozenset(
        {"start_year", "end_year", "start_month", "end_month", "concurrency"}
    ),
    "boards": frozenset({"paperid", "limit", "concurrency"}),
    "articles": frozenset({"limit", "concurrency"}),
}


class ProtocolError(Exception):
    """请求不符合协议或参数校验失败"""


@lru_cache(maxsize=1)
def project_version() -> str:
    """优先读源码树 pyproject,冻结环境回落到安装元数据"""
    if not getattr(sys, "frozen", False):
        try:
            data = tomllib.loads(
                (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
                    encoding="utf-8"
                )
            )
            return str(data["project"]["version"])
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
            pass
    try:
        return package_version("newspapercrawl-raw")
    except PackageNotFoundError:
        return getattr(crawler, "__version__", "0.0.0")


class EventLogHandler(logging.Handler):
    """把 root logger 的记录转成 sidecar 的 log 事件(绝不写 stdout)"""

    def __init__(self, emit: Callable[[str], None]):
        super().__init__()
        self._emit = emit

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit(self.format(record))
        except Exception:  # noqa: BLE001
            # 管道断开时静默,避免错误日志递归
            return


def configure_event_logging(emit: Callable[[str], None]) -> EventLogHandler:
    """给 root logger 挂仅发事件的 handler(调用方负责之后移除)"""
    handler = EventLogHandler(emit)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _LOG_DATE_FORMAT))
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    return handler


@dataclass
class _Operation:
    """正在运行的 run/login;同一时刻最多一个"""

    kind: str
    stage: str | None = None
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None
    process: Any = None
    terminate: Callable[[], None] | None = None


LoginRunner = Callable[[Path, Path, bool, _Operation], Awaitable[int]]


def _login_child_entry(
    credentials_path: str, cookies_path: str, headless: bool
) -> None:
    """
    multiprocessing spawn 的登录入口。

    子进程继承 sidecar 的 stdout,但协议通道只能由父进程写,因此先把
    print 重定向到 stderr,再调用 scripts/login.py 的 run_login。
    """
    if sys.stderr is not None:
        sys.stdout = sys.stderr
    run_login(Path(credentials_path), Path(cookies_path), headless=headless)


class DesktopCore:
    """JSON Lines 协议与操作生命周期"""

    def __init__(
        self,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        settings_factory: Callable[[], Settings] = Settings.from_env,
        run_stage: Callable[..., Awaitable[None]] = default_run_stage,
        browser_installed_func: Callable[[], bool] = browser_installed,
        ensure_browser_func: Callable[..., bool] = ensure_browser,
        notifier_factory: Callable[[Path], Any] = BarkNotifier,
        login_runner: LoginRunner | None = None,
        mp_context: Any | None = None,
    ) -> None:
        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._settings_factory = settings_factory
        self._run_stage = run_stage
        self._browser_installed = browser_installed_func
        self._ensure_browser = ensure_browser_func
        self._notifier_factory = notifier_factory
        self._login_runner = login_runner or self._run_login_process
        self._mp_context = mp_context or multiprocessing.get_context("spawn")
        self._operation: _Operation | None = None
        self._shutdown_requested = False
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------ 协议 I/O

    def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                self._stdout.write(line)
                self._stdout.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._shutdown_requested = True

    def _respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"id": request_id, "ok": True, "result": result})

    def _error(self, request_id: Any, message: str) -> None:
        self._send({"id": request_id, "ok": False, "error": message})

    def emit_log(self, message: str) -> None:
        """发送 log 事件(线程安全)"""
        self._send({"type": "log", "message": message})

    async def handle_line(self, line: str) -> None:
        """处理一行输入,并写回响应帧"""
        line = line.strip()
        if not line:
            return
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            self._error(None, "无效的 JSON 请求")
            return
        if not isinstance(request, dict):
            self._error(None, "请求必须是 JSON 对象")
            return

        request_id = request.get("id")
        try:
            result = await self._dispatch(request)
        except ProtocolError as e:
            self._error(request_id, str(e))
        except Exception as e:  # 协议出口统一转 error 响应
            logger.exception("处理请求失败: %s", request.get("command"))
            self._error(request_id, str(e) or type(e).__name__)
        else:
            self._respond(request_id, result)

    async def serve(self) -> None:
        """从 stdin 逐行读取,直到 EOF 或 shutdown;退出前收尾活动操作"""
        while not self._shutdown_requested:
            line = await asyncio.to_thread(self._readline)
            if not line:
                break
            await self.handle_line(line)
        if self._operation is not None:
            self._request_cancel(self._operation)
            await self.wait_idle()

    def _readline(self) -> str:
        if self._stdin is None:
            return ""
        line = self._stdin.readline()
        return line if isinstance(line, str) else ""

    # ------------------------------------------------------------ 命令分发

    def _settings(self) -> Settings:
        return self._settings_factory()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        if command == "state":
            return self._state()
        if command == "status":
            return self._status()
        if command == "failed":
            return self._failed(request)
        if command == "run":
            return self._start_run(request)
        if command == "cancel":
            return self._cancel()
        if command == "login":
            return self._start_login(request)
        if command == "load_config":
            return self._load_config()
        if command == "save_config":
            return self._save_config(request)
        if command == "test_bark":
            return await self._test_bark()
        if command == "shutdown":
            result = self._shutdown()
            await self.wait_idle()
            return result
        raise ProtocolError(f"未知命令: {command!r}")

    # ------------------------------------------------------------ 只读命令

    def _state(self) -> dict[str, Any]:
        op = self._operation
        return {
            "version": project_version(),
            "operation": op.kind if op is not None else None,
            "stage": op.stage if op is not None else None,
        }

    def _status(self) -> dict[str, Any]:
        queries = StatusQueries(self._settings().db_path)
        per_paper = [
            {
                "paperid": paperid,
                "name": name,
                "issues": cells[0],
                "boards": cells[1],
                "articles": cells[2],
            }
            for paperid, name, cells in queries.per_paper()
        ]
        return {"overview": queries.overview(), "per_paper": per_paper}

    def _failed(self, request: dict[str, Any]) -> dict[str, Any]:
        limit = request.get("limit", 5)
        if limit is None:
            limit = 5
        limit = _positive_int(limit, "limit")
        return {"failed": StatusQueries(self._settings().db_path).failed(limit)}

    # ------------------------------------------------------------ 操作

    def _ensure_idle(self) -> None:
        if self._operation is not None:
            raise ProtocolError("已有操作正在运行,请先取消或等待完成")

    def _start_run(self, request: dict[str, Any]) -> dict[str, Any]:
        stage = request.get("stage")
        if stage not in RUN_STAGES:
            raise ProtocolError(f"未知阶段: {stage!r}")
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ProtocolError("params 必须是 JSON 对象")
        kwargs = _validate_run_params(stage, params)

        self._ensure_idle()
        op = _Operation(kind="run", stage=stage)
        self._operation = op
        op.task = asyncio.create_task(self._run_operation(op, stage, kwargs))
        logger.info("启动阶段: %s %s", stage, kwargs)
        return {"started": True, "operation": "run", "stage": stage}

    async def _run_operation(
        self, op: _Operation, stage: str, kwargs: dict[str, Any]
    ) -> None:
        outcome, message = "done", ""
        try:
            await self._run_stage(stage, **kwargs)
        except asyncio.CancelledError:
            outcome, message = "stopped", "已被用户停止"
        except AlreadyRunningError as e:
            outcome, message = "busy", str(e)
        except AuthError as e:
            outcome, message = "auth", str(e)
        except Exception as e:  # noqa: BLE001
            # 失败原因通过 operation_done 回传
            outcome, message = "error", str(e) or type(e).__name__
            logger.error("阶段 %s 异常中断: %s", stage, message)
        self._finish_operation(op, outcome, message)

    def _start_login(self, request: dict[str, Any]) -> dict[str, Any]:
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ProtocolError("params 必须是 JSON 对象")
        unknown = set(params) - {"headless"}
        if unknown:
            raise ProtocolError(f"login 不支持参数: {', '.join(sorted(unknown))}")
        headless = params.get("headless", False)
        if not isinstance(headless, bool):
            raise ProtocolError("headless 必须是布尔值")

        self._ensure_idle()
        op = _Operation(kind="login")
        self._operation = op
        op.task = asyncio.create_task(self._login_operation(op, headless))
        logger.info("启动登录(headless=%s)", headless)
        return {"started": True, "operation": "login"}

    async def _login_operation(self, op: _Operation, headless: bool) -> None:
        outcome, message = "done", ""
        try:
            settings = self._settings()
            credentials = settings.secrets_dir / "credentials.json"
            cookies = settings.secrets_dir / "cookies.json"

            if not op.cancel_requested:
                installed = await asyncio.to_thread(self._browser_installed)
                if not installed and not op.cancel_requested:
                    logger.info("Chromium 未安装,开始安装")
                    installed = await asyncio.to_thread(
                        self._ensure_browser,
                        output=self._emit_browser_output,
                        cancel_event=op.cancel_event,
                    )
                if op.cancel_requested:
                    outcome, message = "stopped", "已被用户取消"
                elif not installed:
                    outcome, message = "error", "Chromium 安装失败"

            if outcome == "done" and not op.cancel_requested:
                logger.info("启动登录子进程")
                returncode = await self._login_runner(
                    credentials, cookies, headless, op
                )
                if op.cancel_requested:
                    outcome, message = "stopped", "已被用户取消"
                elif returncode == 0:
                    outcome, message = "done", "登录完成"
                else:
                    outcome, message = "error", f"登录脚本退出码 {returncode}"
        except asyncio.CancelledError:
            outcome, message = "stopped", "已被用户取消"
        except BrowserInstallError as e:
            outcome, message = "error", str(e)
        except Exception as e:  # noqa: BLE001
            # 失败原因通过 operation_done 回传
            outcome, message = "error", str(e) or type(e).__name__
            logger.error("登录异常中断: %s", message)
        self._finish_operation(op, outcome, message)

    def _emit_browser_output(self, line: str) -> None:
        self.emit_log(f"playwright | {line}")

    async def _run_login_process(
        self,
        credentials: Path,
        cookies: Path,
        headless: bool,
        op: _Operation,
    ) -> int:
        """spawn 登录子进程;cancel 时 terminate,join 在后台线程等待"""
        process = self._mp_context.Process(
            target=_login_child_entry,
            args=(str(credentials), str(cookies), bool(headless)),
            name="newspapercrawl-login",
        )
        process.start()
        op.process = process
        op.terminate = process.terminate
        if op.cancel_requested:
            try:
                process.terminate()
            except Exception:  # 进程可能已退出
                logger.debug("终止登录子进程失败", exc_info=True)
        try:
            await asyncio.to_thread(process.join)
            return process.exitcode if process.exitcode is not None else -1
        finally:
            op.process = None
            op.terminate = None

    def _finish_operation(self, op: _Operation, outcome: str, message: str) -> None:
        if self._operation is op:
            self._operation = None
        self._send(
            {
                "type": "operation_done",
                "operation": op.kind,
                "outcome": outcome,
                "message": message,
            }
        )

    def _cancel(self) -> dict[str, Any]:
        op = self._operation
        if op is None:
            return {"stopping": False, "operation": None}
        self._request_cancel(op)
        return {"stopping": True, "operation": op.kind}

    def _request_cancel(self, op: _Operation) -> None:
        """爬取取消 task;登录取消安装事件并终止子进程"""
        op.cancel_requested = True
        op.cancel_event.set()
        if op.kind == "login":
            terminate = op.terminate
            if terminate is not None:
                try:
                    terminate()
                except Exception:  # 进程已退出或已终止
                    logger.warning("终止登录子进程失败", exc_info=True)
        elif op.task is not None:
            op.task.cancel()

    def _shutdown(self) -> dict[str, Any]:
        self._shutdown_requested = True
        if self._operation is not None:
            self._request_cancel(self._operation)
        logger.info("sidecar 收到 shutdown")
        return {"shutdown": True}

    async def wait_idle(self, timeout: float = 5.0) -> None:
        """等待当前操作收尾;超时则强杀仍在运行的登录子进程"""
        op = self._operation
        if op is None or op.task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(op.task), timeout)
        except TimeoutError:
            logger.warning("操作收尾超时: %s", op.kind)
            if op.process is not None:
                try:
                    op.process.kill()
                except Exception:  # 进程可能已退出
                    logger.debug("强杀登录子进程失败", exc_info=True)

    # ------------------------------------------------------------ 配置

    def _load_config(self) -> dict[str, Any]:
        settings = self._settings()
        credentials = _read_json_object(settings.secrets_dir / "credentials.json")
        bark = _read_json_object(settings.secrets_dir / "bark.json")
        return {
            "credentials": {
                "username": _clean_secret(credentials.get("username")),
                "password": _clean_secret(credentials.get("password")),
            },
            "bark": {
                "device_key": _clean_secret(bark.get("device_key")),
                "bark_server": _clean_secret(bark.get("bark_server"))
                or "https://api.day.app",
            },
        }

    def _save_config(self, request: dict[str, Any]) -> dict[str, Any]:
        kind = request.get("kind")
        values = request.get("values")
        if not isinstance(values, dict):
            raise ProtocolError("values 必须是 JSON 对象")

        settings = self._settings()
        if kind == "credentials":
            username = _config_string(values.get("username"), "username")
            password = _config_string(values.get("password"), "password")
            if not username or not password:
                raise ProtocolError("账号和密码都不能为空")
            path = settings.secrets_dir / "credentials.json"
            payload = {
                "username": username,
                "password": password,
                "_说明": "登录辅助脚本(login.py)读取本文件自动填表。"
                "认证过期时爬虫会通知重新登录,重新执行登录脚本即可。",
            }
        elif kind == "bark":
            device_key = _config_string(values.get("device_key"), "device_key")
            server = (
                _config_string(values.get("bark_server"), "bark_server")
                or "https://api.day.app"
            )
            path = settings.secrets_dir / "bark.json"
            payload = {
                "device_key": device_key,
                "bark_server": server,
                "_说明": "Bark 是 iOS 推送。device key 填入后,"
                "爬虫会在认证过期等事件时推送通知。",
            }
        else:
            raise ProtocolError(f"未知配置类型: {kind!r}")

        _write_json(path, payload)
        logger.info("已保存配置: %s", path)
        return {"saved": True, "kind": kind}

    async def _test_bark(self) -> dict[str, Any]:
        path = self._settings().secrets_dir / "bark.json"
        notifier = self._notifier_factory(path)
        await notifier.notify("浙图爬虫测试", "Bark 配置生效")
        return {"configured": bool(notifier.is_configured)}


# ---------------------------------------------------------------- 参数校验


def _validate_run_params(stage: str, params: dict[str, Any]) -> dict[str, Any]:
    allowed = _RUN_PARAMS[stage]
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ProtocolError(f"阶段 {stage} 不支持参数: {', '.join(unknown)}")

    if stage == "import":
        csv = _optional_string(params.get("csv"), "csv")
        return {"csv": csv} if csv else {}

    if stage == "issues":
        kwargs: dict[str, Any] = {
            "start_year": _int_value(params.get("start_year"), "start_year"),
            "end_year": _int_value(params.get("end_year"), "end_year"),
        }
        for name in ("start_month", "end_month"):
            value = _optional_int(params.get(name), name)
            if value is not None:
                kwargs[name] = value
        concurrency = _optional_positive_int(params.get("concurrency"), "concurrency")
        if concurrency is not None:
            kwargs["concurrency"] = concurrency
        return kwargs

    kwargs = {}
    if stage == "boards":
        paperid = _optional_string(params.get("paperid"), "paperid")
        if paperid:
            kwargs["paperid"] = paperid
    limit = _optional_positive_int(params.get("limit"), "limit")
    if limit is not None:
        kwargs["limit"] = limit
    concurrency = _optional_positive_int(params.get("concurrency"), "concurrency")
    if concurrency is not None:
        kwargs["concurrency"] = concurrency
    return kwargs


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_value(value: Any, name: str) -> int:
    if not _is_int(value):
        raise ProtocolError(f"{name} 必须是整数")
    return value


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _int_value(value, name)


def _positive_int(value: Any, name: str) -> int:
    value = _int_value(value, name)
    if value < 1:
        raise ProtocolError(f"{name} 必须大于 0")
    return value


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, name)


def _optional_string(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProtocolError(f"{name} 必须是字符串")
    return value.strip()


def _config_string(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProtocolError(f"{name} 必须是字符串")
    return value.strip()


# ---------------------------------------------------------------- 配置文件


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("%s 读取失败: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _clean_secret(value: Any) -> str:
    """模板占位符/非字符串视为未填写"""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return "" if not value or "在此填入" in value else value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 进程入口


async def _serve(protocol_out: TextIO) -> None:
    # sidecar 的日志只走事件帧:先挂 EventLogHandler,再进入主循环。
    # 随后 run_stage 内部调用 setup_logging() 时,root 已有 handler,
    # 不会再添加写 stderr 的 console handler,避免 UI 日志重复。
    core = DesktopCore(stdout=protocol_out)
    handler = configure_event_logging(core.emit_log)
    try:
        logger.info("sidecar 启动,版本 %s", project_version())
        await core.serve()
    finally:
        logging.getLogger().removeHandler(handler)


def main() -> int:
    # PyInstaller 冻结后,multiprocessing spawn 需要先走 freeze_support
    multiprocessing.freeze_support()
    protocol_out = sys.stdout
    # stdout 是协议通道:把普通 print/第三方输出全部导到 stderr
    if sys.stderr is not None:
        sys.stdout = sys.stderr
    try:
        asyncio.run(_serve(protocol_out))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
