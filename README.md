# 浙图“方正数字报”爬虫

抓取浙江图书馆“方正数字报”的期次、版面、版面图、报道正文和配图。数据来源是 `archive/报纸清单.csv`，通过浙江图书馆代理访问，需要有效的图书馆登录状态。

桌面版使用 Tauri 2，爬虫仍由原有 Python 异步流水线执行。支持断点续跑、失败重试、同阶段互斥和 SQLite 原子认领。

## 下载与安装

从 [GitHub Releases](https://github.com/001eander/NewsPaperCrawl/releases/latest) 下载对应系统的正式版本：

- Windows x64：`windows-x86_64` NSIS 安装程序。
- macOS Apple Silicon：`darwin-aarch64` DMG。
- macOS Intel：`darwin-x86_64` DMG。

安装后不需要另外安装 Python、Node.js 或 Playwright。首次点击“登录”时，应用会提示并把 Chromium 下载到用户数据目录；这只在浏览器缺失或 Playwright 需要新版本时发生。

未配置 Apple Developer ID、notarytool 或 Windows 代码签名证书的内部构建是未签名测试包，可能出现 Gatekeeper/SmartScreen 警告。更新包另有强制 Tauri 签名校验，但这不等同于操作系统代码签名。

## 使用

1. 在“配置”页填写图书馆账号密码；Bark 推送可选。
2. 在“运行”页点击“登录”，应用会自动完成 SSO 并保存 cookie。
3. 依次执行 `import`、`issues`、`boards`、`articles`。各阶段可重复运行，已完成项会跳过。
4. 在“状态”页查看总进度和最近失败，在“日志”页查看实时输出。
5. 认证失效时重新登录。`failed` 项可用 `scripts/retry_failed.py` 重置后重试。

“关于/更新”页可以检查正式 Release。发现新版本后，点击更新即可下载、验签、安装并重启。爬取或登录仍在运行时需先停止；更新不会删除数据库、图片、配置或浏览器。桌面应用采用单实例，重复启动只会激活已有窗口，避免重复任务与通知。Bark 使用随应用打包的 CA 证书，测试推送仅在服务端实际接受后提示成功。

## 数据位置与旧版迁移

安装版的数据库、正文、图片、凭据、日志和浏览器都在系统应用数据目录中，不写入安装目录。可在“配置”页点击按钮打开数据、日志和凭据目录。

应用不会自动移动源码版的旧数据。迁移前先退出旧爬虫，再把源码目录中的 `data/`、`.secrets/` 和需要保留的 `logs/` 复制到安装版对应目录；不要覆盖已经产生的新数据。SQLite 相对路径约定不变。

源码 CLI 和 Docker 的默认路径不变：

- SQLite：`data/crawl.db`；正文与图片在 `data/articles/`、`data/boards/` 等目录。
- 凭据与 cookie：`.secrets/`。
- 日志：`logs/crawl.log`，按天滚动。

## 源码运行

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
uv run cli.py import
uv run cli.py issues 2014 2026 --end-month 7
uv run cli.py boards
uv run cli.py articles
uv run scripts/login.py
```

`boards` 和 `articles` 可用 `--limit` 分批执行；各阶段也支持 `--concurrency`。Docker 仍使用 `Dockerfile` 和 `compose.yaml`。

## 桌面开发

需要 Node.js 24、Rust stable（至少 1.90）和上述 Python 环境：

```bash
npm ci
uv sync
npm run sidecar
npm run tauri dev
```

常用检查：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
npm test
npm run build
npm run check-versions
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo clippy --manifest-path src-tauri/Cargo.toml -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
npm run tauri build
```

桌面结构：`desktop/` 是 TypeScript 界面，`src-tauri/` 负责窗口、sidecar 生命周期和更新，`crawler/desktop_core.py` 通过 JSON Lines 执行核心命令。PyInstaller 配置在 `packaging/`，sidecar 按 Rust target triple 命名并嵌入应用。macOS 本地构建 DMG 后执行 `uv run python packaging/polish_dmg.py <dmg路径>`，移除额外卷图标并设置应用扩展名展示；Release 工作流会自动执行该步骤。

## 发布

CI 在 `.github/workflows/ci.yml`。推送 `v*` tag 会触发 `.github/workflows/release.yml`，分别构建 Windows x64、macOS ARM64 和 macOS Intel，等待三者全部成功后生成统一的 `latest.json` 并上传 Release。

首次发布前需要：

1. 用 `npm run tauri signer generate` 生成更新密钥。私钥只保存在 `.secrets/`、GitHub Actions secret `TAURI_SIGNING_PRIVATE_KEY` 和离线备份中；公钥写入 `src-tauri/tauri.conf.json`。可选密码放在 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`。
2. 确认 `pyproject.toml`、`crawler/__init__.py` 和 Tauri 配置版本一致，tag 为相同版本加 `v`。
3. 正式发布建议配置 Apple 证书/notarytool 与 Windows 代码签名。Apple 构建变量包括 `APPLE_CERTIFICATE`、`APPLE_CERTIFICATE_PASSWORD`、`APPLE_SIGNING_IDENTITY`、`APPLE_ID`、`APPLE_PASSWORD`、`APPLE_TEAM_ID`。
4. 发布前用旧版和新版实测检查、下载、验签、安装、重启及数据保留；损坏签名必须被拒绝。

不要提交 `.secrets/`、爬取数据、日志、sidecar 二进制或更新私钥。
