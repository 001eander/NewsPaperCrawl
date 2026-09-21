"""
articles schema 迁移测试
========================
验证 SqliteRepository.init 对既有库的幂等迁移:
  - 旧列 content 被 RENAME 为 content_path
  - 旧正文内容被置空(弃置)
  - 二次 init 幂等无副作用
"""

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from crawler.adapters.config import Settings
from crawler.adapters.sqlite_repo import SqliteRepository

# 旧版 articles 建表语句(含 content 列,无 content_path)
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    metaid       TEXT PRIMARY KEY,
    paperid      TEXT NOT NULL,
    date         TEXT NOT NULL,
    board_metaid TEXT,
    title        TEXT,
    content      TEXT,
    source_meta  TEXT,
    status       TEXT DEFAULT 'pending',
    error        TEXT
);
"""


def _settings(db_path: Path) -> Settings:
    """手工构造 Settings(frozen dataclass,字段全部必填);测试只用 db_path"""
    return Settings(
        data_dir=db_path.parent,
        log_dir=db_path.parent,
        log_interval=10,
        concurrency=8,
        secrets_dir=db_path.parent,
        db_path=db_path,
        paperlist_csv=db_path.parent / "报纸清单.csv",
    )


async def _init_db(db_path: Path) -> None:
    """用 SqliteRepository 初始化(建表 + 迁移),随后关闭连接"""
    repo = SqliteRepository(_settings(db_path))
    await repo.init()
    await repo.close()


@pytest.fixture
def old_db(tmp_path):
    """建一个旧 schema 的库,含两条有正文的行"""
    path = tmp_path / "crawl.db"
    loop = asyncio.new_event_loop()
    con = loop.run_until_complete(aiosqlite.connect(str(path)))
    con.row_factory = aiosqlite.Row
    loop.run_until_complete(con.executescript(OLD_SCHEMA))
    loop.run_until_complete(
        con.executemany(
            "INSERT INTO articles (metaid, paperid, date, title, content, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("nw.D111_1-A01", "n.D111", "2026-01-01", "标题一", "旧正文一", "done"),
                ("nw.D111_2-A01", "n.D111", "2026-01-01", "标题二", "旧正文二", "done"),
            ],
        )
    )
    loop.run_until_complete(con.commit())
    loop.run_until_complete(con.close())
    loop.close()
    return path


def _cols(path):
    con = sqlite3_connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(articles)").fetchall()]
    con.close()
    return cols


def sqlite3_connect(path):
    import sqlite3

    return sqlite3.connect(str(path))


def test_migration_renames_content_to_content_path(old_db):
    """旧库迁移后: 无 content 列,有 content_path 列,旧正文置空"""
    asyncio.run(_init_db(old_db))

    cols = _cols(old_db)
    assert "content" not in cols
    assert "content_path" in cols

    con = sqlite3_connect(old_db)
    rows = con.execute(
        "SELECT title, content_path FROM articles ORDER BY metaid"
    ).fetchall()
    con.close()
    assert [(t, cp) for t, cp in rows] == [
        ("标题一", None),
        ("标题二", None),
    ]


def test_migration_idempotent(old_db):
    """二次 init 幂等,不报错、不再迁移"""
    asyncio.run(_init_db(old_db))
    asyncio.run(_init_db(old_db))

    assert "content" not in _cols(old_db)
    assert "content_path" in _cols(old_db)


def test_new_db_has_content_path(tmp_path):
    """新库直接建 content_path 列,无 content 列"""
    path = tmp_path / "fresh.db"
    asyncio.run(_init_db(path))

    cols = _cols(path)
    assert "content" not in cols
    assert "content_path" in cols
