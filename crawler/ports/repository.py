"""
仓储端口
========
报纸/期次/版面/报道 的元数据与爬取状态读写。status 字段即状态机,
每阶段只取 pending 项处理。连接生命周期(init/close)由实现方与组合根
负责,不属于本端口——服务层只面向"已就绪"的仓储工作。
"""

from typing import Protocol

from ..domain.models import Article, Board, Issue, Paper, Status


class CrawlRepository(Protocol):
    # ---- 报纸清单 ----
    async def import_papers(self, papers: list[Paper]) -> int:
        """批量导入报纸清单(幂等),返回导入条数"""
        ...

    async def get_papers(self) -> list[Paper]: ...

    # ---- 期次 ----
    async def add_issues(self, paperid: str, dates: list[str]) -> int:
        """插入某报纸的一批日期为 pending 期次(幂等),返回插入条数"""
        ...

    async def pending_issues(
        self, paperid: str | None = None, limit: int | None = None
    ) -> list[Issue]:
        """只读查询 pending 期次(不认领)。limit=None 表示不限制数量"""
        ...

    async def claim_issues(
        self,
        paperid: str | None = None,
        limit: int | None = None,
        lease_seconds: int = 3600,
    ) -> list[Issue]:
        """原子认领 pending 期次并标记 processing;多进程不会拿到同一行"""
        ...

    async def set_issue_status(
        self, paperid: str, date: str, status: Status, error: str | None = None
    ) -> None: ...

    # ---- 版面 ----
    async def add_boards(self, boards: list[Board]) -> None:
        """批量插入版面(metaid 主键,幂等)"""
        ...

    async def set_board_status(
        self,
        metaid: str,
        status: Status,
        image_url: str | None = None,
        image_local: str | None = None,
    ) -> None: ...

    # ---- 报道 ----
    async def add_articles(self, articles: list[Article]) -> None:
        """批量插入报道(metaid 主键,幂等)"""
        ...

    async def pending_articles(self, limit: int | None = None) -> list[Article]:
        """只读查询 pending 报道(不认领)。limit=None 表示不限制数量"""
        ...

    async def claim_articles(
        self,
        limit: int | None = None,
        lease_seconds: int = 3600,
    ) -> list[Article]:
        """原子认领 pending 报道并标记 processing;多进程不会拿到同一行"""
        ...

    async def release_claims(self) -> int:
        """把本实例未完成的认领退回 pending,返回释放行数"""
        ...

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
        """更新报道状态;非 None 的内容字段一并写入"""
        ...
