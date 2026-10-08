# 调研：Cloudflare 调度中心技术选型与配额

- 关联 ticket：wayfinder research #6（Part of #3 分布式爬虫改造）
- 调研日期：2026-10-08
- 结论一句话：调度中心只用 **Workers + D1 + Workers Static Assets** 即可，Queues 与 Durable Objects 在当前 2~5 客户端规模下都不必要。配额方面，请求数与 D1 读量远在免费额度内；真正的边界是 **免费版 D1 的 100,000 行写入/天、500 MB 单库上限，以及每此 Worker 调用最多 50 条 D1 查询**。国家报纸全量索引大概率超过 500 MB，建议原型期用免费版验证，进度索引规模上来后升级 Workers Paid（$5/月起）。

---

## 1. 约束回顾

来自 #3 与本 ticket 已定约束：

- 客户端 2~5 台，Windows / macOS 混合，客户端从中心**拉取**任务（中心不主动连客户端）。
- 心跳 + 批量汇报，不做实时逐项上报。
- D1 建爬取进度索引（期次 / 版面 / 报道三级状态）。
- 中心统一下发 cookie，派发任务带租约语义。
- 原始数据（正文 JSON、版面图、配图）留在客户端本地，中心只存索引进度。
- 监控页私有，可用口令或 Cloudflare Access。

这些约束决定了架构是「少量外部客户端轮询一个中心 API」，而不是「高并发实时推送」。选型判断都以此为前提。

---

## 2. 组件角色划分

| 组件 | 角色 | 本票结论 |
| --- | --- | --- |
| **Workers** | 唯一 HTTP 入口：客户端注册 / 心跳 / 拉任务 / 批量汇报，以及监控页的 `/api/*`。承载认证、租约发放、cookie 下发。 | 必需，一个 Worker 足够 |
| **Workers Static Assets** | 托管监控页前端（静态 SPA）。静态资源请求免费且不计入请求数。 | 推荐，与上面的 Worker 同项目 |
| **D1** | 进度索引（期次/版面/报道三级状态）、任务表与租约字段、客户端注册表、可选的 cookie 池。单库即事实来源。 | 必需，且是「协调点」 |
| **Queues** | 生产/消费解耦、批处理、重试与死信。 | 不必要 |
| **Durable Objects** | 全局唯一、强一致的协调对象；WebSocket 推送。 | 不必要（见 §6） |

核心判断：**D1 单库本身就是串行的**，Cloudflare 文档明确写「每个 D1 数据库天然单线程，一次处理一条查询」[S2]。2~5 个客户端争抢任务这种协调强度，一条带条件的 `UPDATE` 就能做成原子租约，不需要引入 Durable Objects。

---

## 3. 配额事实基线（来源为 Cloudflare 官方文档）

### 3.1 Workers

| 项 | Free | Paid (Standard) |
| --- | --- | --- |
| 请求 | 100,000/天（0:00 UTC 重置，超限报 1027） | 无上限，含每月 1,000 万请求，超出 $0.30/百万 |
| CPU 时间 | 10 ms/次 | 默认 30 s，最高 5 min |
| 子请求 | 50/次调用 | 10,000/次调用 |
| Cron Triggers | 5/账号 | 250/账号 |
| 静态资源文件 | 20,000/版本 | 100,000/版本 |

关键细节：**静态资源的请求免费且不计量**；只有命中 Worker 脚本的请求才计费。[S3][S4]

### 3.2 D1

限额 [S2]：

| 项 | Free | Paid |
| --- | --- | --- |
| 每账号数据库数 | 10 | 50,000 |
| 单库最大体积 | 500 MB | 10 GB（不可再提高） |
| 每账号存储 | 5 GB | 1 TB |
| 每次 Worker 调用可执行的 D1 查询数 | 50 | 1,000 |
| 单条 SQL 长度 | 100 KB | 100 KB |
| 绑定参数数 | 100 | 100 |
| 单条 SQL 最长执行 | 30 s | 30 s |
| 单行/字符串/BLOB 上限 | 2 MB | 2 MB |

计费口径 [S5]：

| 项 | Free | Paid |
| --- | --- | --- |
| 行读取 | 500 万/天 | 每月含 250 亿，超出 $0.001/百万 |
| 行写入 | **100,000/天** | 每月含 5,000 万，超出 $1.00/百万 |
| 存储 | 共 5 GB | 含 5 GB，超出 $0.75/GB-月 |

两个容易踩的口径：

1. **索引会计入写入**：文档明确「写入涉及被索引的列时，会额外写一行索引」。即一次 `UPDATE` 改到 N 个被索引列，按 1+N 行写入计费。[S5]
2. **行读取按扫描行数计**：`SELECT * FROM t` 全表扫 5,000 行就是 5,000 行读取，哪怕只返回几行。索引能显著降低读取量。[S5]

### 3.3 Queues [S6][S7]

- Free：每天 10,000 次操作；Paid：每月含 100 万次，超出 $0.40/百万。
- 一条消息投递约 3 次操作（写、读、删）。
- 单批最多 100 条，批等待最长 60 s，保留期 Free 24 h / Paid 最长 14 天。

### 3.4 Durable Objects [S8]

- Free：100,000 请求/天、13,000 GB-s/天；仅支持 SQLite 存储后端。
- Paid：每月含 100 万请求、400,000 GB-s；SQLite 存储的行读写口径与 D1 相同。
- 适合「全局唯一、严格串行」的协调对象与 WebSocket 应用。

### 3.5 Cloudflare Access（监控页认证）[S9]

- Free 计划支持最多 50 个用户，非限时试用。
- 自托管应用可配一次性 PIN 或第三方 IdP（GitHub / Google 等）。
- 支持 service token（机器对机器），可给 API 路径单独放行。
- Free 计划日志保留 24 小时。

---

## 4. D1 写入量估算

### 4.1 假设（自行设定，明确写出）

| 变量 | 取值 |
| --- | --- |
| 客户端数 N | 5（取上限） |
| 心跳间隔 | 30 s |
| 拉任务间隔 | 30 s |
| 批量汇报间隔 | 5 min |
| 每篇报道状态变更次数 | 2（领取写 lease、完成写 done），重试系数 1.2 |
| 每次状态 `UPDATE` 计入的行写入 R | 2（表本身 1 行 + 改到 status 索引 1 行） |
| 聚合吞吐 A | 分三档：400 / 5,000 / 30,000 篇每天 |

### 4.2 心跳与轮询

- 心跳写入：5 × (86400 / 30) = **14,400 行/天**（每次心跳 `UPDATE` 客户端表的 `last_seen` 一行；若不建二级索引，就是 1 行写入）。
- 拉任务请求：5 × 2,880 = 14,400 次/天。拉取以读为主，成功领到任务才写，写入计入下面的进度写入。

### 4.3 分档结果

| 情景 | 报道写入/天 (A×2×1.2×R) | 心跳 | 合计写入/天 | 对免费 10 万/天 |
| --- | --- | --- | --- | --- |
| 低（2 客户，200 篇/台/天） | 400 × 2 × 1.2 × 2 = 1,920 | 5,760 | ≈ 7,700 | 8%，安全 |
| 中（5 客户，1,000 篇/台/天） | 5,000 × 2 × 1.2 × 2 = 24,000 | 14,400 | ≈ 38,400 | 38%，安全 |
| 高（5 客户，6,000 篇/台/天） | 30,000 × 2 × 1.2 × 2 = 144,000 | 14,400 | ≈ 158,400 | 158%，**超限** |

结论：在当前 2~5 客户端、心跳 30 s、批量汇报的设定下，中等吞吐的日写入约 3.8 万行，离免费 10 万/天有不小余量。只有当日聚合吞吐逼近 3 万篇才会触限。付费版每月含 5,000 万行写入（≈167 万/天），任何一档情景都绰绰有余。

### 4.4 批量汇报的正确姿势

- 用 `D1Database.batch()` 把一次汇报的多条状态变更放进一个事务：文档说明 batch 内语句「顺序、非并发执行，任一句失败则整个序列回滚」，且能减少网络往返。[S10]
- 注意 batch 不会减少计费的「行写入」，它按行计费，只是把开销从多次调用摊到一次。
- **免费版的硬约束**：每次 Worker 调用最多 50 条 D1 查询 [S2]。若一次汇报要提交上百条变更，免费版会撞上 50 的上限，需要拆成多次请求（例如每批 ≤ 40 条），或升级付费（1,000 条/调用）。

### 4.5 行读取与监控页

免费版每天 500 万行读取，客户端轮询查询走 `(status, priority)` 索引，只扫少量匹配行，可忽略。风险在监控页：如果每次刷新都对进度大表做 `COUNT(*)` 或全表聚合，会按表行数计费。1 百万行的表刷新一次就是 1 百万行读取，30 s 刷一次一天就远超 500 万。

建议：维护一张小的计数汇总表（按报纸/状态存计数），在每次批量汇报的同一个 `batch()` 里顺带 `+1`；监控页只读汇总表。或者在监控查询上强制索引与 `LIMIT`。R 值因此可能多一行写入，但换掉全表扫描很划算（文档也指出索引带来读收益通常抵过那点写开销 [S5]）。

---

## 5. D1 单库规模估算

单库上限：免费 500 MB、付费 10 GB（不可再提高）[S2]。索引不含原始数据，只存期次/版面/报道三级状态，单行开销估计：

| 层级 | 行规模假设 | 单行估算 | 备注 |
| --- | --- | --- | --- |
| 期次 | 1,000 报 × 12 期/年 × 10 年 ≈ 12 万 | ~120 B | |
| 版面 | 12 万期 × 16 版面 ≈ 190 万 | ~120 B | 磁盘约 0.23 GB |
| 报道 | 190 万 × 8 ≈ 1,500 万 | ~200 B | 磁盘约 3.0 GB |

报道 + 版面 + 索引（约行数 × 50% 开销）合计约 **5~8 GB**，逼近 10 GB 上限，远超免费 500 MB。也就是说：

- **免费版 500 MB 只够原型或少量报纸**（大致能装 100~200 万行报道）。
- 若要把全国报纸全量纳入，付费 10 GB 也可能不够，需**按年份或按报组横向分库**。D1 的设计目标就是「多个 10 GB 小库横向扩展」，付费版每账号可建 50,000 个库 [S2]。分库的代价是跨库查询要由 Worker 汇总，监控页需要聚合多个库。

---

## 6. 租约语义：D1 就够，不需要 Durable Objects

任务表大致字段：`task_id`、`status(pending|leased|done|failed)`、`lease_owner`、`lease_expires_at`、`attempts`、`priority`。

领取任务用一条带条件的 `UPDATE` 完成，检查返回的 `meta.changes`：

```sql
UPDATE tasks
SET status = 'leased', lease_owner = ?, lease_expires_at = ?
WHERE task_id = ? AND (status = 'pending'
       OR (status = 'leased' AND lease_expires_at < ?));
-- 若 meta.changes == 1，领取成功；否则被别的客户端抢先或租约未过期
```

依据：

- D1 单库串行执行查询 [S2]，配合条件 `UPDATE` 与 `meta.changes` 判断，天然不会两个客户端同时拿到同一任务，无需分布式锁。
- `D1Database.batch()` 提供事务语义 [S10]，可以把「写租约」和「更新客户端状态」放进同一事务。
- 过期租约回收可以用 Cron Trigger（免费版每账号 5 个）扫描 `lease_expires_at`，或由客户端下次心跳顺带触发。`lease_expires_at` 建议建索引，避免回收时全表扫描。

Durable Objects 值得引入的时机（当前都不满足）：客户端数量到几百且高频争抢同一个协调点；需要中心向客户端 **WebSocket 推送**任务；需要按客户端隔离的 SQL 状态。当前是「少量客户端主动拉取」的形态，引入 DO 只会增加计费维度（requests + duration）和一份额外代码。

---

## 7. Queues 的必要性

当前调用模式是**外部客户端拉取**。Queues 面向的是 Workers 内部的生产者/消费者解耦、至少一次投递、批处理与死信队列 [S6]，它并不会把消息推给仓库外的 Windows/macOS 客户端。用它替代「客户端轮询 + D1 租约」既不合场景，也多个组件。

中心侧将来若有「服务端定时任务」（例如定期清理过期租约、刷新 cookie 池健康状态），用 Cron Trigger 更直接。结论：本期不引入 Queues。

---

## 8. 监控页托管方式与认证

### 8.1 托管方式

推荐 **Workers Static Assets**：把监控前端构建成静态 SPA，用同一个 Worker 项目托管，`assets.directory` 指向构建产物，`not_found_handling: "single-page-application"`，并用 `run_worker_first` 只把 `/api/*` 交给 Worker 脚本 [S11][S4]。

- 静态资源请求免费且不限量，只有 `/api/*` 命中 Worker 脚本会计入请求数 [S4]。
- 相比「Worker 服务端渲染页面」，静态托管省掉每次页面加载的 Worker 调用，也省掉模板代码。低流量下 SSR 也能跑，但没必要。

免费版注意事项：`run_worker_first` 命中的路径在请求量超限时返回 429，而负向模式（`!/assets/*`）仍直接走静态资源 [S4]，所以把构建产物放在 `/assets/*` 并让 Worker 只接管 `/api/*` 是稳妥的。

### 8.2 认证方案对比与建议

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| **Cloudflare Access**（推荐） | 免费 ≤50 用户；支持 OTP / 第三方 IdP；几乎零 Worker 代码；service token 可给机器放行 [S9] | 需要一个接入 Cloudflare 的域名；免费版日志仅 24 h |
| Worker 口令 + 签名 Cookie | 不依赖外部配置，代码量小 | 安全全自己扛：暴力破解、限流、会话签名都要自己实现 |

**建议**：监控页走 **Cloudflare Access**（自托管应用 + allow 你自己的邮箱 / IdP），能省掉鉴权代码。

一个必须注意的冲突：客户端的心跳/汇报 API 走同一 Worker 时，如果整站套 Access，客户端无法完成交互式 OTP 登录。两种解法：

1. **按路径拆分**：Access 只保护监控页路径（交互式登录），`/api/*` 用 Access **service token** 放行（客户端带 `CF-Access-Client-Id` / `CF-Access-Client-Secret` 头）。Access 支持按 hostname 或 path 建应用 [S12]。
2. **按主机拆分**：监控页 `monitor.example.com` 套 Access；客户端 API 用 `api.example.com`，由 Worker 校验每个客户端的 bearer token（存 Worker secret）。

若不想引入 Access，退而求其次用 Worker secret 存一个共享口令，校验后下发 HMAC 签名的 HttpOnly Cookie，并给登录接口加基本限流。这条兜底方案足够私有监控页，但安全性弱于 Access。

---

## 9. cookie 下发的落点（背景约束的补充）

cookie 是凭据，写少读多。Cloudflare 官方存储选型文档把「凭据 / 会话数据」明确推荐给 **Workers KV**（读多写少、无需强一致）[S13]。可选做法：

- cookie 池存 KV（或 D1 里一个加密列），客户端领到任务时由 Worker 通过 HTTPS 返回该任务对应的 cookie。
- 只在租约创建时下发，租约过期即失效，缩小暴露窗口。
- 任何日志、监控页响应里都不要回显 cookie 明文。

本期核心索引与租约放 D1；cookie 池若顺手，KV 是更贴合其访问特征的落点。此点不改变主选型。

---

## 10. 结论与建议

**选型清单**

1. 一个 Worker 承担全部 HTTP 接口与认证；同项目用 Workers Static Assets 托管监控 SPA。
2. D1 单库作为进度索引 + 任务/租约 + 客户端注册表的事实来源；用条件 `UPDATE` + `meta.changes` 实现租约，`batch()` 保证汇报事务。
3. 不引入 Queues；服务端定时任务用 Cron Trigger。
4. 不引入 Durable Objects；当客户端规模或推送需求显著增长时再评估。
5. 监控页认证首选 Cloudflare Access；客户端 API 用 service token 或独立 bearer token，与交互式登录解耦。
6. cookie 池可放 KV，按任务租约下发。

**免费 vs 付费**

- 免费版大概率撑得住原型：请求、D1 读量都富余；中等吞吐日写入约 3.8 万行，低于 10 万上限。
- 免费版的三个真实边界：D1 单库 500 MB、D1 写入 10 万行/天、每次 Worker 调用最多 50 条 D1 查询。
- 建议：先免费跑通；一旦全量索引接近 500 MB、日写入接近 10 万、或单次汇报超过 ~50 条变更，就升级 Workers Paid（$5/月起），一次解决这三条并放大子请求等上限。

**风险 / 待确认**

- 国家报纸全量索引的实际行数未定，10 GB 单库是否需要分库取决于最终纳入的报纸与年份范围，建议先做一次行数与单行大小的实测。
- 监控页若做全表聚合会迅速吃掉 D1 读额度，需在实现时锁定「汇总表 + 索引」策略。
- 客户端并发/重试策略会影响租约回收频率与写入量，实现前需定心跳与汇总的具体参数。

---

## 参考来源

- [S2] Cloudflare D1 — Limits：https://developers.cloudflare.com/d1/platform/limits/（last updated 2026-04-21）
- [S3] Cloudflare Workers — Limits：https://developers.cloudflare.com/workers/platform/limits/（last updated 2026-09-05）
- [S4] Cloudflare Workers — Static Assets / Billing and Limitations：https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/（last updated 2026-04-23）
- [S5] Cloudflare D1 — Pricing：https://developers.cloudflare.com/d1/platform/pricing/（last updated 2026-04-21）
- [S6] Cloudflare Queues — Pricing：https://developers.cloudflare.com/queues/platform/pricing/（last updated 2026-04-21）
- [S7] Cloudflare Queues — Limits：https://developers.cloudflare.com/queues/platform/limits/
- [S8] Cloudflare Durable Objects — Pricing：https://developers.cloudflare.com/durable-objects/platform/pricing/（last updated 2026-09-30）
- [S9] Cloudflare Zero Trust / Access 定价（Free ≤50 用户）：https://www.cloudflare.com/plans/zero-trust-services/
- [S10] Cloudflare D1 — D1Database.batch()：https://developers.cloudflare.com/d1/worker-api/d1-database/
- [S11] Cloudflare Workers — Static Assets 路由：https://developers.cloudflare.com/workers/static-assets/routing/
- [S12] Cloudflare Workers — Cloudflare Access 集成：https://developers.cloudflare.com/workers/configuration/cloudflare-access/
- [S13] Cloudflare Workers — Choose a data or storage product：https://developers.cloudflare.com/workers/platform/storage-options/（last updated 2026-10-06）

> 部分事实同时经 context7 `/cloudflare/cloudflare-docs`、`/llmstxt/developers_cloudflare_d1_llms-full_txt`、`/llmstxt/developers_cloudflare_workers_llms-full_txt` 检索交叉核对；上表内数字以 2026-10-08 抓取的官方页面为准。
