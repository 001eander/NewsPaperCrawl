#!/usr/bin/env python3
"""校验桌面端发布版本号三处一致
=================================
版本号在三个地方各存一份,发版前必须完全一致,否则 Tauri updater 会认为
"已是最新版本"或下载到错误版本的 sidecar:

  pyproject.toml            [project].version
  crawler/__init__.py       __version__
  src-tauri/tauri.conf.json version(Tauri v1 为 package.version)

用法:
  uv run scripts/check_versions.py
  uv run scripts/check_versions.py --tag v0.2.0
  uv run scripts/check_versions.py --root /path/to/repo --tag v0.2.0

`--tag` 会去掉前缀 v 后参与比较。任何一处缺失、不一致或 tag 不匹配都以
非零码退出,便于 CI 在构建前拦截。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PYPROJECT = "pyproject.toml"
CRAWLER_INIT = Path("crawler") / "__init__.py"
TAURI_CONF = Path("src-tauri") / "tauri.conf.json"

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
_VERSION_VALUE_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class VersionError(ValueError):
    """版本号缺失或不一致"""


def read_pyproject_version(path: Path) -> str:
    if not path.is_file():
        raise VersionError(f"缺少 {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    version = (data.get("project") or {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise VersionError(f"{path}: 缺少 [project].version")
    return version.strip()


def read_crawler_version(path: Path) -> str:
    if not path.is_file():
        raise VersionError(f"缺少 {path}")
    match = _VERSION_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        raise VersionError(f"{path}: 未找到 __version__")
    return match.group(1).strip()


def read_tauri_version(path: Path) -> str:
    if not path.is_file():
        raise VersionError(f"缺少 {path}: Tauri 桌面端尚未接入时无法校验发行版本")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VersionError(f"{path}: 不是合法 JSON ({exc})") from exc
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        # Tauri v1 把版本放在 package.version
        package = data.get("package")
        version = package.get("version") if isinstance(package, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise VersionError(f"{path}: 缺少 version(或 package.version)")
    return version.strip()


def normalize_tag(tag: str) -> str:
    """v0.2.0 / 0.2.0 -> 0.2.0;非法 tag 直接报错"""
    match = _TAG_RE.match(tag.strip())
    if not match:
        raise VersionError(f"无法识别的 tag: {tag!r}(期望 vX.Y.Z)")
    return match.group(1)


def collect_versions(root: Path) -> dict[str, str]:
    """读取三处版本号,按来源返回"""
    return {
        PYPROJECT: read_pyproject_version(root / PYPROJECT),
        CRAWLER_INIT.as_posix(): read_crawler_version(root / CRAWLER_INIT),
        TAURI_CONF.as_posix(): read_tauri_version(root / TAURI_CONF),
    }


def check_versions(root: Path = ROOT, tag: str | None = None) -> dict[str, str]:
    """校验三处一致(以及可选 tag),返回读取到的版本号;不一致抛 VersionError"""
    versions = collect_versions(root)
    for source, version in versions.items():
        if not _VERSION_VALUE_RE.match(version):
            raise VersionError(f"{source}: 版本号格式不合法: {version!r}")
    unique = set(versions.values())
    if len(unique) != 1:
        detail = ", ".join(
            f"{source}={version}" for source, version in versions.items()
        )
        raise VersionError(f"版本号不一致: {detail}")
    expected = next(iter(unique))
    if tag is not None:
        tag_version = normalize_tag(tag)
        if tag_version != expected:
            raise VersionError(
                f"tag {tag!r} 对应 {tag_version},但项目版本为 {expected}"
            )
    return versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验发布版本号三处一致")
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根目录")
    parser.add_argument("--tag", help="发布 tag(如 v0.2.0),提供时一并校验")
    args = parser.parse_args(argv)

    try:
        versions = check_versions(args.root, tag=args.tag)
    except VersionError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    for source, version in versions.items():
        print(f"{source}: {version}")
    if args.tag:
        print(f"tag: {args.tag} -> {normalize_tag(args.tag)}")
    print("OK: 版本号一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
