"""
文章存储测试(_save_article_images / FileContentStore)
====================================================
覆盖:
  - 配图按序下载,文件名序号与 img 块 index 对应
  - 下载失败时 local_path 为 None
  - save_article_json 回填 local_path 并落盘,路径为相对 DATA_DIR 的 content.json
  - content.json 的相对路径规则(domain.rules.article_json_rel)
"""

import asyncio
import json

from crawler.adapters.fs_storage import FileContentStore
from crawler.domain.models import Article, ImageResult, ParsedArticle
from crawler.domain.rules import article_json_rel
from crawler.services.pipeline import CrawlService

ART = Article(
    metaid="nw.D330100hzrb_20260110_1-A01",
    paperid="n.D330100hzrb",
    date="2026-01-10",
    board_metaid="nb.D330100hzrb_20260110_A01",
)
IMG_URLS = [
    "https://example.com/cnml.files/a.resbrief.1.jpg",
    "https://example.com/cnml.files/b.resbrief.1.jpg",
]
# 相对 DATA_DIR 的文章目录:被测代码以 as_posix() 统一输出 / 分隔,与平台无关
ART_REL = "articles/330100hzrb/2026/01/10/D330100hzrb_20260110_1-A01"


class FakeHttpGateway:
    """假的下载器: 记录下载调用,成功/失败可控(被测路径只用到 download)"""

    def __init__(self, fail: set[str] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail or set()

    async def download(self, url, save_path) -> bool:
        self.calls.append((url, str(save_path)))
        if url in self.fail:
            return False
        save_path.write_bytes(b"fake-jpeg")
        return True


class FakeRepo:
    """空仓储:配图下载路径不碰仓储,仅为满足构造签名"""


def _service(tmp_path, gateway) -> CrawlService:
    return CrawlService(
        http=gateway, repo=FakeRepo(), store=FileContentStore(tmp_path), concurrency=4
    )


def _save_images(tmp_path, gateway, img_urls=IMG_URLS):
    service = _service(tmp_path, gateway)
    return asyncio.run(service._save_article_images(ART, img_urls))


def test_save_images_sequential_indexes_match_blocks(tmp_path):
    """配图按序下载,文件名序号 1/2 对应 img 块 index,返回有序结果"""
    gateway = FakeHttpGateway()

    results = _save_images(tmp_path, gateway)

    assert [r.ok for r in results] == [True, True]
    assert [r.local_path for r in results] == [
        f"{ART_REL}/1.jpg",
        f"{ART_REL}/2.jpg",
    ]
    # 下载目标与 local_path 相对路径一致
    for r in results:
        assert (tmp_path / r.local_path).exists()


def test_save_images_failure_sets_local_path_none(tmp_path):
    """下载失败时 local_path 为 None,ok 为 False"""
    gateway = FakeHttpGateway(fail={IMG_URLS[1]})

    results = _save_images(tmp_path, gateway)

    assert results[0].local_path is not None
    assert results[0].ok is True
    assert results[1].local_path is None
    assert results[1].ok is False


def test_save_article_json_backfills_local_path(tmp_path):
    """save_article_json 回填 img 块 local_path,JSON 落盘且结构正确"""
    parsed = ParsedArticle(
        title="标题",
        source_meta="杭州日报/2026-01-10/ 第A01版面/要闻",
        blocks=[
            {
                "type": "img",
                "src": IMG_URLS[0],
                "alt": "",
                "index": 1,
                "local_path": None,
            },
            {"type": "p", "text": "正文第一段"},
            {
                "type": "img",
                "src": IMG_URLS[1],
                "alt": "图2",
                "index": 2,
                "local_path": None,
            },
        ],
    )
    img_results = [
        ImageResult(url=IMG_URLS[0], local_path="articles/.../1.jpg", ok=True),
        ImageResult(url=IMG_URLS[1], local_path=None, ok=False),  # 下载失败
    ]

    rel = FileContentStore(tmp_path).save_article_json(ART, parsed, img_results)

    # 相对路径正确
    assert rel == f"{ART_REL}/content.json"
    abs_path = tmp_path / rel
    assert abs_path.exists()

    data = json.loads(abs_path.read_text(encoding="utf-8"))
    assert data["title"] == "标题"
    assert data["metaid"] == ART.metaid
    assert data["board_metaid"] == ART.board_metaid
    assert [b["type"] for b in data["blocks"]] == ["img", "p", "img"]
    # 成功的图回填 local_path,失败的为 None
    assert data["blocks"][0]["local_path"] == "articles/.../1.jpg"
    assert data["blocks"][2]["local_path"] is None


def test_article_json_rel():
    """正文 JSON 为相对 DATA_DIR 的 content.json 路径(as_posix)"""
    assert article_json_rel(ART) == f"{ART_REL}/content.json"
