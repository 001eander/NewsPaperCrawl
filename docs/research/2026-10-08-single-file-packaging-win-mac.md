# 调研：Windows / macOS 单文件可执行打包方案

> Wayfinder research ticket #5 · 分支 `research/packaging` · 2026-10-08
> 结论对应 issue：[001eander/NewsPaperCrawl#5](https://github.com/001eander/NewsPaperCrawl/issues/5)

本文只依据官方文档、官方仓库一手资料。每个论断后附来源；文末统一列出链接。

---

## 1. 结论（TL;DR）

**首选 PyInstaller，发布形态用「onefile」；macOS 的交付物用带签名的 `.app`（配 DMG）而不是裸二进制。** 备选 Nuitka（当启动速度或反编译保护成为硬指标时切换）。Briefcase 不作为主方案：它在 macOS 的签名/公证体验最好，但产物是 `.app` / DMG / MSI，**不支持单文件**。pyApp 需要 Rust 工具链且首次运行自举下载，不满足「离线双击即跑」。

三条与 ticket 背景直接相关的判断：

1. **客户端把 `playwright` 依赖移除**。cookie 由中心统一下发，客户端不需要浏览器。若保留登录兜底，Chromium 单浏览器缓存即约 281 MB（官方给出），且 either 打包器都要额外处理浏览器分发，得不偿失。
2. **「单文件」在 macOS 上有语义差异**。macOS 的 GUI 应用天然是 `.app` bundle（Finder 把它当单个文件双击）。PyInstaller 官方明确不建议 `onefile + windowed`，并指出该组合无法在启用沙箱时签名公证。所以 macOS 的「一键运行」= 已签名公证的 `.app`。
3. **杀毒误报是两家的共同风险，不是选型能消除的**。PyInstaller 与 Nuitka 官方都承认默认构建会被杀毒引擎标记，代码签名证书能降低概率但不保证。面向非开发者用户，签名证书（Apple Developer ID + Windows 代码签名）应列入方案预算。

---

## 2. 候选方案速览

| 维度 | PyInstaller | Nuitka | Briefcase | pyApp |
|---|---|---|---|---|
| 真正单文件 | ✅ `--onefile` | ✅ `--mode=onefile` | ❌ 输出 MSI / `.app` / DMG / PKG | ⚠️ 单一二进制，但首次运行自举 |
| Windows | `.exe` | `.exe` | MSI 安装包 / ZIP | `.exe` |
| macOS | 裸可执行 或 `.app` | 裸可执行 或 `.app`（`--macos-create-app-bundle`） | `.app` / DMG / PKG | 裸可执行 |
| 构建前提 | 在目标平台构建，装 PyInstaller | 在目标平台构建，需 C 编译器 | 需平台工具链 | 需 Rust/Cargo |
| 单文件启动 | 每次运行解包到临时目录，较慢（官方） | onefile 同样解包，官方称「启动快」 | 不适用 | 首次自举，后续直接 exec |
| 体积 | 体积较大（含 CPython 解释器） | 通常更小、无字节码 | 包含完整运行时 | 运行时单独下载/可内嵌 |
| macOS 签名公证 | 支持 `--codesign-identity`（触发 hardened runtime） | 支持 `--macos-sign-identity` / `--macos-sign-notarization` | `briefcase package` 默认签名+公证 | 需自行处理 |
| AV 误报 | 官方承认普遍，签名可缓解 | 官方承认普遍，商业版有专门指引 | 未专门讨论 | 未专门讨论 |
| uv 集成 | `uv run --with pyinstaller` 顺畅 | `uv tool run --from nuitka==X` 官方示例 | `uvx briefcase` | 运行时可调用 uv |

---

## 3. 逐维度证据

### 3.1 单文件能力

- PyInstaller：`-F, --onefile` 生成单文件可执行；启动时把内嵌归档解压到 `_MEIxxxxxx` 临时目录再运行。官方明确说明「单文件比单目录启动慢」，且解压目录在崩溃或被强杀时不会清理。（PyInstaller operating-mode / usage）
- Nuitka：`--mode=onefile` 生成自解压单二进制，运行时解包到临时目录；官方推荐先用 `--mode=standalone` 调试再切 onefile。可用 `--onefile-tempdir-spec` 指定缓存目录，**避免每次运行重复解包**（对启动速度与 Windows 防火墙识别都有好处）。（Nuitka use-cases）
- Briefcase：官方产物列表为 macOS 的 `.app`/DMG/PKG、Windows 的 MSI/ZIP、Linux 的 AppImage 等，**没有单文件可执行**。`.app` 可被当作一个文件复制或压缩，但本质是目录。（Briefcase FAQ / macOS app 页）
- pyApp：用 Rust 包装，产物是单一二进制；但运行时首次启动会自举（下载 Python 发行版、按需下载 uv/pip 并安装依赖），之后才直接 exec。可把依赖内嵌以避免联网，但构建需要 Rust 工具链。（pyApp about / runtime）

### 3.2 体积

- PyInstaller 会把「当前 Python 解释器本身」一起打包，因此体积下限 = CPython 运行时 + 站点库。官方 description：「collects copies of all those files – including the active Python interpreter」。（PyInstaller operating-mode）
- Nuitka 把 Python 编译成 C 再编译为机器码，运行时不依赖解释器字节码，通常更小；代价是构建慢、需要 C 编译器。（Nuitka user-manual requirements）
- **Playwright 是体积的决定性变量**。官方给出浏览器缓存实测：Chromium 281 MB、Firefox 187 MB、WebKit 180 MB，且「会占用数百 MB 磁盘」。（Playwright browsers）
- 若把浏览器内嵌进单文件：官方指引用 `PLAYWRIGHT_BROWSERS_PATH=0 playwright install chromium` 再 `pyinstaller -F`，并注明「会把浏览器打进可执行文件，体积更大；建议只打用到的浏览器」。（Playwright library-python）

### 3.3 启动速度

- PyInstaller：单目录 `onedir` 启动快，`onefile` 每次运行要解压，官方反复强调 onefile「a little slower」，并建议先验证 onedir。（PyInstaller operating-mode）
- Nuitka：官方称 onefile 启动很快（「The startup of Nuitka in Onefile is fast」），且提供缓存解包路径来进一步优化；onefile 默认每次解包到带 PID/TIME 的唯一临时路径。（Nuitka user-manual / use-cases）
- 结论：两者 onefile 都有一次解包开销。要稳定低延迟，PyInstaller 的 onedir（或 Nuitka 的 cached onefile）更稳，但 onedir 不符合「单文件」直觉。

### 3.4 签名与公证

- **macOS 强制签名**：Apple Silicon 上必须代码签名（哪怕是 ad-hoc）。PyInstaller 在改动二进制头后会重签，默认 ad-hoc 重签全部收集的二进制，`--codesign-identity` 可换成真实身份并自动开启 hardened runtime。（PyInstaller feature-notes）
- **公证（notarization）**：Apple 要求分发软件用 Developer ID 证书签名、带安全时间戳、可含 hardened runtime；公证不是 App Review，是自动扫描。（Apple：Notarizing macOS software）
- PyInstaller：`--codesign-identity` + `--osx-entitlements-file`；`--windowed` 生成 `.app` 并 deep sign。（PyInstaller usage / feature-notes）
- Nuitka：`--macos-sign-identity`（默认 ad-hoc）、`--macos-sign-notarization`（开启 runtime 签名选项）、`--macos-sign-keyring-filename`。（Nuitka man / Signing.py）
- Briefcase：`briefcase package` 默认对 macOS 应用**同时签名与公证**；`--no-notarize`、`--resume`（公证可能耗时数小时，可断点续）、`--adhoc-sign`（仅本机可运行）。这是 Briefcase 相对前两者最明显的差异化优势。（Briefcase macOS platform）
- 注意：PyInstaller 官方指出 `onefile + windowed` 的 `.app`「效率低，且启用沙箱时无法签名公证」（沙箱是 Mac App Store 硬要求）。这也是建议 macOS 用 onedir `.app` 的原因。（PyInstaller usage）

### 3.5 杀毒误报

- PyInstaller 官方 issue 模板直言：**没有魔法开关**，机器学习型杀毒会把「用了 PyInstaller 的恶意软件」泛化到所有 PyInstaller 产物；只能向 AV 厂商提交误报或自编译 bootloader；**购买代码签名证书可以让误报「更少但不保证消除」**。官方也不建议给 onefile 可执行「以管理员运行」。（PyInstaller `.github/ISSUE_TEMPLATE/antivirus.md`）
- Nuitka 官方 common-issue 页：Windows 默认设置编译的二进制常被杀毒标记，**规避方法在商业版里才有完整支持与指引，且不保证**。维护者进一步解释：Nuitka 因保护源码成为恶意软件作者偏好，onefile 因「落盘并执行临时文件」尤其容易被 ML 引擎判为 Trojan。（Nuitka common-issue-solutions；issue #2685 / #3842）
- 两条都指向同一缓解手段：**Windows 代码签名证书**。这会让「非开发者双击即跑」在 SmartScreen/Defender 下顺利很多。

### 3.6 uv 生态工作流

- 运行构建工具：`uv run --with pyinstaller pyinstaller ...`（PyInstaller 在项目 venv 的依赖树里，`uv run` 保证锁定版本）；Nuitka 官方 uv 文档给出了 `uv tool run --from nuitka==2.6.7 nuitka --version` 的示例。（uv tools）
- 构建分发包：`uv build` 产出 wheel/sdist；Briefcase 用 pip 安装 `requires`，其 FAQ 明确可用 `uvx briefcase` 运行。（uv projects；Briefcase FAQ）
- pyApp 运行时支持调用 uv（「UV enabled」分支）来建 venv/装依赖。（pyApp runtime）
- 跨平台构建：PyInstaller 官方要求在**每个目标 OS 上分别构建**（「you must install PyInstaller on each platform and bundle your app separately on each」），不能交叉编译。Nuitka 同理需匹配目标架构。建议用 GitHub Actions 的 Windows/macOS runner 各跑一次。（PyInstaller usage）

### 3.7 tkinter（现有 GUI）

- PyInstaller 内置对 TkInter、Qt、wx 等 GUI 包的支持。（PyInstaller operating-mode）
- Nuitka 的插件与包配置覆盖 Tkinter 的 DLL 收集。（Nuitka common-issue-solutions）
- 注意：PyInstaller 的 splash 功能基于 Tcl/Tk，**在 macOS 上不兼容**（Tcl/Tk 不允许 splash 的线程模型）。本仓库 GUI 若想加 splash，仅 Windows 可行。（PyInstaller usage）

---

## 4. 对本仓库的具体落地建议

### 4.1 依赖与入口

- 入口用 `gui.py`。它调用 `crawler.bootstrap.run_stage`，与 `cli.py` 平级，打包时以 `gui.py` 为 spec 入口即可。
- **把 `playwright` 从客户端依赖移除**（当前在 `pyproject.toml` 第 12 行）。这直接决定单文件是「几十 MB」还是「数百 MB」。`scripts/login.py` 依赖 Playwright，打包时不能把该脚本当普通源码塞进去运行。

### 4.2 必须处理的路径问题（否则打包后必崩）

- `crawler/adapters/config.py:43` 用 `Path(__file__).resolve().parents[2]` 推导项目根。冻结后 `__file__` 指向解包/安装目录，`data/`、`logs/`、`.secrets/` 会落到只读或临时位置。**打包版必须把 `NEWSPAPER_DATA_DIR` / `NEWSPAPER_LOG_DIR` / `NEWSPAPER_SECRETS_DIR` 指到用户目录**（Windows `%APPDATA%`，macOS `~/Library/Application Support`），或在 `Settings.from_env` 里识别 `sys.frozen` 后改用 `platformdirs` 风格的用户目录。
- `gui.py:51` 的 `LOGIN_SCRIPT = Path(__file__).resolve().parent / "scripts" / "login.py"` 在冻结后不存在；且 `scripts/login.py` 是 Playwright 同步脚本，需要独立解释器与浏览器。移除登录兜底后，这段子进程逻辑应一并调整。
- `archive/报纸清单.csv` 是打包数据文件，用 `--add-data`（PyInstaller）或 `--include-data-files`（Nuitka）带上；注意中文文件名在 Windows 构建环境的编码。

### 4.3 PyInstaller 起步命令（建议流程）

先出 onedir 验证功能，再出 onefile 发布：

```bash
# 验证（更快、易排查）
uv run --with pyinstaller pyinstaller --noconfirm --windowed --name NewsPaperCrawl \
  --add-data "archive/报纸清单.csv;archive" \
  --exclude-module playwright --exclude-module pytest \
  gui.py
# 发布单文件（Windows）
uv run --with pyinstaller pyinstaller --noconfirm --onefile --windowed --name NewsPaperCrawl \
  --add-data "archive/报纸清单.csv;archive" \
  --exclude-module playwright --exclude-module pytest \
  gui.py
```

macOS 上把 `--windowed` 生成的 `.app` 再用 `--codesign-identity` 签名并走公证。

### 4.4 何时切 Nuitka

满足任一条件就评估迁移：单文件启动时间不可接受、需要更强源码保护、或愿意采购 Nuitka 商业版以获得其 AV 规避与签名指引。Nuitka 的 `--mode=onefile --onefile-tempdir-spec="{CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}"` 可把「每次解包」变成「首次解包后缓存」，兼顾单文件与启动速度。

### 4.5 Briefcase 的定位

如果 macOS 分发体验（自动签名+公证、DMG/PKG）被证明是主要痛点，可以为 macOS 单开一条 Briefcase 流水线，Windows 继续 PyInstaller。这是「两套工具链」的代价，但能拿到最省心的 Apple 侧合规。Briefcase 需要把项目改造成它要求的 `pyproject.toml` 布局与 `requires` 声明。

---

## 5. 未决问题 / 建议的验证动作

1. 在 Windows 上实测 PyInstaller onedir 与 onefile 的实际体积与冷启动时间，并核对 `最新` Windows Defender 是否误报（当前无法从文档预判）。
2. 在 macOS 上确认 `.app` 经 Developer ID 签名+公证后，从浏览器下载再双击是否被 Gatekeeper 拦。
3. 确认「cookie 由中心统一下发」的具体落地方式：是否还需要客户端浏览器登录。若不需要，删除 `playwright` 依赖与 `scripts/login.py` 的 GUI 入口；若需要，单独评估外置浏览器的分发方案。
4. 决定打包版的数据目录策略（用户目录 vs 安装目录旁），并据此改 `Settings.from_env`。
5. 决定是否购买代码签名证书（Windows OV/EV + Apple Developer ID）。这几乎是「非开发者双击即跑」体验的前置条件。

---

## 6. 参考来源（一手）

- PyInstaller — *Using PyInstaller*：https://pyinstaller.org/en/stable/usage.html
- PyInstaller — *What PyInstaller Does and How It Does It*（onefile/onedir、tkinter）：https://pyinstaller.org/en/stable/operating-mode.html
- PyInstaller — *Notes about specific Features*（macOS 签名、hardened runtime）：https://pyinstaller.org/en/stable/feature-notes.html
- PyInstaller — 官方反病毒误报说明（issue 模板）：https://github.com/pyinstaller/pyinstaller/blob/develop/.github/ISSUE_TEMPLATE/antivirus.md
- Nuitka — *User Manual*（编译器要求、onefile、签名选项、AV 说明）：https://nuitka.net/user-documentation/user-manual.html
- Nuitka — *Solutions to the Common Issues*（Windows Virus Scanners、data files、Tkinter）：https://nuitka.net/user-documentation/common-issue-solutions.html
- Nuitka — *Use Cases*（onefile、`--onefile-tempdir-spec`）：https://nuitka.net/user-documentation/use-cases.html
- Nuitka — man page（`--macos-sign-identity` 等）：https://github.com/Nuitka/Nuitka/blob/develop/doc/nuitka.1
- Briefcase — FAQ（产物清单、`uvx briefcase`）：https://briefcase.readthedocs.io/en/stable/about/faq.html
- Briefcase — macOS platform（签名/公证）：https://briefcase.beeware.org/en/stable/reference/platforms/macOS/
- Briefcase — `.app` bundle：https://briefcase.readthedocs.io/en/stable/reference/platforms/macOS/app.html
- pyApp — About / How-to / Runtime behavior：https://ofek.dev/pyapp/latest/ · https://ofek.dev/pyapp/latest/how-to/ · https://ofek.dev/pyapp/latest/runtime/
- uv — *Using tools*（`uv tool run --from nuitka`）：https://docs.astral.sh/uv/guides/tools/
- uv — *Working on projects*（`uv build`）：https://docs.astral.sh/uv/guides/projects/
- Playwright Python — *Browsers*（浏览器体积、缓存路径）：https://playwright.dev/python/docs/browsers
- Playwright Python — *Library / Pyinstaller*（`PLAYWRIGHT_BROWSERS_PATH=0`）：https://playwright.dev/python/docs/library
- Apple — *Notarizing macOS software before distribution*：https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
