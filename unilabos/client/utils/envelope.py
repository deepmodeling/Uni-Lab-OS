"""响应信封解析（微后端 V1 合同与旧云端 Backend 共用）。

微后端 ``/api/v1`` 的 JSON 路由（见 ``unilabos/server/api/envelope.py``）::

    成功：{"code": 0, "data": ...}                     （无数据命令省略 data）
    失败：{"code": N, "error": {"msg": "...", "code"?: "<符号码>", "details"?: {...}}}

业务失败固定 HTTP 200；控制面 token 缺失（HTTP 401，1002）与全量重置维护闸门（HTTP 503，
5001）是中间件级拒绝，状态码不是 200，但正文同样是信封。健康 / 就绪探针、SSE 与文件下载
不包信封。

旧云端 Backend 的形状是 ``{"code", "error": "<文案>", "data", "timestamp"}``，同样能解析。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

#: 信封顶层只会出现这些键（``timestamp`` 来自旧云端 Backend）
_ENVELOPE_KEYS = frozenset({"code", "data", "error", "timestamp"})

#: 业务码 → 语义等价的 HTTP 状态码。各领域客户端的 ``status_code`` 沿用 HTTP 语义
#: （404 不存在、409 冲突、422 参数不合法、503 暂不可用），调用方判断逻辑不用跟着码表改。
_HTTP_STATUS_FOR_CODE: Dict[int, int] = {
    1000: 422,
    1001: 403,
    1002: 401,
    2005: 409,
    3002: 404,
    3003: 409,
    4000: 409,
    4001: 404,
    4002: 409,
    5000: 404,
    5001: 503,
    5003: 422,
    5004: 409,
    6000: 404,
    6001: 404,
    6002: 409,
    6003: 404,
    6004: 409,
    6006: 404,
    6007: 409,
    6008: 409,
    6009: 409,
    6010: 409,
    6011: 409,
}


def http_status_for_code(code: int) -> int:
    """业务码对应的 HTTP 语义状态码；成功为 200，未归类错误为 500。"""

    if code == 0:
        return 200
    return _HTTP_STATUS_FOR_CODE.get(int(code), 500)


def code_for_http_status(status: int) -> int:
    """没有信封正文的 HTTP 错误（路由不存在、代理故障）换算一个最接近的业务码。"""

    return {400: 1000, 401: 1002, 403: 1001, 404: 4001, 409: 3003, 422: 1000, 503: 5001}.get(
        int(status), 1
    )


@dataclass
class Envelope:
    """响应信封"""

    code: int
    error: str
    data: Any
    timestamp: int
    #: 失败时 ``error.code`` 的符号码（如 ``workflow_identity_mismatch``）
    symbol: Optional[str] = None
    #: 失败时 ``error.details``
    details: Any = None

    def is_success(self) -> bool:
        """是否成功"""
        return self.code == 0


class EnvelopeError(Exception):
    """信封错误：``code`` 是业务码，``error`` 是说明文案。"""

    def __init__(
        self,
        code: int,
        error: str,
        *,
        symbol: Optional[str] = None,
        details: Any = None,
        http_status: Optional[int] = None,
    ):
        self.code = code
        self.error = error
        self.symbol = symbol
        self.details = details
        #: 实际收到的 HTTP 状态码（业务失败通常是 200）
        self.http_status = http_status
        super().__init__(f"[{code}] {error}")

    @property
    def message(self) -> str:
        return self.error

    @property
    def status_code(self) -> int:
        """HTTP 语义状态码：真实状态码是错误码时用它，否则按业务码换算。"""

        if self.http_status is not None and self.http_status >= 400:
            return int(self.http_status)
        return http_status_for_code(int(self.code))


class APIHTTPError(RuntimeError):
    """dev 内部各领域 HTTP 客户端（runtime / telemetry / history / materials）的错误基类。

    ``status_code`` 保持 HTTP 语义（业务码按 :func:`http_status_for_code` 换算），``detail`` 是
    ``error.msg``；``business_code`` / ``symbol`` / ``details`` 是信封原样的业务码、符号码与细节。
    """

    api_name = "API"

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        business_code: Optional[int] = None,
        symbol: Optional[str] = None,
        details: Any = None,
    ):
        super().__init__(f"{self.api_name} API returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.business_code = business_code if business_code is not None else code_for_http_status(status_code)
        self.symbol = symbol
        self.details = details

    @classmethod
    def from_envelope_error(cls, error: "EnvelopeError") -> "APIHTTPError":
        return cls(
            error.status_code,
            error.error,
            business_code=error.code,
            symbol=error.symbol,
            details=error.details,
        )


def is_envelope(payload: Any) -> bool:
    """是不是信封形状：对象、整数 ``code``，且顶层只有信封字段。"""

    if not isinstance(payload, Mapping):
        return False
    code = payload.get("code")
    return isinstance(code, int) and not isinstance(code, bool) and set(payload) <= _ENVELOPE_KEYS


def parse_envelope(response_json: Dict[str, Any]) -> Envelope:
    """解析响应信封

    Args:
        response_json: HTTP 响应的 JSON 数据

    Returns:
        Envelope 对象

    Raises:
        ValueError: 响应格式不正确
    """
    if not isinstance(response_json, dict):
        raise ValueError("响应必须是 JSON 对象")

    if "code" not in response_json:
        raise ValueError("响应缺少 code 字段")

    error = response_json.get("error", "")
    symbol = None
    details = None
    if isinstance(error, dict):
        # 微后端错误形状 {"msg": "...", "code"?: "...", "details"?: {...}}
        symbol = error.get("code") if isinstance(error.get("code"), str) else None
        details = error.get("details")
        error = str(error.get("msg") or "")
    return Envelope(
        code=response_json.get("code", -1),
        error=error or "",
        data=response_json.get("data"),
        timestamp=response_json.get("timestamp", 0),
        symbol=symbol,
        details=details,
    )


def unwrap_envelope(response_json: Dict[str, Any], *, http_status: Optional[int] = None) -> Any:
    """解析响应信封并提取数据

    Args:
        response_json: HTTP 响应的 JSON 数据
        http_status: 实际 HTTP 状态码（写进错误，便于区分中间件级拒绝）

    Returns:
        data 字段的内容

    Raises:
        EnvelopeError: 响应 code 非 0
        ValueError: 响应格式不正确
    """
    envelope = parse_envelope(response_json)

    if not envelope.is_success():
        raise EnvelopeError(
            envelope.code,
            envelope.error,
            symbol=envelope.symbol,
            details=envelope.details,
            http_status=http_status,
        )

    return envelope.data


def _error_detail(payload: Any, text: str) -> str:
    if isinstance(payload, Mapping):
        detail = payload.get("detail", payload.get("error", text))
        if isinstance(detail, Mapping):
            detail = detail.get("msg", detail)
        return str(detail)
    return text


def decode_json_body(
    body: bytes | str,
    *,
    http_status: int = 200,
    envelope: bool = True,
) -> Any:
    """解析一个 HTTP 响应体。

    - 信封：``code == 0`` 返回 ``data``；否则抛 :class:`EnvelopeError`（任何 HTTP 状态码都一样）；
    - 非信封的错误响应（路由不存在、代理 / 网关错误）：抛 :class:`EnvelopeError`，业务码按
      HTTP 状态换算，``http_status`` 为真实状态码；
    - 非信封的成功响应：``envelope=False``（探针等不包信封的端点）原样返回，否则抛 ``ValueError``。
    """

    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    try:
        payload = json.loads(text) if text.strip() else None
    except ValueError:
        payload = None
        if http_status < 400:
            raise ValueError(f"响应不是合法 JSON（HTTP {http_status}）") from None
    if is_envelope(payload):
        return unwrap_envelope(payload, http_status=http_status)
    if http_status >= 400:
        raise EnvelopeError(
            code_for_http_status(http_status),
            _error_detail(payload, text),
            http_status=http_status,
        )
    if envelope:
        raise ValueError(f"响应不是信封形状（缺少 code 字段，HTTP {http_status}）")
    return payload


def request_json(
    request: Request,
    *,
    timeout: float,
    opener: Optional[Callable[..., Any]] = None,
    envelope: bool = True,
) -> Any:
    """用 urllib 发请求并解析响应体（见 :func:`decode_json_body`）；HTTP 错误码不另抛 ``HTTPError``。"""

    try:
        with (opener or urlopen)(request, timeout=timeout) as response:  # noqa: S310 - 调用方给定的微后端地址
            status = int(getattr(response, "status", None) or 200)
            raw = response.read()
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    return decode_json_body(raw, http_status=status, envelope=envelope)


__all__ = [
    "APIHTTPError",
    "Envelope",
    "EnvelopeError",
    "code_for_http_status",
    "decode_json_body",
    "http_status_for_code",
    "is_envelope",
    "parse_envelope",
    "request_json",
    "unwrap_envelope",
]
