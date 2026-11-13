"""The runtime interface exposed to backend-independent device drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from contextlib import contextmanager, nullcontext
import inspect
import json
import threading
import traceback
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterator, List, Optional

from unilabos.backend.runtime.async_utils import DeviceAsyncMutex, schedule_async_func

if TYPE_CHECKING:
    from unilabos.backend.runtime.resource import ResourceService

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
    # 本设备在权威库（微后端 materials/graph 初始化）分配的资源 UUID；
    # 各 backend 节点构造时必须赋值，运行期不允许再从别处 getattr 兜底。
    resource_uuid: str = ""
    #: 驱动 ``post_init`` 可按名声明的可选入参，见 :meth:`post_init_kwargs`。
    POST_INIT_OPTIONAL_ARGUMENTS: tuple[str, ...] = ("sites", "resources", "site_resources")

    @property
    def identifier(self) -> str:
        return self.device_id

    def post_init_kwargs(
        self, post_init: Callable[..., Any], device_config: Any = None
    ) -> Dict[str, Any]:
        """按驱动 ``post_init`` 的签名注入可选入参；只写 ``post_init(self, node)`` 的驱动不受影响。

        三个入参都是权威优先装配完成后的设备状态，驱动不必再从
        ``node.resource_tracker`` 或物料权威反查：

        - ``sites``:          ``{label: ResourceSite}``，设备自身的位点（uuid / pose / 占用）；
        - ``resources``:      ``{name: PLR 实例}``，设备持有的物料（位点上的占用物、
          直接挂在设备上的台面）；
        - ``site_resources``: ``{label: PLR 实例 | None}``，位点 → 占用物的对应。

        ``device_config`` 缺省取节点自身的 ``device_config``（ROS2 的配置在包装节点上，
        由调用方传入）。
        """

        try:
            parameters = inspect.signature(post_init).parameters
        except (TypeError, ValueError):
            return {}
        wanted = [name for name in self.POST_INIT_OPTIONAL_ARGUMENTS if name in parameters]
        if not wanted:
            return {}

        config = device_config if device_config is not None else getattr(self, "device_config", None)
        resource = getattr(config, "res_content", None)
        sites = list(getattr(resource, "sites", None) or [])
        tracker = getattr(self, "resource_tracker", None)
        held = list(getattr(tracker, "resources", None) or [])
        by_uuid = dict(getattr(tracker, "uuid_to_resources", None) or {})
        values: Dict[str, Any] = {
            "sites": {str(site.label): site for site in sites},
            "resources": {
                str(item.name): item for item in held if getattr(item, "name", None)
            },
            "site_resources": {
                str(site.label): (
                    by_uuid.get(site.occupied_material_uuid)
                    if site.occupied_material_uuid
                    else None
                )
                for site in sites
            },
        }
        return {name: values[name] for name in wanted}

    @abstractmethod
    def lab_logger(self) -> Any:
        """Return the logger associated with this device."""

    @abstractmethod
    async def sleep(self, rel_time: float, callback_group: Any = None) -> None:
        """Sleep without blocking the backend executor."""

    @abstractmethod
    def create_task(self, coroutine: Awaitable[Any]) -> Any:
        """Schedule an awaitable on the backend executor."""

    def create_wait_future(self) -> Any:
        """创建可在当前 backend 执行器上 await 的 Future。

        默认返回 asyncio Future（HostLink 节点跑在 asyncio loop 上）；
        ROS2 backend 覆写为 rclpy Future。
        """

        return asyncio.get_running_loop().create_future()

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
        from unilabos.backend.runtime.resource import MaterialSnapshotObserver

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

    def _invalidate_material_baselines(self) -> None:
        """权威侧结构已变（下行投影 / 挂载）：本地缓存的根树基线作废，下次 flush 重拉一次。"""

        service = self.__dict__.get("_device_resource_service")
        invalidate = getattr(service, "invalidate_baselines", None)
        if callable(invalidate):
            invalidate()

    @contextmanager
    def material_authority_sync(self) -> Iterator[None]:
        """权威 load/unload 投影本地 PLR 时禁止产生 snapshot 回声。"""

        self._invalidate_material_baselines()
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

    async def update_resource(self, *resources: Any) -> Any:
        """把带权威 uuid 的物料快照回权威。

        ``materials.update(node, *物料)`` 的 async 包装——参数归一、身份
        与网关选择全部收敛在 :func:`unilabos.resources.materials.update`。
        直接接收物料：单个 PLR 实例、多个实例（多参）、实例列表或
        ``ResourceTreeSet`` 均可；重复节点在服务内按 uuid 去重。
        """

        from unilabos.backend.runtime.async_utils import run_blocking
        from unilabos.resources import materials

        return await run_blocking(materials.update, self, *resources)

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

    async def get_resource_by_id(
        self,
        resource_id: str,
        with_children: bool = True,
    ) -> Any:
        """按 resource id（dir）向权威取物料树，返回 ResourceTreeSet。"""

        return await self._require_resource_service().get_resource_by_id(
            self.device_id,
            resource_id,
            with_children,
        )

    async def get_resource_with_dir(
        self,
        resource_id: str,
        with_children: bool = True,
    ) -> Any:
        """按 resource id 取物料并实例化，返回单个 PLR 资源对象。"""

        tree_set = await self.get_resource_by_id(resource_id, with_children)
        resources = tree_set.to_plr_resources()
        if not resources:
            raise ValueError(f"物料 {resource_id!r} 在权威中不存在")
        return resources[0]

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
        from unilabos.resources.objects.resource import EXTRA_SITES

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
        # canonical Site sidecar（权威实例化的 PLR 对象）：uuid/index 命中后
        # 取 label 再落 ordering 索引，不依赖 sidecar dict 的顺序
        extra = getattr(parent, "unilabos_extra", None)
        sidecar = extra.get(EXTRA_SITES) if isinstance(extra, dict) else None
        if isinstance(sidecar, dict):
            for label, dumped in sidecar.items():
                if not isinstance(dumped, dict):
                    continue
                values = {
                    str(dumped.get("uuid", "") or ""),
                    str(label),
                    str(dumped.get("index", "") or ""),
                }
                if normalized in values:
                    resolved = resolve_site_spot(parent, str(label))
                    if resolved is not None:
                        return resolved
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

    @classmethod
    def _carrier_placement(
        cls, parent: Any, selector: Any | None
    ) -> tuple[Any, int] | None:
        """物料要经 PLR 载架的 Site 放置时返回 ``(载架, spot)``，否则 None。

        PLR ``Carrier``（含 ``ItemizedCarrier`` / ``WareHouse``）的直接子节点只能是槽位
        holder，物料只能经 ``assign_resource_to_site`` 放到 holder 上（载架同时登记
        Site 占用）。两种目标都走这里：

        - ``parent`` 是载架本身：按 ``selector``（Site uuid / label / 索引）解析 spot，
          不指定 Site 时报错；
        - ``parent`` 是载架某个 Site 的 holder（权威把载架 Site 上的物料挂在 holder
          下）：spot 就是这个 holder 的位置，``selector`` 若给出必须指向同一个 Site。
        """

        from pylabrobot.resources import ResourceHolder
        from pylabrobot.resources.carrier import Carrier

        if isinstance(parent, Carrier):
            spot = cls._site_spot(parent, selector)
            if spot is None:
                raise ValueError(
                    f"载架 {parent.name!r} 只能按 Site 放置物料，请指定 site 或 slot"
                )
            return parent, spot
        carrier = getattr(parent, "parent", None)
        if not isinstance(parent, ResourceHolder) or not isinstance(carrier, Carrier):
            return None
        holder_spot = next(
            (spot for spot, holder in carrier.sites.items() if holder is parent), None
        )
        if holder_spot is None:
            return None
        if selector is not None and str(selector).strip() != "":
            try:
                selected = cls._site_spot(carrier, selector)
            except ValueError:
                selected = None
            if selected is not None and selected != holder_spot:
                raise ValueError(
                    f"Site {selector!r} 不是载架 {carrier.name!r} 上 holder "
                    f"{parent.name!r} 所在的 Site"
                )
        return carrier, holder_spot

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
            parent = None
            if parent_uuid and parent_uuid != self.resource_uuid:
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
                placement = self._carrier_placement(parent, site)
                spot = (
                    placement[1]
                    if placement is not None
                    else self._site_spot(parent, site)
                )
                assign_site = getattr(
                    placement[0] if placement is not None else parent,
                    "assign_resource_to_site",
                    None,
                )
                if callable(assign_site) and spot is not None:
                    # 载架 Site：经载架放到 holder 上（权威父节点可能是载架或其 holder）
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

    async def _report_loaded_poses(self, resources: list[Any]) -> None:
        """transfer load 之后把物料在新父节点下的位姿按增量报给权威。

        权威只提交了父子与 Site 占用；位姿（holder 的默认子位置、台面 Site 坐标）由本地
        assign 决定，而投影期间观察者被抑制。从未有过位姿的物料不补报，下次从权威反序列化
        时报“没有 location”。在 load 返回前完成，transfer 结束时权威树即可反序列化；
        补报失败只记日志，不撤销已经提交的位置。
        """

        service = self.__dict__.get("_device_resource_service")
        apply_deltas = getattr(service, "apply_node_deltas", None)
        if not callable(apply_deltas):
            return
        for resource in resources:
            if getattr(resource, "parent", None) is None:
                continue
            try:
                await apply_deltas(self.device_id, self.resource_uuid, resource, [resource])
            except Exception as exc:  # noqa: BLE001 - 位置已提交，位姿补报失败不影响转移结果
                self.lab_logger().warning(
                    f"[物料同步] 物料 {getattr(resource, 'name', resource)} load 后位姿补报失败：{exc}"
                )

    async def material_sync(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """微后端 transfer 的设备侧投影（unload/load），幂等（按 transfer_uuid+action）。

        与 append_resource / apply_resource_tree_update 同构：实例协程承接下行，
        本进程直调、跨机经 HostLink RPC，不依赖任何 per-device service。
        """
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
                        attached = await self._attach_local_materials(
                            material_uuids, sites
                        )
                    else:
                        raise ValueError(
                            f"未知 material_sync action：{action!r}"
                        )
                if action == "load":
                    await self._report_loaded_poses(attached)
                if sync_key:
                    completed.add(sync_key)
        return {
            "success": True,
            "action": action,
            "material_uuids": material_uuids,
            "transfer_uuid": transfer_uuid,
            "replayed": replayed,
        }

    async def device_manage(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Host 下发的设备管理（动态 add/remove 设备）。

        与物料下行同构：本进程直调、跨机经 HostLink RPC，不依赖 ROS service。
        command: {"action": "add"|"remove", "data": {...device config, 含 id...}}
        """
        action = str(command.get("action") or "").strip()
        data = command.get("data") or {}
        device_id = str(data.get("device_id") or data.get("id") or "")
        if not device_id:
            return {"success": False, "error": "device_id required"}
        try:
            if action == "add":
                return self.create_device(device_id, data)
            if action == "remove":
                return self.destroy_device(device_id)
            return {"success": False, "error": f"Unknown action: {action}"}
        except NotImplementedError as exc:
            self.lab_logger().warning(f"[DeviceManage] {exc}")
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            self.lab_logger().error(f"[DeviceManage] Error: {exc}")
            return {"success": False, "error": str(exc)}

    def create_device(self, device_id: str, config: Any) -> Dict[str, Any]:
        """动态创建设备；由 HostNode / WorkstationNode / HostLink 节点覆写。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support dynamic device creation"
        )

    def destroy_device(self, device_id: str) -> Dict[str, Any]:
        """动态移除设备；由 HostNode / WorkstationNode / HostLink 节点覆写。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support dynamic device removal"
        )

    # ------------------------------------------------------------------
    # 物料下行（Host → 设备）：append_resource / apply_resource_tree_update
    #
    # 两个入口都不走 ROS service：本进程由 Host 直调实例方法，跨机经
    # HostLink 下行 RPC；对权威的读写（get/update）经 ResourceService，
    # Slave 上自动通过 HostLink 代理，全链路不依赖 ROS 服务发现。
    # ------------------------------------------------------------------

    def _short_device_id(self) -> str:
        return str(self.device_id).split("/")[-1]

    def _append_resource_mutex(self) -> DeviceAsyncMutex:
        mutex = self.__dict__.get("_append_resource_lock")
        if mutex is None:
            mutex = DeviceAsyncMutex(name=f"AR:{self.device_id}")
            self.__dict__["_append_resource_lock"] = mutex
        return mutex

    async def _acquire_resource_tree_uuid_locks(
        self, uuids: List[str], tag: str = ""
    ) -> List[DeviceAsyncMutex]:
        """按资源 UUID 获取本节点实例内的资源树锁。"""

        lock_keys = sorted({str(uid) for uid in uuids if uid})
        guard = self.__dict__.setdefault(
            "_resource_tree_uuid_locks_guard", threading.Lock()
        )
        with guard:
            table = self.__dict__.setdefault("_resource_tree_uuid_locks", {})
            locks = [
                table.setdefault(
                    uid, DeviceAsyncMutex(name=f"RT:{self.device_id}:{uid}")
                )
                for uid in lock_keys
            ]
        acquired_locks: List[DeviceAsyncMutex] = []
        try:
            for lock in locks:
                await lock.acquire(self, tag=tag)
                acquired_locks.append(lock)
        except BaseException:
            self._release_resource_tree_uuid_locks(acquired_locks)
            raise
        return acquired_locks

    def _release_resource_tree_uuid_locks(self, locks: List[DeviceAsyncMutex]) -> None:
        for lock in reversed(locks):
            lock.release(self)

    def transfer_to_new_resource(
        self, plr_resource: Any, tree: Any, additional_add_params: Dict[str, Any]
    ) -> Optional[Any]:
        """把（已实例化的）物料挂载到 tree 指定的父物料下，返回父物料。

        parent 未知或就是节点自身时不做 assign，返回 None；挂载时自动探测
        父物料的 site/spot 能力。目标位经 _site_spot 统一解析：``site``
        （权威 Site uuid）与 ``slot``（label / 0-based 索引）均可。
        """

        parent_uuid = tree.root_node.res_content.parent_uuid
        if not parent_uuid:
            # 根级物料：图里没有父节点，直接归属当前设备节点，不需要 PLR assign
            self.lab_logger().info(
                f"物料{plr_resource}无父物料，作为{self.identifier}的根级物料，额外参数：{additional_add_params}"
            )
            return None
        if parent_uuid == self.resource_uuid:
            self.lab_logger().info(
                f"物料{plr_resource}挂载到{self.identifier}，额外参数：{additional_add_params}"
            )
            return None
        parent_resource = self.resource_tracker.uuid_to_resources.get(parent_uuid)
        if parent_resource is None:
            self.lab_logger().warning(
                f"物料{plr_resource}请求挂载{tree.root_node.res_content.name}的父节点{parent_uuid}不存在"
            )
            return None
        try:
            # 将 Uni-Lab-OS 的 site/slot 元数据转换为 PLR 挂载参数。
            additional_params: Dict[str, Any] = {}
            extra = getattr(plr_resource, "unilabos_extra", {})
            if len(extra):
                self.lab_logger().info(f"发现物料{plr_resource}额外参数: " + str(extra))
            if "update_resource_site" in extra:
                # 工作站拖拽上料的库位编号（如 "A01"）是 label 语义，走 slot
                additional_add_params["slot"] = extra["update_resource_site"]
            # site 只承载权威 Site uuid；slot 承载 label / 0-based 索引
            selector = additional_add_params.get("site") or additional_add_params.get("slot")
            # PLR 载架（仓库等）只能经 Site 放物料；目标也可能是载架 Site 的 holder
            placement = self._carrier_placement(parent_resource, selector)
            spec = inspect.signature(parent_resource.assign_child_resource)
            if placement is None and "spot" in spec.parameters:
                # _site_spot 解析 uuid、label 或索引；未指定时由父级选择默认位置，
                # 无法解析的显式选择器直接报错。
                additional_params["spot"] = self._site_spot(parent_resource, selector)
            old_parent = plr_resource.parent
            if old_parent is not None:
                # PLR 资源在重新挂载前必须先从当前父资源解除。
                self.lab_logger().info(f"物料{plr_resource}从{old_parent}卸载")
                old_parent.unassign_child_resource(plr_resource)
            self.lab_logger().info(
                f"物料{plr_resource}挂载到{parent_resource}，额外参数：{additional_params}"
            )

            # 挂载后资源属于 parent_resource；先从顶级列表移除可避免
            # figure_resource 同时从顶级列表和 children 命中同一实例。
            resource_id = id(plr_resource)
            for i, r in enumerate(self.resource_tracker.resources):
                if id(r) == resource_id:
                    self.resource_tracker.resources.pop(i)
                    self.lab_logger().debug(
                        f"从顶级资源列表中移除 {plr_resource.name}（即将成为 {parent_resource.name} 的子资源）"
                    )
                    break
            # 成为子资源后由父根树统一快照；若继续按独立根树观察，其子树的状态
            # 变化会以"半棵树"提交并被权威判为结构漂移。
            observer = getattr(self.resource_tracker, "_material_snapshot_observer", None)
            if observer is not None:
                observer.unobserve(plr_resource)

            if placement is not None:
                carrier, spot = placement
                self.lab_logger().info(f"物料{plr_resource}经载架{carrier}放到 Site[{spot}]")
                carrier.assign_resource_to_site(plr_resource, spot)
                # 上报从载架起：它的 Site 占用随同一棵树回报
                parent_resource = carrier
            else:
                parent_resource.assign_child_resource(plr_resource, location=None, **additional_params)

            func = getattr(self._resource_driver(), "resource_tree_transfer", None)
            if callable(func):
                # 驱动回调接收原父资源、已挂载实例和新父资源。
                func(old_parent, plr_resource, parent_resource)
            return parent_resource
        except Exception:
            self.lab_logger().warning(
                f"物料{plr_resource}请求挂载{tree.root_node.res_content.name}的父节点"
                f"{parent_resource}[{parent_uuid}]失败！\n{traceback.format_exc()}"
            )
            return None

    async def _attach_root_to_device(self, material_uuid: str) -> None:
        """把一棵根树的权威 parent 设为本设备物料（设备挂载不占 Site，只落父子关系）。

        已经挂在本设备下则无事发生；挂在别的物料 / 设备下的不在这里改父（那是 move /
        transfer 的事）。设备在权威里没有物料行（旧图 / 未登记）时静默跳过。
        """

        try:
            tree_set = await self.get_resource(resources_uuid=[material_uuid], with_children=False)
        except Exception:  # noqa: BLE001 - 读不到就不改父，挂载本身已完成
            return
        if not tree_set.trees:
            return
        current_parent = tree_set.trees[0].root_node.res_content.uuid_parent
        if current_parent:
            return
        try:
            await self._require_resource_service().move_resource(
                self.device_id,
                self.resource_uuid,
                material_uuid,
                parent_material_uuid=self.resource_uuid,
            )
        except Exception as exc:  # noqa: BLE001 - 设备不是权威物料时（未登记）保持原样
            self.lab_logger().debug(f"[AR:{material_uuid}] 设备挂载未落 parent（{exc}），按根树保留")

    @staticmethod
    def _occupied_site_uuid(
        tree_set: Any, owner_uuid: str, occupant_uuid: str
    ) -> Optional[str]:
        """在快照树中找被 occupant 占用的 Site uuid（无则 None）。

        先找 owner 物料自己的 Site；owner 是载架 Site 的 holder 时，Site 在它的父载架上，
        退回到整棵快照树里找。
        """

        fallback: Optional[str] = None
        for tree in tree_set.trees:
            for node in tree.get_all_nodes():
                for site in node.res_content.sites or []:
                    if site.occupied_material_uuid != occupant_uuid:
                        continue
                    if node.res_content.uuid == owner_uuid:
                        return site.uuid
                    fallback = fallback or site.uuid
        return fallback

    async def append_resource(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """把微后端权威已创建的物料（带 uuid）assign 到本设备的目标父物料下。

        物料创建只发生在微后端，设备侧不做本地 initialize、不发 "add" 上报。四步：

        1. materials：按 uuid 从权威拉取物料实例化；
        2. assign：复用 transfer_to_new_resource（自动探测父物料 site/spot 能力）；
        3. move：物料挂到物料下（设备挂载除外）经权威 move 落父子关系与 Site 占用；
        4. 同步：挂载后的父树经 update_resource 直连权威快照（严禁 add，防创建上报冲突）。

        入口不走 ROS service：本进程由 Host 直调，跨机经 HostLink 下行 RPC；
        失败直接抛异常，由调用侧承接。

        payload: {"resource_uuid": [...], "bind_parent_id": str,
                  "bind_location": {x,y,z}, "other_calling_param": {site|slot, ...}}

        目标位参数（二选一）：``site`` 只承载权威 ResourceSite 的 uuid（机器路径，
        与 MATERIAL_MOVE 的 destination_site_uuid 同一标识，免反查直传权威）；
        ``slot`` 是字符串——优先按 Site label 匹配（如 "A1"），非 label 且为
        纯数字（isdigit）时按 0-based 索引找对应 Site。
        """

        from pylabrobot.resources import Coordinate

        from unilabos.resources.objects.resource import EXTRA_BOUND_DEVICE
        from unilabos.resources.resource_tracker import ResourceTreeSet

        resource_uuids: List[str] = list(payload.get("resource_uuid") or [])
        if not resource_uuids:
            raise ValueError("append_resource 需要至少一个 resource_uuid（物料创建发生在微后端，必须带 uuid）")
        bind_parent_id: str = payload["bind_parent_id"]
        additional_params: Dict[str, Any] = dict(payload.get("other_calling_param") or {})
        site_uuid = str(additional_params.get("site") or "").strip() or None
        loc = payload.get("bind_location") or {}
        bind_location = Coordinate(loc.get("x", 0.0), loc.get("y", 0.0), loc.get("z", 0.0))
        tag = ",".join(map(str, resource_uuids))
        await self._append_resource_mutex().acquire(self, tag=tag)
        resource_locks: List[DeviceAsyncMutex] = []
        try:
            # 与资源树同步共享 uuid 锁：防止挂载与创建分发(notify add)并发处理同一物料
            resource_locks = await self._acquire_resource_tree_uuid_locks(resource_uuids, tag=f"append:{tag}")
            # 1. materials：按 uuid 从权威拉取（uuid 不存在时直接失败）
            tree_set = await self.get_resource(resources_uuid=resource_uuids, with_children=True)
            if len(tree_set.trees) != len(resource_uuids):
                raise ValueError(
                    f"权威返回的物料数量与请求不一致: requested={len(resource_uuids)} actual={len(tree_set.trees)}"
                )
            # 2. assign：投影父 uuid 后走统一挂载（site/spot 探测在 transfer_to_new_resource 内）；
            #    bind_parent_id 为节点自身时仅登记 tracker，不做 assign
            parent_uuid = (
                self.resource_uuid
                if bind_parent_id in (self.device_id, self._short_device_id())
                else self.resource_tracker.figure_resource({"name": bind_parent_id}).unilabos_uuid
            )
            drafts = tree_set.to_plr_resources()
            plr_instances: List[Any] = []
            added_instances: List[Any] = []
            report_roots: List[Any] = []
            # 本流程自身就是权威的投影方，且第 4 步会显式回报快照：物理挂载触发的
            # PLR assign/state 回调不得再排队一份自动快照，否则同一事件双写，
            # 必然造成一次乐观锁冲突或"结构漂移"丢弃。
            with self.material_authority_sync():
                for tree, draft in zip(tree_set.trees, drafts):
                    tree.root_node.res_content.parent_uuid = parent_uuid
                    root_uuid = tree.root_node.res_content.uuid
                    plr_instance = self.resource_tracker.uuid_to_resources.get(root_uuid)
                    if plr_instance is None:
                        plr_instance = draft
                        self.resource_tracker.add_resource(plr_instance)
                        added_instances.append(plr_instance)
                    else:
                        # 已在本设备台面上（换位/重复挂载）：物理上是同一物件，复用
                        # 现有实例，transfer 内先从旧父位卸载再挂新位。重新实例化会
                        # 造成同 uuid 双实例且旧 spot 不释放。
                        self.lab_logger().info(f"[AR:{tag}] 物料 {root_uuid} 已在台面，按换位处理")
                    if parent_uuid == self.resource_uuid:
                        # 根树上台面：extra 登记所属设备，随第 4 步快照落权威，供
                        # materials.owner_device_of 推断（挂到已有树时归属跟随其根，无需写）
                        extra = getattr(plr_instance, "unilabos_extra", None) or {}
                        extra[EXTRA_BOUND_DEVICE] = self._short_device_id()
                        plr_instance.unilabos_extra = extra
                    plr_instances.append(plr_instance)
                    parent = self.transfer_to_new_resource(plr_instance, tree, dict(additional_params))
                    if parent is None and parent_uuid != self.resource_uuid:
                        raise ValueError(f"物料 {plr_instance.name} 挂载到 {bind_parent_id} 失败（详见设备日志）")
                    if plr_instance.location is None:
                        # 未经 site/spot 排布时回退显式挂载坐标（默认原点）
                        plr_instance.location = bind_location
                    root = parent if parent is not None else plr_instance
                    if all(id(root) != id(existing) for existing in report_roots):
                        report_roots.append(root)
            if added_instances:
                # 换位不重复触发 add 回调；transfer 已在挂载时回调 resource_tree_transfer
                await self._invoke_resource_hook("resource_tree_add", added_instances)
            report_tree_set = ResourceTreeSet.from_plr_resources(report_roots)
            # 3. 挂载的权威事实是 materials.db 的父子关系 + Site 占用；快照协议只更新
            #    既有聚合、无法表达跨树合并，因此先经 move 落库，随后的快照才能按合并后
            #    的新树分组对齐。挂到设备自身（台面 Deck、驱动运行期 ensure 的根树）同样
            #    要落 parent=设备物料：否则它在权威里仍是独立根，前端设备卡片下看不到它
            #    的 sites，materials.owner_device_of 也只能靠 extra 推断。
            if parent_uuid != self.resource_uuid:
                for material_uuid in resource_uuids:
                    # site 参数即权威 Site uuid，直传；slot/未指定则从挂载快照反查
                    await self._require_resource_service().move_resource(
                        self.device_id,
                        self.resource_uuid,
                        material_uuid,
                        parent_material_uuid=parent_uuid,
                        destination_site_uuid=site_uuid
                        or self._occupied_site_uuid(
                            report_tree_set, parent_uuid, material_uuid
                        ),
                    )
            elif self.resource_uuid:
                for material_uuid in resource_uuids:
                    await self._attach_root_to_device(material_uuid)
            # 4. 同步：挂载后的父树经 update_resource 直连权威（权威已有 create 记录，严禁 add）
            for tree in report_tree_set.trees:
                if tree.root_node.res_content.uuid_parent is None:
                    tree.root_node.res_content.parent_uuid = self.resource_uuid
            await self.update_resource(report_tree_set)
            self.lab_logger().info(f"[AR:{tag}] 挂载完成并回报快照: {len(resource_uuids)} 个物料 -> {bind_parent_id}")
            # 响应：完整分组形态（经 handle @flatten 给用户扁平）
            return {
                "created_resource_tree": ResourceTreeSet.from_plr_resources(plr_instances).dump(),
                "substance_resource_tree": [],
            }
        except Exception as ex:
            self.lab_logger().error(f"[AR:{tag}] 挂载权威物料(append_resource)出错: {ex}\n{traceback.format_exc()}")
            raise
        finally:
            self._release_resource_tree_uuid_locks(resource_locks)
            self._append_resource_mutex().release(self)

    async def apply_resource_tree_update(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """处理 Host 分发的资源树变更（本进程直调 / 跨机走 HostLink）。

        operations: [{"action": "add"|"update"|"remove", "data": [uuid...],
                      "additional_add_params": {...}}]

        支持三种操作：

        - add: 添加新资源到资源树
        - update: 更新现有资源
        - remove: 从资源树中移除资源

        add/update 处理后的父树经 update_resource 直连权威快照回报。
        """

        from pylabrobot.resources.resource import Resource as ResourcePLR

        from unilabos.resources.resource_tracker import ResourceDictInstance, ResourceTreeSet

        async def _handle_add(
            plr_resources: List[ResourcePLR], tree_set: ResourceTreeSet, additional_add_params: Dict[str, Any]
        ) -> tuple[Dict[str, Any], List[ResourcePLR]]:
            """处理资源添加操作，返回（结果，被变更的物料或其父级列表）。"""
            parents = []  # 放的是被变更的物料 / 被变更的物料父级
            for plr_resource, tree in zip(plr_resources, tree_set.trees):
                self.resource_tracker.add_resource(plr_resource)
                parent = self.transfer_to_new_resource(plr_resource, tree, additional_add_params)
                if parent is not None:
                    parents.append(parent)
                else:
                    parents.append(plr_resource)

            await self._invoke_resource_hook("resource_tree_add", plr_resources)

            return {"success": True, "action": "add"}, parents

        async def _handle_remove(resources_uuid: List[str]) -> Dict[str, Any]:
            """处理资源移除操作。"""
            found_resources = self.resource_tracker.figure_resource(
                [{"uuid": uid} for uid in resources_uuid], try_mode=True
            )
            found_plr_resources = []
            other_plr_resources = []

            for found_resource in found_resources:
                for resource in found_resource:
                    if issubclass(resource.__class__, ResourcePLR):
                        found_plr_resources.append(resource)
                    else:
                        other_plr_resources.append(resource)

            # 调用driver的remove回调
            await self._invoke_resource_hook("resource_tree_remove", found_plr_resources)

            # 从parent卸载并从tracker移除
            for plr_resource in found_plr_resources:
                if plr_resource.parent is not None:
                    plr_resource.parent.unassign_child_resource(plr_resource)
                self.resource_tracker.remove_resource(plr_resource)
                self.lab_logger().info(f"[资源同步] 移除物料 {plr_resource} 及其子节点")

            for other_plr_resource in other_plr_resources:
                self.resource_tracker.remove_resource(other_plr_resource)
                self.lab_logger().info(f"[资源同步] 移除物料 {other_plr_resource} 及其子节点")

            return {
                "success": True,
                "action": "remove",
            }

        async def _handle_update(
            plr_resources: List[Any],
            tree_set: ResourceTreeSet,
            additional_add_params: Dict[str, Any],
        ) -> tuple[Dict[str, Any], List[ResourcePLR]]:
            """处理资源更新操作（含改名、换父、Site 重登记与状态加载）。"""
            original_instances = []
            for plr_resource, tree in zip(plr_resources, tree_set.trees):
                if isinstance(plr_resource, ResourceDictInstance):
                    self.lab_logger().info(f"跳过 非资源{plr_resource.res_content.name} 的更新")
                    continue
                states = plr_resource.serialize_all_state()
                original_instance: ResourcePLR = self.resource_tracker.figure_resource(
                    {"uuid": tree.root_node.res_content.uuid}, try_mode=False
                )
                original_parent_resource = original_instance.parent
                original_parent_resource_uuid = getattr(original_parent_resource, "unilabos_uuid", None)
                target_parent_resource_uuid = tree.root_node.res_content.uuid_parent
                if target_parent_resource_uuid == self.resource_uuid:
                    not_same_parent = False
                    original_parent_resource = None
                    original_parent_resource_uuid = self.resource_uuid
                else:
                    not_same_parent = (
                        original_parent_resource_uuid != target_parent_resource_uuid
                        and original_parent_resource is not None
                    )
                old_name = original_instance.name
                new_name = plr_resource.name
                parent_appended = False

                # Update操作中包含改名：需要先remove再add，这里更新父节点即可
                if not not_same_parent and old_name != new_name:
                    self.lab_logger().info(f"物料改名操作：{old_name} -> {new_name}")

                    # 收集所有相关的uuid（包括子节点）
                    await _handle_remove([original_instance.unilabos_uuid])
                    original_instance.name = new_name
                    await _handle_add([original_instance], tree_set, additional_add_params)

                    self.lab_logger().info(f"物料改名完成：{old_name} -> {new_name}")
                    original_instances.append(original_parent_resource)
                    parent_appended = True

                # 常规更新：不涉及改名
                self.lab_logger().info(
                    f"物料{original_instance} 原始父节点{original_parent_resource_uuid} "
                    f"目标父节点{target_parent_resource_uuid} 更新"
                )

                # 更新extra
                if getattr(plr_resource, "unilabos_extra", None) is not None:
                    original_instance.unilabos_extra = getattr(plr_resource, "unilabos_extra")

                # 如果父节点变化，需要重新挂载
                if not_same_parent:
                    parent = self.transfer_to_new_resource(original_instance, tree, additional_add_params)
                    original_instances.append(parent)
                    parent_appended = True
                else:
                    # 判断是否变更了resource_site，重新登记
                    target_site = original_instance.unilabos_extra.get("update_resource_site")
                    sites = (
                        original_parent_resource.sites
                        if original_parent_resource is not None and hasattr(original_parent_resource, "sites")
                        else None
                    )
                    site_names = (
                        list(original_parent_resource._ordering.keys())
                        if original_parent_resource is not None and hasattr(original_parent_resource, "sites")
                        else []
                    )
                    if target_site is not None and sites is not None and site_names is not None:
                        site_index = None
                        try:
                            # sites 可能是 ItemizedCarrier 的 Resource 列表，或 PRCXI 的 ResourceSite 列表。
                            # Resource 列表通过对象身份直接定位。
                            site_index = sites.index(original_instance)
                        except ValueError:
                            # canonical Site 只按占用物料 UUID 匹配。
                            for idx, site in enumerate(sites):
                                if isinstance(site, dict):
                                    occupied_uuid = site.get("occupied_material_uuid")
                                else:
                                    occupied_uuid = getattr(site, "occupied_material_uuid", None)
                                if occupied_uuid and occupied_uuid == getattr(
                                    original_instance, "unilabos_uuid", None
                                ):
                                    site_index = idx
                                    break
                        if site_index is None:
                            site_name = None
                        else:
                            site_name = site_names[site_index]
                        if site_name != target_site:
                            parent = self.transfer_to_new_resource(original_instance, tree, additional_add_params)
                            if parent is not None:
                                original_instances.append(parent)
                                parent_appended = True

                # 加载状态
                # noinspection PyProtectedMember
                original_instance._size_x = plr_resource._size_x
                # noinspection PyProtectedMember
                original_instance._size_y = plr_resource._size_y
                # noinspection PyProtectedMember
                original_instance._size_z = plr_resource._size_z
                # noinspection PyProtectedMember
                original_instance._local_size_z = plr_resource._local_size_z
                original_instance.location = plr_resource.location
                original_instance.rotation = plr_resource.rotation
                original_instance.barcode = plr_resource.barcode
                original_instance.load_all_state(states)
                child_count = len(original_instance.get_all_children())
                self.lab_logger().info(
                    f"更新了资源属性 {plr_resource}[{tree.root_node.res_content.uuid}] " f"及其子节点 {child_count} 个"
                )
                if not parent_appended:
                    original_instances.append(original_instance)

            # 调用driver的update回调
            await self._invoke_resource_hook("resource_tree_update", original_instances)

            return {"success": True, "action": "update"}, original_instances

        def _dedupe_roots(resources: List[Any]) -> ResourceTreeSet:
            """按对象身份去重并补全根 parent 后生成上报树集。"""
            from unilabos.config.config import resolve_host_node_name

            de_dupe = []
            seen_ids = set()
            for item in resources:
                if item is None or id(item) in seen_ids:
                    continue
                seen_ids.add(id(item))
                de_dupe.append(item)
            new_tree_set = ResourceTreeSet.from_plr_resources(de_dupe)
            # host 服务设备实例可重命名（--host_node_id），按解析后的实例名判断。
            is_host_device = self._short_device_id() == resolve_host_node_name()
            for tree in new_tree_set.trees:
                if tree.root_node.res_content.uuid_parent is None and not is_host_device:
                    tree.root_node.res_content.parent_uuid = self.resource_uuid
            return new_tree_set

        from unilabos.config.config import BasicConfig

        results = []
        for i in operations:
            action = i.get("action")  # remove, add, update
            resources_uuid: List[str] = i.get("data") or []  # 资源数据
            if not isinstance(resources_uuid, list):
                resources_uuid = [resources_uuid]
            additional_add_params = i.get("additional_add_params", {})  # 额外参数
            self.lab_logger().debug(f"[资源同步] 处理 {action}, " f"resources count: {len(resources_uuid)}")
            # 下行说明权威已经改了结构：本地基线作废
            self._invalidate_material_baselines()
            # 锁范围由请求中的 UUID 决定；调用方必须同时传入会被修改的关联
            # 父节点或子节点 UUID。
            resource_locks = await self._acquire_resource_tree_uuid_locks(
                resources_uuid, tag=f"{action}:{','.join(map(str, resources_uuid))}"
            )
            try:
                tree_set = None
                if action in ["add", "update"]:
                    tree_set = await self.get_resource(
                        resources_uuid=resources_uuid, with_children=True if action == "add" else False
                    )
                try:
                    if action == "add":
                        if tree_set is None:
                            raise ValueError("tree_set不能为None")
                        plr_resources = tree_set.to_plr_resources()
                        result, parents = await _handle_add(plr_resources, tree_set, additional_add_params)
                        # 挂载后的父树经 update_resource 直连权威快照回报
                        authoritative = await self.update_resource(_dedupe_roots(parents))
                        self.lab_logger().debug(f"确认资源权威 Add 结果: {len(authoritative.trees)} trees")
                        results.append(result)
                    elif action == "update":
                        if tree_set is None:
                            raise ValueError("tree_set不能为None")
                        plr_resources = []
                        for tree in tree_set.trees:
                            if tree.root_node.res_content.type == "device":
                                plr_resources.append(tree.root_node)
                            else:
                                plr_resources.append(ResourceTreeSet([tree]).to_plr_resources()[0])
                        result, original_instances = await _handle_update(
                            plr_resources, tree_set, additional_add_params
                        )
                        if not BasicConfig.no_update_feedback:
                            authoritative = await self.update_resource(_dedupe_roots(original_instances))
                            self.lab_logger().debug(f"确认资源权威 Update 结果: {len(authoritative.trees)} trees")
                        results.append(result)
                    elif action == "remove":
                        result = await _handle_remove(resources_uuid)
                        results.append(result)
                except Exception as e:
                    error_msg = f"Error processing {action} operation: {str(e)}"
                    self.lab_logger().error(f"[Resource Tree Update] {error_msg}")
                    self.lab_logger().error(traceback.format_exc())
                    results.append({"success": False, "action": action, "error": error_msg})
            finally:
                self._release_resource_tree_uuid_locks(resource_locks)

        # 返回处理结果（本进程直调拿 dict；跨机由 HostLink 序列化响应）
        return {"results": results, "total": len(operations)}

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
