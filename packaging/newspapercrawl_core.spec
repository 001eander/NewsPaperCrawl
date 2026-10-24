# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile 配置:桌面端 Python sidecar
================================================
入口是 ``crawler/desktop_core.py``(由 Tauri 通过 sidecar 协议启动),打包内容:

* ``crawler`` 全部子模块(四阶段流水线 + SQLite/HTTP 适配器)
* ``scripts.login`` 及其依赖
* Playwright Python 包与 driver(node + cli.js),但**不包含 Chromium**
  —— 桌面端首次登录时自动下载浏览器到用户数据目录,
  避免每个安装包多出数百 MB

构建入口:
  uv run python packaging/build_sidecar.py
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

import playwright

ROOT = Path(SPECPATH).resolve().parent
ENTRY = Path(
    os.environ.get(
        "NEWSPAPERCRAWL_SIDECAR_ENTRY", ROOT / "crawler" / "desktop_core.py"
    )
).resolve()
if not ENTRY.is_file():
    raise SystemExit(
        f"找不到 sidecar 入口 {ENTRY};crawler/desktop_core.py 尚未实现时无法打包"
    )

# 默认保留控制台(Windows 用 hide-early 隐藏窗口),这样 Tauri 能通过
# stdin/stdout 与 sidecar 通信;特殊诊断时可设环境变量改成 windowed。
CONSOLE = os.environ.get("NEWSPAPERCRAWL_SIDECAR_WINDOWED") != "1"

playwright_package = Path(playwright.__file__).resolve().parent
driver_dir = playwright_package / "driver"

# driver 的 node 必须按"可执行文件"收集,否则 onefile 解包后没有 +x 权限,
# Playwright 启动时会报 driver 不可执行。其余 driver 资源仍作为 data 收集。
playwright_binaries = []
playwright_datas = []
for src, dest in collect_data_files("playwright", include_py_files=False):
    source = Path(src)
    if source.parent == driver_dir and source.name in {"node", "node.exe"}:
        playwright_binaries.append((str(source), dest))
    else:
        playwright_datas.append((src, dest))

hiddenimports = sorted(
    {
        *collect_submodules("crawler"),
        *collect_submodules("playwright"),
        # scripts 是隐式命名空间包(没有 __init__.py),显式声明登录模块
        "scripts.login",
        "aiosqlite",
        "sqlite3",
        "aiohttp",
        "bs4",
    }
)

a = Analysis(
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=playwright_binaries,
    datas=playwright_datas + collect_data_files("certifi"),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "pyinstaller_smoke_hook.py")],
    # 桌面端 sidecar 不需要绘图/Excel/旧 GUI 依赖,排除以缩小体积
    excludes=["matplotlib", "numpy", "openpyxl", "tkinter", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="newspapercrawl-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=CONSOLE,
    disable_windowed_traceback=not CONSOLE,
    hide_console="hide-early" if CONSOLE and sys.platform == "win32" else None,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
