"""
并发认领测试
============
用真实 SqliteRepository + 临时库验证多进程共享 pending 队列时不会重复取到同一行:
  - 两个实例并发认领,结果互不相交
  - 认领中的行对后续认领不可见
  - 超过租约的 processing 行可被其他实例回收
  - release_claims 把本实例未完成的行退回 pending
  - 期次认领支持 paperid 过滤
"""

import asyncio
import sqlite3
from pathlib import Path

from crawler.adapters.config import Settings
from crawler.adapters.sqlite_repo import SqliteRepository
from crawler.domain.models import Article, Issue, ParsedArticle, Status
from crawler.services import pipeline
from crawler.services.pipeline import CrawlService


def _settings(db_path: Path) -> Settings:
    return Settings(
        data_dir=db_path.parent,
        log_dir=db_path.parent,
        log_interval=10,
        concurrency=8,
        secrets_dir=db_path.parent,
        db_path=db_path,
        paperlist_csv=db_path.parent / "报纸清单.csv",
    )


async def _repo(db_path: Path) -> SqliteRepository:
    repo = SqliteRepository(_settings(db_path))
    await repo.init()
    return repo


def _articles(n: int) -> list[Article]:
    return [
        Article(
            metaid=f"nw.D111_202601{i:02d}_1-A01",
            paperid="n.D111",
            date=f"2026-01-{i:02d}",
        )
        for i in range(1, n + 1)
    ]


def _drive(db_path: Path, scenario) -> None:
    async def main():
        repo = await _repo(db_path)
        try:
            await scenario(repo, db_path)
        finally:
            await repo.close()

    asyncio.run(main())


def test_claim_articles_disjoint_across_instances(tmp_path):
    path = tmp_path / "crawl.db"

    async def scenario(repo, db_path):
        await repo.add_articles(_articles(4))
        other = await _repo(db_path)
        try:
            first, second = await asyncio.gather(
                repo.claim_articles(2), other.claim_articles(2)
            )
        finally:
            await other.close()

        assert len(first) == len(second) == 2
        assert {a.metaid for a in first}.isdisjoint({a.metaid for a in second})
        # 4 行都已被认领,再取为空
        assert await repo.claim_articles(10) == []

    _drive(path, scenario)


def test_expired_claim_is_reclaimed(tmp_path):
    path = tmp_path / "crawl.db"

    async def scenario(repo, db_path):
        await repo.add_articles(_articles(1))
        claimed = await repo.claim_articles(1)
        assert len(claimed) == 1

        other = await _repo(db_path)
        try:
            # 未过期:另一个实例拿不到
            assert await other.claim_articles(1) == []
            con = sqlite3.connect(db_path)
            con.execute("UPDATE articles SET claimed_at = claimed_at - 7200")
            con.commit()
            con.close()
            reclaimed = await other.claim_articles(1)
        finally:
            await other.close()

        assert [a.metaid for a in reclaimed] == [claimed[0].metaid]

    _drive(path, scenario)


def test_release_claims_returns_pending(tmp_path):
    path = tmp_path / "crawl.db"

    async def scenario(repo, _db_path):
        await repo.add_articles(_articles(1))
        assert len(await repo.claim_articles(1)) == 1
        assert await repo.release_claims() == 1

        pending = await repo.pending_articles()
        assert [a.metaid for a in pending] == [_articles(1)[0].metaid]
        assert pending[0].status is Status.PENDING
        assert len(await repo.claim_articles(1)) == 1

    _drive(path, scenario)


class _Gateway:
    def __init__(self):
        self.fetched: list[str] = []

    async def get(self, url: str) -> str:
        self.fetched.append(url)
        return ""

    async def download(self, url: str, save_path: Path) -> bool:
        return True


class _Store:
    def save_article_json(self, article, parsed, img_results) -> str:
        return "content.json"


def test_two_services_never_fetch_same_article(tmp_path, monkeypatch):
    """两个进程各自跑 articles:同一篇报道只会被请求一次"""
    path = tmp_path / "crawl.db"
    gateway = _Gateway()
    monkeypatch.setattr(
        pipeline.apabi_parser,
        "parse_article",
        lambda html: ParsedArticle(title="标题", source_meta="", blocks=[]),
    )

    async def scenario():
        repo_a, repo_b = await _repo(path), await _repo(path)
        try:
            await repo_a.add_articles(_articles(150))
            service_a = CrawlService(gateway, repo_a, _Store(), concurrency=4)
            service_b = CrawlService(gateway, repo_b, _Store(), concurrency=4)
            await asyncio.gather(service_a.crawl_articles(), service_b.crawl_articles())
        finally:
            await repo_a.close()
            await repo_b.close()

    asyncio.run(scenario())

    assert len(gateway.fetched) == len(set(gateway.fetched)) == 150


def test_claim_issues_respects_paperid(tmp_path):
    path = tmp_path / "crawl.db"

    async def scenario(repo, _db_path):
        await repo.add_issues("n.D111", ["2026-01-01"])
        await repo.add_issues("n.D222", ["2026-01-02"])

        claimed = await repo.claim_issues(paperid="n.D222", limit=10)

        assert [(i.paperid, i.date) for i in claimed] == [("n.D222", "2026-01-02")]
        assert isinstance(claimed[0], Issue)
        assert claimed[0].status is Status.PROCESSING

    _drive(path, scenario)
