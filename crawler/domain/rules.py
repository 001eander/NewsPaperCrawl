"""
领域规则(纯函数)
================
跨层共享的编码与判定规则,全部无 I/O:

  - metaid 编码规则: extract_date / extract_board_code / article_tail
  - 报纸代码: paper_dir_code(存储路径用,去 "n.D")与
    paper_image_code(版面图 URL 用,去 "n.")——两者不同属有意为之
  - 存储相对路径规则: 统一 as_posix(/ 分隔),DB 里存的相对路径跨平台可解析,
    便于 Docker 卷在 Windows 开发机与 Linux 容器间迁移
  - 认证失效特征判定: response_is_auth_fail(站点登录页的响应特征)

metaid 编码约定(改任何一处要同步验证):
  版面: nb.{code}_{YYYYMMDD}_{board}(如 nb.D411300nyrb_20260731_A1)
  报道: nw.{code}_{YYYYMMDD}_{board}-{seq}(报道 metaid 必须含 -,否则请求会因截断失败)
"""

import re

from .models import Article


def paper_dir_code(paperid: str) -> str:
    """存储路径用的报纸代码: n.D330100hzrb -> 330100hzrb"""
    return paperid.replace("n.D", "")


def paper_image_code(paperid: str) -> str:
    """版面图 URL 用的报纸代码: n.D411300nyrb -> D411300nyrb(目录名和 metaid 都带 D)"""
    return paperid.replace("n.", "")


def extract_date(metaid: str) -> str:
    """从 metaid 提取 8 位日期(YYYYMMDD),无则空串"""
    m = re.search(r"_(\d{8})_", metaid)
    return m.group(1) if m else ""


def extract_board_code(metaid: str) -> str:
    """从版面 metaid 取尾段作为版面编码,如 nb.D411300nyrb_20260731_A1 -> A1"""
    return metaid.split("_")[-1]


def article_tail(metaid: str) -> str:
    """报道 metaid 去掉 nw. 前缀的尾段: nw.D330100hzrb_20260110_1-A01 -> D330100hzrb_20260110_1-A01"""
    return metaid.split(".")[-1]


def date_path(date: str) -> str:
    """YYYY-MM-DD -> YYYY/MM/DD"""
    return date.replace("-", "/")


def article_dir_rel(article: Article) -> str:
    """文章目录(相对 DATA_DIR,as_posix): articles/{code}/{YYYY}/{MM}/{DD}/{tail}"""
    return (
        f"articles/{paper_dir_code(article.paperid)}"
        f"/{date_path(article.date)}/{article_tail(article.metaid)}"
    )


def article_json_rel(article: Article) -> str:
    """正文 JSON 相对 DATA_DIR 的路径: {article_dir}/content.json"""
    return f"{article_dir_rel(article)}/content.json"


def board_image_rel(paperid: str, date: str, board_code: str) -> str:
    """版面图相对 DATA_DIR 的路径: boards/{code}/{YYYY}/{MM}/{DD}/{board_code}.jpg"""
    return f"boards/{paper_dir_code(paperid)}/{date_path(date)}/{board_code}.jpg"


def positions_rel(paperid: str, date: str, board_code: str) -> str:
    """位置 JSON 相对 DATA_DIR 的路径: positions/{code}/{YYYY}/{MM}/{DD}/{board_code}.json"""
    return f"positions/{paper_dir_code(paperid)}/{date_path(date)}/{board_code}.json"


def response_is_auth_fail(text: str) -> bool:
    """
    判断响应文本是否表示认证失效。
    核心规则: 若响应是 ELib 授权访问系统的登录页(重定向后),则 cookie 已过期。
    """
    text = text[:4000]  # 只检查前部
    low = text.lower()
    # ① ELib 登录页特征: 重定向到 index.php?r=site/login, 表单字段 FrontLoginForm
    if (
        "frontloginform" in low
        or "index.php?r=site%2flogin" in low
        or "index.php?r=site/login" in low
    ):
        return True
    # ② 微信扫码登录特征(ELib 也支持)
    if "微信扫一扫登录" in text:
        return True
    # ③ 通用: sso/login 跳转
    if "sso/login" in low or "login.aspx" in low:
        return True
    # ④ 内容为空或极小,且含"登录"
    return len(text) < 200 and any(m in text for m in ["登录", "login"])
