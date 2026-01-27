from typing import Any, Optional


class DriverError(Exception):
    """
    功能:
        driver 统一异常基类.
    """


class ConfigError(DriverError):
    """
    功能:
        配置错误, 例如 base_url 为空.
    """


class ValidationError(DriverError):
    """
    功能:
        参数校验错误, 例如缺少必填字段.
    """


class RequestError(DriverError):
    """
    功能:
        网络请求错误, 例如连接失败, 超时, HTTP 非 200.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class ResponseError(DriverError):
    """
    功能:
        响应解析错误, 例如非 JSON, 或结构不符合预期.
    """


class ApiError(DriverError):
    """
    功能:
        业务错误, 即 HTTP 成功但 JSON 中 code != 200 或 msg 表示失败.
    参数:
        code: 设备返回的业务 code.
        msg: 设备返回的 msg.
        payload: 原始响应 JSON, 便于排查.
    """

    def __init__(self, code: Any, msg: str, payload: Optional[dict] = None):
        super().__init__(f"API error, code={code}, msg={msg}")
        self.code = code
        self.msg = msg
        self.payload = payload or {}


class AuthenticationError(DriverError):
    """
    功能:
        登录失败或 token 无效.
    """


class AuthorizationExpiredError(AuthenticationError):
    """
    功能:
        登录失效(401), 需要重新登录.
    """
