"""清理 Tauri DMG 的卷图标文件并隐藏应用扩展名。"""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"命令执行失败: {' '.join(command)}\n{detail}")


def polish_dmg(source: Path) -> None:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if shutil.which("hdiutil") is None:
        raise RuntimeError("只能在 macOS 上处理 DMG")

    with tempfile.TemporaryDirectory(prefix="newspapercrawl-dmg-") as tmp:
        work = Path(tmp)
        writable = work / "writable.dmg"
        polished = work / "polished.dmg"
        mount = work / "volume"
        mount.mkdir()
        mounted = False
        try:
            run(
                [
                    "hdiutil",
                    "convert",
                    str(source),
                    "-format",
                    "UDRW",
                    "-o",
                    str(writable),
                ]
            )
            run(
                [
                    "hdiutil",
                    "attach",
                    str(writable),
                    "-mountpoint",
                    str(mount),
                    "-nobrowse",
                    "-noautoopen",
                ]
            )
            mounted = True
            (mount / ".VolumeIcon.icns").unlink(missing_ok=True)
            app = mount / "浙图报纸爬虫.app"
            if app.is_dir() and shutil.which("SetFile"):
                run(["SetFile", "-a", "E", str(app)])
            run(["sync"])
            run(["hdiutil", "detach", str(mount)])
            mounted = False
            run(
                [
                    "hdiutil",
                    "convert",
                    str(writable),
                    "-format",
                    "UDZO",
                    "-imagekey",
                    "zlib-level=9",
                    "-o",
                    str(polished),
                ]
            )
            polished.replace(source)
        finally:
            if mounted:
                subprocess.run(
                    ["hdiutil", "detach", str(mount)],
                    capture_output=True,
                    check=False,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dmg", type=Path)
    args = parser.parse_args()
    polish_dmg(args.dmg)
    print(f"DMG 展示已优化: {args.dmg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
