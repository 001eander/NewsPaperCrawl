"""
桌面端只读状态查询(适配器层)
============================

stdlib sqlite3 直读 WAL 模式的 crawl.db,供桌面 sidecar 的 status/failed
命令返回进度数据。SQL 与 scripts/status.py 同源,但返回结构化数据而非打印:

  - overview(): 每表一行 total/done/pending/failed;表未建时 exists=False
  - per_paper(): 按报纸汇总各阶段 done/total
  - failed(limit): 每表最近 limit 条失败

processing 是运行中的临时认领,统计上并入 pending。查询都是毫秒级 GROUP BY,
可在事件循环内直接执行。
"""

import sqlite3
from pathlib import Path
from typing import Any

STAGE_TABLES = ("issues", "boards", "articles")
STATUS_ORDER = ("pending", "done", "failed")


class StatusQueries:
    """状态页只读查询(stdlib sqlite3)"""

    def __init__(self, db_path: Path):
        self._db_path = db_path

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return cur.fetchone() is not None

    def overview(self) -> list[dict[str, Any]]:
        """每表一行:total/done/pending/failed;表未建时 exists=False"""
        if not self._db_path.exists():
            return []
        rows = []
        conn = sqlite3.connect(str(self._db_path))
        try:
            for table in STAGE_TABLES:
                if not self._table_exists(conn, table):
                    rows.append({"stage": table, "exists": False})
                    continue
                counts = dict.fromkeys(STATUS_ORDER, 0)
                cur = conn.execute(
                    f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status"
                )
                for status, n in cur.fetchall():
                    # processing 是运行中的临时认领,统计上并入 pending
                    counts["pending" if status == "processing" else status] += n
                rows.append(
                    {
                        "stage": table,
                        "exists": True,
                        "total": sum(counts.values()),
                        **counts,
                    }
                )
        finally:
            conn.close()
        return rows

    def per_paper(self) -> list[tuple[str, str, list[str]]]:
        """按报纸汇总:(paperid, name, [issues done/total, boards, articles])"""
        if not self._db_path.exists():
            return []
        conn = sqlite3.connect(str(self._db_path))
        try:
            if not self._table_exists(conn, "papers"):
                return []
            papers = conn.execute(
                "SELECT paperid, name FROM papers ORDER BY paperid"
            ).fetchall()
            per: dict[str, dict[str, dict[str, int]]] = {}
            for table in STAGE_TABLES:
                if not self._table_exists(conn, table):
                    continue
                cur = conn.execute(
                    f"SELECT paperid, status, COUNT(*) AS n FROM {table} "
                    "GROUP BY paperid, status"
                )
                for paperid, status, n in cur.fetchall():
                    bucket = per.setdefault(paperid, {}).setdefault(
                        table, dict.fromkeys(STATUS_ORDER, 0)
                    )
                    bucket["pending" if status == "processing" else status] += n
        finally:
            conn.close()
        rows = []
        for paperid, name in papers:
            cells = []
            for table in STAGE_TABLES:
                tb = per.get(paperid, {}).get(table)
                cells.append("--" if tb is None else f"{tb['done']}/{sum(tb.values())}")
            rows.append((paperid, name, cells))
        return rows

    def failed(self, limit: int = 5) -> dict[str, list[tuple[str, str, str]]]:
        """每表最近 limit 条失败:{table: [(paperid, date, error)]}"""
        result: dict[str, list[tuple[str, str, str]]] = {}
        if not self._db_path.exists():
            return result
        conn = sqlite3.connect(str(self._db_path))
        try:
            for table in STAGE_TABLES:
                if not self._table_exists(conn, table):
                    continue
                cur = conn.execute(
                    f"SELECT paperid, date, error FROM {table} "
                    "WHERE status='failed' ORDER BY date DESC, paperid LIMIT ?",
                    (limit,),
                )
                rows = cur.fetchall()
                if rows:
                    result[table] = [
                        (pid, date, (err or "").replace("\n", " "))
                        for pid, date, err in rows
                    ]
        finally:
            conn.close()
        return result
