"""PyInstaller 运行时钩子:sidecar 无网络 import 烟测
=====================================================
构建脚本设置 ``NEWSPAPERCRAWL_SIDECAR_SMOKE=1`` 后运行打包产物,本钩子在真正
的 crawler 入口执行前完成一次自检:

* 导入 crawler 流水线、``scripts.login``、aiohttp/aiosqlite/bs4
* 导入 Playwright sync API,确认 driver(node + cli.js)被正确打包
* 用临时 ``NEWSPAPER_*`` 环境变量构造 Settings,确认路径覆盖生效
* 结果写入 ``NEWSPAPERCRAWL_SMOKE_REPORT``,随后立即退出,不进入 sidecar 主循环

不做任何网络请求,也不启动浏览器。未设置烟测变量时本钩子无副作用。
"""

import json
import os
import sys
import traceback
from pathlib import Path

SMOKE_ENV = "NEWSPAPERCRAWL_SIDECAR_SMOKE"
REPORT_ENV = "NEWSPAPERCRAWL_SMOKE_REPORT"
SMOKE_OK = "SIDECAR_SMOKE_OK"

IMPORTS = (
    "crawler",
    "crawler.bootstrap",
    "crawler.services.pipeline",
    "crawler.adapters.apabi_http",
    "crawler.adapters.sqlite_repo",
    "scripts.login",
    "aiohttp",
    "aiosqlite",
    "bs4",
    "playwright.sync_api",
)


def _write_report(payload):
    target = os.environ.get(REPORT_ENV)
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _check_settings():
    from crawler.adapters.config import Settings

    settings = Settings.from_env()
    expected = {
        "data_dir": "NEWSPAPER_DATA_DIR",
        "db_path": "NEWSPAPER_DB_PATH",
        "log_dir": "NEWSPAPER_LOG_DIR",
        "secrets_dir": "NEWSPAPER_SECRETS_DIR",
        "paperlist_csv": "NEWSPAPER_PAPERLIST_CSV",
    }
    actual = {
        "data_dir": settings.data_dir,
        "db_path": settings.db_path,
        "log_dir": settings.log_dir,
        "secrets_dir": settings.secrets_dir,
        "paperlist_csv": settings.paperlist_csv,
    }
    result = {}
    for field, env_name in expected.items():
        wanted = os.environ.get(env_name)
        if not wanted:
            raise RuntimeError(f"烟测缺少环境变量 {env_name}")
        if Path(wanted) != actual[field]:
            raise RuntimeError(
                f"Settings.{field} 未按 {env_name} 生效: {actual[field]} != {wanted}"
            )
        result[field] = str(actual[field])
    return result


def _check_playwright():
    from playwright._impl._driver import compute_driver_executable

    node, cli = compute_driver_executable()
    node_path = Path(node)
    cli_path = Path(cli)
    if not node_path.is_file():
        raise RuntimeError(f"Playwright driver node 未打包: {node_path}")
    if not cli_path.is_file():
        raise RuntimeError(f"Playwright driver cli.js 未打包: {cli_path}")
    if os.name != "nt" and not os.access(node_path, os.X_OK):
        raise RuntimeError(f"Playwright driver node 没有可执行权限: {node_path}")
    return {"node": str(node_path), "cli": str(cli_path)}


def _run_smoke():
    modules = {}
    for name in IMPORTS:
        __import__(name)
        modules[name] = "ok"

    from crawler import __version__

    payload = {
        "ok": True,
        "version": __version__,
        "python": sys.version,
        "executable": sys.executable,
        "modules": modules,
        "settings": _check_settings(),
        "playwright": _check_playwright(),
    }
    _write_report(payload)
    if sys.stdout is not None:
        print(f"{SMOKE_OK} version={__version__} modules={len(modules)}")
        sys.stdout.flush()
    raise SystemExit(0)


if os.environ.get(SMOKE_ENV) == "1":
    try:
        _run_smoke()
    except Exception:  # noqa: BLE001  # 任何失败都要写进烟测报告
        # 烟测要把任何失败都写进报告,便于 windowed 构建没有控制台时排查
        failure = {
            "ok": False,
            "error": traceback.format_exc(),
            "python": sys.version,
        }
        _write_report(failure)
        if sys.stderr is not None:
            print("sidecar 烟测失败", file=sys.stderr)
            traceback.print_exc()
        raise SystemExit(1)
