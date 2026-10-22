# 浙图"方正数字报"爬虫数据完整性说明

爬取目标:浙江图书馆"方正数字报"电子报纸(期次 → 版面 → 版面图 → 报道正文 + 配图)。

数据来源:全国报纸清单(`archive/报纸清单.csv`),经图书馆代理镜像 `https://apabi--com.elib.zyproxy.zjlib.cn/zjlib/` 访问。

- 认证依赖登录 cookie(`vpn358_sid`),失效时抛 `AuthError` 并经 Bark 推送通知;`scripts/login.py` 全自动登录(含验证码),可用 `--headless` 在后台运行
- 数据断点续跑:各表以 `status` 列为状态机,`failed` 项可经 `scripts/retry_failed.py` 重置为 `pending` 后重试
