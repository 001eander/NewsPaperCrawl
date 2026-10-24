#!/usr/bin/env python3
"""构建桌面端 Python sidecar(PyInstaller onefile)
==================================================
流程:
  1. 用 ``rustc --print host-tuple`` 或 ``--target`` 确定 target triple
  2. 调 PyInstaller 按 ``packaging/newspapercrawl_core.spec`` 打 onefile
  3. 复制并重命名为 Tauri 约定的 sidecar 名:
       src-tauri/binaries/newspapercrawl-core-<triple>[.exe]
  4. 打印产物 status(大小 / sha256),默认再跑一次无网络 import 烟测

烟测说明:用临时目录注入 ``NEWSPAPER_*`` 路径运行 sidecar,运行时钩子只在
内存中 import 模块并检查 Playwright driver,不发起网络请求、不启动浏览器。
特殊情况下可用 ``--no-smoke`` 跳过,但默认必须通过。

用法:
  uv run python packaging/build_sidecar.py
  uv run python packaging/build_sidecar.py --target aarch64-apple-darwin
  uv run python packaging/build_sidecar.py --status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "newspapercrawl_core.spec"
ENTRY = ROOT / "crawler" / "desktop_core.py"
DEFAULT_OUTPUT_DIR = ROOT / "src-tauri" / "binaries"
BINARY_STEM = "newspapercrawl-core"

SMOKE_ENV = "NEWSPAPERCRAWL_SIDECAR_SMOKE"
REPORT_ENV = "NEWSPAPERCRAWL_SMOKE_REPORT"
SMOKE_OK = "SIDECAR_SMOKE_OK"

_TRIPLE_RE = re.compile(r"^[a-z0-9_]+(?:-[a-z0-9_]+){2,}$")
_SMOKE_SETTINGS = {
    "data_dir": "NEWSPAPER_DATA_DIR",
    "db_path": "NEWSPAPER_DB_PATH",
    "log_dir": "NEWSPAPER_LOG_DIR",
    "secrets_dir": "NEWSPAPER_SECRETS_DIR",
    "paperlist_csv": "NEWSPAPER_PAPERLIST_CSV",
}


class BuildError(RuntimeError):
    """构建或烟测失败"""


def _validate_triple(target: str) -> str:
    value = target.strip()
    if not _TRIPLE_RE.match(value):
        raise BuildError(f"target triple 格式不合法: {target!r}")
    return value


def detect_target(explicit: str | None = None) -> str:
    """优先用显式目标;否则问 rustc 当前 host"""
    if explicit:
        return _validate_triple(explicit)

    rustc = shutil.which("rustc")
    if not rustc:
        raise BuildError(
            "无法确定目标三元组: 未找到 rustc,请安装 Rust 工具链或传 --target"
        )

    result = subprocess.run(
        [rustc, "--print", "host-tuple"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return _validate_triple(result.stdout.strip())

    # 旧版 rustc 没有 --print host-tuple,退回解析 -vV 的 host 行
    result = subprocess.run(
        [rustc, "-vV"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    match = re.search(r"^host:\s*(\S+)\s*$", result.stdout, re.MULTILINE)
    if result.returncode != 0 or not match:
        detail = (result.stderr or result.stdout).strip()
        raise BuildError(f"无法从 rustc 解析 host triple: {detail}")
    return _validate_triple(match.group(1))


def _host_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _normalize_arch(machine: str) -> str:
    machine = machine.lower()
    return {
        "arm64": "aarch64",
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "aarch64",
    }.get(machine, machine)


def _check_target_matches_host(target: str) -> None:
    """PyInstaller 只能打本机 Python 架构;目标写错会导致产物名与内容不符"""
    target_os = target.split("-")[2] if len(target.split("-")) >= 3 else ""
    target_arch = target.split("-")[0]
    host_os = _host_os()
    host_arch = _normalize_arch(platform.machine())
    if target_os != host_os or target_arch != host_arch:
        raise BuildError(
            f"目标 {target} 与当前构建机 {host_os}/{host_arch} 不一致;"
            "PyInstaller 不支持交叉编译,请在对应平台的 runner 上构建"
        )


def artifact_name(target: str) -> str:
    suffix = ".exe" if "windows" in target else ""
    return f"{BINARY_STEM}-{target}{suffix}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_status(path: Path, target: str) -> dict:
    info: dict = {"target": target, "path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return info
    stat = path.stat()
    info.update(
        {
            "size": stat.st_size,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "sha256": _sha256(path),
            "mode": oct(stat.st_mode & 0o777),
        }
    )
    return info


def run_pyinstaller(
    *,
    spec: Path = SPEC,
    dist_dir: Path,
    work_dir: Path,
    windowed: bool = False,
) -> Path:
    """执行 PyInstaller,返回 dist 中生成的原始可执行文件"""
    if not ENTRY.is_file():
        raise BuildError(
            f"找不到 sidecar 入口 {ENTRY};请先实现 crawler/desktop_core.py"
        )
    if not spec.is_file():
        raise BuildError(f"找不到 PyInstaller spec: {spec}")

    env = os.environ.copy()
    if windowed:
        env["NEWSPAPERCRAWL_SIDECAR_WINDOWED"] = "1"
    else:
        env.pop("NEWSPAPERCRAWL_SIDECAR_WINDOWED", None)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        str(spec),
    ]
    try:
        subprocess.run(cmd, cwd=ROOT, env=env, check=True)
    except FileNotFoundError as exc:
        raise BuildError("找不到 PyInstaller,请先执行 uv sync") from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"PyInstaller 构建失败(退出码 {exc.returncode})") from exc

    for candidate in (
        dist_dir / f"{BINARY_STEM}.exe",
        dist_dir / BINARY_STEM,
    ):
        if candidate.is_file():
            return candidate
    raise BuildError(f"PyInstaller 未在 {dist_dir} 生成 {BINARY_STEM}")


def _tail(text: str, limit: int = 4000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def smoke_test(binary: Path, *, timeout: int = 180, windowed: bool = False) -> dict:
    """无网络 import 烟测;失败抛 BuildError,成功返回报告内容"""
    with tempfile.TemporaryDirectory(prefix="newspapercrawl-smoke-") as tmp:
        base = Path(tmp)
        data_dir = base / "data"
        log_dir = base / "logs"
        secrets_dir = base / "secrets"
        for directory in (data_dir, log_dir, secrets_dir):
            directory.mkdir(parents=True)
        report_path = base / "report.json"

        env = os.environ.copy()
        env.pop("NEWSPAPERCRAWL_SIDECAR_WINDOWED", None)
        env.update(
            {
                SMOKE_ENV: "1",
                REPORT_ENV: str(report_path),
                "NEWSPAPER_DATA_DIR": str(data_dir),
                "NEWSPAPER_DB_PATH": str(data_dir / "crawl.db"),
                "NEWSPAPER_LOG_DIR": str(log_dir),
                "NEWSPAPER_SECRETS_DIR": str(secrets_dir),
                "NEWSPAPER_PAPERLIST_CSV": str(data_dir / "报纸清单.csv"),
            }
        )

        try:
            proc = subprocess.run(
                [str(binary)],
                cwd=tmp,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BuildError(f"sidecar 烟测超时({timeout}s)") from exc

        report = None
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise BuildError(f"烟测报告不是合法 JSON: {exc}") from exc

        output = _tail((proc.stdout or "") + (proc.stderr or ""))
        if proc.returncode != 0 or not report or not report.get("ok"):
            reason = (report or {}).get("error", "").strip()
            raise BuildError(
                "sidecar 烟测失败"
                f"(退出码 {proc.returncode})\n{reason}\n--- 输出 ---\n{output}"
            )

        settings = report.get("settings") or {}
        for field, env_name in _SMOKE_SETTINGS.items():
            # 报告里是子进程实际读到的值,应与本次注入的临时路径一致
            expected = env[env_name]
            if settings.get(field) != expected:
                raise BuildError(
                    f"烟测 Settings.{field} 未使用临时路径: "
                    f"{settings.get(field)!r} != {expected!r}"
                )
        if not windowed and SMOKE_OK not in (proc.stdout or ""):
            raise BuildError("sidecar 烟测未输出成功标记")
        return report


def _build_and_install(
    target: str,
    output_dir: Path,
    *,
    spec: Path,
    windowed: bool,
    keep_build: bool,
) -> Path:
    _check_target_matches_host(target)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / artifact_name(target)

    if keep_build:
        build_root = ROOT / "build" / "sidecar"
        build_root.mkdir(parents=True, exist_ok=True)
        dist_dir = build_root / "dist"
        work_dir = build_root / "work"
        built = run_pyinstaller(
            spec=spec,
            dist_dir=dist_dir,
            work_dir=work_dir,
            windowed=windowed,
        )
        shutil.copy2(built, destination)
    else:
        with tempfile.TemporaryDirectory(prefix="newspapercrawl-sidecar-") as tmp:
            built = run_pyinstaller(
                spec=spec,
                dist_dir=Path(tmp) / "dist",
                work_dir=Path(tmp) / "work",
                windowed=windowed,
            )
            shutil.copy2(built, destination)

    if os.name != "nt":
        destination.chmod(0o755)
    return destination


def _print_summary(status: dict, smoke: dict | None) -> None:
    print(f"target: {status['target']}")
    print(f"sidecar: {status['path']}")
    if status.get("exists"):
        print(f"size: {status['size_mb']} MB ({status['size']} bytes)")
        print(f"sha256: {status['sha256']}")
    if smoke is not None:
        modules = smoke.get("modules") or {}
        print(f"smoke: ok (version={smoke.get('version')}, modules={len(modules)})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Tauri Python sidecar")
    parser.add_argument("--target", help="target triple(默认 rustc host-tuple)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"sidecar 输出目录(默认 {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--spec", type=Path, default=SPEC)
    parser.add_argument("--no-smoke", action="store_true", help="跳过构建后烟测")
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="构建 windowed 版本(Windows 下无控制台,仅用于诊断)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="只输出已有 sidecar 的 status,不构建",
    )
    parser.add_argument("--smoke-timeout", type=int, default=180)
    parser.add_argument(
        "--keep-build",
        action="store_true",
        help="把 PyInstaller dist/work 保留在 build/sidecar/ 便于排查",
    )
    args = parser.parse_args(argv)

    try:
        target = detect_target(args.target)
        if args.status:
            path = args.output_dir / artifact_name(target)
            status = artifact_status(path, target)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status["exists"] else 1

        destination = _build_and_install(
            target,
            args.output_dir,
            spec=args.spec,
            windowed=args.windowed,
            keep_build=args.keep_build,
        )
        status = artifact_status(destination, target)
        smoke = None
        if args.no_smoke:
            print("警告: 已跳过 sidecar 烟测(--no-smoke)", file=sys.stderr)
        else:
            smoke = smoke_test(
                destination,
                timeout=args.smoke_timeout,
                windowed=args.windowed,
            )
    except BuildError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    _print_summary(status, smoke)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
