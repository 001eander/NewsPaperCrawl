"""
组合根
======
唯一的装配点:读配置、建适配器、注入服务,再把对应阶段跑起来。
cli.py 只负责参数解析与异常出口,"谁依赖谁"的知识全部集中在这里。

import 阶段不构造 HttpClient(它需要 cookies.json,缺失会抛 AuthError),
只读 CSV + 落库,因此不参与 Bark 通知——与旧入口行为一致。
"""

import logging
from pathlib import Path

from .adapters.apabi_http import HttpClient
from .adapters.bark import BarkNotifier
from .adapters.config import Settings
from .adapters.cookie_auth import AuthManager
from .adapters.fs_storage import FileContentStore
from .adapters.paperlist_csv import load_papers
from .adapters.sqlite_repo import SqliteRepository
from .domain.exceptions import AuthError
from .logging_config import setup_logging
from .services.pipeline import CrawlService

logger = logging.getLogger(__name__)


async def run_stage(stage: str, **kwargs) -> None:
    """装配并运行一个阶段(cli.py 子命令与之一一对应)"""
    setup_logging()
    settings = Settings.from_env()

    if stage == "import":
        csv_path = Path(kwargs["csv"]) if kwargs.get("csv") else settings.paperlist_csv
        papers = load_papers(csv_path)
        repo = SqliteRepository(settings)
        await repo.init()
        try:
            n = await repo.import_papers(papers)
        finally:
            await repo.close()
        logger.info("阶段0 完成,导入报纸 %s", n)
        return

    repo = SqliteRepository(settings)
    notifier = BarkNotifier(settings.secrets_dir / "bark.json")
    # CLI --concurrency 未给时(被 cli.py 过滤掉)回落环境变量/默认;max(1,...) 挡掉 0
    concurrency = max(1, kwargs.get("concurrency") or settings.concurrency)
    n = 0
    try:
        await repo.init()
        auth = AuthManager(settings.secrets_dir / "cookies.json")
        async with HttpClient(auth, notifier, concurrency=concurrency) as http:
            service = CrawlService(
                http,
                repo,
                FileContentStore(settings.data_dir),
                concurrency=concurrency,
            )
            if stage == "issues":
                n = await service.crawl_issues(
                    kwargs["start_year"],
                    kwargs["end_year"],
                    kwargs.get("end_month"),
                    concurrency,
                )
                logger.info("阶段1 完成,新增期次 %s", n)
            elif stage == "boards":
                n = await service.crawl_boards(
                    kwargs.get("paperid"), kwargs.get("limit"), concurrency
                )
                logger.info("阶段2 完成,处理期次 %s", n)
            elif stage == "articles":
                n = await service.crawl_articles(kwargs.get("limit"), concurrency)
                logger.info("阶段3 完成,处理报道 %s", n)
    except AuthError as e:
        # 认证失效:推送 Bark 告知需要重新登录,再向外抛(CLI 以非零码退出)
        await _notify_job_result(notifier, outcome="auth", stage=stage)
        logger.error("认证失效: %s", e)
        raise
    except Exception as e:
        # 未知错误:推送 Bark 告知异常中断;str(e) 为空串时文案回落"未知错误"
        await _notify_job_result(notifier, outcome="error", stage=stage, message=str(e))
        raise
    else:
        # 正常完成:推送 Bark 告知本轮处理结果
        await _notify_job_result(notifier, outcome="done", stage=stage, processed=n)
    finally:
        # 无论正常完成还是异常(AuthError 等),都必须关闭 DB 连接,
        # 否则 aiosqlite 后台线程永远阻塞在队列等待,进程无法退出。
        try:
            await repo.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭数据库连接失败: %s", e)


async def _notify_job_result(
    notifier: BarkNotifier,
    *,
    outcome: str,
    stage: str,
    processed: int | None = None,
    message: str = "",
) -> None:
    """
    爬虫一次运行收尾的统一通知(推送是异步 I/O,必须 await 完成)。

    outcome:
      "done"      正常完成(全部处理完毕或本轮处理完)
      "auth"      认证失效,需要重新登录
      "error"     未知错误导致中断
    """
    if outcome == "auth":
        title = "浙图爬虫:需要重新登录"
        body = "登录状态失效,请重新执行登录后继续爬取"
    elif outcome == "error":
        title = "浙图爬虫:异常中断"
        body = f"阶段 {stage} 运行出错: {message or '未知错误'}"
    else:  # done
        title = "浙图爬虫:爬取完成"
        body = f"阶段 {stage} 完成,处理 {processed or 0} 项"
    await notifier.notify(title, body)
    # 若认证失效且 Bark 未配置,退化为提示日志(console 也能看到)
    if outcome == "auth" and not notifier.is_configured:
        logger.warning("认证失效但未配置 bark.json,无法推送通知;请配置后重试")
