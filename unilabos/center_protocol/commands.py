"""中心控制命令的纯协议校验，供收件执行与派发闸门共用。"""

from __future__ import annotations

from typing import Any
from uuid import UUID


def validate_cancellation(payload: Any) -> dict[str, Any]:
    """验证取消目标和原因；无效正文抛 ValueError，不授予控制权。"""
    if (not isinstance(payload, dict)
            or set(payload) != {"station_invocation_uuid", "global_task_uuid", "reason"}
            or not isinstance(payload["reason"], str)
            or not 1 <= len(payload["reason"].strip().encode("utf-8")) <= 1000):
        raise ValueError("取消通知字段无效")
    for field in ("station_invocation_uuid", "global_task_uuid"):
        value = payload[field]
        try:
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("取消通知 UUID 无效") from None
    return payload
