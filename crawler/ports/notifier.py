"""
通知端口
========
向用户推送消息(认证失效/任务收尾)。实现必须"配置缺失/推送失败只记日志,
不抛异常"。推送是异步 I/O,调用方必须 await——CLI 是"一次 asyncio.run
即退出"的程序,挂后台任务会被事件循环关闭时取消。
"""

from typing import Protocol


class Notifier(Protocol):
    @property
    def is_configured(self) -> bool:
        """是否已配置(可真正推送);未配置时 notify 静默跳过"""
        ...

    async def notify(self, title: str, body: str = "") -> None: ...
