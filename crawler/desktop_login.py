"""桌面登录子进程的轻量入口，不加载桌面协议与爬虫流水线。"""

import argparse
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.login import run_login


def _login_child_entry(
    credentials_path: str, cookies_path: str, headless: bool, error_channel: Any
) -> None:
    """在独立进程中执行登录，并把脱敏后的失败原因返回 sidecar。"""
    if sys.stderr is not None:
        sys.stdout = sys.stderr
    try:
        run_login(Path(credentials_path), Path(cookies_path), headless=headless)
    except SystemExit as e:
        if isinstance(e.code, str):
            error_channel.send(e.code)
        raise
    except Exception as e:
        error_channel.send(str(e) or type(e).__name__)
        raise


class _StderrChannel:
    def send(self, message: str) -> None:
        if sys.stderr is not None:
            print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="浙图 SSO 桌面登录子进程")
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--cookies", required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)
    try:
        _login_child_entry(
            args.credentials, args.cookies, args.headless, _StderrChannel()
        )
    except SystemExit as e:
        if e.code is None:
            return 0
        return e.code if isinstance(e.code, int) else 1
    except Exception:  # noqa: BLE001  # 子进程出口统一转成非零退出码
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
