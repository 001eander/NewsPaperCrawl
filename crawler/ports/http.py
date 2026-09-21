"""
HTTP 网关端口
=============
服务层对"抓网页/下载文件"的全部需求。实现细节(限速、重试、cookie、
认证检测、TLS 校验)由适配器负责。
"""

from pathlib import Path
from typing import Protocol


class HttpGateway(Protocol):
    async def get(self, url: str) -> str:
        """GET 并返回文本;404 返回空串;认证失效抛 AuthError;重试耗尽抛 RuntimeError"""
        ...

    async def download(self, url: str, save_path: Path) -> bool:
        """下载二进制文件到 save_path,返回是否成功(失败不抛异常)"""
        ...
