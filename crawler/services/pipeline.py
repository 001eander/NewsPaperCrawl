"""
四阶段流水线编排
================
服务层:把领域规则与端口装配成爬虫的四阶段流水线,每阶段独立、可断点续跑
(以仓储的 status 状态机跳过已 done 项):

  阶段0  import_papers    导入报纸清单(幂等,可重复执行)
  阶段1  crawl_issues     每报×每月调 newspaper.issue,枚举全部期次
  阶段2  crawl_boards     每期抓版面页 -> 版面列表 + 版面位置 + 版面图
  阶段3  crawl_articles   每篇抓报道页 -> 保序正文 JSON + 配图

HTTP 网关 / 仓储 / 内容存储三个端口与并发度由组合根注入,本层不碰
SQLite、aiohttp、文件系统细节。站点解析来自适配器(解析即站点知识)。
认证失效时抛 AuthError,由组合根兜底通知用户重新登录。
"""

import asyncio
import json
import logging
from pathlib import Path

from ..adapters import apabi_parser
from ..domain import rules
from ..domain.exceptions import AuthError
from ..domain.models import Article, Board, ImageResult, Issue, Paper, Status
from ..logging_config import ProgressLogger
from ..ports.http import HttpGateway
from ..ports.repository import CrawlRepository
from ..ports.storage import ContentStore

logger = logging.getLogger(__name__)

# 报道落库是最高频事件,用节流日志避免 crawl.log 膨胀(见 ProgressLogger)
_article_progress = ProgressLogger(__name__)
# 单张配图下载超时:CDN 偶发挂起/极慢的图若无限时,会拖垮整批 gather
_IMG_TIMEOUT = 20


class CrawlService:
    """四阶段流水线的编排:只依赖 domain 规则与三个端口(无具体实现细节)。"""

    def __init__(
        self,
        http: HttpGateway,
        repo: CrawlRepository,
        store: ContentStore,
        concurrency: int = 8,
    ):
        self._http = http
        self._repo = repo
        self._store = store
        self.concurrency = concurrency

    # ---------------- 阶段0:导入报纸清单 ----------------

    async def import_papers(self, papers: list[Paper]) -> int:
        """导入报纸清单(幂等)。返回导入条数。CSV 读取已移到组合根。"""
        return await self._repo.import_papers(papers)

    # ---------------- 阶段1:枚举期次 ----------------

    async def crawl_issues(
        self,
        start_year: int,
        end_year: int,
        end_month: int | None = None,
        concurrency: int | None = None,
    ) -> int:
        """对每份报纸,按月查询期次。返回新增期次数。"""
        papers = await self._repo.get_papers()
        sem = asyncio.Semaphore(concurrency or self.concurrency)
        total = 0

        async def one_paper(p: Paper) -> None:
            nonlocal total
            paperid = p.paperid
            # 枚举该报所有月份
            for y in range(start_year, end_year + 1):
                months = (
                    range(1, 13)
                    if y < end_year or end_month is None
                    else range(1, end_month + 1)
                )
                for m in months:
                    async with sem:
                        url = apabi_parser.build_issue_url(paperid, y, m)
                        try:
                            xml = await self._http.get(url)
                        except AuthError:
                            raise
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "期次查询失败 %s %s-%s: %s", paperid, y, m, e
                            )
                            continue
                        dates = apabi_parser.parse_issue_dates(xml)
                        if dates:
                            n = await self._repo.add_issues(paperid, dates)
                            total += n
            logger.info("完成 %s (%s)", paperid, p.name)

        await asyncio.gather(*[one_paper(p) for p in papers])
        return total

    # ---------------- 阶段2:版面 + 位置 + 版面图 ----------------

    async def crawl_boards(
        self,
        paperid: str | None = None,
        limit: int | None = None,
        concurrency: int | None = None,
    ) -> int:
        """处理待爬期次:抓版面列表、版面位置、下载版面图。返回处理期次数。"""
        issues = await self._repo.pending_issues(paperid, limit)
        processed = 0
        # 每期内部还会摊开到各版面(http 适配器已全局封顶),外批保守取小,避免峰值协程过多
        batch_size = max(concurrency or self.concurrency, 4)

        async def one_issue(issue: Issue) -> None:
            nonlocal processed
            paperid_i, date_i = issue.paperid, issue.date
            try:
                ok = await self._crawl_one_issue(paperid_i, date_i)
                await self._repo.set_issue_status(
                    paperid_i, date_i, Status.DONE if ok else Status.FAILED
                )
                processed += 1
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("期次 %s %s 失败: %s", paperid_i, date_i, e)
                await self._repo.set_issue_status(
                    paperid_i, date_i, Status.FAILED, str(e)
                )

        await _gather_in_batches(issues, one_issue, batch_size)
        return processed

    async def _crawl_one_issue(self, paperid: str, date_i: str) -> bool:
        """
        抓取一个期次:先探测任一版面页,拿到全部版面。
        版面编码不统一(A01 vs A1),必须从版面页探测,不能猜。
        """
        # 探测:先请求 paperid 版面页(默认最新期)拿当日版面列表
        # 方法:构造 nb.{code}_{date}_A1 试探?不。更稳:请求 paperid 页,页面含日期导航
        # 实际上最稳的是:newspaper.page&paperid=... 会返回某期,再取其 nb.* 链接
        probe_url = apabi_parser.build_issue_probe_url(paperid)
        html = await self._http.get(probe_url)
        # 若该页是默认最新期,日期可能不是我们要的日期。我们只想要它的版面编码格式。
        # 正确做法:拿默认期版面 metaid,替换日期为目标日期,再请求。
        boards = apabi_parser.parse_boards_from_page(html, paperid, date_i)
        logger.debug("第一轮(paperid 页)解析出 %s 个版面", len(boards))
        if not boards:
            # 默认期不是目标期,尝试用日期已知格式:需要知道编码。
            # 从页面里拿一个 nb.* 的日期模板,替换日期。
            template = apabi_parser.find_board_metaid_template(html)
            logger.debug("模板 metaid: %s", template)
            if template:
                nb_meta = template.replace(
                    rules.extract_date(template), date_i.replace("-", "")
                )
                html2 = await self._http.get(apabi_parser.build_board_page_url(nb_meta))
                boards = apabi_parser.parse_boards_from_page(html2, paperid, date_i)
                logger.debug("第二轮(替换日期版)解析出 %s 个版面", len(boards))
        if not boards:
            logger.warning("期次 %s %s 无版面(可能未收录)", paperid, date_i)
            return False

        # 写入版面(board_name 列保留但已并入 board_no,置空)
        await self._repo.add_boards(boards)
        logger.info("期次落库 %s %s: 版面 %s 个", paperid, date_i, len(boards))

        # 各版面并行:下载版面图 + 解析位置。http 适配器全局封顶真实并发。
        # 版面异常直接冒泡:gather 首异常取消同批其余版面 → 整期 failed(与串行语义一致)。
        await asyncio.gather(*(self._crawl_one_board(b) for b in boards))
        return True

    async def _crawl_one_board(self, board: Board) -> None:
        """下载版面图 + 解析报道位置"""
        board_metaid = board.metaid
        board_code = rules.extract_board_code(board_metaid)
        paperid, date_i = board.paperid, board.date
        # 版面图 URL(传 metaid,由 build_board_image_url 取真实编码)
        img_url = apabi_parser.build_board_image_url(paperid, date_i, board_metaid)
        rel = rules.board_image_rel(paperid, date_i, board_code)
        ok = await self._http.download(img_url, self._store.abs_path(rel))
        if ok:
            await self._repo.set_board_status(
                board_metaid, Status.DONE, image_url=img_url, image_local=rel
            )
        else:
            logger.debug("版面图下载失败 %s", img_url)

        # 解析报道位置(从版面页 HTML)
        board_html = await self._http.get(
            apabi_parser.build_board_page_url(board_metaid)
        )
        positions = apabi_parser.parse_article_positions(board_html, date_i)
        if positions:
            articles = [
                Article(
                    metaid=pos.metaid,
                    paperid=paperid,
                    date=date_i,
                    board_metaid=board_metaid,
                    title=pos.title,
                    bbox_x=pos.bbox["x"],
                    bbox_y=pos.bbox["y"],
                    bbox_w=pos.bbox["w"],
                    bbox_h=pos.bbox["h"],
                    poly_points=json.dumps(pos.poly_points),
                )
                for pos in positions
            ]
            await self._repo.add_articles(articles)
            logger.info(
                "版面 %s %s 落库 %s 篇报道: %s",
                paperid,
                date_i,
                len(articles),
                board_code,
            )
            # 保存位置 JSON 到 positions/
            self._store.save_positions(board, positions)

    # ---------------- 阶段3:报道正文 + 配图 ----------------

    async def crawl_articles(
        self, limit: int | None = None, concurrency: int | None = None
    ) -> int:
        """处理待爬报道:抓报道页、下载配图、写正文 JSON。返回处理报道数。"""
        articles = await self._repo.pending_articles(limit)
        processed = 0
        # 单篇 worker 轻量(正文 1 请求 + 至多几张配图),批可放大;http 适配器已全局封顶
        batch_size = max((concurrency or self.concurrency) * 2, 100)

        async def one_article(art: Article) -> None:
            nonlocal processed
            metaid = art.metaid
            try:
                await self._crawl_one_article(art)
                processed += 1
            except AuthError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("报道 %s 失败: %s", metaid, e)
                await self._repo.update_article_status(
                    metaid, Status.FAILED, error=str(e)
                )

        await _gather_in_batches(articles, one_article, batch_size)
        return processed

    async def _crawl_one_article(self, art: Article) -> None:
        html = await self._http.get(apabi_parser.build_article_url(art.metaid))
        parsed = apabi_parser.parse_article(html)
        if not parsed:
            await self._repo.update_article_status(
                art.metaid, Status.FAILED, error="无法解析"
            )
            return
        # 按 blocks 中图片顺序取 URL,保证下载序号与 img 块 index 一致
        img_urls = [b["src"] for b in parsed.blocks if b["type"] == "img"]
        # 先下载全部配图,拿到最终 local_path,再写 JSON(只写一次,内容与已下载文件一致)
        img_results = await self._save_article_images(art, img_urls)
        content_rel = self._store.save_article_json(art, parsed, img_results)
        await self._repo.update_article_status(
            art.metaid,
            Status.DONE,
            title=parsed.title,
            content_path=content_rel,  # 相对 DATA_DIR 的路径
            source_meta=parsed.source_meta,
            image_count=len(img_urls),  # = img 块数
        )
        _article_progress.tick("报道落库 %s: %s", art.metaid, parsed.title)

    async def _save_article_images(
        self, art: Article, img_urls: list[str]
    ) -> list[ImageResult]:
        """
        按序下载配图到文章目录(每篇文章一个目录),返回有序结果。
        文件名序号(1 起)与 blocks 中 img 块 index 对应,保证 JSON 引用最终文件。

        返回 [ImageResult];local_path 为相对 DATA_DIR 的路径,
        下载失败为 None(JSON 里该 img 块回退 src)。
        """
        rel_dir = rules.article_dir_rel(art)
        self._store.abs_path(rel_dir).mkdir(parents=True, exist_ok=True)

        async def download_one(i: int, url: str) -> ImageResult:
            suffix = Path(url).suffix or ".jpg"
            # 相对路径统一用 / 分隔(as_posix),Windows 上跑出的 DB 路径也能被 Linux 容器解析
            rel = (Path(rel_dir) / f"{i}{suffix}").as_posix()
            try:
                # 单图限时:CDN 偶发挂起/极慢的图若不限时,会拖垮整批 gather(长尾)。
                # 超时的图 local_path 置空(JSON 回退 src),后续可单独补图。
                async with asyncio.timeout(_IMG_TIMEOUT):
                    ok = await self._http.download(url, self._store.abs_path(rel))
            except TimeoutError:
                ok = False
            return ImageResult(url=url, local_path=rel if ok else None, ok=ok)

        # 并行下载配图:一篇多图文章(尤其 10+ 图)串行下载会拖垮吞吐,
        # 实际并发受 http 适配器全局封顶,不会因此打爆站点。
        results = await asyncio.gather(
            *(download_one(i, url) for i, url in enumerate(img_urls, 1))
        )
        return list(results)


async def _gather_in_batches(items, worker, batch_size: int) -> None:
    """把 items 切成 batch_size 大小的批,逐批 gather,一批完成才取下一批。

    - worker 处理单个 item,内部自行捕获非致命异常并计数(nonlocal processed)。
    - 致命异常(AuthError)由 worker 重新 raise,gather 首异常取消同批其余任务并上抛。
    - items 切片已物化,不再发 DB 查询;items 为空时 range 为空,直接 no-op。
    """
    for start in range(0, len(items), batch_size):
        await asyncio.gather(*(worker(it) for it in items[start : start + batch_size]))
