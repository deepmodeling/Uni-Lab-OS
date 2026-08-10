"""The runtime interface exposed to backend-independent device drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, Optional

StatusListener = Callable[[str, str, Any], None]


class BackendCapabilityError(RuntimeError):
    """The selected backend does not implement a requested device operation."""


class DeviceNode(ABC):
    """Small backend-neutral API passed to ``driver.post_init``.

    Transport-specific methods such as ROS publishers and subscriptions are
    deliberately not part of this class. A driver using those APIs remains a
    backend-specific driver.
    """

    backend_name = "unknown"
    device_id: str

    @property
    def identifier(self) -> str:
        return self.device_id

    @abstractmethod
    def lab_logger(self) -> Any:
        """Return the logger associated with this device."""

    @abstractmethod
    async def sleep(self, rel_time: float, callback_group: Any = None) -> None:
        """Sleep without blocking the backend executor."""

    @abstractmethod
    def create_task(self, coroutine: Awaitable[Any]) -> Any:
        """Schedule an awaitable on the backend executor."""

    async def update_resource(self, resources: Any) -> Any:
        raise BackendCapabilityError(
            f"backend '{self.backend_name}' 尚未实现设备物料更新"
        )

    async def get_resource(
        self,
        resources_uuid: list[str],
        with_children: bool = True,
    ) -> Any:
        del resources_uuid, with_children
        raise BackendCapabilityError(
            f"backend '{self.backend_name}' 尚未实现设备物料查询"
        )

    async def call_device_action(
        self,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Any:
        del device_id, action_name, arguments
        raise BackendCapabilityError(
            f"backend '{self.backend_name}' 尚未实现跨设备动作调用"
        )

    def add_status_listener(self, listener: StatusListener) -> None:
        listeners = self.__dict__.setdefault("_device_status_listeners", [])
        if listener not in listeners:
            listeners.append(listener)

    def remove_status_listener(self, listener: StatusListener) -> None:
        listeners = self.__dict__.setdefault("_device_status_listeners", [])
        if listener in listeners:
            listeners.remove(listener)

    def emit_status(self, name: str, value: Any) -> None:
        cache = self.__dict__.setdefault("_device_status_cache", {})
        cache[str(name)] = value
        for listener in tuple(
            self.__dict__.setdefault("_device_status_listeners", [])
        ):
            listener(self.device_id, str(name), value)

    def latest_status(self) -> Dict[str, Any]:
        return dict(self.__dict__.setdefault("_device_status_cache", {}))


__all__ = [
    "BackendCapabilityError",
    "DeviceNode",
    "StatusListener",
]
