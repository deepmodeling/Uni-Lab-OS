"""设备运行时到微后端 Materials Authority 的唯一资源服务边界。"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Callable, Protocol, Sequence
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

    async def get_resources(
        self,
        device_id: str,
        resources_uuid: list[str],
        with_children: bool,
    ) -> ResourceTreeSet: ...


GatewayProvider = Callable[[], MaterialGateway]


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
                allow_partial=True,
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


__all__ = ["AuthorityResourceService", "ResourceService"]
