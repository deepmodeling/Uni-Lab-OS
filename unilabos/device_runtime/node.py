"""The runtime interface exposed to backend-independent device drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from contextlib import contextmanager, nullcontext
import inspect
import json
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterator, Optional

from unilabos.device_runtime.async_utils import schedule_async_func

if TYPE_CHECKING:
    from unilabos.device_runtime.resource import ResourceService

StatusListener = Callable[[str, str, Any], None]


class BackendCapabilityError(RuntimeError):
    """The selected backend does not implement a requested device operation."""


class DeviceNode(ABC):
    """Small backend-neutral API passed to ``driver.post_init``.

    Device actions and JSON-compatible topics are available on every backend.
    ROS2 keeps using native DDS implementations through ``rclpy.node.Node``;
    HostLink uses the topic bus configured by its local runtime.
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

    def _require_resource_service(self) -> "ResourceService":
        service = self.__dict__.get("_device_resource_service")
        if service is None:
            raise BackendCapabilityError(
                f"backend '{self.backend_name}' 尚未接入微后端 Materials Authority"
            )
        return service

    def set_resource_service(self, service: "ResourceService") -> None:
        self.__dict__["_device_resource_service"] = service
        tracker = getattr(self, "resource_tracker", None)
        if tracker is None:
            return
        from unilabos.device_runtime.resource import MaterialSnapshotObserver

        observer = getattr(tracker, "_material_snapshot_observer", None)
        if observer is None:
            observer = MaterialSnapshotObserver(
                service,
                device_id=lambda: str(self.device_id),
                device_uuid=lambda: str(self.resource_uuid),
                schedule=self.create_task,
            )
            tracker._material_snapshot_observer = observer
        else:
            observer.set_service(service)
        self.__dict__["_material_snapshot_observer"] = observer
        observer.observe_all(list(tracker.resources))

    @contextmanager
    def material_authority_sync(self) -> Iterator[None]:
        """权威 load/unload 投影本地 PLR 时禁止产生 snapshot 回声。"""

        observer = self.__dict__.get("_material_snapshot_observer")
        context = (
            observer.suppress_authority_projection()
            if observer is not None
            else nullcontext()
        )
        with context:
            yield

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

    @staticmethod
    def _material_uuid(value: Any, role: str) -> str:
        from unilabos.resources.materials import material_uuid

        return material_uuid(value, role)

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
                observer = getattr(
                    tracker, "_material_snapshot_observer", None
                )
                if observer is not None:
                    observer.unobserve(resource)
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
        requested_action = str(command.get("action") or "").strip()
        action = {
            "remove": "unload",
            "attach": "load",
            "unload": "unload",
            "load": "load",
        }.get(requested_action, requested_action)
        material_uuids = [
            str(value).strip()
            for value in command.get("material_uuids", [])
            if str(value).strip()
        ]
        transfer_uuid = str(command.get("transfer_uuid") or "").strip()
        sync_key = f"{transfer_uuid}:{action}" if transfer_uuid else ""
        lock = self.__dict__.get("_material_sync_lock")
        if lock is None:
            lock = asyncio.Lock()
            self.__dict__["_material_sync_lock"] = lock
        replayed = False
        async with lock:
            completed = self.__dict__.setdefault(
                "_completed_material_sync_commands", set()
            )
            if sync_key and sync_key in completed:
                replayed = True
            else:
                with self.material_authority_sync():
                    if action == "unload":
                        await self._remove_local_materials(material_uuids)
                    elif action == "load":
                        sites = list(
                            command.get("destination_site_uuids")
                            or command.get("sites")
                            or [None] * len(material_uuids)
                        )
                        if len(sites) != len(material_uuids):
                            raise ValueError(
                                "material_sync 的物料与 Site 数量必须一致"
                            )
                        await self._attach_local_materials(
                            material_uuids, sites
                        )
                    else:
                        raise ValueError(
                            f"未知 material_sync action：{action!r}"
                        )
                if sync_key:
                    completed.add(sync_key)
        response.response = json.dumps(
            {
                "success": True,
                "action": action,
                "material_uuids": material_uuids,
                "transfer_uuid": transfer_uuid,
                "replayed": replayed,
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
        from unilabos.resources.materials import transfer

        return await transfer(
            plr_resources,
            target_device_id,
            target_resources,
            sites,
            source_device_id=self.device_id,
            source_device_uuid=self.resource_uuid,
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
