"""设备运行时到微后端 Materials Authority 的唯一资源服务边界。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Protocol, Sequence
from uuid import uuid4

from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.adapters.plr_materials import (
    CreatedPLRMaterials,
    MaterialGateway,
    create_plr_materials,
    material_tree_to_resource_tree,
    resource_tree_to_create,
    resource_tree_to_snapshot,
)
from unilabos.server.protocol.common import (
    AggregatePrecondition,
    InventoryMutation,
)
from unilabos.server.protocol.materials import MaterialDelete


logger = logging.getLogger(__name__)


@dataclass
class MaterialSyncRequest:
    command: str = ""


@dataclass
class MaterialSyncResponse:
    response: str = ""


class MaterialSyncService:
    """HostLink 使用的无 ROS 本地物料同步消息类型。"""

    Request = MaterialSyncRequest
    Response = MaterialSyncResponse


class ResourceService(Protocol):
    """所有 backend 向设备节点提供的权威物料操作。"""

    async def create_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> CreatedPLRMaterials: ...

    async def update_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> ResourceTreeSet: ...

    async def snapshot_resource_tree(
        self,
        device_id: str,
        device_uuid: str,
        root_resource: Any,
    ) -> ResourceTreeSet:
        """提交一棵 UUID 集合完整的运行时物料树快照。"""
        ...

    async def get_resources(
        self,
        device_id: str,
        resources_uuid: list[str],
        with_children: bool,
    ) -> ResourceTreeSet: ...

    def get_resources_sync(
        self,
        resources_uuid: Sequence[str],
        with_children: bool = True,
    ) -> ResourceTreeSet: ...

    async def get_resource_by_id(
        self,
        device_id: str,
        resource_id: str,
        with_children: bool,
    ) -> ResourceTreeSet: ...

    def get_resource_by_id_sync(
        self,
        resource_id: str,
        with_children: bool = True,
    ) -> ResourceTreeSet: ...

    async def delete_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources_uuid: list[str],
    ) -> list[str]: ...

    def delete_resources_sync(
        self,
        device_id: str,
        device_uuid: str,
        resources_uuid: Sequence[str],
    ) -> list[str]: ...


GatewayProvider = Callable[[], MaterialGateway]


@dataclass
class _ObservedMaterialRoot:
    root: Any
    did_assign: Callable[[Any], None]
    did_unassign: Callable[[Any], None]
    state_callbacks: dict[int, tuple[Any, Callable[[dict[str, Any]], None]]]
    dirty: bool = False
    scheduled: bool = False


class MaterialSnapshotObserver:
    """把任意 PLR 后代变更合并成完整根物料树 snapshot。

    PLR 只会向父节点传播 assign/unassign callback，state callback 不传播。
    因此这里递归监听每个后代的 state，但以根对象为唯一排队键。一次事件循环
    内连续修改多个孔位只提交一次；提交期间再次发生变化则紧接着再提交一轮。
    """

    def __init__(
        self,
        service: ResourceService,
        *,
        device_id: Callable[[], str],
        device_uuid: Callable[[], str],
        schedule: Callable[[Awaitable[Any]], Any],
    ) -> None:
        self._service = service
        self._device_id = device_id
        self._device_uuid = device_uuid
        self._schedule = schedule
        self._roots: dict[int, _ObservedMaterialRoot] = {}
        self._guard = threading.RLock()
        self._suppression_depth: ContextVar[int] = ContextVar(
            f"material_snapshot_suppression_{id(self)}",
            default=0,
        )
        self._errors: list[BaseException] = []

    def set_service(self, service: ResourceService) -> None:
        """运行时链路重绑时复用 tracker 上的同一个 observer。"""

        self._service = service

    @property
    def errors(self) -> tuple[BaseException, ...]:
        with self._guard:
            return tuple(self._errors)

    @contextmanager
    def suppress_authority_projection(self) -> Iterator[None]:
        """权威快照投影回 PLR 时禁止产生反向 snapshot。"""

        token = self._suppression_depth.set(
            self._suppression_depth.get() + 1
        )
        try:
            yield
        finally:
            self._suppression_depth.reset(token)

    @staticmethod
    def _can_observe(resource: Any) -> bool:
        return bool(
            str(getattr(resource, "unilabos_uuid", "") or "").strip()
            and callable(
                getattr(resource, "register_state_update_callback", None)
            )
            and callable(
                getattr(resource, "register_did_assign_resource_callback", None)
            )
            and callable(
                getattr(resource, "register_did_unassign_resource_callback", None)
            )
        )

    @staticmethod
    def _walk(resource: Any) -> list[Any]:
        result = [resource]
        for child in list(getattr(resource, "children", None) or []):
            result.extend(MaterialSnapshotObserver._walk(child))
        return result

    def observe(self, root: Any) -> bool:
        """监听一棵已经由微后端分配 UUID 的 PLR 根树。"""

        if not self._can_observe(root):
            return False
        root_key = id(root)
        with self._guard:
            if root_key in self._roots:
                return False

            def did_assign(resource: Any, *, _root_key: int = root_key) -> None:
                self._observe_state_subtree(_root_key, resource)
                self._queue(_root_key)

            def did_unassign(resource: Any, *, _root_key: int = root_key) -> None:
                self._drop_state_subtree(_root_key, resource)
                self._queue(_root_key)

            observed = _ObservedMaterialRoot(
                root=root,
                did_assign=did_assign,
                did_unassign=did_unassign,
                state_callbacks={},
            )
            self._roots[root_key] = observed

        root.register_did_assign_resource_callback(did_assign)
        root.register_did_unassign_resource_callback(did_unassign)
        self._observe_state_subtree(root_key, root)
        return True

    def observe_all(self, roots: Sequence[Any]) -> None:
        for root in roots:
            self.observe(root)

    def _observe_state_subtree(self, root_key: int, resource: Any) -> None:
        for node in self._walk(resource):
            register = getattr(node, "register_state_update_callback", None)
            if not callable(register):
                continue
            node_key = id(node)
            with self._guard:
                observed = self._roots.get(root_key)
                if observed is None or node_key in observed.state_callbacks:
                    continue

                def state_updated(
                    _state: dict[str, Any],
                    *,
                    _root_key: int = root_key,
                ) -> None:
                    self._queue(_root_key)

                observed.state_callbacks[node_key] = (node, state_updated)
            register(state_updated)

    def _drop_state_subtree(self, root_key: int, resource: Any) -> None:
        for node in self._walk(resource):
            with self._guard:
                observed = self._roots.get(root_key)
                entry = (
                    observed.state_callbacks.pop(id(node), None)
                    if observed is not None
                    else None
                )
            if entry is None:
                continue
            deregister = getattr(
                entry[0], "deregister_state_update_callback", None
            )
            if callable(deregister):
                try:
                    deregister(entry[1])
                except ValueError:
                    pass

    def unobserve(self, root: Any) -> bool:
        root_key = id(root)
        with self._guard:
            observed = self._roots.pop(root_key, None)
        if observed is None:
            return False
        for method_name, callback in (
            ("deregister_did_assign_resource_callback", observed.did_assign),
            (
                "deregister_did_unassign_resource_callback",
                observed.did_unassign,
            ),
        ):
            deregister = getattr(root, method_name, None)
            if callable(deregister):
                try:
                    deregister(callback)
                except ValueError:
                    pass
        for node, callback in list(observed.state_callbacks.values()):
            deregister = getattr(node, "deregister_state_update_callback", None)
            if callable(deregister):
                try:
                    deregister(callback)
                except ValueError:
                    pass
        return True

    def _queue(self, root_key: int) -> None:
        if self._suppression_depth.get() > 0:
            return
        with self._guard:
            observed = self._roots.get(root_key)
            if observed is None:
                return
            observed.dirty = True
            if observed.scheduled:
                return
            observed.scheduled = True
        coroutine = self._flush(root_key)
        try:
            self._schedule(coroutine)
        except Exception as exc:
            coroutine.close()
            with self._guard:
                current = self._roots.get(root_key)
                if current is not None:
                    current.scheduled = False
                self._errors.append(exc)
            logger.exception("物料 snapshot 无法进入 backend 执行队列")

    async def _flush(self, root_key: int) -> None:
        # 合并同一个同步 tick 内多个 child 的变化。
        await asyncio.sleep(0)
        while True:
            with self._guard:
                observed = self._roots.get(root_key)
                if observed is None:
                    return
                observed.dirty = False
                root = observed.root
            try:
                # 先在设备执行线程冻结整棵 PLR 树，避免后台 I/O 时继续读取
                # 一半旧、一半新的 child state。
                runtime_tree = ResourceTreeSet.from_plr_resources([root])
                snapshot_method = getattr(
                    self._service, "snapshot_resource_tree", None
                )
                if callable(snapshot_method):
                    await snapshot_method(
                        self._device_id(),
                        self._device_uuid(),
                        runtime_tree,
                    )
                else:
                    # 仅供旧测试替身使用；生产 ResourceService 必须提供严格入口。
                    await self._service.update_resources(
                        self._device_id(),
                        self._device_uuid(),
                        runtime_tree,
                    )
            except asyncio.CancelledError:
                with self._guard:
                    current = self._roots.get(root_key)
                    if current is not None:
                        current.scheduled = False
                raise
            except Exception as exc:
                logger.exception("提交完整物料根树 snapshot 失败")
                with self._guard:
                    self._errors.append(exc)
                    current = self._roots.get(root_key)
                    if current is not None:
                        current.scheduled = False
                return
            with self._guard:
                current = self._roots.get(root_key)
                if current is None:
                    return
                if current.dirty:
                    continue
                current.scheduled = False
                return

    async def wait_idle(self) -> None:
        """等待当前已排队的 snapshot 完成，主要供停机排空和测试使用。"""

        while True:
            with self._guard:
                pending = any(item.scheduled for item in self._roots.values())
            if not pending:
                return
            await asyncio.sleep(0)


def _runtime_gateway() -> MaterialGateway:
    # 延迟导入，避免 device_runtime 与启动配置形成模块循环。
    from unilabos.resources.materials import resolve_materials_gateway

    return resolve_materials_gateway()


def _normalize_plr_resources(resources: Any) -> list[Any]:
    normalized = (
        list(resources)
        if isinstance(resources, (list, tuple))
        else [resources]
    )
    if not normalized or normalized == [None]:
        raise ValueError("物料操作至少需要一个 PLR resource")
    return normalized


def _existing_tree_set(resources: Any) -> ResourceTreeSet:
    if isinstance(resources, ResourceTreeSet):
        tree_set = ResourceTreeSet.load(resources.dump())
    else:
        tree_set = ResourceTreeSet.from_plr_resources(
            _normalize_plr_resources(resources)
        )
    if not tree_set.all_nodes:
        raise ValueError("更新物料时至少需要一个已登记资源")
    return tree_set


class AuthorityResourceService:
    """通过嵌入式、HTTP 或 HostLink client 访问同一个微后端权威。"""

    def __init__(
        self,
        gateway: MaterialGateway | None = None,
        *,
        gateway_provider: GatewayProvider | None = None,
    ) -> None:
        if gateway is not None and gateway_provider is not None:
            raise ValueError("gateway 与 gateway_provider 不能同时提供")
        self._configured_gateway = gateway
        self._gateway_provider = gateway_provider or _runtime_gateway

    def _gateway(self) -> MaterialGateway:
        gateway = self._configured_gateway
        if gateway is None:
            gateway = self._gateway_provider()
        if gateway is None:
            raise RuntimeError("微后端 Materials Authority 尚未配置")
        return gateway

    @staticmethod
    def _mutation(
        operation: str,
        *,
        device_id: str,
        device_uuid: str,
        root_material_uuid: str | None = None,
        preconditions: list[AggregatePrecondition] | None = None,
    ) -> InventoryMutation:
        command_uuid = str(uuid4())
        target = root_material_uuid or "new-tree"
        return InventoryMutation(
            command_uuid=command_uuid,
            effect_key=f"{operation}:{target}:{command_uuid}",
            operation=operation,
            actor_type="device",
            actor_uuid=str(device_uuid or device_id),
            observed_at_ms=int(time.time() * 1000),
            preconditions=preconditions or [],
        )

    def _create_sync(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> CreatedPLRMaterials:
        mutation = self._mutation(
            "create_material_tree",
            device_id=device_id,
            device_uuid=device_uuid,
        )
        if isinstance(resources, ResourceTreeSet):
            draft = ResourceTreeSet.load(resources.dump())
            request = resource_tree_to_create(draft)
            result = self._gateway().create_tree(mutation, request)
            tree = material_tree_to_resource_tree(result.data)
            return CreatedPLRMaterials(
                result=result,
                tree=tree,
                resources=tree.to_plr_resources(),
            )
        normalized = _normalize_plr_resources(resources)
        return create_plr_materials(self._gateway(), mutation, normalized)

    async def create_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> CreatedPLRMaterials:
        return await asyncio.to_thread(
            self._create_sync,
            device_id,
            device_uuid,
            resources,
        )

    @staticmethod
    def _root_material_uuid(
        gateway: MaterialGateway,
        material_uuid: str,
        aggregate_cache: dict[str, Any],
        root_cache: dict[str, str],
    ) -> str:
        cached = root_cache.get(material_uuid)
        if cached is not None:
            return cached
        path: list[str] = []
        seen: set[str] = set()
        current_uuid = material_uuid
        while True:
            if current_uuid in seen:
                raise ValueError("微后端返回了循环物料父子关系")
            seen.add(current_uuid)
            path.append(current_uuid)
            aggregate = aggregate_cache.get(current_uuid)
            if aggregate is None:
                aggregate = gateway.get_material(current_uuid)
                aggregate_cache[current_uuid] = aggregate
            parent_uuid = aggregate.material.parent_material_uuid
            if parent_uuid is None:
                root_uuid = current_uuid
                break
            current_uuid = parent_uuid
        for item in path:
            root_cache[item] = root_uuid
        return root_uuid

    @staticmethod
    def _snapshot_preconditions(base: Any) -> list[AggregatePrecondition]:
        conditions = [
            AggregatePrecondition(
                aggregate_type="material",
                aggregate_uuid=node.material.material_uuid,
                expected_version=node.material.version,
                expected_state_hash=node.state_hash,
            )
            for node in base.nodes
        ]
        conditions.extend(
            AggregatePrecondition(
                aggregate_type="site",
                aggregate_uuid=site.site_uuid,
                expected_version=site.version,
            )
            for node in base.nodes
            for site in node.sites
        )
        return conditions

    def _update_sync(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
        *,
        allow_partial: bool = True,
    ) -> ResourceTreeSet:
        runtime = _existing_tree_set(resources)
        gateway = self._gateway()
        aggregate_cache: dict[str, Any] = {}
        root_cache: dict[str, str] = {}
        by_root: dict[str, list[Any]] = defaultdict(list)
        seen_runtime_uuids: set[str] = set()
        for instance in runtime.all_nodes:
            material_uuid = instance.res_content.uuid
            if material_uuid in seen_runtime_uuids:
                continue
            seen_runtime_uuids.add(material_uuid)
            root_uuid = self._root_material_uuid(
                gateway,
                material_uuid,
                aggregate_cache,
                root_cache,
            )
            by_root[root_uuid].append(instance.res_content)

        authoritative = ResourceTreeSet([])
        for root_uuid, changed_resources in by_root.items():
            base = gateway.get_tree(root_uuid)
            partial = ResourceTreeSet.from_raw_dict_list(
                [
                    resource.model_dump(by_alias=True)
                    for resource in changed_resources
                ]
            )
            snapshot = resource_tree_to_snapshot(
                partial,
                base,
                allow_partial=allow_partial,
            )
            diff = gateway.compare_snapshot(snapshot)
            if diff.changed:
                mutation = self._mutation(
                    "apply_material_snapshot",
                    device_id=device_id,
                    device_uuid=device_uuid,
                    root_material_uuid=root_uuid,
                    preconditions=self._snapshot_preconditions(base),
                )
                current = gateway.apply_snapshot(mutation, snapshot).data
            else:
                current = base
            authoritative.trees.extend(
                material_tree_to_resource_tree(current).trees
            )
        return authoritative

    async def update_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> ResourceTreeSet:
        return await asyncio.to_thread(
            self._update_sync,
            device_id,
            device_uuid,
            resources,
        )

    async def snapshot_resource_tree(
        self,
        device_id: str,
        device_uuid: str,
        root_resource: Any,
    ) -> ResourceTreeSet:
        """严格提交完整根树；缺少任一权威 child 都拒绝，不做局部合并。"""

        return await asyncio.to_thread(
            self._update_sync,
            device_id,
            device_uuid,
            root_resource,
            allow_partial=False,
        )

    def _get_sync(
        self,
        resources_uuid: Sequence[str],
        with_children: bool,
    ) -> ResourceTreeSet:
        gateway = self._gateway()
        result = ResourceTreeSet([])
        seen: set[str] = set()
        for raw_uuid in resources_uuid:
            material_uuid = str(raw_uuid or "").strip()
            if not material_uuid or material_uuid in seen:
                continue
            seen.add(material_uuid)
            tree_set = material_tree_to_resource_tree(
                gateway.get_tree(material_uuid)
            )
            if not with_children:
                for tree in tree_set.trees:
                    tree.root_node.children = []
            result.trees.extend(tree_set.trees)
        return result

    async def get_resources(
        self,
        device_id: str,
        resources_uuid: list[str],
        with_children: bool,
    ) -> ResourceTreeSet:
        del device_id
        return await asyncio.to_thread(
            self._get_sync,
            resources_uuid,
            with_children,
        )

    def get_resources_sync(
        self,
        resources_uuid: Sequence[str],
        with_children: bool = True,
    ) -> ResourceTreeSet:
        """同步查询入口，供 ROS service callback 使用。"""

        return self._get_sync(resources_uuid, with_children)

    def get_resource_by_id_sync(
        self,
        resource_id: str,
        with_children: bool = True,
    ) -> ResourceTreeSet:
        gateway = self._gateway()
        aggregate = gateway.get_material_by_resource_id(str(resource_id))
        return self._get_sync(
            [aggregate.material.material_uuid],
            with_children,
        )

    async def get_resource_by_id(
        self,
        device_id: str,
        resource_id: str,
        with_children: bool,
    ) -> ResourceTreeSet:
        del device_id
        return await asyncio.to_thread(
            self.get_resource_by_id_sync,
            resource_id,
            with_children,
        )

    def delete_resources_sync(
        self,
        device_id: str,
        device_uuid: str,
        resources_uuid: Sequence[str],
    ) -> list[str]:
        gateway = self._gateway()
        deleted: list[str] = []
        for raw_uuid in resources_uuid:
            material_uuid = str(raw_uuid or "").strip()
            if not material_uuid:
                continue
            mutation = self._mutation(
                "delete_material",
                device_id=device_id,
                device_uuid=device_uuid,
                root_material_uuid=material_uuid,
            )
            result = gateway.delete_material(
                mutation,
                MaterialDelete(material_uuid=material_uuid, recursive=True),
            )
            deleted.extend(result.data.deleted_material_uuids)
        return deleted

    async def delete_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources_uuid: list[str],
    ) -> list[str]:
        return await asyncio.to_thread(
            self.delete_resources_sync,
            device_id,
            device_uuid,
            resources_uuid,
        )


__all__ = [
    "AuthorityResourceService",
    "MaterialSnapshotObserver",
    "MaterialSyncRequest",
    "MaterialSyncResponse",
    "MaterialSyncService",
    "ResourceService",
]
