"""
领域异常契约
============
各层共享的异常类型:适配器抛出、服务层识别、组合根兜底。
"""


class AuthError(Exception):
    """认证失效异常"""


class LoginError(AuthError):
    """自动登录失败(凭据缺失、浏览器启动失败、登录被拒等)"""
