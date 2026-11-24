"""Backend-neutral action execution state."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol

FeedbackCallback = Callable[[str, Dict[str, Any]], None]
# (action_id, 返回值, 异常)：硬超时之后驱动线程才真实返回时回调（ADR 0008）
LateResultCallback = Callable[[str, Any, Optional[BaseException]], None]


class DeviceActionRouter(Protocol):
    """Route a driver's cross-device call through the active backend."""

    def route_action(
        self,
        caller_device_id: str,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **options: Any,
    ) -> Any: ...

    async def route_action_async(
        self,
        caller_device_id: str,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **options: Any,
    ) -> Any: ...


class ActionCancelled(RuntimeError):
    """Raised when an action notices that cancellation was requested."""


@dataclass
class ActionContext:
    """Identify one action and carry feedback/cancellation across backends."""

    action_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    feedback_callback: Optional[FeedbackCallback] = None
    late_result_callback: Optional[LateResultCallback] = None
    _cancelled: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )

    def publish_feedback(self, data: Optional[Dict[str, Any]] = None) -> None:
        if self.feedback_callback is not None:
            self.feedback_callback(self.action_id, dict(data or {}))

    def publish_late_result(self, value: Any = None, error: Optional[BaseException] = None) -> None:
        """动作已按硬超时报失败、驱动线程之后才返回：把真实结果交给调用方（无回调则丢弃）。"""

        if self.late_result_callback is not None:
            self.late_result_callback(self.action_id, value, error)

    def request_cancel(self) -> None:
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise ActionCancelled(f"action cancelled: {self.action_id}")


def bind_action_context(
    function: Callable[..., Any],
    kwargs: Dict[str, Any],
    context: Optional[ActionContext],
) -> Dict[str, Any]:
    """按类型给驱动动作注入 :class:`ActionContext`，返回补好参数的新字典（HostLink / ROS 2 / 跨设备调用共用）。

    注解为 ``ActionContext``、``Optional[ActionContext]`` 或 ``ActionContext | None`` 的参数都注入，与参数名无关；
    goal 里同名的值被框架的上下文覆盖。``context`` 为 ``None`` 表示这次执行没有上下文（如 ROS 2 动作）：
    可空的参数得到 ``None``，不可空的参数得到框架现建的上下文。
    """

    from unilabos.registry.action_context import action_context_parameters

    parameters = action_context_parameters(function)
    if not parameters:
        return kwargs
    bound = dict(kwargs)
    created: Optional[ActionContext] = None
    for name, optional in parameters.items():
        if context is not None:
            bound[name] = context
        elif optional:
            bound[name] = None
        else:
            created = created or ActionContext()
            bound[name] = created
    return bound


__all__ = [
    "ActionCancelled",
    "ActionContext",
    "DeviceActionRouter",
    "FeedbackCallback",
    "LateResultCallback",
    "bind_action_context",
]
