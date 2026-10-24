#!/usr/bin/env python3
"""生成 Tauri updater 的 latest.json
====================================
从各平台构建产物目录中收集:

  windows-x86_64  NSIS 安装包  *-setup.exe  + *-setup.exe.sig
  darwin-aarch64  *.app.tar.gz               + *.app.tar.gz.sig
  darwin-x86_64   *.app.tar.gz               + *.app.tar.gz.sig

生成 Tauri v2 updater 使用的 latest.json。三个平台缺一不可,签名内联为
signature 字段,url 指向 GitHub Release 资产。

产物文件名必须能明确区分平台/架构(带 target triple 或 x86_64/aarch64 等
标记),否则视为歧义并报错;同一平台出现多个候选产物同样报错。

用法:
  uv run python packaging/build_update_manifest.py \\
      --version 0.2.0 --artifacts dist --output dist/latest.json

只依赖标准库,不触网;网络请求由 Tauri updater 在实际更新时发起。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

DEFAULT_REPO = "001eander/NewsPaperCrawl"
PLATFORM_KEYS = ("windows-x86_64", "darwin-aarch64", "darwin-x86_64")

_ARCH_PATTERNS = {
    "aarch64": re.compile(
        r"(?:^|[^a-z0-9])(?:aarch64|arm64)(?:[^a-z0-9]|$)", re.IGNORECASE
    ),
    "x86_64": re.compile(
        r"(?:^|[^a-z0-9])(?:x86_64|amd64|x64)(?:[^a-z0-9]|$)", re.IGNORECASE
    ),
}
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ManifestError(ValueError):
    """产物缺失、命名歧义或签名无效"""


@dataclass(frozen=True)
class Artifact:
    path: Path
    platform: str
    signature_path: Path
    signature: str


def detect_arch(filename: str) -> str:
    """从文件名判断架构;同时出现多个架构标记视为歧义"""
    found = {
        arch for arch, pattern in _ARCH_PATTERNS.items() if pattern.search(filename)
    }
    if len(found) > 1:
        raise ManifestError(f"{filename}: 文件名同时包含多个架构标记,无法区分")
    if not found:
        raise ManifestError(
            f"{filename}: 无法判断架构,文件名应包含 target triple"
            "(如 aarch64-apple-darwin / x86_64-pc-windows-msvc)或明确的架构标记"
        )
    return found.pop()


def classify_artifact(path: Path) -> str | None:
    """返回平台 key;与 updater 无关的文件返回 None"""
    name = path.name
    lower = name.lower()
    if lower.endswith(".exe"):
        if "setup" not in lower:
            return None
        if detect_arch(name) != "x86_64":
            raise ManifestError(f"{name}: Windows 仅支持 x86_64 产物")
        return "windows-x86_64"
    if lower.endswith(".app.tar.gz"):
        arch = detect_arch(name)
        return "darwin-aarch64" if arch == "aarch64" else "darwin-x86_64"
    return None


def _read_signature(artifact: Path) -> tuple[Path, str]:
    signature_path = artifact.with_name(artifact.name + ".sig")
    if not signature_path.is_file():
        raise ManifestError(f"{artifact.name}: 缺少签名文件 {signature_path.name}")
    try:
        signature = signature_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ManifestError(f"读取签名失败: {signature_path} ({exc})") from exc
    if not signature:
        raise ManifestError(f"{signature_path.name}: 签名为空")
    return signature_path, signature


def discover_artifacts(artifact_dirs: list[Path]) -> dict[str, Artifact]:
    """扫描产物目录,返回 {平台: Artifact};缺失/重复/签名问题都会报错"""
    if not artifact_dirs:
        raise ManifestError("未提供产物目录")
    files: list[Path] = []
    for directory in artifact_dirs:
        if not directory.is_dir():
            raise ManifestError(f"产物目录不存在: {directory}")
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file() and not path.name.endswith(".sig")
        )

    found: dict[str, Artifact] = {}
    for path in sorted(files, key=lambda item: str(item)):
        platform = classify_artifact(path)
        if platform is None:
            continue
        if platform in found:
            previous = found[platform]
            raise ManifestError(
                f"平台 {platform} 匹配到多个产物:"
                f"{previous.path.name} / {path.name},无法确定上传哪个"
            )
        signature_path, signature = _read_signature(path)
        found[platform] = Artifact(
            path=path,
            platform=platform,
            signature_path=signature_path,
            signature=signature,
        )

    missing = [platform for platform in PLATFORM_KEYS if platform not in found]
    if missing:
        found_names = ", ".join(sorted(item.path.name for item in found.values()))
        raise ManifestError(
            "缺少平台产物: "
            + ", ".join(missing)
            + (f"(已发现: {found_names})" if found_names else "")
            + ";文件名需带 target triple,如 "
            "NewsPaperCrawl_0.2.0_aarch64-apple-darwin.app.tar.gz 或 "
            "NewsPaperCrawl_0.2.0_x86_64-pc-windows-msvc-setup.exe"
        )
    return found


def normalize_version(version: str) -> str:
    value = (version or "").strip()
    if value[:1] in {"v", "V"}:
        value = value[1:]
    if not _VERSION_RE.match(value):
        raise ManifestError(f"版本号格式不合法: {version!r}(期望 X.Y.Z)")
    return value


def normalize_pub_date(value: str | None) -> str:
    """统一成 UTC 的 YYYY-MM-DDTHH:MM:SSZ;Tauri 不保证解析带时区的其他写法"""
    if value is None:
        moment = datetime.now(UTC)
    else:
        text = value.strip()
        if not text:
            raise ManifestError("pub_date 不能为空")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ManifestError(f"pub_date 不是合法的 ISO8601 时间: {value!r}") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def artifact_url(
    filename: str,
    *,
    repo: str,
    tag: str,
    base_url: str | None = None,
) -> str:
    quoted = quote(filename, safe="")
    if base_url:
        return f"{base_url.rstrip('/')}/{quoted}"
    return f"https://github.com/{repo}/releases/download/{tag}/{quoted}"


def build_manifest(
    *,
    version: str,
    repo: str = DEFAULT_REPO,
    artifacts: dict[str, Artifact],
    notes: str,
    pub_date: str | None = None,
    tag: str | None = None,
    base_url: str | None = None,
) -> dict:
    """组装 latest.json 内容(不写盘)"""
    version = normalize_version(version)
    if not _REPO_RE.match(repo):
        raise ManifestError(f"repo 格式不合法: {repo!r}(期望 owner/name)")
    tag = (tag or f"v{version}").strip()
    if not tag:
        raise ManifestError("tag 不能为空")
    notes = notes.strip() or f"NewsPaperCrawl v{version}"

    platforms: dict[str, dict[str, str]] = {}
    for platform in PLATFORM_KEYS:
        artifact = artifacts.get(platform)
        if artifact is None:
            raise ManifestError(f"缺少平台产物: {platform}")
        if not artifact.signature.strip():
            raise ManifestError(f"{artifact.path.name}: 签名为空")
        platforms[platform] = {
            "signature": artifact.signature,
            "url": artifact_url(
                artifact.path.name,
                repo=repo,
                tag=tag,
                base_url=base_url,
            ),
        }
    return {
        "version": version,
        "notes": notes,
        "pub_date": normalize_pub_date(pub_date),
        "platforms": platforms,
    }


def write_manifest(manifest: dict, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output


def _read_notes(args: argparse.Namespace) -> str:
    if args.notes_file:
        try:
            return args.notes_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManifestError(f"读取发布说明失败: {args.notes_file} ({exc})") from exc
    return args.notes or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 Tauri updater latest.json")
    parser.add_argument("--version", required=True, help="发布版本,如 0.2.0")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub owner/repo")
    parser.add_argument(
        "--artifacts",
        type=Path,
        action="append",
        required=True,
        help="构建产物目录,可重复传入",
    )
    parser.add_argument("--output", type=Path, default=Path("latest.json"))
    parser.add_argument("--notes", help="发布说明(默认为空时使用通用文案)")
    parser.add_argument("--notes-file", type=Path, help="从文件读取发布说明")
    parser.add_argument("--pub-date", help="RFC3339 时间,默认当前 UTC 时间")
    parser.add_argument("--tag", help="Release tag,默认 v<version>")
    parser.add_argument("--base-url", help="自定义下载前缀(默认 GitHub Release)")
    args = parser.parse_args(argv)

    try:
        notes = _read_notes(args)
        artifacts = discover_artifacts(args.artifacts)
        manifest = build_manifest(
            version=args.version,
            repo=args.repo,
            artifacts=artifacts,
            notes=notes,
            pub_date=args.pub_date,
            tag=args.tag,
            base_url=args.base_url,
        )
        output = write_manifest(manifest, args.output)
    except ManifestError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(f"已生成 {output}")
    for platform, entry in manifest["platforms"].items():
        print(f"  {platform}: {entry['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
