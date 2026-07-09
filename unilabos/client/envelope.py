"""响应信封解析

uni-lab-backend 的 HTTP 响应格式：
{
    "code": 0,           # 0 表示成功，非 0 表示错误
    "error": "",         # 错误信息
    "data": {...},       # 实际数据
    "timestamp": 1234567890
}
"""

from typing import Any, Dict, Optional
from dataclasses import dataclass


@dataclass
class Envelope:
    """响应信封"""
    code: int
    error: str
    data: Any
    timestamp: int

    def is_success(self) -> bool:
        """是否成功"""
        return self.code == 0


class EnvelopeError(Exception):
    """信封错误"""
    def __init__(self, code: int, error: str):
        self.code = code
        self.error = error
        super().__init__(f"[{code}] {error}")


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

    return Envelope(
        code=response_json.get("code", -1),
        error=response_json.get("error", ""),
        data=response_json.get("data"),
        timestamp=response_json.get("timestamp", 0),
    )


def unwrap_envelope(response_json: Dict[str, Any]) -> Any:
    """解析响应信封并提取数据

    Args:
        response_json: HTTP 响应的 JSON 数据

    Returns:
        data 字段的内容

    Raises:
        EnvelopeError: 响应 code 非 0
        ValueError: 响应格式不正确
    """
    envelope = parse_envelope(response_json)

    if not envelope.is_success():
        raise EnvelopeError(envelope.code, envelope.error)

    return envelope.data
