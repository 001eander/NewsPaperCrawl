# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

异步爬虫,抓取浙江图书馆"方正数字报"电子报纸资源的**原始数据**(期次 → 版面 → 版面图 → 报道正文 + 配图)。目标是全国报纸清单(`archive/报纸清单.csv`,含刊号/刊期/出版地/订阅起止日期),通过图书馆代理镜像 `https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/` 访问 apabi 站点。

关键特性:
- **认证依赖登录 cookie**(SSO 经 vpn358 中转,`vpn358_sid` 是关键会话 cookie),cookie 过期时爬虫抛 `AuthError` 并推 Bark 通知
- **断点续跑**:所有阶段以 SQLite 的 `status` 列为状态机,可反复执行、跳过已完成项
- **礼貌限速**:并发信号量 + 每次请求前 0.3~1.0s 随机延迟,连接忽略 HTTPS 证书校验(站点 TLS 证书过期)

## 常用命令

项目使用 `uv` 管理依赖(Python 3.12,见 `.python-version`)。

```bash
uv sync                          # 安装依赖(含 dev 组: ruff/pytest)
uv run cli.py import             # 阶段0: 导入报纸清单 CSV
uv run cli.py issues 2014 2026 --end-month 7   # 阶段1: 枚举期次(2014~2026-07)
uv run cli.py boards             # 阶段2: 抓版面/位置/版面图(省略 --limit 处理全部 pending)
uv run cli.py articles           # 阶段3: 抓报道正文+配图(省略 --limit 处理全部 pending)
uv run cli.py boards --paperid n.D330100hzrb --limit 10   # 限量或限定报纸跑阶段2
uv run scripts/login.py          # Playwright 登录: 自动填表/验证码/提交,保存 cookie
```

`boards`/`articles` 阶段省略 `--limit` 即处理全部 pending 项;显式 `--limit N` 每轮限量、可分批续跑。

### 测试与 lint

```bash
uv run pytest                    # 全部测试(pyproject.toml 已配置 pythonpath=["."])
uv run pytest tests/test_parser_blocks.py::test_text_only_article   # 单个测试
uv run pytest tests/test_migration.py -k idempotent                 # 按关键字筛
uv run ruff check .              # lint(启用 import 排序 I 规则)
uv run ruff format --check .     # 格式检查
uvx pre-commit run --all-files   # 手动跑全部钩子(ruff-format + ruff)
```

测试均不触网、不落真 DB:`test_parser_blocks.py` 测解析纯函数;`test_migration.py` 用 `SqliteRepository` + 临时库验证迁移;`test_article_store.py` 用 Fake 网关 + `tmp_path` 存储验证配图与正文 JSON 落盘;`test_pipeline.py` 用全 Fake 端口(Gateway/Repo/Store)+ stub 解析函数验证四阶段状态机。

## 架构

四阶段流水线,入口是 [cli.py](cli.py)(argparse 分发),装配与调度在 [crawler/bootstrap.py](crawler/bootstrap.py) 的 `run_stage(stage, **kwargs)`(组合根)。每阶段独立可重复执行:

| 阶段 | 子命令 | 产出 |
|------|--------|------|
| 0 | `import` | `papers` 表 ← 报纸清单 CSV(幂等) |
| 1 | `issues` | `issues` 表 ← 每报每月 `newspaper.issue` 的 `<IssueDate>` 列表 |
| 2 | `boards` | `boards` 表 + 版面图(JPG)+ 位置 JSON;解析 `<map>` area 得报道 bbox → 写 `articles` 表 |
| 3 | `articles` | `articles` 表标 done + 正文 `content.json` + 配图(JPG) |

状态机:每个表都有 `status` 列(`pending/done/failed`),每阶段只取 `pending` 项处理。

### 数据双存储(重要约定)

元数据与爬取状态在 SQLite([data/crawl.db](data/crawl.db),WAL 模式,aiosqlite 避免阻塞事件循环);**正文和图片在文件系统**,DB 只存相对 `DATA_DIR` 的路径:

- 版面图: `data/boards/{code}/{YYYY}/{MM}/{DD}/{board_code}.jpg`
- 位置 JSON: `data/positions/{code}/{YYYY}/{MM}/{DD}/{board_code}.json`
- 报道正文: `data/articles/{code}/{YYYY}/{MM}/{DD}/{metaid_tail}/content.json`(保序 blocks,含图片的 `local_path`)
- 报道配图: 同目录下 `{n}{suffix}.jpg`,文件名序号 = blocks 中 img 块 `index`

其中 `code` = `paperid` 去掉 `n.` 前缀(如 `n.D330100hzrb` → `D330100hzrb`),`metaid_tail` = metaid 去掉 `nw.` 前缀。路径统一**相对 `DATA_DIR`** 存 DB,便于 Docker 卷可移植。

### metaid 编码规则(跨模块约定)

`metaid` 格式横跨解析适配器([apabi_parser.py](crawler/adapters/apabi_parser.py))、领域规则([rules.py](crawler/domain/rules.py))与流水线([pipeline.py](crawler/services/pipeline.py)),改任何一处要同步验证:

- 版面: `nb.{code}_{YYYYMMDD}_{board}`(如 `nb.D411300nyrb_20260731_A1`)
- 报道: `nw.{code}_{YYYYMMDD}_{board}-{seq}`(如 `nw.D330100hzrb_20260110_1-A01`;报道 metaid 必须含 `-`,否则请求会因截断失败)
- **版面编码不统一**(`A01` vs `A1` vs `01`),不能靠猜:[pipeline.py:139-176](crawler/services/pipeline.py#L139-L176) 的 `_crawl_one_issue` 先请求 `paperid` 页面探测真实编码,再把 metaid 里的日期替换成目标日期后请求
- URL 里编码用 `-`(构建版面图 URL 时把 `_` 换掉);图片文件名序号从 1 起

### 模块职责(六边形分层)

依赖方向单向:`domain ← ports ← services ← adapters/bootstrap`;领域层不依赖任何外层,服务层只依赖 domain 与 ports(唯一例外是站点解析来自 `adapters.apabi_parser`,项目约定:解析即站点知识)。

- [cli.py](cli.py) — 薄壳:argparse 子命令定义 + 异常出口(`AuthError` → 报错并以非零码退出)
- [crawler/domain/](crawler/domain/) — 领域层,纯逻辑无 I/O:`models.py`(Paper/Issue/Board/Article/ParsedArticle 等 dataclass 与 `Status` 状态机)、`rules.py`(metaid 编码 / 存储相对路径 / 认证失效特征)、`exceptions.py`(`AuthError`)
- [crawler/ports/](crawler/ports/) — 端口层,用 `typing.Protocol` 声明服务层需要的能力:`http` / `repository` / `storage` / `notifier` / `auth`
- [crawler/services/pipeline.py](crawler/services/pipeline.py) — 服务层:`CrawlService` 编排四阶段,只依赖 domain 规则、三个端口与并发度
- [crawler/adapters/](crawler/adapters/) — 适配器层,端口的具体实现:
  - `apabi_parser.py` — 纯函数 HTML/XML 解析(BeautifulSoup + 正则)与全部 URL 构造(`build_*` 系列)。**全部基于文本解析,不依赖浏览器**;解析逻辑是"浏览器验证过"的固化成代码
  - `apabi_http.py` — aiohttp 异步会话:并发限速、重试(指数退避)、cookie 注入、认证失效检测(重定向到登录域 / 响应含登录页特征)
  - `cookie_auth.py` — 加载 cookie(兼容 Playwright 导出的 list 格式与旧 dict 格式,按 domain 匹配注入);失效判定委托领域规则
  - `sqlite_repo.py` — 全部 SQLite 操作与表结构(`SCHEMA`);含旧库迁移(articles.content → content_path,幂等)
  - `bark.py` — Bark 手机推送,未配置时静默跳过(推送必须 await 完成,见本节末)
  - `fs_storage.py` — 正文 JSON / 位置 JSON 落盘到 `DATA_DIR`
  - `paperlist_csv.py` — 报纸清单 CSV → `domain.Paper` 列表
  - `config.py` — `Settings`(frozen dataclass):所有运行时路径统一收口,支持环境变量覆盖(Docker 用 `NEWSPAPER_*` 注入)
- [crawler/bootstrap.py](crawler/bootstrap.py) — 组合根:读配置、建适配器、注入服务,并负责收尾通知(正常完成 / 认证失效 / 异常中断;`import` 阶段不构造 HttpClient,也不推送)
- [crawler/logging_config.py](crawler/logging_config.py) — 控制台 + 按日滚动文件双通道,幂等;`ProgressLogger` 用于高频事件节流(如报道落库)
- [scripts/login.py](scripts/login.py) — Playwright 同步登录流程:打开 share 资源页 → 点"方正数字报"让 JS 弹出新标签页 → 自动填账号/选机构/填验证码/点登录 → 轮询到 `vpn358_sid` 后保存全部 cookie。验证码由页面本地 GVerify 生成,答案存在 Vue 实例里,直接读取即可(登录接口不校验验证码)

### 认证与通知

- `.secrets/cookies.json`(gitignore)存登录 cookie,`vpn358_sid` 是核心会话 cookie
- `.secrets/credentials.json` 存账号密码,`.secrets/bark.json` 存 Bark 设备 key;均有 `.example` 模板
- 认证失效 → 抛 `AuthError` → `bootstrap.run_stage` 推 Bark、`cli.py` 以非零码退出(便于 cron/systemd 感知)
- 每次运行收尾(正常完成/认证失效/异常中断)都会推 Bark;推送是异步 I/O,必须在 `run_stage` 返回前 await 完成,挂后台任务会被事件循环关闭时取消

## 部署

[Dockerfile](Dockerfile) 两阶段构建(uv 装依赖 → 运行时),入口 `cli.py`;[compose.yaml](compose.yaml) 挂载 `data/`(命名卷)、`.secrets/`(只读)、`archive/`(只读),注入 `NEWSPAPER_*` 环境变量。数据目录变化会被 `data/` 卷持久化。

## 注意事项

- `.secrets/`、`data/`、`logs/` 均 gitignore,不要提交认证文件或爬取数据
- 测试从 `crawler.adapters` / `crawler.services` 等子包导入(项目未安装为包,靠 pytest 的 `pythonpath=["."]` 配置)
- 同步导入 CSV 用同步 `open`(ruff 忽略 `ASYNC230`,属有意为之)
- `.playwright-mcp/` 是浏览器调试工具的临时产物,与代码无关,可忽略
