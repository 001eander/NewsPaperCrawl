#!/usr/bin/env python3
"""
浙图 SSO 登录(手动入口)
=========================
爬虫本身已内置自动登录(认证失效时自动重新登录),本脚本用于人工确认登录
状态或排查凭据问题。登录实现在 crawler/adapters/sso_login.py。

用法:
  uv run scripts/login.py
  uv run scripts/login.py --headless
  uv run scripts/login.py --credentials .secrets/credentials.json --cookies .secrets/cookies.json
"""

import argparse
import sys
from pathlib import Path

# 项目未安装为包(uv sync --no-install-project),直接运行脚本时
# sys.path[0] 是 scripts/ 而非项目根,需手动把项目根加进来
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.adapters.sso_login import (  # 依赖上方 sys.path 调整
    DEFAULT_COOKIES,
    DEFAULT_CREDENTIALS,
    run_login,
)
from crawler.domain.exceptions import LoginError
from crawler.logging_config import setup_logging


def main() -> None:
    ap = argparse.ArgumentParser(description="浙图 SSO 登录")
    ap.add_argument(
        "--credentials",
        default=str(DEFAULT_CREDENTIALS),
        help=f"凭据文件(默认 {DEFAULT_CREDENTIALS})",
    )
    ap.add_argument(
        "--cookies",
        default=str(DEFAULT_COOKIES),
        help=f"Cookie 保存路径(默认 {DEFAULT_COOKIES})",
    )
    ap.add_argument(
        "--headless",
        action="store_true",
        help="不打开浏览器窗口(适合定时任务/服务器)",
    )
    args = ap.parse_args()

    setup_logging()
    try:
        run_login(
            credentials_path=args.credentials,
            cookies_path=args.cookies,
            headless=args.headless,
        )
    except LoginError as e:
        print(f"登录失败: {e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
