"""
报纸清单 CSV 读取(适配器层)
==========================
把 archive/报纸清单.csv 解析为 domain.Paper 列表(纯读取,无副作用)。
同步 open 是有意为之: 一次性导入用同步 IO 更简洁,无需异步文件库。
"""

import csv
from pathlib import Path

from ..domain.models import Paper


def load_papers(csv_path: Path) -> list[Paper]:
    """读取报纸清单 CSV(utf-8-sig,列名见项目 archive 目录)"""
    if not csv_path.exists():
        raise FileNotFoundError(
            f"找不到报纸清单 CSV: {csv_path}\n"
            "请用 cli.py import --csv <路径> 或设置环境变量 NEWSPAPER_PAPERLIST_CSV"
        )
    papers = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            papers.append(
                Paper(
                    paperid=row["paperid"],
                    name=row["报纸名称"],
                    issue_no=row["刊号"],
                    period=row["刊期"],
                    place=row["出版地"],
                    start_date=row["订阅开始"],
                    end_date=row["订阅结束"],
                )
            )
    return papers
