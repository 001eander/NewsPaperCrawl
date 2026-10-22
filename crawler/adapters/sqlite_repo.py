"""
SQLite 仓储(适配器层)
====================
CrawlRepository 端口的本地实现:
- 存报纸/期次/版面/报道 的元数据与爬取状态
- status 字段作为状态机: pending / processing / done / failed
- 取任务时在 BEGIN IMMEDIATE 事务里原子认领为 processing,多进程不会拿到同一行;
  进程异常退出后,超过租约的 processing 行可被其他进程回收
- 使用 aiosqlite 避免阻塞事件循环

表结构:
  papers         报纸清单(paperid PK)
  issues         期次(报纸×日期),UNIQUE(paperid,date)
  boards         版面(metaid PK)
  articles       报道(metaid PK,含版面位置坐标)
  article_images 报道配图(id PK)
"""

import logging
import os
import socket
import time
import uuid

import aiosqlite

from ..domain.models import Article, Board, Issue, Paper, Status
from .config import Settings

logger = logging.getLogger(__name__)

# 认领租约:进程崩溃后,processing 行最多滞留这么久再被其他进程回收。
# 单批任务远短于该值,正常运行时不会与他人重叠。
CLAIM_LEASE_SECONDS = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paperid    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    issue_no   TEXT,
    period     TEXT,
    place      TEXT,
    start_date TEXT,
    end_date   TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    paperid     TEXT NOT NULL,
    date        TEXT NOT NULL,          -- YYYY-MM-DD
    status      TEXT DEFAULT 'pending', -- pending/processing/done/failed
    claimed_by  TEXT,                   -- 认领者(主机:pid:实例)
    claimed_at  INTEGER,                -- 认领时间(epoch 秒)
    board_count INTEGER DEFAULT 0,
    error       TEXT,
    PRIMARY KEY (paperid, date)
);

CREATE TABLE IF NOT EXISTS boards (
    metaid      TEXT PRIMARY KEY,       -- nb.*
    paperid     TEXT NOT NULL,
    date        TEXT NOT NULL,
    board_no    TEXT,                   -- A01 / A1 ...
    board_name  TEXT,
    image_url   TEXT,
    img_w       INTEGER,
    img_h       INTEGER,
    image_local TEXT,                   -- 相对 data/boards/ 的路径
    status      TEXT DEFAULT 'pending',
    error       TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    metaid       TEXT PRIMARY KEY,      -- nw.*
    paperid      TEXT NOT NULL,
    date         TEXT NOT NULL,
    board_metaid TEXT,
    title        TEXT,
    content_path TEXT,              -- 正文 JSON 相对 data/ 的路径(旧列 content 迁移而来)
    source_meta  TEXT,                  -- "报纸/日期/版面/栏目" 元信息
    bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL,  -- 版面位置(像素,版面图坐标)
    poly_points TEXT,                   -- 原始多边形坐标 JSON
    image_count  INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'pending',
    claimed_by   TEXT,
    claimed_at   INTEGER,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS article_images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_metaid TEXT NOT NULL,
    url           TEXT NOT NULL,
    local_path    TEXT,
    w             INTEGER,
    h             INTEGER,
    status        TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_boards_issue ON boards(paperid, date);
CREATE INDEX IF NOT EXISTS idx_articles_issue ON articles(paperid, date);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
"""


class SqliteRepository:
    """基于单个 aiosqlite 连接的 CrawlRepository 实现"""

    def __init__(self, settings: Settings):
        self._path = settings.db_path
        self._db: aiosqlite.Connection | None = None
        # 认领者标识:同一台机器上也区分进程/实例,便于释放只属于自己的认领
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    async def init(self) -> None:
        """初始化数据库连接(启用 WAL + busy_timeout,支持多进程并发)"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(self._path), timeout=30)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await _migrate_articles_schema(db)  # 旧库: articles.content -> content_path
        await _migrate_claim_columns(db)  # 旧库: 补认领列
        # WAL 模式: 读写不互斥,多进程友好
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")  # 等锁最多 30s
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.commit()
        self._db = db
        logger.info("数据库已初始化: %s", self._path)

    async def close(self) -> None:
        """关闭连接(幂等);未初始化时什么也不做"""
        if self._db is not None:
            db, self._db = self._db, None
            await db.close()

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("数据库未初始化:请先 await init()")
        return self._db

    # ---- 报纸清单 ----

    async def import_papers(self, papers: list[Paper]) -> int:
        """批量导入报纸清单(幂等,ON CONFLICT 忽略)"""
        db = self._conn()
        rows = [
            {
                "paperid": p.paperid,
                "name": p.name,
                "issue_no": p.issue_no,
                "period": p.period,
                "place": p.place,
                "start_date": p.start_date,
                "end_date": p.end_date,
            }
            for p in papers
        ]
        await db.executemany(
            """INSERT OR IGNORE INTO papers
           (paperid, name, issue_no, period, place, start_date, end_date)
           VALUES (:paperid, :name, :issue_no, :period, :place, :start_date, :end_date)""",
            rows,
        )
        await db.commit()
        return len(rows)

    async def get_papers(self) -> list[Paper]:
        db = self._conn()
        cur = await db.execute("SELECT * FROM papers ORDER BY paperid")
        return [
            Paper(
                paperid=r["paperid"],
                name=r["name"],
                issue_no=r["issue_no"],
                period=r["period"],
                place=r["place"],
                start_date=r["start_date"],
                end_date=r["end_date"],
            )
            for r in await cur.fetchall()
        ]

    # ---- 期次 ----

    async def add_issues(self, paperid: str, dates: list[str]) -> int:
        """插入某报纸的一批日期为 pending 期次(幂等)"""
        db = self._conn()
        await db.executemany(
            """INSERT OR IGNORE INTO issues (paperid, date) VALUES (?, ?)""",
            [(paperid, d) for d in dates],
        )
        await db.commit()
        return len(dates)

    async def pending_issues(
        self, paperid: str | None = None, limit: int | None = None
    ) -> list[Issue]:
        """只读查询 pending 期次(不认领)。limit=None 表示不限制数量。"""
        db = self._conn()
        sql = "SELECT * FROM issues WHERE status='pending'"
        args: list = []
        if paperid:
            sql += " AND paperid=?"
            args.append(paperid)
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        cur = await db.execute(sql, tuple(args))
        return [_issue_from_row(r) for r in await cur.fetchall()]

    async def claim_issues(
        self,
        paperid: str | None = None,
        limit: int | None = None,
        lease_seconds: int = CLAIM_LEASE_SECONDS,
    ) -> list[Issue]:
        """原子认领 pending 期次(可限定报纸);limit=None 表示不限制数量。"""
        now = int(time.time())
        args: list = [
            Status.PROCESSING,
            self._owner,
            now,
            Status.PENDING,
            Status.PROCESSING,
            now - lease_seconds,
        ]
        sql = """
            UPDATE issues SET status=?, claimed_by=?, claimed_at=?
             WHERE (paperid, date) IN (
                   SELECT paperid, date FROM issues
                    WHERE (status=? OR (status=? AND (claimed_at IS NULL OR claimed_at < ?)))
            """
        if paperid:
            sql += " AND paperid=?"
            args.append(paperid)
        sql += """
                    ORDER BY date, paperid
                    LIMIT ?
             )
            RETURNING *
        """
        args.append(limit if limit is not None else -1)
        return [_issue_from_row(r) for r in await self._claim(sql, args)]

    async def set_issue_status(
        self, paperid: str, date: str, status: Status, error: str | None = None
    ) -> None:
        db = self._conn()
        await db.execute(
            "UPDATE issues SET status=?, error=?, claimed_by=NULL, claimed_at=NULL "
            "WHERE paperid=? AND date=?",
            (status, error, paperid, date),
        )
        await db.commit()

    # ---- 版面 ----

    async def add_boards(self, boards: list[Board]) -> None:
        """批量插入版面(metaid 主键,幂等)"""
        db = self._conn()
        rows = [
            {
                "metaid": b.metaid,
                "paperid": b.paperid,
                "date": b.date,
                "board_no": b.board_no,
                "board_name": b.board_name,
            }
            for b in boards
        ]
        await db.executemany(
            """INSERT OR IGNORE INTO boards
           (metaid, paperid, date, board_no, board_name, status)
           VALUES (:metaid, :paperid, :date, :board_no, :board_name, 'pending')""",
            rows,
        )
        await db.commit()

    async def get_boards_for_issue(self, paperid: str, date: str) -> list[Board]:
        db = self._conn()
        cur = await db.execute(
            "SELECT * FROM boards WHERE paperid=? AND date=? ORDER BY board_no",
            (paperid, date),
        )
        return [
            Board(
                metaid=r["metaid"],
                paperid=r["paperid"],
                date=r["date"],
                board_no=r["board_no"],
                board_name=r["board_name"],
                image_url=r["image_url"],
                image_local=r["image_local"],
                status=Status(r["status"]),
                error=r["error"],
            )
            for r in await cur.fetchall()
        ]

    async def set_board_status(
        self,
        metaid: str,
        status: Status,
        image_url: str | None = None,
        image_local: str | None = None,
    ) -> None:
        db = self._conn()
        fields = {
            k: v
            for k, v in {"image_url": image_url, "image_local": image_local}.items()
            if v is not None
        }
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            sql = f"UPDATE boards SET {cols}, status=? WHERE metaid=?"
            await db.execute(sql, (*fields.values(), status, metaid))
        else:
            await db.execute(
                "UPDATE boards SET status=? WHERE metaid=?", (status, metaid)
            )
        await db.commit()

    # ---- 报道 ----

    async def add_articles(self, articles: list[Article]) -> None:
        """批量插入报道(metaid 主键,幂等)"""
        db = self._conn()
        rows = [
            {
                "metaid": a.metaid,
                "paperid": a.paperid,
                "date": a.date,
                "board_metaid": a.board_metaid,
                "title": a.title,
                "bbox_x": a.bbox_x,
                "bbox_y": a.bbox_y,
                "bbox_w": a.bbox_w,
                "bbox_h": a.bbox_h,
                "poly_points": a.poly_points,
            }
            for a in articles
        ]
        await db.executemany(
            """INSERT OR IGNORE INTO articles
           (metaid, paperid, date, board_metaid, title, bbox_x, bbox_y, bbox_w, bbox_h, poly_points, status)
           VALUES (:metaid, :paperid, :date, :board_metaid, :title,
                   :bbox_x, :bbox_y, :bbox_w, :bbox_h, :poly_points, 'pending')""",
            rows,
        )
        await db.commit()

    async def pending_articles(self, limit: int | None = None) -> list[Article]:
        """只读查询 pending 报道(不认领)。limit=None 表示不限制数量。"""
        db = self._conn()
        sql = "SELECT * FROM articles WHERE status='pending'"
        args: list = []
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        cur = await db.execute(sql, tuple(args))
        return [_article_from_row(r) for r in await cur.fetchall()]

    async def claim_articles(
        self,
        limit: int | None = None,
        lease_seconds: int = CLAIM_LEASE_SECONDS,
    ) -> list[Article]:
        """原子认领 pending 报道;limit=None 表示不限制数量。"""
        now = int(time.time())
        sql = """
            UPDATE articles SET status=?, claimed_by=?, claimed_at=?
             WHERE metaid IN (
                   SELECT metaid FROM articles
                    WHERE status=? OR (status=? AND (claimed_at IS NULL OR claimed_at < ?))
                    ORDER BY date, metaid
                    LIMIT ?
             )
            RETURNING *
        """
        args = [
            Status.PROCESSING,
            self._owner,
            now,
            Status.PENDING,
            Status.PROCESSING,
            now - lease_seconds,
            limit if limit is not None else -1,
        ]
        return [_article_from_row(r) for r in await self._claim(sql, args)]

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
        """更新报道状态与内容字段"""
        db = self._conn()
        fields = {
            k: v
            for k, v in {
                "error": error,
                "title": title,
                "content_path": content_path,
                "source_meta": source_meta,
                "image_count": image_count,
            }.items()
            if v is not None
        }
        cols = ", ".join(f"{k}=?" for k in fields)
        sets = f"{cols}, " if cols else ""
        await db.execute(
            f"UPDATE articles SET {sets}status=?, claimed_by=NULL, claimed_at=NULL "
            "WHERE metaid=?",
            (*fields.values(), status, metaid),
        )
        await db.commit()

    # ---- 认领 ----

    async def release_claims(self) -> int:
        """把本实例尚未完成的认领退回 pending(取消/异常退出时用)。"""
        db = self._conn()
        released = 0
        for table in ("issues", "articles"):
            cur = await db.execute(
                f"UPDATE {table} SET status=?, claimed_by=NULL, claimed_at=NULL "
                "WHERE claimed_by=? AND status=?",
                (Status.PENDING, self._owner, Status.PROCESSING),
            )
            released += cur.rowcount
        await db.commit()
        return released

    async def _claim(self, sql: str, args: list) -> list[aiosqlite.Row]:
        """
        在 IMMEDIATE 事务里执行认领 UPDATE ... RETURNING。

        IMMEDIATE 先拿写锁再求值子查询,两个进程不会读到同一批 pending;
        后到者等锁(busy_timeout)后看到的是前者已提交的 processing 行。
        """
        db = self._conn()
        await db.commit()  # 结束可能存在的隐式事务,BEGIN 不能在事务内执行
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(sql, tuple(args))
            rows = await cur.fetchall()
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return rows


def _issue_from_row(r: aiosqlite.Row) -> Issue:
    return Issue(
        paperid=r["paperid"],
        date=r["date"],
        status=Status(r["status"]),
        board_count=r["board_count"],
        error=r["error"],
    )


def _article_from_row(r: aiosqlite.Row) -> Article:
    return Article(
        metaid=r["metaid"],
        paperid=r["paperid"],
        date=r["date"],
        board_metaid=r["board_metaid"],
        title=r["title"],
        content_path=r["content_path"],
        source_meta=r["source_meta"],
        bbox_x=r["bbox_x"],
        bbox_y=r["bbox_y"],
        bbox_w=r["bbox_w"],
        bbox_h=r["bbox_h"],
        poly_points=r["poly_points"],
        image_count=r["image_count"],
        status=Status(r["status"]),
        error=r["error"],
    )


async def _migrate_articles_schema(db: aiosqlite.Connection) -> None:
    """
    既有库迁移: articles.content -> content_path(幂等)。

    正文已改为文件系统 JSON,content 列的旧文本弃置(开发环境 breaking change)。
    仅当旧列 content 存在且新列 content_path 不存在时执行一次 RENAME,
    随后立即把 content_path 置 NULL,避免"路径列里存全文"的脏状态。
    新库由 SCHEMA 直接建 content_path,此处自动跳过。
    """
    cur = await db.execute("PRAGMA table_info(articles)")
    cols = [r["name"] for r in await cur.fetchall()]
    if "content" in cols and "content_path" not in cols:
        await db.execute("ALTER TABLE articles RENAME COLUMN content TO content_path")
        await db.execute("UPDATE articles SET content_path = NULL")
        await db.commit()
        logger.info("articles.content 已迁移为 articles.content_path")


async def _migrate_claim_columns(db: aiosqlite.Connection) -> None:
    """既有库补认领列(幂等)。表名来自固定白名单,不接受外部输入。"""
    # 多个阶段可能同时启动:先拿写锁再检查列,避免两边都读到缺列后重复 ALTER
    await db.commit()
    await db.execute("BEGIN IMMEDIATE")
    try:
        for table in ("issues", "articles"):
            cur = await db.execute(f"PRAGMA table_info({table})")
            cols = {r["name"] for r in await cur.fetchall()}
            if "claimed_by" not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN claimed_by TEXT")
            if "claimed_at" not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN claimed_at INTEGER")
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
