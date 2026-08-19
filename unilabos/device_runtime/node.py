"""The runtime interface exposed to backend-independent device drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
import inspect
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterable, Optional

from unilabos.device_runtime.async_utils import schedule_async_func
from unilabos.device_runtime.primitives import (
    DeviceClock,
    DeviceParameter,
    DeviceRate,
    DeviceTimer,
    SetParametersResult,
)

if TYPE_CHECKING:
    from unilabos.device_runtime.action import DeviceActionRouter
    from unilabos.device_runtime.resource import ResourceService
    from unilabos.device_runtime.service import ServiceBus
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

    def get_name(self) -> str:
        return self.device_id.strip("/").split("/")[-1]

    def get_namespace(self) -> str:
        return f"/devices/{self.device_id.strip('/')}"

    def get_fully_qualified_name(self) -> str:
        return f"{self.get_namespace()}/{self.get_name()}"

    def get_logger(self) -> Any:
        return self.lab_logger()

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

    def get_clock(self) -> DeviceClock:
        clock = self.__dict__.get("_device_clock")
        if clock is None:
            clock = DeviceClock()
            self.__dict__["_device_clock"] = clock
        return clock

    def create_timer(
        self,
        timer_period_sec: float,
        callback: Callable[[], Any],
        callback_group: Any = None,
        clock: Any = None,
        autostart: bool = True,
    ) -> DeviceTimer:
        del callback_group, clock
        timer = DeviceTimer(
            self,
            timer_period_sec,
            callback,
            autostart=autostart,
        )
        self.__dict__.setdefault("_device_timers", []).append(timer)
        return timer

    def destroy_timer(self, timer: DeviceTimer) -> bool:
        timers = self.__dict__.setdefault("_device_timers", [])
        timer.cancel()
        if timer in timers:
            timers.remove(timer)
            return True
        return False

    def create_rate(self, frequency: float, clock: Any = None) -> DeviceRate:
        del clock
        return DeviceRate(frequency)

    def declare_parameter(
        self,
        name: str,
        value: Any = None,
        descriptor: Any = None,
        ignore_override: bool = False,
    ) -> DeviceParameter:
        del descriptor, ignore_override
        parameters = self.__dict__.setdefault("_device_parameters", {})
        key = str(name)
        if key in parameters:
            raise ValueError(f"parameter 已声明：{key}")
        parameters[key] = value
        return DeviceParameter(key, value)

    def declare_parameters(
        self,
        namespace: str,
        parameters: Iterable[Any],
        ignore_override: bool = False,
    ) -> list[DeviceParameter]:
        prefix = str(namespace or "").strip(".")
        declared = []
        for item in parameters:
            if isinstance(item, DeviceParameter):
                name, value = item.name, item.value
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                name, value = item[0], item[1]
            else:
                name, value = str(item), None
            full_name = f"{prefix}.{name}" if prefix else str(name)
            declared.append(
                self.declare_parameter(
                    full_name,
                    value,
                    ignore_override=ignore_override,
                )
            )
        return declared

    def has_parameter(self, name: str) -> bool:
        return str(name) in self.__dict__.setdefault("_device_parameters", {})

    def get_parameter(self, name: str) -> DeviceParameter:
        key = str(name)
        parameters = self.__dict__.setdefault("_device_parameters", {})
        if key not in parameters:
            return DeviceParameter(key, None)
        return DeviceParameter(key, parameters[key])

    def get_parameters(self, names: Iterable[str]) -> list[DeviceParameter]:
        return [self.get_parameter(name) for name in names]

    def get_parameter_or(
        self,
        name: str,
        alternative_value: DeviceParameter,
    ) -> DeviceParameter:
        return (
            self.get_parameter(name) if self.has_parameter(name) else alternative_value
        )

    def set_parameters(self, parameters: Iterable[Any]) -> list[SetParametersResult]:
        normalized = [
            item
            if isinstance(item, DeviceParameter)
            else DeviceParameter(
                str(getattr(item, "name", "")), getattr(item, "value", None)
            )
            for item in parameters
        ]
        callbacks = tuple(self.__dict__.setdefault("_device_parameter_callbacks", []))
        for callback in callbacks:
            result = callback(normalized)
            if getattr(result, "successful", True) is False:
                reason = str(getattr(result, "reason", "parameter 被回调拒绝"))
                return [SetParametersResult(False, reason) for _ in normalized]
        storage = self.__dict__.setdefault("_device_parameters", {})
        for parameter in normalized:
            storage[parameter.name] = parameter.value
        return [SetParametersResult() for _ in normalized]

    def set_parameters_atomically(
        self, parameters: Iterable[Any]
    ) -> SetParametersResult:
        results = self.set_parameters(parameters)
        return next(
            (result for result in results if not result.successful),
            SetParametersResult(),
        )

    def undeclare_parameter(self, name: str) -> None:
        self.__dict__.setdefault("_device_parameters", {}).pop(str(name), None)

    def add_on_set_parameters_callback(self, callback: Callable[[Any], Any]) -> Any:
        callbacks = self.__dict__.setdefault("_device_parameter_callbacks", [])
        callbacks.append(callback)
        return callback

    def remove_on_set_parameters_callback(self, callback: Callable[[Any], Any]) -> None:
        callbacks = self.__dict__.setdefault("_device_parameter_callbacks", [])
        if callback in callbacks:
            callbacks.remove(callback)

    def _require_resource_service(self) -> "ResourceService":
        service = self.__dict__.get("_device_resource_service")
        if service is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未接入微后端 Materials Authority"
            )
        return service

    def set_resource_service(self, service: "ResourceService") -> None:
        self.__dict__["_device_resource_service"] = service

    async def create_material(self, resources: Any) -> Any:
        return await self._require_resource_service().create_resources(
            self.device_id,
            self.resource_uuid,
            resources,
        )

    async def update_resource(self, resources: Any) -> Any:
        return await self._require_resource_service().update_resources(
            self.device_id,
            self.resource_uuid,
            resources,
        )

    async def get_resource(
        self,
        resources_uuid: list[str],
        with_children: bool = True,
    ) -> Any:
        return await self._require_resource_service().get_resources(
            self.device_id,
            resources_uuid,
            with_children,
        )

    def set_service_bus(self, bus: "ServiceBus") -> None:
        self.__dict__["_device_service_bus"] = bus

    def create_service(
        self,
        srv_type: Any,
        srv_name: str,
        callback: Callable[..., Any],
        *,
        qos_profile: Any = None,
        callback_group: Any = None,
    ) -> Any:
        del qos_profile, callback_group
        from unilabos.device_runtime.service import (
            DeviceService,
            build_service_callback,
            normalize_service_name,
        )

        bus = self.__dict__.get("_device_service_bus")
        if bus is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未实现 service"
            )
        name = normalize_service_name(srv_name, self.device_id)
        bus.register_service(
            name,
            build_service_callback(self, srv_type, callback),
            owner_device_id=self.device_id,
        )
        service = DeviceService(bus, name, srv_type, self.device_id)
        self.__dict__.setdefault("_device_services", []).append(service)
        return service

    def destroy_service(self, service: Any) -> bool:
        services = self.__dict__.setdefault("_device_services", [])
        service.destroy()
        if service in services:
            services.remove(service)
            return True
        return False

    def create_client(
        self,
        srv_type: Any,
        srv_name: str,
        *,
        qos_profile: Any = None,
        callback_group: Any = None,
    ) -> Any:
        del qos_profile, callback_group
        from unilabos.device_runtime.service import (
            DeviceServiceClient,
            normalize_service_name,
        )

        bus = self.__dict__.get("_device_service_bus")
        if bus is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未实现 service client"
            )
        client = DeviceServiceClient(
            bus,
            normalize_service_name(srv_name, self.device_id),
            srv_type,
            self.device_id,
        )
        self.__dict__.setdefault("_device_service_clients", []).append(client)
        return client

    def destroy_client(self, client: Any) -> bool:
        clients = self.__dict__.setdefault("_device_service_clients", [])
        if client in clients:
            clients.remove(client)
            return True
        return False

    def service_names(self) -> list[str]:
        return sorted(
            str(service.service_name)
            for service in self.__dict__.setdefault("_device_services", [])
        )

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

    def destroy_subscription(self, subscription: "TopicSubscription") -> bool:
        subscription.destroy()
        return True

    def destroy_publisher(self, publisher: "TopicPublisher") -> bool:
        del publisher
        return True

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
        for listener in tuple(self.__dict__.setdefault("_device_status_listeners", [])):
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
