"""The runtime interface exposed to backend-independent device drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
import inspect
import json
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
        internal = self.__dict__.get("_material_sync_service")
        return sorted(
            str(service.service_name)
            for service in self.__dict__.setdefault("_device_services", [])
            if service is not internal
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
        if self.backend_name == "ros2" and message_type is None:
            from std_msgs.msg import Bool, Float64, Int64, String

            if isinstance(value, bool):
                message_type, payload = Bool, value
            elif isinstance(value, int):
                message_type, payload = Int64, value
            elif isinstance(value, float):
                message_type, payload = Float64, value
            else:
                message_type = String
                payload = (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False)
                )
            key = (self.resolve_topic_name(topic), message_type)
            publishers = self.__dict__.setdefault(
                "_device_dynamic_publishers", {}
            )
            publisher = publishers.get(key)
            if publisher is None:
                publisher = self.create_publisher(message_type, key[0], 10)
                publishers[key] = publisher
            publisher.publish(message_type(data=payload))
            return
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

    @staticmethod
    def _material_uuid(value: Any, role: str) -> str:
        if isinstance(value, dict):
            material_uuid = value.get("uuid") or value.get("unilabos_uuid")
            if not material_uuid and isinstance(value.get("data"), dict):
                material_uuid = value["data"].get("unilabos_uuid")
        else:
            material_uuid = getattr(value, "unilabos_uuid", None)
        normalized = str(material_uuid or "").strip()
        if not normalized:
            raise ValueError(f"{role}物料 {value!r} 缺少微后端 UUID")
        return normalized

    def _resource_driver(self) -> Any:
        return getattr(self, "driver_instance", getattr(self, "driver", None))

    async def _invoke_resource_hook(self, name: str, *args: Any) -> None:
        callback = getattr(self._resource_driver(), name, None)
        if not callable(callback):
            return
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    async def _remove_local_materials(self, material_uuids: list[str]) -> list[Any]:
        tracker = getattr(self, "resource_tracker", None)
        if tracker is None:
            raise BackendCapabilityError(
                f"设备 {self.device_id!r} 尚未配置 resource tracker"
            )
        resources = [
            tracker.uuid_to_resources[material_uuid]
            for material_uuid in material_uuids
            if material_uuid in tracker.uuid_to_resources
        ]
        if resources:
            await self._invoke_resource_hook("resource_tree_remove", resources)
        for resource in resources:
            parent = getattr(resource, "parent", None)
            if parent is not None:
                parent.unassign_child_resource(resource)
            tracker.remove_resource(resource)
        return resources

    @staticmethod
    def _site_spot(parent: Any, selector: Any | None) -> int | None:
        from unilabos.resources.materials import resolve_site_spot

        spot = resolve_site_spot(parent, selector)
        if spot is not None or selector is None or str(selector).strip() == "":
            return spot
        normalized = str(selector).strip()
        resource_sites = getattr(parent, "resource_sites", None) or []
        for ordinal, site in enumerate(resource_sites):
            values = {
                str(getattr(site, "uuid", "") or ""),
                str(getattr(site, "label", "") or ""),
                str(getattr(site, "index", "") or ""),
            }
            if normalized in values:
                return ordinal
        sites = getattr(parent, "sites", None)
        holders = list(sites.values()) if isinstance(sites, dict) else list(sites or [])
        for ordinal, holder in enumerate(holders):
            values = {
                str(getattr(holder, "unilabos_site_uuid", "") or ""),
                str(getattr(holder, "name", "") or ""),
            }
            if normalized in values:
                return ordinal
        raise ValueError(
            f"本地目标物料 {getattr(parent, 'name', parent)!r} 不存在 Site {selector!r}"
        )

    async def _attach_local_materials(
        self,
        material_uuids: list[str],
        sites: list[Any | None],
    ) -> list[Any]:
        tracker = getattr(self, "resource_tracker", None)
        if tracker is None:
            raise BackendCapabilityError(
                f"设备 {self.device_id!r} 尚未配置 resource tracker"
            )
        tree_set = await self._require_resource_service().get_resources(
            self.device_id,
            material_uuids,
            True,
        )
        resources = tree_set.to_plr_resources()
        if len(resources) != len(material_uuids):
            raise ValueError(
                "微后端返回的移动物料数量与请求不一致："
                f"requested={len(material_uuids)} actual={len(resources)}"
            )

        attached: list[Any] = []
        for resource, tree, site in zip(resources, tree_set.trees, sites):
            material_uuid = self._material_uuid(resource, "目标同步")
            existing = tracker.uuid_to_resources.get(material_uuid)
            if existing is not None:
                await self._remove_local_materials([material_uuid])
            tracker.add_resource(resource)
            parent_uuid = str(tree.root_node.res_content.uuid_parent or "")
            device_uuids = {
                str(getattr(self, "resource_uuid", "") or ""),
                str(getattr(self, "uuid", "") or ""),
            }
            parent = None
            if parent_uuid and parent_uuid not in device_uuids:
                parent = tracker.uuid_to_resources.get(parent_uuid)
                if parent is None:
                    tracker.remove_resource(resource)
                    raise ValueError(
                        f"目标设备 {self.device_id!r} 找不到挂载物料 {parent_uuid}"
                    )
                tracker.resources = [
                    item for item in tracker.resources if item is not resource
                ]
                spot = self._site_spot(parent, site)
                assign_site = getattr(parent, "assign_resource_to_site", None)
                if callable(assign_site) and spot is not None:
                    assign_site(resource, spot)
                else:
                    assign = parent.assign_child_resource
                    parameters = inspect.signature(assign).parameters
                    kwargs: dict[str, Any] = {}
                    if "spot" in parameters:
                        kwargs["spot"] = spot
                    assign(resource, location=None, **kwargs)
            if parent is not None:
                await self._invoke_resource_hook(
                    "resource_tree_transfer",
                    None,
                    resource,
                    parent,
                )
            attached.append(resource)
        if attached:
            await self._invoke_resource_hook("resource_tree_add", attached)
        return attached

    async def _material_sync_callback(self, request: Any, response: Any) -> Any:
        command = json.loads(str(getattr(request, "command", "") or "{}"))
        action = str(command.get("action") or "").strip()
        material_uuids = [
            str(value).strip()
            for value in command.get("material_uuids", [])
            if str(value).strip()
        ]
        if action == "remove":
            await self._remove_local_materials(material_uuids)
        elif action == "attach":
            sites = list(command.get("sites") or [None] * len(material_uuids))
            if len(sites) != len(material_uuids):
                raise ValueError("material_sync 的物料与 Site 数量必须一致")
            await self._attach_local_materials(material_uuids, sites)
        else:
            raise ValueError(f"未知 material_sync action：{action!r}")
        response.response = json.dumps(
            {
                "success": True,
                "action": action,
                "material_uuids": material_uuids,
            },
            ensure_ascii=False,
        )
        return response

    def setup_material_sync_service(self) -> Any:
        existing = self.__dict__.get("_material_sync_service")
        if existing is not None:
            return existing
        if self.backend_name == "ros2":
            from unilabos_msgs.srv import SerialCommand as service_type
        else:
            from unilabos.device_runtime.resource import (
                MaterialSyncService as service_type,
            )

        service = self.create_service(
            service_type,
            f"/srv{self.get_namespace()}/material_sync",
            self._material_sync_callback,
            callback_group=getattr(self, "callback_group", None),
        )
        self.__dict__["_material_sync_service"] = service
        return service

    async def transfer_resource_to_another(
        self,
        plr_resources: list[Any],
        target_device_id: str,
        target_resources: list[Any],
        sites: list[Optional[str]],
    ) -> Any:
        material_uuids = [
            self._material_uuid(resource, "来源") for resource in plr_resources
        ]
        target_uuids = [
            self._material_uuid(resource, "目标") for resource in target_resources
        ]
        site_selectors = list(sites)
        if not (
            len(material_uuids) == len(target_uuids) == len(site_selectors)
        ):
            raise ValueError("来源物料、目标物料和 Site 数量必须一致")
        normalized_target = str(target_device_id or "").strip()
        if normalized_target.startswith("/devices/"):
            normalized_target = normalized_target[len("/devices/") :]
        normalized_target = normalized_target.strip("/")
        if not normalized_target:
            raise ValueError("目标设备 ID 不能为空")

        moved = await self._require_resource_service().move_resources(
            self.device_id,
            self.resource_uuid,
            material_uuids,
            target_uuids,
            site_selectors,
        )

        # 微后端先成为唯一权威；本地 tracker 只消费已经提交的结果。
        await self._remove_local_materials(material_uuids)
        if self.backend_name == "ros2":
            from unilabos_msgs.srv import SerialCommand as service_type
        else:
            from unilabos.device_runtime.resource import (
                MaterialSyncService as service_type,
            )

        service_name = f"/srv/devices/{normalized_target}/material_sync"
        client = self.create_client(
            service_type,
            service_name,
            callback_group=getattr(self, "callback_group", None),
        )
        if not client.wait_for_service(timeout_sec=5.0):
            raise BackendCapabilityError(
                f"目标设备 {normalized_target!r} 未暴露 material_sync service"
            )
        request = service_type.Request()
        request.command = json.dumps(
            {
                "action": "attach",
                "material_uuids": material_uuids,
                "sites": site_selectors,
            },
            ensure_ascii=False,
        )
        sync_response = await client.call_async(request)
        sync_result = json.loads(str(sync_response.response or "{}"))
        if not sync_result.get("success"):
            raise RuntimeError(
                f"目标设备 {normalized_target!r} 本地物料同步失败：{sync_result}"
            )
        return {
            "success": True,
            "material_uuids": material_uuids,
            "target_device_id": normalized_target,
            "target_resources_uuid": target_uuids,
            "moves": [
                value.model_dump(mode="json", exclude_none=False)
                if hasattr(value, "model_dump")
                else value
                for value in moved
            ],
        }

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
