"""
认证端口
========
HTTP 适配器对"凭证"的需求:构造 Cookie 头 + 判定响应是否认证失效。
cookie 的加载/保存/域名匹配是实现细节;失效后的通知与抛错由调用方
(HTTP 适配器)负责,本端口不依赖通知。
"""

from typing import Protocol


class AuthProvider(Protocol):
    def cookie_header(self, url: str = "") -> str:
        """构造 Cookie 请求头;提供 url 时按域名匹配,否则返回全部 cookie"""
        ...

    def is_auth_fail(self, text: str) -> bool:
        """判断响应文本是否表示认证失效"""
        ...
