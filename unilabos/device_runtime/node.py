"""The runtime interface exposed to backend-independent device drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
import inspect
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from unilabos.device_runtime.async_utils import schedule_async_func

if TYPE_CHECKING:
    from unilabos.device_runtime.action import DeviceActionRouter
    from unilabos.device_runtime.resource import ResourceService
    from unilabos.device_runtime.topic import (
        TopicBus,
        TopicPublisher,
        TopicSubscription,
    )

StatusListener = Callable[[str, str, Any], None]


class BackendCapabilityError(RuntimeError):
    """The selected backend does not implement a requested device operation."""


class DeviceNode(ABC):
    """Small backend-neutral API passed to ``driver.post_init``.

    Device actions and JSON-compatible topics are available on every backend.
    ROS2 keeps using native DDS implementations through ``rclpy.node.Node``;
    Basic and HostLink use the topic bus configured by their runtime.
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

    def run_async_func(
        self,
        func: Any,
        trace_error: bool = True,
        inner_trace_callback: Optional[Callable[[Any], None]] = None,
        **kwargs: Any,
    ) -> Any:
        """在当前 backend 的执行器上运行异步函数，并返回对应 Future。"""

        return schedule_async_func(
            self.create_task,
            func,
            trace_error=trace_error,
            inner_trace_callback=inner_trace_callback,
            error_callback=self.lab_logger().error,
            **kwargs,
        )

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

    def set_topic_bus(self, bus: "TopicBus") -> None:
        self.__dict__["_device_topic_bus"] = bus

    def resolve_topic_name(self, topic: str) -> str:
        from unilabos.device_runtime.topic import normalize_topic

        return normalize_topic(topic, self.device_id)

    def create_publisher(
        self,
        msg_type: Any,
        topic: str,
        qos_profile: Any = 10,
        **kwargs: Any,
    ) -> "TopicPublisher":
        """Create a Basic/HostLink publisher with the familiar ROS call shape."""

        del qos_profile
        from unilabos.device_runtime.topic import TopicPublisher

        bus = self.__dict__.get("_device_topic_bus")
        if bus is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未实现消息发布"
            )
        return TopicPublisher(
            bus,
            self.resolve_topic_name(topic),
            self.device_id,
            msg_type,
            retain=bool(kwargs.get("retain", False)),
        )

    def create_subscription(
        self,
        msg_type: Any,
        topic: str,
        callback: Callable[[Any], Any],
        qos_profile: Any = 10,
        **kwargs: Any,
    ) -> "TopicSubscription":
        """Create a Basic/HostLink subscription and pass decoded Python data."""

        del msg_type, qos_profile
        bus = self.__dict__.get("_device_topic_bus")
        if bus is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未实现消息订阅"
            )

        def invoke(value: Any) -> Any:
            async def run_callback() -> Any:
                result = callback(value)
                if inspect.isawaitable(result):
                    return await result
                return result

            return self.create_task(run_callback())

        return bus.subscribe(
            self.resolve_topic_name(topic),
            invoke,
            trigger_when_change=bool(kwargs.get("trigger_when_change", False)),
            replay_retained=bool(kwargs.get("replay_retained", True)),
        )

    def publish_topic(
        self,
        topic: str,
        value: Any,
        *,
        message_type: Any = None,
        retain: bool = False,
    ) -> None:
        publisher = self.create_publisher(
            message_type or type(value),
            topic,
            retain=retain,
        )
        publisher.publish(value)

    def subscribe_topic(
        self,
        topic: str,
        callback: Callable[[Any], Any],
        *,
        message_type: Any = None,
        trigger_when_change: bool = False,
        replay_retained: bool = True,
    ) -> "TopicSubscription":
        return self.create_subscription(
            message_type,
            topic,
            callback,
            trigger_when_change=trigger_when_change,
            replay_retained=replay_retained,
        )

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
        if self.__dict__.get("_device_topic_bus") is not None:
            self.publish_topic(str(name), value, retain=True)

    def latest_status(self) -> Dict[str, Any]:
        return dict(self.__dict__.setdefault("_device_status_cache", {}))


__all__ = [
    "BackendCapabilityError",
    "DeviceNode",
    "StatusListener",
]
