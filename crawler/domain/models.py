"""
领域模型
========
爬虫的核心实体与值对象(dataclass,无 I/O):

  Paper            报纸清单条目
  Issue            期次(报纸×日期)
  Board            版面
  ArticlePosition  版面位置(解析产出,几何信息)
  Article          报道(持久化实体,含版面位置坐标)
  ParsedArticle    报道页解析结果(标题 + 元信息 + 保序 blocks)
  ImageResult      单张配图的下载结果

status 字段为状态机: pending / processing / done / failed(Status 枚举,值即 DB
存储串)。processing 是认领中的临时状态:多个进程可同时消费 pending 队列,
每行只会被一个进程认领。
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Paper:
    paperid: str
    name: str
    issue_no: str = ""
    period: str = ""
    place: str = ""
    start_date: str = ""
    end_date: str = ""


@dataclass
class Issue:
    paperid: str
    date: str  # YYYY-MM-DD
    status: Status = Status.PENDING
    board_count: int = 0
    error: str | None = None


@dataclass
class Board:
    metaid: str  # nb.*
    paperid: str
    date: str
    board_no: str = ""  # A01 / A1 / "A1版：时政"(版号+版面名整体)
    board_name: str = ""
    image_url: str | None = None
    image_local: str | None = None  # 相对 DATA_DIR 的路径
    status: Status = Status.PENDING
    error: str | None = None


@dataclass
class ArticlePosition:
    """版面页 <map> area 解析出的报道位置"""

    metaid: str
    title: str
    poly_points: list[tuple[float, float]]  # 原始多边形坐标
    bbox: dict  # {x, y, w, h} 包围盒(版面图坐标,像素)


@dataclass
class Article:
    metaid: str  # nw.*
    paperid: str
    date: str
    board_metaid: str = ""
    title: str = ""
    content_path: str | None = None  # 正文 JSON 相对 DATA_DIR 的路径
    source_meta: str = ""  # "报纸/日期/版面/栏目" 元信息
    bbox_x: float | None = None
    bbox_y: float | None = None
    bbox_w: float | None = None
    bbox_h: float | None = None
    poly_points: str = ""  # 原始多边形坐标 JSON 串(DB 存储形态)
    image_count: int = 0
    status: Status = Status.PENDING
    error: str | None = None


@dataclass
class ParsedArticle:
    """报道页解析结果。blocks 为保序的段落+图片列表:

    - 段落块: {"type": "p", "text"}
    - 图片块: {"type": "img", "src", "alt", "index", "local_path"}
      index 为该文第几个图片块(1 起),与配图下载文件名序号对应;
      local_path 由阶段3下载后回填,解析阶段为 None。
    """

    title: str
    source_meta: str
    blocks: list[dict] = field(default_factory=list)


@dataclass
class ImageResult:
    """单张配图下载结果;local_path 为相对 DATA_DIR 路径,下载失败为 None"""

    url: str
    local_path: str | None
    ok: bool
