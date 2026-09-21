"""
文件系统内容存储(适配器层)
==========================
ContentStore 端口的本地实现:正文 JSON 与版面位置 JSON 落盘到 DATA_DIR。

相对路径统一用 / 分隔(as_posix),DB 里存的相对路径跨平台可解析,
便于 Docker 卷在 Windows 开发机与 Linux 容器间迁移。
"""

import json
from dataclasses import asdict
from pathlib import Path

from ..domain import rules
from ..domain.models import Article, ArticlePosition, Board, ImageResult, ParsedArticle


class FileContentStore:
    """把正文/位置 JSON 写到 data_dir 下的实现"""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir

    def abs_path(self, rel: str) -> Path:
        """相对 DATA_DIR 的路径 -> 绝对路径"""
        return self._data_dir / rel

    def save_article_json(
        self, article: Article, parsed: ParsedArticle, img_results: list[ImageResult]
    ) -> str:
        """
        把保序 blocks 写为 content.json;按 img_results 回填各 img 块的 local_path。
        返回相对 DATA_DIR 的路径(便于 Docker 卷可移植)。
        """
        img_by_src = {r.url: r for r in img_results}
        final_blocks = []
        for b in parsed.blocks:
            if b["type"] == "img" and b["src"] in img_by_src:
                b = {**b, "local_path": img_by_src[b["src"]].local_path}
            final_blocks.append(b)
        rel = rules.article_json_rel(article)
        abs_path = self.abs_path(rel)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(
            json.dumps(
                {
                    "metaid": article.metaid,
                    "paperid": article.paperid,
                    "date": article.date,
                    "board_metaid": article.board_metaid,
                    "title": parsed.title,
                    "source_meta": parsed.source_meta,
                    "blocks": final_blocks,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return rel

    def save_positions(self, board: Board, positions: list[ArticlePosition]) -> None:
        """保存某版面的报道位置 JSON 到 positions/"""
        rel = rules.positions_rel(
            board.paperid, board.date, rules.extract_board_code(board.metaid)
        )
        path = self.abs_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "board_metaid": board.metaid,
                    "paperid": board.paperid,
                    "date": board.date,
                    "board_no": board.board_no,
                    "img_w": 350,
                    "img_h": 550,
                    "articles": [asdict(p) for p in positions],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
