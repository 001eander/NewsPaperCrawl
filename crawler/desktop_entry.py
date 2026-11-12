"""冻结桌面入口，在加载桌面核心前分流轻量登录子进程。"""

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--login-child"]:
        from crawler.desktop_login import main as login_main

        return login_main(args[1:])

    from crawler.desktop_core import main as core_main

    return core_main()


if __name__ == "__main__":
    raise SystemExit(main())
