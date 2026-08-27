"""Task Action 返回值失败判定与异常类型。"""

from __future__ import annotations

from typing import Any


class ActionReturnedFailure(RuntimeError):
    """Action 已返回失败结果，且结果日志已在抛出前写入。"""

    def __init__(
        self,
        *,
        device_id: str,
        action_name: str,
        failure: Any,
    ) -> None:
        self.device_id = device_id
        self.action_name = action_name
        self.failure = failure
        super().__init__(f"动作返回失败: {device_id}.{action_name}: {failure}")


def find_action_failure(
    value: Any,
    *,
    _allow_bare_false: bool = True,
) -> Any | None:
    """查找动作返回契约中的严格 False，不把 0、空值或描述文本误判为失败。"""
    if _allow_bare_false and type(value) is bool:
        return value if value is False else None
    if isinstance(value, dict):
        if value.get("success") is False:
            return value
        if "result" in value:
            failure = find_action_failure(
                value["result"],
                _allow_bare_false=True,
            )
            if failure is not None:
                return failure
        for key, nested in value.items():
            if key == "result":
                continue
            failure = find_action_failure(
                nested,
                _allow_bare_false=False,
            )
            if failure is not None:
                return failure
    if isinstance(value, (list, tuple)):
        if (
            _allow_bare_false
            and value
            and type(value[0]) is bool
            and value[0] is False
        ):
            return value
        for item in value:
            failure = find_action_failure(
                item,
                _allow_bare_false=False,
            )
            if failure is not None:
                return failure
    return None
