"""
Playwright 运行时定位与 Chromium 安装
====================================

Tauri sidecar 可能以源码方式运行,也可能被 PyInstaller 冻结。两种情况下
Playwright 的 driver(node + cli.js)位置不同:

  - 源码: playwright._impl._driver.compute_driver_executable()
  - 冻结: PyInstaller 把 playwright/driver 解包到 sys._MEIPASS/playwright/driver

本模块只负责定位 driver 与按需安装 Chromium,不把浏览器打进应用包。
浏览器安装位置由 Playwright 自己决定,因此 PLAYWRIGHT_BROWSERS_PATH 会被
原样透传给安装子进程;未设置时 Playwright 使用平台默认缓存目录。

安装/检测命令都通过 driver CLI 或 Playwright 自身 API 完成,不启动真实网页。
"""

import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

INSTALL_TARGET = "chromium"


class BrowserRuntimeError(RuntimeError):
    """Playwright 运行时或浏览器安装失败"""


class DriverNotFoundError(BrowserRuntimeError):
    """找不到 Playwright driver(node / cli.js)"""


class BrowserInstallError(BrowserRuntimeError):
    """Chromium 安装失败"""


def _compute_driver_executable() -> tuple[str, str]:
    """Playwright 自带的 driver 定位(单独成函数便于测试替换)"""
    from playwright._impl._driver import compute_driver_executable

    return compute_driver_executable()


def _driver_candidates() -> list[Path]:
    """按优先级列出可能的 playwright/driver 目录"""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "playwright" / "driver")
    try:
        import playwright

        candidates.append(Path(playwright.__file__).resolve().parent / "driver")
    except Exception:  # 冻结环境模块信息异常时继续尝试其他候选
        logger.debug("无法从 playwright 包定位 driver 目录", exc_info=True)
    return candidates


def driver_command() -> tuple[str, str]:
    """
    返回 (node 可执行文件, Playwright cli.js) 的绝对路径。

    优先使用 Playwright 自己的计算结果(它会尊重 PLAYWRIGHT_NODEJS_PATH),
    文件不存在时再按冻结/源码布局回退。找不到时抛 DriverNotFoundError。
    """
    try:
        node, cli = _compute_driver_executable()
        if Path(node).is_file() and Path(cli).is_file():
            return str(node), str(cli)
    except Exception:  # 模块未按预期打包时走回退布局
        logger.debug("Playwright driver 自动定位失败,尝试回退布局", exc_info=True)

    node_override = os.environ.get("PLAYWRIGHT_NODEJS_PATH")
    node_name = "node.exe" if sys.platform == "win32" else "node"
    searched: list[str] = []
    for driver_dir in _driver_candidates():
        node_path = Path(node_override) if node_override else driver_dir / node_name
        cli_path = driver_dir / "package" / "cli.js"
        searched.append(str(driver_dir))
        if node_path.is_file() and cli_path.is_file():
            return str(node_path), str(cli_path)

    raise DriverNotFoundError(
        "找不到 Playwright driver(node/cli.js);已检查: "
        + (", ".join(searched) if searched else "无可用候选目录")
    )


def browser_installed() -> bool:
    """Chromium 是否已安装(用 Playwright 自己的 executable_path 判断)"""
    try:
        with sync_playwright() as p:
            executable = p.chromium.executable_path
    except Exception:  # noqa: BLE001  # driver 缺失/损坏都按"未安装"处理
        return False
    return bool(executable) and Path(executable).is_file()


def ensure_browser(
    output: Callable[[str], None] | None = None,
    cancel_event=None,
) -> bool:
    """
    确保 Chromium 可用:已安装直接返回 True,否则运行
    `playwright install chromium`,并把安装输出逐行交给 output。

    返回 True 表示安装后可用;False 表示被 cancel_event 取消。
    安装失败(退出码非 0、driver 缺失、安装后仍检测不到)抛
    BrowserInstallError,由调用方转成 operation_done 的 error。
    """
    if cancel_event is not None and cancel_event.is_set():
        return False
    if browser_installed():
        return True

    node, cli = driver_command()
    command = [node, cli, "install", INSTALL_TARGET]
    logger.info("开始安装 Playwright Chromium: %s", " ".join(command))

    # 环境原样继承,PLAYWRIGHT_BROWSERS_PATH 因此会被 Playwright 尊重
    env = os.environ.copy()
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as e:
        raise BrowserInstallError(f"无法启动 Playwright 安装程序: {e}") from e

    canceled = False
    try:
        if proc.stdout is not None:
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                if line and output is not None:
                    try:
                        output(line)
                    except Exception:  # 输出回调失败不影响安装
                        logger.debug("浏览器安装输出回调失败", exc_info=True)
                if cancel_event is not None and cancel_event.is_set():
                    canceled = True
                    try:
                        proc.terminate()
                    except Exception:  # 进程可能已自行退出
                        logger.debug("终止安装进程失败", exc_info=True)
                    break
        if canceled:
            try:
                returncode = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()
        else:
            returncode = proc.wait()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    if canceled or (cancel_event is not None and cancel_event.is_set()):
        return False
    if returncode != 0:
        raise BrowserInstallError(f"Chromium 安装失败(退出码 {returncode})")
    if not browser_installed():
        raise BrowserInstallError("Chromium 安装完成后仍未检测到浏览器")
    return True
