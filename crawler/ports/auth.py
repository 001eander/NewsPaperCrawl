"""
认证端口
========
HTTP 适配器对"凭证"的需求:构造 Cookie 头、判定响应是否认证失效、
失效时触发重新登录。cookie 的加载/保存/域名匹配与登录流程都是实现细节;
失效后的通知与抛错由调用方(HTTP 适配器)负责,本端口不依赖通知。
"""

from typing import Protocol


class LoginRunner(Protocol):
    """执行一次完整登录,把新 cookie 写回凭据文件"""

    def can_login(self) -> bool:
        """凭据是否齐全(缺失时爬虫不做自动登录,直接走人工处理)"""
        ...

    async def login(self) -> None:
        """重新登录;失败抛 LoginError"""
        ...


class AuthProvider(Protocol):
    @property
    def generation(self) -> int:
        """cookie 代数:每成功重新登录一次加一,用于并发调用去重"""
        ...

    def cookie_header(self, url: str = "") -> str:
        """构造 Cookie 请求头;提供 url 时按域名匹配,否则返回全部 cookie"""
        ...

    def is_auth_fail(self, text: str) -> bool:
        """判断响应文本是否表示认证失效"""
        ...

    async def relogin(self, seen_generation: int | None = None) -> bool:
        """
        尝试重新登录恢复认证。

        seen_generation 为调用方开始请求前看到的代数:若期间已有别的调用
        完成刷新(cookie 代数已变),直接返回 True,不再重复登录。
        冷却期内重复触发返回 False;登录失败抛 LoginError。
        """
        ...
