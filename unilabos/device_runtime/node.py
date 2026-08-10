"""The runtime interface exposed to backend-independent device drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

if TYPE_CHECKING:
    from unilabos.device_runtime.action import DeviceActionRouter
    from unilabos.device_runtime.resource import ResourceService

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
    resource_uuid = ""

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
        service = self.__dict__.get("_device_resource_service")
        if service is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未实现设备物料更新"
            )
        return await service.update_resources(
            self.device_id,
            self.resource_uuid,
            resources,
        )

    async def get_resource(
        self,
        resources_uuid: list[str],
        with_children: bool = True,
    ) -> Any:
        service = self.__dict__.get("_device_resource_service")
        if service is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未实现设备物料查询"
            )
        return await service.get_resources(
            self.device_id,
            resources_uuid,
            with_children,
        )

    def set_resource_service(self, service: "ResourceService") -> None:
        self.__dict__["_device_resource_service"] = service

    def call_device_action(
        self,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **options: Any,
    ) -> Any:
        router = self.__dict__.get("_device_action_router")
        if router is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未实现跨设备动作调用"
            )
        return router.route_action(
            self.device_id,
            device_id,
            action_name,
            arguments,
            **options,
        )

    async def call_device_action_async(
        self,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **options: Any,
    ) -> Any:
        router = self.__dict__.get("_device_action_router")
        if router is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未实现跨设备动作调用"
            )
        return await router.route_action_async(
            self.device_id,
            device_id,
            action_name,
            arguments,
            **options,
        )

    def set_action_router(self, router: "DeviceActionRouter") -> None:
        self.__dict__["_device_action_router"] = router

    async def transfer_resource_to_another(
        self,
        plr_resources: list[Any],
        target_device_id: str,
        target_resources: list[Any],
        sites: list[Optional[str]],
    ) -> Any:
        del plr_resources, target_device_id, target_resources, sites
        raise BackendCapabilityError(
            f"backend '{self.backend_name}' 尚未实现跨设备物料转移"
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
