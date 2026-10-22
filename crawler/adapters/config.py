"""
全局配置(适配器层)
==================
所有"运行时路径"统一收口在这里,支持环境变量覆盖 + 合理默认值。

环境变量:
  NEWSPAPER_DATA_DIR     数据目录(DB/版面图/正文/配图/位置JSON)
  NEWSPAPER_DB_PATH      数据库文件路径(默认 <DATA_DIR>/crawl.db)
  NEWSPAPER_LOG_DIR      日志目录(默认项目根目录 logs/,含当日 crawl.log 与归档)
  NEWSPAPER_LOG_INTERVAL 报道落库进度日志节流:每成功落库 K 篇打一条汇总(默认 10)
  NEWSPAPER_CONCURRENCY  HTTP 并发上限(默认 16,Docker 可通过 environment 注入)
  NEWSPAPER_SECRETS_DIR  认证与通知配置目录(默认 .secrets)
  NEWSPAPER_PAPERLIST_CSV  报纸清单 CSV 路径(默认项目根目录 报纸清单.csv)

Docker / Dev Container 里通过 environment 注入;宿主机用 export 覆盖。
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


@dataclass(frozen=True)
class Settings:
    """运行时路径与并发/日志参数(构造后不可变)"""

    data_dir: Path
    log_dir: Path
    log_interval: int
    concurrency: int
    secrets_dir: Path
    db_path: Path
    paperlist_csv: Path

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "Settings":
        """从环境变量构造配置;base_dir 为项目根(默认由本文件位置回溯两层)"""
        base = base_dir or Path(__file__).resolve().parents[2]
        data_dir = _env("NEWSPAPER_DATA_DIR", base / "data")
        return cls(
            data_dir=data_dir,
            log_dir=_env("NEWSPAPER_LOG_DIR", base / "logs"),
            log_interval=int(os.environ.get("NEWSPAPER_LOG_INTERVAL", "10")),
            concurrency=int(os.environ.get("NEWSPAPER_CONCURRENCY", "16")),
            secrets_dir=_env("NEWSPAPER_SECRETS_DIR", base / ".secrets"),
            db_path=_env("NEWSPAPER_DB_PATH", data_dir / "crawl.db"),
            paperlist_csv=_env(
                "NEWSPAPER_PAPERLIST_CSV", base / "archive" / "报纸清单.csv"
            ),
        )
