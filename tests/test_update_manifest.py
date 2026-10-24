"""打包脚本的纯逻辑测试:latest.json 生成 + 版本一致性校验

不触网、不构建产物:manifest 测试用 tmp_path 造文件和签名;版本校验用临时
仓库目录验证三处版本号与 tag。两个脚本都按文件路径加载,避免与 site-packages
里的 ``packaging`` 包重名。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.0"
WIN = f"NewsPaperCrawl_{VERSION}_x86_64-pc-windows-msvc-setup.exe"
ARM = f"NewsPaperCrawl_{VERSION}_aarch64-apple-darwin.app.tar.gz"
INTEL = f"NewsPaperCrawl_{VERSION}_x86_64-apple-darwin.app.tar.gz"


def _load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 在 exec 时需要通过 sys.modules 找回模块
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


manifest = _load_module("build_update_manifest", "packaging/build_update_manifest.py")
version_check = _load_module("check_versions", "scripts/check_versions.py")


def _artifact(tmp_path: Path, name: str, signature: str | None) -> Path:
    directory = tmp_path / "artifacts"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_bytes(b"payload")
    if signature is not None:
        (directory / f"{name}.sig").write_text(signature, encoding="utf-8")
    return path


def _release(tmp_path: Path) -> Path:
    _artifact(tmp_path, WIN, "win-signature")
    _artifact(tmp_path, ARM, "arm-signature")
    _artifact(tmp_path, INTEL, "intel-signature")
    return tmp_path / "artifacts"


def test_build_manifest_inlines_signature_and_urls(tmp_path):
    artifacts = manifest.discover_artifacts([_release(tmp_path)])
    doc = manifest.build_manifest(
        version=VERSION,
        repo="001eander/NewsPaperCrawl",
        artifacts=artifacts,
        notes="发布说明",
        pub_date="2026-10-24T12:34:56+08:00",
    )

    assert doc["version"] == VERSION
    assert doc["pub_date"] == "2026-10-24T04:34:56Z"
    assert doc["notes"] == "发布说明"
    assert doc["platforms"]["windows-x86_64"]["signature"] == "win-signature"
    assert doc["platforms"]["darwin-aarch64"]["signature"] == "arm-signature"
    assert doc["platforms"]["darwin-x86_64"]["signature"] == "intel-signature"
    assert doc["platforms"]["darwin-aarch64"]["url"] == (
        "https://github.com/001eander/NewsPaperCrawl/releases/download/"
        f"v{VERSION}/{ARM}"
    )


def test_build_manifest_accepts_v_prefix_and_custom_tag(tmp_path):
    artifacts = manifest.discover_artifacts([_release(tmp_path)])
    doc = manifest.build_manifest(
        version="v0.2.0",
        artifacts=artifacts,
        notes="",
        tag="release-0.2.0",
        pub_date="2026-10-24T00:00:00Z",
    )
    assert doc["version"] == "0.2.0"
    assert doc["notes"] == "NewsPaperCrawl v0.2.0"
    assert "/download/release-0.2.0/" in doc["platforms"]["windows-x86_64"]["url"]


def test_write_manifest_outputs_utf8_json(tmp_path):
    artifacts = manifest.discover_artifacts([_release(tmp_path)])
    doc = manifest.build_manifest(
        version=VERSION,
        artifacts=artifacts,
        notes="中文说明",
        pub_date="2026-10-24T00:00:00Z",
    )
    output = manifest.write_manifest(doc, tmp_path / "out" / "latest.json")
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written == doc
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_artifact_url_can_use_custom_base(tmp_path):
    artifacts = manifest.discover_artifacts([_release(tmp_path)])
    doc = manifest.build_manifest(
        version=VERSION,
        artifacts=artifacts,
        notes="",
        base_url="https://cdn.example.com/releases/",
        pub_date="2026-10-24T00:00:00Z",
    )
    assert doc["platforms"]["windows-x86_64"]["url"] == (
        f"https://cdn.example.com/releases/{WIN}"
    )


def test_missing_platform_fails(tmp_path):
    _artifact(tmp_path, WIN, "win-signature")
    _artifact(tmp_path, ARM, "arm-signature")
    with pytest.raises(manifest.ManifestError, match="darwin-x86_64"):
        manifest.discover_artifacts([tmp_path / "artifacts"])


def test_missing_signature_fails(tmp_path):
    directory = _release(tmp_path)
    (directory / f"{ARM}.sig").unlink()
    with pytest.raises(manifest.ManifestError, match="缺少签名"):
        manifest.discover_artifacts([directory])


def test_empty_signature_fails(tmp_path):
    directory = _release(tmp_path)
    (directory / f"{ARM}.sig").write_text("  \n", encoding="utf-8")
    with pytest.raises(manifest.ManifestError, match="签名为空"):
        manifest.discover_artifacts([directory])


def test_ambiguous_arch_fails(tmp_path):
    name = f"NewsPaperCrawl_{VERSION}_x86_64_aarch64-apple-darwin.app.tar.gz"
    _artifact(tmp_path, WIN, "win-signature")
    _artifact(tmp_path, INTEL, "intel-signature")
    _artifact(tmp_path, name, "both-signature")
    with pytest.raises(manifest.ManifestError, match="多个架构"):
        manifest.discover_artifacts([tmp_path / "artifacts"])


def test_missing_arch_fails(tmp_path):
    _artifact(tmp_path, WIN, "win-signature")
    _artifact(tmp_path, ARM, "arm-signature")
    _artifact(tmp_path, "NewsPaperCrawl.app.tar.gz", "intel-signature")
    with pytest.raises(manifest.ManifestError, match="无法判断架构"):
        manifest.discover_artifacts([tmp_path / "artifacts"])


def test_duplicate_platform_fails(tmp_path):
    second_arm = f"NewsPaperCrawl_{VERSION}_arm64-apple-darwin.app.tar.gz"
    _release(tmp_path)
    _artifact(tmp_path, second_arm, "second-arm-signature")
    with pytest.raises(manifest.ManifestError, match="多个产物"):
        manifest.discover_artifacts([tmp_path / "artifacts"])


def test_cli_writes_latest_json(tmp_path, capsys):
    directory = _release(tmp_path)
    output = tmp_path / "latest.json"
    code = manifest.main(
        [
            "--version",
            VERSION,
            "--artifacts",
            str(directory),
            "--notes",
            "notes",
            "--pub-date",
            "2026-10-24T00:00:00Z",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["version"] == VERSION
    assert "已生成" in capsys.readouterr().out


def test_cli_missing_artifacts_returns_error(tmp_path, capsys):
    code = manifest.main(["--version", VERSION, "--artifacts", str(tmp_path / "nope")])
    assert code == 1
    assert "错误" in capsys.readouterr().err


def _version_tree(
    root: Path,
    *,
    pyproject: str = VERSION,
    crawler: str = VERSION,
    tauri: str = VERSION,
    tauri_v1_layout: bool = False,
) -> Path:
    (root / "crawler").mkdir(parents=True)
    (root / "src-tauri").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "{pyproject}"\n',
        encoding="utf-8",
    )
    (root / "crawler" / "__init__.py").write_text(
        f'__version__ = "{crawler}"\n', encoding="utf-8"
    )
    if tauri_v1_layout:
        data = {"package": {"version": tauri}}
    else:
        data = {"version": tauri}
    (root / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    return root


def test_check_versions_accepts_matching_tag(tmp_path):
    root = _version_tree(tmp_path)
    versions = version_check.check_versions(root, tag="v0.2.0")
    assert set(versions.values()) == {VERSION}
    assert version_check.normalize_tag("0.2.0") == VERSION


def test_check_versions_reads_tauri_v1_layout(tmp_path):
    root = _version_tree(tmp_path, tauri_v1_layout=True)
    versions = version_check.check_versions(root, tag="v0.2.0")
    assert versions["src-tauri/tauri.conf.json"] == VERSION


def test_check_versions_rejects_mismatch(tmp_path):
    root = _version_tree(tmp_path, crawler="0.1.0")
    with pytest.raises(version_check.VersionError, match="不一致"):
        version_check.check_versions(root, tag="v0.2.0")


def test_check_versions_rejects_tag_mismatch(tmp_path):
    root = _version_tree(tmp_path)
    with pytest.raises(version_check.VersionError, match="项目版本"):
        version_check.check_versions(root, tag="v0.3.0")


def test_check_versions_rejects_missing_tauri(tmp_path):
    _version_tree(tmp_path)
    (tmp_path / "src-tauri" / "tauri.conf.json").unlink()
    with pytest.raises(version_check.VersionError, match="tauri.conf.json"):
        version_check.check_versions(tmp_path)


def test_check_versions_cli_reports_missing_tauri(tmp_path, capsys):
    _version_tree(tmp_path)
    (tmp_path / "src-tauri" / "tauri.conf.json").unlink()
    assert version_check.main(["--root", str(tmp_path), "--tag", "v0.2.0"]) == 1
    assert "错误" in capsys.readouterr().err
