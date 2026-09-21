"""
内容存储端口
============
正文 JSON / 位置 JSON 的落盘,以及相对路径到绝对路径的解析
(配图二进制由 HttpGateway.download 直接写到 abs_path,不重复抽象)。
"""

from pathlib import Path
from typing import Protocol

from ..domain.models import Article, ArticlePosition, Board, ImageResult, ParsedArticle


class ContentStore(Protocol):
    def abs_path(self, rel: str) -> Path:
        """相对 DATA_DIR 的路径 -> 绝对路径"""
        ...

    def save_article_json(
        self, article: Article, parsed: ParsedArticle, img_results: list[ImageResult]
    ) -> str:
        """把保序 blocks 写为 content.json;按 img_results 回填各 img 块的
        local_path。返回相对 DATA_DIR 的路径(便于 Docker 卷可移植)。"""
        ...

    def save_positions(self, board: Board, positions: list[ArticlePosition]) -> None:
        """保存某版面的报道位置 JSON 到 positions/"""
        ...
