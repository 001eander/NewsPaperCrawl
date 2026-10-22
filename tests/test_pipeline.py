"""
四阶段流水线状态机测试
======================
全 Fake 端口(Gateway/Repo/Store)+ stub 解析函数驱动 CrawlService,
验证状态流转与调用序列,不触网、不落真 DB:

  - crawl_articles: 成功 -> done 且字段齐全;解析失败 -> failed "无法解析";
    抓取异常 -> failed 且记录原因;AuthError 上抛且不标 failed
  - crawl_boards: 一期成功 -> 期次 done + 版面落库 + 版面图路径入库;
    探测两轮回退(第一轮无版面 -> 模板替换日期后第二轮命中);无版面 -> 期次 failed
  - crawl_issues: 多月份日期累计入库;单月查询异常只 warning,不中断;
    start_month 裁剪起始年的月份下限
"""

import asyncio
from pathlib import Path

import pytest

from crawler.domain import rules
from crawler.domain.exceptions import AuthError
from crawler.domain.models import (
    Article,
    ArticlePosition,
    Board,
    ImageResult,
    Issue,
    Paper,
    ParsedArticle,
    Status,
)
from crawler.services import pipeline

PAPERID = "n.D330100hzrb"
DATE = "2026-01-10"
BOARD_METAID = "nb.D330100hzrb_20260110_A01"
ART_METAID = "nw.D330100hzrb_20260110_1-A01"
IMG_URL = "https://example.com/cnml.files/a.resbrief.1.jpg"
SOURCE_META = "杭州日报/2026-01-10/ 第A01版面/要闻"

ART = Article(metaid=ART_METAID, paperid=PAPERID, date=DATE, board_metaid=BOARD_METAID)
ART_DIR = rules.article_dir_rel(ART)


# ---------------- Fake 端口 ----------------


class FakeGateway:
    """假 HTTP 网关:get 按 URL 返回预置文本(未预置返回空串),download 恒成功"""

    def __init__(
        self, pages: dict[str, str] | None = None, fail: set[str] | None = None
    ):
        self.pages = pages or {}
        self.fail = fail or set()
        self.get_urls: list[str] = []
        self.downloads: list[tuple[str, Path]] = []

    async def get(self, url: str) -> str:
        self.get_urls.append(url)
        if url in self.fail:
            raise RuntimeError(f"HTTP 500 for {url}")
        return self.pages.get(url, "")

    async def download(self, url: str, save_path: Path) -> bool:
        self.downloads.append((url, save_path))
        return True


class FakeRepo:
    """假仓储:内存记录各阶段的状态写入与落库内容"""

    def __init__(
        self,
        papers: list[Paper] | None = None,
        issues: list[Issue] | None = None,
        articles: list[Article] | None = None,
    ):
        self.papers = papers or []
        self.issues = issues or []
        self.articles = articles or []
        self.added_issues: list[tuple[str, list[str]]] = []
        self.issue_status: list[tuple[str, str, Status, str | None]] = []
        self.boards: list[Board] = []
        self.board_status: list[tuple[str, Status, str | None, str | None]] = []
        self.added_articles: list[Article] = []
        self.article_status: list[dict] = []
        self.released_claims = 0

    async def get_papers(self) -> list[Paper]:
        return self.papers

    async def add_issues(self, paperid: str, dates: list[str]) -> int:
        self.added_issues.append((paperid, dates))
        return len(dates)

    async def pending_issues(
        self, paperid: str | None = None, limit: int | None = None
    ) -> list[Issue]:
        return self.issues

    async def claim_issues(
        self,
        paperid: str | None = None,
        limit: int | None = None,
        lease_seconds: int = 3600,
    ) -> list[Issue]:
        pool = [i for i in self.issues if paperid is None or i.paperid == paperid]
        taken = pool if limit is None else pool[:limit]
        self.issues = [i for i in self.issues if i not in taken]
        return taken

    async def set_issue_status(
        self, paperid: str, date: str, status: Status, error: str | None = None
    ) -> None:
        self.issue_status.append((paperid, date, status, error))

    async def add_boards(self, boards: list[Board]) -> None:
        self.boards.extend(boards)

    async def set_board_status(
        self,
        metaid: str,
        status: Status,
        image_url: str | None = None,
        image_local: str | None = None,
    ) -> None:
        self.board_status.append((metaid, status, image_url, image_local))

    async def add_articles(self, articles: list[Article]) -> None:
        self.added_articles.extend(articles)

    async def pending_articles(self, limit: int | None = None) -> list[Article]:
        return self.articles

    async def claim_articles(
        self, limit: int | None = None, lease_seconds: int = 3600
    ) -> list[Article]:
        taken = self.articles if limit is None else self.articles[:limit]
        self.articles = [a for a in self.articles if a not in taken]
        return taken

    async def release_claims(self) -> int:
        self.released_claims += 1
        return 0

    async def update_article_status(
        self,
        metaid: str,
        status: Status,
        *,
        error: str | None = None,
        title: str | None = None,
        content_path: str | None = None,
        source_meta: str | None = None,
        image_count: int | None = None,
    ) -> None:
        self.article_status.append(
            {
                "metaid": metaid,
                "status": status,
                "error": error,
                "title": title,
                "content_path": content_path,
                "source_meta": source_meta,
                "image_count": image_count,
            }
        )


class FakeStore:
    """假内容存储:相对路径映射到 tmp 目录,记录落盘调用"""

    def __init__(self, root: Path):
        self.root = root
        self.saved_articles: list[tuple[str, ParsedArticle, list[ImageResult]]] = []
        self.saved_positions: list[tuple[Board, list[ArticlePosition]]] = []

    def abs_path(self, rel: str) -> Path:
        return self.root / rel

    def save_article_json(
        self, article: Article, parsed: ParsedArticle, img_results: list[ImageResult]
    ) -> str:
        self.saved_articles.append((article.metaid, parsed, img_results))
        return rules.article_json_rel(article)

    def save_positions(self, board: Board, positions: list[ArticlePosition]) -> None:
        self.saved_positions.append((board, positions))


def _service(gateway, repo, store) -> pipeline.CrawlService:
    return pipeline.CrawlService(gateway, repo, store, concurrency=4)


def _article_blocks() -> list[dict]:
    return [
        {"type": "p", "text": "正文"},
        {"type": "img", "src": IMG_URL, "alt": "", "index": 1, "local_path": None},
    ]


# ---------------- 阶段3:报道 ----------------


def test_crawl_articles_success_marks_done(tmp_path, monkeypatch):
    """报道抓取成功 -> done,标题/正文路径/元信息/配图数齐全"""
    monkeypatch.setattr(
        pipeline.apabi_parser,
        "parse_article",
        lambda html: ParsedArticle(
            title="标题", source_meta=SOURCE_META, blocks=_article_blocks()
        ),
    )
    gateway, repo, store = FakeGateway(), FakeRepo(articles=[ART]), FakeStore(tmp_path)

    n = asyncio.run(_service(gateway, repo, store).crawl_articles())

    assert n == 1
    assert repo.article_status == [
        {
            "metaid": ART_METAID,
            "status": Status.DONE,
            "error": None,
            "title": "标题",
            "content_path": f"{ART_DIR}/content.json",
            "source_meta": SOURCE_META,
            "image_count": 1,
        }
    ]
    # 配图按 img 块顺序下载到文章目录(序号与 index 对应)
    assert gateway.downloads == [(IMG_URL, store.abs_path(f"{ART_DIR}/1.jpg"))]
    assert [metaid for metaid, _, _ in store.saved_articles] == [ART_METAID]


def test_crawl_articles_parse_failure_marks_failed(tmp_path, monkeypatch):
    """无法解析 -> failed,error 为"无法解析"(不抛异常,仍计入处理数)"""
    monkeypatch.setattr(pipeline.apabi_parser, "parse_article", lambda html: None)
    gateway, repo, store = FakeGateway(), FakeRepo(articles=[ART]), FakeStore(tmp_path)

    n = asyncio.run(_service(gateway, repo, store).crawl_articles())

    assert n == 1
    assert [(s["metaid"], s["status"], s["error"]) for s in repo.article_status] == [
        (ART_METAID, Status.FAILED, "无法解析")
    ]


def test_crawl_articles_fetch_error_marks_failed(tmp_path):
    """抓取抛普通异常 -> failed 且 error 记录原因"""
    gateway = FakeGateway(fail={pipeline.apabi_parser.build_article_url(ART_METAID)})
    repo, store = FakeRepo(articles=[ART]), FakeStore(tmp_path)

    n = asyncio.run(_service(gateway, repo, store).crawl_articles())

    assert n == 0
    assert repo.article_status[0]["status"] == Status.FAILED
    assert "HTTP 500" in repo.article_status[0]["error"]


def test_crawl_articles_auth_error_propagates(tmp_path):
    """认证失效向上抛给组合根,不把该篇标为 failed"""

    class AuthFailGateway(FakeGateway):
        async def get(self, url: str) -> str:
            raise AuthError("认证失效")

    repo = FakeRepo(articles=[ART])

    with pytest.raises(AuthError):
        asyncio.run(
            _service(AuthFailGateway(), repo, FakeStore(tmp_path)).crawl_articles()
        )

    assert repo.article_status == []


# ---------------- 阶段2:版面 ----------------


def _boards_from_html(html: str, paperid: str, date: str) -> list[Board]:
    """stub:HTML 里出现目标日期(YYYYMMDD)才算命中该期,模拟真实探测"""
    if date.replace("-", "") not in html:
        return []
    return [
        Board(metaid=BOARD_METAID, paperid=paperid, date=date, board_no="A1版：时政")
    ]


def _positions(html: str, date: str) -> list[ArticlePosition]:
    return [
        ArticlePosition(
            metaid=ART_METAID,
            title="标题",
            poly_points=[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)],
            bbox={"x": 0.0, "y": 0.0, "w": 10.0, "h": 10.0},
        )
    ]


def test_crawl_boards_success_marks_issue_done(tmp_path, monkeypatch):
    """一期成功 -> 期次 done、版面落库、版面图路径入库、报道与位置 JSON 落库"""
    monkeypatch.setattr(
        pipeline.apabi_parser, "parse_boards_from_page", _boards_from_html
    )
    monkeypatch.setattr(pipeline.apabi_parser, "parse_article_positions", _positions)
    probe_url = pipeline.apabi_parser.build_issue_probe_url(PAPERID)
    gateway = FakeGateway(pages={probe_url: f"<html>{DATE.replace('-', '')}</html>"})
    repo = FakeRepo(issues=[Issue(paperid=PAPERID, date=DATE)])
    store = FakeStore(tmp_path)

    n = asyncio.run(_service(gateway, repo, store).crawl_boards())

    assert n == 1
    assert repo.issue_status == [(PAPERID, DATE, Status.DONE, None)]
    assert [b.metaid for b in repo.boards] == [BOARD_METAID]
    assert repo.board_status == [
        (
            BOARD_METAID,
            Status.DONE,
            pipeline.apabi_parser.build_board_image_url(PAPERID, DATE, BOARD_METAID),
            rules.board_image_rel(PAPERID, DATE, "A01"),
        )
    ]
    assert [a.metaid for a in repo.added_articles] == [ART_METAID]
    assert [board.metaid for board, _ in store.saved_positions] == [BOARD_METAID]


def test_crawl_boards_probe_falls_back_to_date_template(tmp_path, monkeypatch):
    """第一轮(paperid 页)是默认最新期、无目标日期版面 -> 替换日期后第二轮命中"""
    monkeypatch.setattr(
        pipeline.apabi_parser, "parse_boards_from_page", _boards_from_html
    )
    monkeypatch.setattr(pipeline.apabi_parser, "parse_article_positions", _positions)
    probe_url = pipeline.apabi_parser.build_issue_probe_url(PAPERID)
    board_page_url = pipeline.apabi_parser.build_board_page_url(BOARD_METAID)
    # 探测页给的是另一日期(2026-01-01)的 metaid 模板,替换日期后才等于目标版面
    default_metaid = "nb.D330100hzrb_20260101_A01"
    gateway = FakeGateway(
        pages={
            probe_url: f'<a href="?metaid={default_metaid}&cult=CN">A1版</a>',
            board_page_url: f"<html>{DATE.replace('-', '')}</html>",
        }
    )
    repo = FakeRepo(issues=[Issue(paperid=PAPERID, date=DATE)])

    n = asyncio.run(_service(gateway, repo, FakeStore(tmp_path)).crawl_boards())

    assert n == 1
    # 先探测 paperid 页,再用替换日期后的 metaid 请求版面页(第三次取版面页是解析报道位置)
    assert gateway.get_urls[:2] == [probe_url, board_page_url]
    assert repo.boards[0].metaid == BOARD_METAID


def test_crawl_boards_no_boards_marks_issue_failed(tmp_path, monkeypatch):
    """两轮都没有版面(可能未收录)-> 期次 failed,不写版面"""
    monkeypatch.setattr(
        pipeline.apabi_parser,
        "parse_boards_from_page",
        lambda html, paperid, date: [],
    )
    repo = FakeRepo(issues=[Issue(paperid=PAPERID, date=DATE)])

    n = asyncio.run(_service(FakeGateway(), repo, FakeStore(tmp_path)).crawl_boards())

    assert n == 1  # 期次已处理(记为 failed),仍计入本轮处理数
    assert repo.issue_status == [(PAPERID, DATE, Status.FAILED, None)]
    assert repo.boards == []
    assert repo.board_status == []


# ---------------- 阶段1:期次 ----------------


def _issue_xml(dates: list[str]) -> str:
    return "".join(f"<IssueDate>{d}</IssueDate>" for d in dates)


def test_crawl_issues_accumulates_months(tmp_path):
    """按月查询命中日期 -> 逐月累计入库,返回新增总数"""
    gateway = FakeGateway(
        pages={
            pipeline.apabi_parser.build_issue_url(PAPERID, 2026, m): _issue_xml(
                [f"2026-0{m}-05", f"2026-0{m}-06"]
            )
            for m in (1, 2)
        }
    )
    repo = FakeRepo(papers=[Paper(paperid=PAPERID, name="杭州日报")])

    n = asyncio.run(
        _service(gateway, repo, FakeStore(tmp_path)).crawl_issues(2026, 2026, 2)
    )

    assert n == 4
    assert repo.added_issues == [
        (PAPERID, ["2026-01-05", "2026-01-06"]),
        (PAPERID, ["2026-02-05", "2026-02-06"]),
    ]


def test_crawl_issues_respects_start_month(tmp_path):
    """同年内 start_month=2、end_month=2 -> 只查询并入库 2 月,跳过 1 月"""
    feb_url = pipeline.apabi_parser.build_issue_url(PAPERID, 2026, 2)
    gateway = FakeGateway(
        pages={
            pipeline.apabi_parser.build_issue_url(PAPERID, 2026, m): _issue_xml(
                [f"2026-0{m}-05"]
            )
            for m in (1, 2)
        }
    )
    repo = FakeRepo(papers=[Paper(paperid=PAPERID, name="杭州日报")])

    n = asyncio.run(
        _service(gateway, repo, FakeStore(tmp_path)).crawl_issues(2026, 2026, 2, 2)
    )

    assert n == 1
    assert repo.added_issues == [(PAPERID, ["2026-02-05"])]
    # 1 月的 URL 根本不应被请求
    assert all(url == feb_url for url in gateway.get_urls)


def test_crawl_issues_continues_after_month_error(tmp_path):
    """单月查询异常只 warning,其他月份照常入库"""
    jan_url = pipeline.apabi_parser.build_issue_url(PAPERID, 2026, 1)
    feb_url = pipeline.apabi_parser.build_issue_url(PAPERID, 2026, 2)
    gateway = FakeGateway(pages={jan_url: _issue_xml(["2026-01-05"])}, fail={feb_url})
    repo = FakeRepo(papers=[Paper(paperid=PAPERID, name="杭州日报")])

    n = asyncio.run(
        _service(gateway, repo, FakeStore(tmp_path)).crawl_issues(2026, 2026, 2)
    )

    assert n == 1
    assert repo.added_issues == [(PAPERID, ["2026-01-05"])]
