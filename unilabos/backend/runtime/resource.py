"""设备运行时到微后端 Materials Authority 的唯一资源服务边界。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, Optional, Protocol, Sequence
from uuid import uuid4

from unilabos.backend.runtime.async_utils import run_blocking
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.resources.plr_identity import has_legacy_tip_channels, material_children
from unilabos.resources.adapters.plr_materials import (
    CreatedPLRMaterials,
    MaterialGateway,
    SnapshotUuidMismatchError,
    create_plr_materials,
    material_tree_to_resource_tree,
    resource_to_node_delta,
    resource_tree_to_create,
    resource_tree_to_snapshot,
)
from unilabos.protocol.materials import AggregatePrecondition, InventoryMutation
from unilabos.protocol.materials import MaterialDelete, MaterialDelta, MaterialMove


logger = logging.getLogger(__name__)


def _is_service_error(exc: BaseException, code: str, type_name: str) -> bool:
    """判定权威侧业务异常类型（本地直抛或跨 HostLink 均可判别）。

    本地网关直抛带 ``code`` 的 ``MaterialsServiceError``；跨 HostLink 时服务端把
    异常类型与 MRO 编入 ``RemoteError.error_info``。
    """

    if getattr(exc, "code", None) == code:
        return True
    info = getattr(exc, "error_info", None)
    if isinstance(info, dict):
        if info.get("exception_type") == type_name:
            return True
        mro = info.get("exception_mro")
        if isinstance(mro, list) and type_name in mro:
            return True
    return False


def _is_conflict_error(exc: BaseException) -> bool:
    """权威侧乐观锁冲突。"""

    return _is_service_error(exc, "conflict", "MaterialConflictError")


def _is_no_change_error(exc: BaseException) -> bool:
    """权威判定快照与其状态一致（本地基线已过期到"刚好一致"的竞态）。"""

    return _is_service_error(exc, "no_change", "MaterialNoChangeError")


def _local_root_uuid(resource: Any) -> Optional[str]:
    """PLR 实例沿 ``.parent`` 走到顶层持有根的 uuid。

    设备持有的顶层根在权威里 parent 是设备物料，但设备不在 PLR 树里，所以 PLR
    树的顶层就是快照根——无需向权威反查每个节点的归属。
    """

    top = resource
    while getattr(top, "parent", None) is not None:
        top = top.parent
    return str(getattr(top, "unilabos_uuid", "") or "") or None


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

    def update_resources_sync(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> ResourceTreeSet:
        """同步 update；``materials.update`` 唯一汇聚点经此下发。"""
        ...

    async def snapshot_resource_tree(
        self,
        device_id: str,
        device_uuid: str,
        root_resource: Any,
    ) -> ResourceTreeSet:
        """提交一棵 UUID 集合完整的运行时物料树快照。"""
        ...

    async def apply_node_deltas(
        self,
        device_id: str,
        device_uuid: str,
        root: Any,
        nodes: Sequence[Any],
    ) -> bool:
        """状态类增量上行：只报根树里变了的节点（data / 内容物 / 位姿）。"""
        ...

    async def move_resource(
        self,
        device_id: str,
        device_uuid: str,
        material_uuid: str,
        *,
        parent_material_uuid: Optional[str] = None,
        destination_site_uuid: Optional[str] = None,
    ) -> None:
        """把权威物料挂载/移动到另一个权威物料（或其 Site）下。"""
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
    #: 自上次提交以来 state 变过的节点（id → 节点）；结构没变时只上报这些
    dirty_nodes: dict[int, Any] = field(default_factory=dict)
    #: 自上次提交以来发生过 assign / unassign：必须走整树快照
    structural: bool = False


class MaterialSnapshotObserver:
    """把 PLR 后代的变更上报给权威：状态变化按节点增量，结构变化整树快照。

    PLR 只会向父节点传播 assign/unassign callback，state callback 不传播。
    因此这里递归监听每个后代的 state，记下具体哪个节点变了，但以根对象为唯一排队
    键。一次事件循环内连续修改多个孔位只提交一次；提交期间再次发生变化则紧接着再
    提交一轮。只有 state 变化时用 ``apply_node_deltas`` 只发变了的节点；发生过
    assign / unassign（父子、位点占用变化）则用严格整树快照，保留结构漂移检测。
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
        for child in material_children(resource):
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
                self._queue(_root_key, structural=True)

            def did_unassign(resource: Any, *, _root_key: int = root_key) -> None:
                self._drop_state_subtree(_root_key, resource)
                self._queue(_root_key, structural=True)

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
                    _node: Any = node,
                ) -> None:
                    # legacy 通道取放不触发 Resource.children 的 assign 回调，
                    # head_state 变化可能代表物料拓扑变化，必须冻结完整树。
                    head_changed = has_legacy_tip_channels(_node) and (
                        "head_state" in _state or "head96_state" in _state
                    )
                    if head_changed:
                        self._observe_state_subtree(_root_key, _node)
                    self._queue(_root_key, node=_node, structural=head_changed)

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

    def _queue(
        self, root_key: int, *, node: Any = None, structural: bool = False
    ) -> None:
        if self._suppression_depth.get() > 0:
            return
        with self._guard:
            observed = self._roots.get(root_key)
            if observed is None:
                return
            observed.dirty = True
            if structural:
                observed.structural = True
            elif node is not None:
                observed.dirty_nodes[id(node)] = node
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

    _FLUSH_CONFLICT_RETRIES = 5

    async def _flush(self, root_key: int) -> None:
        # 合并同一个同步 tick 内多个 child 的变化。
        await asyncio.sleep(0)
        conflict_attempts = 0
        force_full = False
        while True:
            with self._guard:
                observed = self._roots.get(root_key)
                if observed is None:
                    return
                observed.dirty = False
                root = observed.root
                changed_nodes = list(observed.dirty_nodes.values())
                structural = observed.structural or force_full
                observed.dirty_nodes.clear()
                observed.structural = False
            force_full = False
            try:
                delta_method = getattr(self._service, "apply_node_deltas", None)
                if (
                    not structural
                    and changed_nodes
                    and callable(delta_method)
                    and all(
                        str(getattr(node, "unilabos_uuid", "") or "") for node in changed_nodes
                    )
                ):
                    # 只有状态变化：按节点增量上报，不冻结整棵树
                    try:
                        await delta_method(
                            self._device_id(),
                            self._device_uuid(),
                            root,
                            changed_nodes,
                        )
                    except Exception as exc:
                        if not _is_conflict_error(exc):
                            raise
                        # 增量版本仍冲突：权威结构可能已变，退回整树快照对齐
                        logger.info("物料根树增量上报冲突，改走整树快照：%s", exc)
                        with self._guard:
                            current = self._roots.get(root_key)
                            if current is not None:
                                current.dirty = True
                        force_full = True
                        continue
                else:
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
            except SnapshotUuidMismatchError as exc:
                # 挂载/转移主流程（move/transfer）改写权威结构的竞态窗口：
                # 冻结树与新基线集合暂态不一致。放弃本次提交；结构变化本身
                # 会触发新的 assign/state 回调（dirty），下轮重新冻结即收敛。
                logger.warning(
                    "物料根树 snapshot 与权威基线结构漂移，放弃本次提交等待收敛：%s",
                    exc,
                )
                with self._guard:
                    current = self._roots.get(root_key)
                    if current is None:
                        return
                    if current.dirty:
                        continue
                    current.scheduled = False
                    return
            except Exception as exc:
                if _is_conflict_error(exc):
                    # 乐观锁冲突：冻结态已过期（权威被并发 move/transfer/快照
                    # 推进）。旧冻结不可重放——必须回到执行线程重新冻结最新
                    # runtime 树再提交，否则会用陈旧 Site 占用回滚权威落位。
                    conflict_attempts += 1
                    if conflict_attempts < self._FLUSH_CONFLICT_RETRIES:
                        logger.info(
                            "物料根树 snapshot 版本冲突，重新冻结 runtime 树重试"
                            "（第 %s 次）",
                            conflict_attempts,
                        )
                        continue
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
    # 延迟导入，避免 runtime 与启动配置形成模块循环。
    from unilabos.resources.materials import resolve_materials_gateway

    return resolve_materials_gateway()


def _is_device_material(aggregate: Any) -> bool:
    """权威物料行是否是设备本身（图里的 type=device 节点），而不是一件耗材 / 台面。"""

    return str(getattr(aggregate.material, "resource_type", "") or "").lower() == "device"


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
        # dump/load 只复制节点；运行时证据（设备仍持有哪些物料）随树集一起带上
        tree_set.runtime_material_uuids = resources.runtime_material_uuids
    else:
        tree_set = ResourceTreeSet.from_plr_resources(
            _normalize_plr_resources(resources)
        )
    if not tree_set.all_nodes:
        raise ValueError("更新物料时至少需要一个已登记资源")
    return tree_set


class AuthorityResourceService:
    """通过嵌入式、HTTP 或 HostLink client 访问同一个微后端权威。

    设备侧持有的 PLR 实例是它所持物料的工作真相，权威是被同步的一方：快照
    上行不为了算归属或算 diff 去读权威——每棵根树的权威基线只在首次、乐观锁
    冲突、结构漂移和下行变更之后读一次，其余时间用本地缓存的基线做 diff 与
    precondition；本地 diff 为空的 flush 一次服务器调用都没有。
    """

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
        self._baselines: dict[str, Any] = {}
        # 物料 uuid → (version, state_hash)；位点 uuid → version。增量上报的乐观锁
        # 只需要这两张小表，不需要整棵基线树。
        self._versions: dict[str, tuple[int, str]] = {}
        self._site_versions: dict[str, int] = {}
        self._baseline_lock = threading.Lock()

    def _gateway(self) -> MaterialGateway:
        gateway = self._configured_gateway
        if gateway is None:
            gateway = self._gateway_provider()
        if gateway is None:
            raise RuntimeError("微后端 Materials Authority 尚未配置")
        return gateway

    # ── 权威基线缓存与版本表 ─────────────────────────────────────

    def remember_baseline(self, tree: Any) -> None:
        """记住一棵刚从权威拿到的根树（apply 结果、下行拉取），后续 flush 直接用。"""

        root_uuid = str(getattr(tree, "root_material_uuid", "") or "")
        if not root_uuid:
            return
        with self._baseline_lock:
            self._baselines[root_uuid] = tree
            for node in getattr(tree, "nodes", None) or []:
                self._versions[node.material.material_uuid] = (
                    node.material.version,
                    node.state_hash,
                )
                for site in node.sites:
                    self._site_versions[site.site_uuid] = site.version

    def record_affected(self, affected: Sequence[Any]) -> None:
        """用 mutation 返回的 ``affected`` 刷新版本表——不用回整棵树。"""

        with self._baseline_lock:
            for item in affected:
                if item.aggregate_type == "material":
                    self._versions[item.aggregate_uuid] = (item.version, item.state_hash)
                elif item.aggregate_type == "site":
                    self._site_versions[item.aggregate_uuid] = item.version

    def invalidate_baselines(self, *root_uuids: str) -> None:
        """基线过期：不传即全部作废。权威侧结构变了（move / transfer / 下行增删）时调用。

        连同版本表一起作废：过期的版本号只会换来一次冲突再重拉，不如直接重拉。
        """

        with self._baseline_lock:
            if not root_uuids:
                self._baselines.clear()
                self._versions.clear()
                self._site_versions.clear()
                return
            for root_uuid in root_uuids:
                tree = self._baselines.pop(str(root_uuid), None)
                for node in getattr(tree, "nodes", None) or []:
                    self._versions.pop(node.material.material_uuid, None)
                    for site in node.sites:
                        self._site_versions.pop(site.site_uuid, None)

    def _forget_tree(self, root_uuid: str) -> None:
        """只丢整树缓存、保留版本表：增量 apply 之后基线树里的 data / 位姿已旧。"""

        with self._baseline_lock:
            self._baselines.pop(root_uuid, None)

    def _baseline(self, gateway: MaterialGateway, root_uuid: str) -> Any:
        with self._baseline_lock:
            cached = self._baselines.get(root_uuid)
        if cached is not None:
            return cached
        tree = gateway.get_tree(root_uuid)
        self.remember_baseline(tree)
        return tree

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
        return await run_blocking(
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
        """物料在权威里所属的快照根：沿 parent 链向上，停在第一个设备物料之下。

        设备本身是权威里的一行物料（开机图对齐落的），台面 / 驱动 ensure 的根树挂到
        设备时 parent 指向它；但设备不是 PLR 树的一部分，快照按"设备下的那棵树"分组
        提交，而不是把整个设备连同它下面所有台面当成一棵树。
        """

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
            parent = aggregate_cache.get(parent_uuid)
            if parent is None:
                parent = gateway.get_material(parent_uuid)
                aggregate_cache[parent_uuid] = parent
            if _is_device_material(parent):
                root_uuid = current_uuid
                break
            current_uuid = parent_uuid
        for item in path:
            root_cache[item] = root_uuid
        return root_uuid

    @staticmethod
    def _local_roots(
        resources: Any, runtime: ResourceTreeSet, device_uuid: str = ""
    ) -> dict[str, str]:
        """不问权威就能确定的 ``节点 uuid → 快照根 uuid``。

        - 传入 PLR 实例：沿 ``.parent`` 到 PLR 顶层即根，其子树全部归它；
        - 传入 ResourceTreeSet：``uuid_parent`` 为空、或就是本设备物料的树根就是根
          （观察者冻结的整棵持有树；append_resource 挂到设备上、根 parent 投影为
          设备的台面树）——与 ``_root_material_uuid`` 停在设备物料之下同一规则。
          父在权威里但不在本地树里的子树（脚本传来的局部权威树）留给
          ``_root_material_uuid`` 反查：那里每个节点至少一次权威往返，整棵台面
          （带枪头时数百节点）逐个反查会把一次挂载拖到超时。
        """

        roots: dict[str, str] = {}
        if isinstance(resources, ResourceTreeSet):
            for tree in runtime.trees:
                root = tree.root_node.res_content
                parent_uuid = root.uuid_parent
                if root.uuid and (
                    parent_uuid is None or (device_uuid and parent_uuid == device_uuid)
                ):
                    for node in tree.get_all_nodes():
                        roots[node.res_content.uuid] = root.uuid
            return roots
        for resource in _normalize_plr_resources(resources):
            root_uuid = _local_root_uuid(resource)
            if not root_uuid:
                continue
            stack = [resource]
            while stack:
                current = stack.pop()
                node_uuid = str(getattr(current, "unilabos_uuid", "") or "")
                if node_uuid:
                    roots[node_uuid] = root_uuid
                stack.extend(material_children(current))
        return roots

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
        conflict_retries: int | None = None,
    ) -> ResourceTreeSet:
        runtime = _existing_tree_set(resources)
        gateway = self._gateway()
        aggregate_cache: dict[str, Any] = {}
        root_cache: dict[str, str] = self._local_roots(
            resources, runtime, str(device_uuid or "")
        )
        by_root: dict[str, list[Any]] = defaultdict(list)
        seen_runtime_uuids: set[str] = set()
        for instance in runtime.all_nodes:
            material_uuid = instance.res_content.uuid
            if material_uuid in seen_runtime_uuids:
                continue
            seen_runtime_uuids.add(material_uuid)
            root_uuid = root_cache.get(material_uuid) or self._root_material_uuid(
                gateway,
                material_uuid,
                aggregate_cache,
                root_cache,
            )
            by_root[root_uuid].append(instance.res_content)

        # 运行时证据：设备仍持有的物料（含上报树之外、同一 PLR 顶层祖先下的物料）
        # 加上本次上报的全部节点——枪头若被上报到另一棵根树，就不是被丢弃。
        held = runtime.runtime_material_uuids
        if held is not None:
            held = held | seen_runtime_uuids
        authoritative = ResourceTreeSet([])
        for root_uuid, changed_resources in by_root.items():
            current = self._push_root_snapshot(
                gateway,
                device_id,
                device_uuid,
                root_uuid,
                changed_resources,
                allow_partial=allow_partial,
                conflict_retries=conflict_retries,
                runtime_material_uuids=held,
            )
            authoritative.trees.extend(
                material_tree_to_resource_tree(current).trees
            )
        return authoritative

    _SNAPSHOT_CONFLICT_RETRIES = 3

    def _push_root_snapshot(
        self,
        gateway: MaterialGateway,
        device_id: str,
        device_uuid: str,
        root_uuid: str,
        changed_resources: list[Any],
        *,
        allow_partial: bool,
        conflict_retries: int | None = None,
        runtime_material_uuids: frozenset[str] | None = None,
    ) -> Any:
        """把一组节点投影进单棵权威根树，带乐观锁冲突重试。

        基线来自本地缓存（首次一次 ``get_tree``），diff 在本地算：没有语义变化的
        flush 不发任何请求；有变化才 ``apply_snapshot``，成功后用返回的树刷新基线。
        precondition 版本冲突说明基线过期（权威被并发 move/transfer 推进），作废
        基线重拉一次再算；本地基线与冻结树结构不一致（刚发生过挂载 / 移走）同样
        重拉一次，仍不一致才视为真实漂移抛给调用方。

        ``conflict_retries=1`` 表示冲突不在本层重试、直接抛给调用方——
        观察者严格快照走该模式：``changed_resources`` 是排队时刻的冻结态，
        权威被并发 mutation 推进后旧冻结不可重放，必须由观察者重新冻结最新
        runtime 树再提交。

        ``runtime_material_uuids`` 是设备运行时仍持有的物料；上报父节点下缺席、
        又不在其中的枪头视为已丢弃，随同一快照退役（见 ``resource_tree_to_snapshot``）。
        """

        from unilabos.server.services.materials.snapshot import compare_material_snapshot

        retries = conflict_retries or self._SNAPSHOT_CONFLICT_RETRIES
        conflicts = 0
        refreshed_for_drift = False
        while True:
            base = self._baseline(gateway, root_uuid)
            # 分组后的节点集合可能跨树引用（site occupied 指向别的权威树），
            # 不构成完整树，直接按 ResourceDict 集合投影快照。
            try:
                snapshot = resource_tree_to_snapshot(
                    changed_resources,
                    base,
                    allow_partial=allow_partial,
                    runtime_material_uuids=runtime_material_uuids,
                )
            except SnapshotUuidMismatchError:
                if refreshed_for_drift:
                    raise
                # 基线可能只是过期（刚 append / 移走），重拉一次再判断
                refreshed_for_drift = True
                self.invalidate_baselines(root_uuid)
                continue
            diff = compare_material_snapshot(base, snapshot)
            if not diff.changed:
                return base
            mutation = self._mutation(
                "apply_material_snapshot",
                device_id=device_id,
                device_uuid=device_uuid,
                root_material_uuid=root_uuid,
                preconditions=self._snapshot_preconditions(base),
            )
            try:
                applied = gateway.apply_snapshot(mutation, snapshot).data
            except Exception as exc:
                if _is_no_change_error(exc):
                    # 本地基线落后于权威、而权威恰好已是这个状态：刷新基线即收敛
                    self.invalidate_baselines(root_uuid)
                    return self._baseline(gateway, root_uuid)
                if not _is_conflict_error(exc):
                    raise
                conflicts += 1
                self.invalidate_baselines(root_uuid)
                if conflicts >= retries:
                    raise
                logger.info(
                    "物料根树 %s snapshot 版本冲突，重拉基线重试（第 %s 次）",
                    root_uuid,
                    conflicts,
                )
                continue
            self.remember_baseline(applied)
            return applied

    async def update_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> ResourceTreeSet:
        return await run_blocking(
            self._update_sync,
            device_id,
            device_uuid,
            resources,
        )

    def update_resources_sync(
        self,
        device_id: str,
        device_uuid: str,
        resources: Any,
    ) -> ResourceTreeSet:
        """同步更新入口，供 materials.update 等无事件循环上下文使用。"""

        return self._update_sync(device_id, device_uuid, resources)

    _DELTA_CONFLICT_RETRIES = 2

    def apply_node_deltas_sync(
        self,
        device_id: str,
        device_uuid: str,
        root: Any,
        nodes: Sequence[Any],
    ) -> bool:
        """状态类增量上报：只把变了的节点（data / 内容物 / 位姿）发给权威，按 uuid 合并。

        乐观锁用本地版本表里每个节点的 version；版本表由基线树、mutation 的
        ``affected`` 和下行刷新。缺版本的节点先拉一次根树补齐（仅首次）。版本冲突说明
        权威被别处推进：作废后重拉一次再试，仍冲突则抛给调用方（观察者退回整树快照）。
        返回权威是否接受了变化（``no_change`` 视为 False）。
        """

        gateway = self._gateway()
        root_uuid = _local_root_uuid(root)
        if not root_uuid:
            raise ValueError("增量上报的根物料缺少权威 uuid")
        node_uuids = [str(getattr(node, "unilabos_uuid", "") or "") for node in nodes]
        if any(not item for item in node_uuids):
            raise ValueError("增量上报的物料缺少权威 uuid")
        conflicts = 0
        while True:
            with self._baseline_lock:
                missing = [item for item in node_uuids if item not in self._versions]
            if missing:
                self._forget_tree(root_uuid)
                self._baseline(gateway, root_uuid)
            deltas = []
            for node, node_uuid in zip(nodes, node_uuids):
                with self._baseline_lock:
                    known = self._versions.get(node_uuid)
                if known is None:
                    raise ValueError(f"物料 {node_uuid} 不在权威根树 {root_uuid} 里")
                serialized = (
                    ResourceTreeSet.from_plr_resources([node]).trees[0].root_node.res_content
                )
                deltas.append(resource_to_node_delta(serialized, expected_version=known[0]))
            mutation = self._mutation(
                "apply_material_delta",
                device_id=device_id,
                device_uuid=device_uuid,
                root_material_uuid=root_uuid,
            )
            try:
                result = gateway.apply_delta(
                    mutation, MaterialDelta(root_material_uuid=root_uuid, nodes=deltas)
                )
            except Exception as exc:
                if _is_no_change_error(exc):
                    return False
                if not _is_conflict_error(exc):
                    raise
                conflicts += 1
                self.invalidate_baselines(root_uuid)
                with self._baseline_lock:
                    for node_uuid in node_uuids:
                        self._versions.pop(node_uuid, None)
                if conflicts >= self._DELTA_CONFLICT_RETRIES:
                    raise
                logger.info("物料根树 %s 增量版本冲突，刷新版本表重试", root_uuid)
                continue
            self.record_affected(result.affected)
            # 整树基线里这些节点的 data / 位姿已经旧了；下次结构快照重拉一次
            self._forget_tree(root_uuid)
            return bool(result.affected)

    async def apply_node_deltas(
        self,
        device_id: str,
        device_uuid: str,
        root: Any,
        nodes: Sequence[Any],
    ) -> bool:
        return await run_blocking(
            self.apply_node_deltas_sync, device_id, device_uuid, root, nodes
        )

    async def snapshot_resource_tree(
        self,
        device_id: str,
        device_uuid: str,
        root_resource: Any,
    ) -> ResourceTreeSet:
        """严格提交完整根树；缺少任一权威 child 都拒绝，不做局部合并。

        冲突不在服务层重试（``conflict_retries=1``）：入参是观察者排队
        时刻的冻结态，权威并发变化后必须重新冻结，由观察者 ``_flush``
        循环承接。
        """

        return await run_blocking(
            self._update_sync,
            device_id,
            device_uuid,
            root_resource,
            allow_partial=False,
            conflict_retries=1,
        )

    def move_resource_sync(
        self,
        device_id: str,
        device_uuid: str,
        material_uuid: str,
        *,
        parent_material_uuid: Optional[str] = None,
        destination_site_uuid: Optional[str] = None,
    ) -> None:
        """物料挂载/移动的权威事实：更新 parent 与 Site 占用（原子）。

        物料挂到另一个物料下是 materials.db 的真实父子关系（设备挂载除外，
        设备不是权威物料）；快照协议只更新既有聚合、无法表达跨树合并，
        因此挂载必须先经 move 落库，随后的状态快照才能按新树分组对齐。
        """

        mutation = self._mutation(
            "move_material",
            device_id=device_id,
            device_uuid=device_uuid,
            root_material_uuid=material_uuid,
        )
        self._gateway().move_material(
            mutation,
            MaterialMove(
                material_uuid=material_uuid,
                destination_site_uuid=destination_site_uuid,
                parent_material_uuid=parent_material_uuid,
            ),
        )
        # 父子 / 占用关系变了：来源与目标两棵根树的缓存基线都已过期
        self.invalidate_baselines()

    async def move_resource(
        self,
        device_id: str,
        device_uuid: str,
        material_uuid: str,
        *,
        parent_material_uuid: Optional[str] = None,
        destination_site_uuid: Optional[str] = None,
    ) -> None:
        await run_blocking(
            self.move_resource_sync,
            device_id,
            device_uuid,
            material_uuid,
            parent_material_uuid=parent_material_uuid,
            destination_site_uuid=destination_site_uuid,
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
        return await run_blocking(
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
        return await run_blocking(
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
        if deleted:
            self.invalidate_baselines()
        return deleted

    async def delete_resources(
        self,
        device_id: str,
        device_uuid: str,
        resources_uuid: list[str],
    ) -> list[str]:
        return await run_blocking(
            self.delete_resources_sync,
            device_id,
            device_uuid,
            resources_uuid,
        )


__all__ = [
    "AuthorityResourceService",
    "MaterialSnapshotObserver",
    "ResourceService",
]
