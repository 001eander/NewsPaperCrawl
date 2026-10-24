"""
日志统一配置
============
控制台 + 文件双通道:
  - 控制台: INFO 起,人类可读,实时查看进度
  - 文件:   <LOG_DIR>/crawl.log,INFO 起,按天滚动归档(保留 30 天)

幂等:重复调用不会叠加 handler(多次运行时也只写一份)。
归档文件名形如 crawl.log.2026-08-02 —— RotatingFileHandler 用时间戳后缀,
便于按日追溯当天完整日志。
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .adapters.config import Settings

# 日志路径与节流间隔与组合根同源(导入时求值,与原模块级常量语义一致)
LOG_DIR = Settings.from_env().log_dir
LOG_INTERVAL = Settings.from_env().log_interval

# 单一格式即可:时间 / 级别 / 模块 / 消息
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_FILE = "crawl.log"


def setup_logging(level: int = logging.INFO) -> None:
    """配置控制台与按日滚动文件；已有 GUI handler 时只补文件通道。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)

    if not root.handlers:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        root.addHandler(console)

    log_path = LOG_DIR / _LOG_FILE
    file_handler = next(
        (
            handler
            for handler in root.handlers
            if isinstance(handler, TimedRotatingFileHandler)
            and Path(handler.baseFilename) == log_path
        ),
        None,
    )
    if file_handler is None:
        file_handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
            utc=False,
        )
        file_handler.suffix = "%Y-%m-%d"
        root.addHandler(file_handler)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)


class ProgressLogger:
    """
    节流进度日志:每 count 次调用打一条汇总(默认 INFO),其余打明细(DEBUG)。

    防止高频事件(如报道逐篇落库)把 crawl.log 撑爆,同时保留逐条可查的
    DEBUG 明细。用法:

        plog = ProgressLogger("crawler", interval=10)
        plog.tick("报道落库 %s: %s", metaid, title)
        # 每次调用内部计数;计数为 interval 的倍数 -> logger.info(汇总)
        # 其余调用 -> logger.debug(明细)
    """

    def __init__(self, name: str, interval: int = LOG_INTERVAL):
        self.logger = logging.getLogger(name)
        # 有效区间:1 表示每条都打(退化),避免 0/负值造成除零或永不汇总
        self.interval = max(1, interval)
        self.count = 0

    def tick(self, msg: str, *args) -> None:
        self.count += 1
        if self.count % self.interval == 0:
            self.logger.info(f"{msg} [累计 %s]", *args, self.count)
        else:
            self.logger.debug(msg, *args)
