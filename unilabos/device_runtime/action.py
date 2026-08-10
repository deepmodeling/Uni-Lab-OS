"""Backend-neutral action execution state."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol

FeedbackCallback = Callable[[str, Dict[str, Any]], None]


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
    _cancelled: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )

    def publish_feedback(self, data: Optional[Dict[str, Any]] = None) -> None:
        if self.feedback_callback is not None:
            self.feedback_callback(self.action_id, dict(data or {}))

    def request_cancel(self) -> None:
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise ActionCancelled(f"action cancelled: {self.action_id}")


__all__ = [
    "ActionCancelled",
    "ActionContext",
    "DeviceActionRouter",
    "FeedbackCallback",
]
