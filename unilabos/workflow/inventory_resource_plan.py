"""在来源准入后、首次派发前补齐资源计划的库存身份。"""

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from unilabos.app.scheduler.inventory.station_resource import TargetSiteRequest
from unilabos.registry.action_resource_contract import (
    ActionResourceContractError,
    normalize_action_resource_contract,
    operate_in_place_parameters,
)
from unilabos.workflow._execution_plan_graph import final_target_data_key
from unilabos.workflow.execution_plan import ExecutionPlanBuilder, ExecutionPlanBuildError
from unilabos.workflow.material_source_context import CenterMaterialSourceContext
from unilabos.workflow.resource_lock_key import (
    canonical_resource_lock_scope,
    device_lock_key,
    material_lock_key,
    site_lock_key,
)
from unilabos.workflow.resource_lock_plan import (
    ResourcePlan,
    ResourcePlanError,
    bind_station_resource_plan,
    compile_template_resource_plan,
    deserialize_resource_plan,
    reservation_sites,
    resource_plan_for_node,
    serialize_resource_plan,
    with_hashed_resource_plan_metadata,
)
from unilabos.workflow.store import StoreConflict

_INVENTORY_RESOURCE_KEYS_METADATA = "inventory_resource_lock_keys_by_node"


class _StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ExecutionInputWire(_StrictWireModel):
    handle_uuid: str
    data_key: str
    type: str
    required: bool


class _ActionResourceParameterWire(_StrictWireModel):
    param: str
    role: Literal["device", "tool", "motion", "site", "material"]


class _DeviceTenancyWire(_StrictWireModel):
    mode: Literal["task_while_loaded"]
    material_param: str
    acquire_device_param: str | None = None
    release_device_param: str | None = None


class _TransferWire(_StrictWireModel):
    material_param: str
    source_owner_param: str | None = None
    source_site_uuid_param: str | None = None
    source_site_name_param: str | None = None
    target_owner_param: str
    target_site_uuid_param: str | None = None
    target_site_name_param: str | None = None
    gripper_site_role: str
    motion_resource_roles: list[str] = Field(default_factory=list)
    tool_resource_roles: list[str] = Field(default_factory=list)


class _OperateInPlaceWire(_StrictWireModel):
    material_param: str | None = None
    material_params: list[str] | None = None


class _RuntimeMountBindingWire(_StrictWireModel):
    """运行时物料来源挂载边的冻结身份。"""

    source_node_uuid: str
    source_data_key: str


class _TransferStepWire(_StrictWireModel):
    operation: Literal["pick", "place"]
    material_param: str
    owner_param: str
    site_param: str
    carrier_params: list[str]


class _AliquotWire(_StrictWireModel):
    source_material_param: str
    target_material_params: list[str]


class _ActionResourceContractWire(_StrictWireModel):
    version: StrictInt = 1
    required_device_params: list[str] = Field(default_factory=list)
    resource_params: list[_ActionResourceParameterWire] = Field(default_factory=list)
    device_tenancy: _DeviceTenancyWire | None = None
    transfer: _TransferWire | None = None
    operate_in_place: _OperateInPlaceWire | None = None
    transfer_step: _TransferStepWire | None = None
    order_sensitive: bool = False
    aliquot: _AliquotWire | None = None

    @model_validator(mode="after")
    def validate_contract_semantics(self) -> "_ActionResourceContractWire":
        """复用注册表规范，验证字段组合而不重新解释持久化数据。"""

        contract = self.model_dump(exclude_unset=True)
        if not contract:
            return self
        try:
            normalize_action_resource_contract(contract)
        except ActionResourceContractError as error:
            raise ValueError(error.message) from error
        return self


class _ExecutionNodeWire(_StrictWireModel):
    uuid: str
    parent_uuid: str | None = None
    topological_index: int
    kind: str
    param: dict[str, Any] = Field(default_factory=dict)
    execution_policy: dict[str, Any] = Field(default_factory=dict)
    action_resource_contract: _ActionResourceContractWire = Field(
        default_factory=_ActionResourceContractWire
    )
    inputs: list[_ExecutionInputWire] = Field(default_factory=list)
    source_handle_uuids: list[str] = Field(default_factory=list)
    meta_data: dict[str, Any] = Field(default_factory=dict)
    resource_defaults: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    branch_id: str = ""
    order_sensitive: bool = False
    physical_hold_resources: list[str] = Field(default_factory=list)
    planned_material_uuid: str = ""
    runtime_material_source: bool = False
    runtime_mount_binding: _RuntimeMountBindingWire | None = None
    result_name: str = ""
    carry_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    output_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    site_selectors: list[dict[str, Any]] = Field(default_factory=list)
    control_region: dict[str, Any] = Field(default_factory=dict)
    device_id: str = ""
    device_selector: dict[str, Any] = Field(default_factory=dict)
    action_name: str | None = None
    action_type: str = ""
    always_free: bool = False
    param_schema: dict[str, Any] | None = None
    manual_confirmation: dict[str, Any] = Field(default_factory=dict)
    material_uuid: str = ""
    script: Any = None
    material_requirements: list[dict[str, Any]] = Field(default_factory=list)
    material_binding_targets: list[dict[str, Any]] = Field(default_factory=list)
    resource_plan_id: str = ""
    resource_interval_ids: list[str] = Field(default_factory=list)
    resource_acquire_set_id: str = ""
    inventory_resource_lock_keys: list[str] = Field(default_factory=list)


class _ExecutionEdgeWire(_StrictWireModel):
    uuid: str
    source_node_uuid: str
    target_node_uuid: str
    source_handle_uuid: str = ""
    target_handle_uuid: str = ""
    source_data_key: str = ""
    target_data_key: str = ""
    source_type: str = ""
    target_type: str = ""
    dependency_only: bool = False


class _ExecutionHandleWire(_StrictWireModel):
    uuid: str
    node_uuid: str
    template_handle_uuid: str
    data_source: str
    handle_key: str
    data_key: str
    io_type: str
    type: str
    required: bool
    site_selector: dict[str, Any] | None = None


class _InventoryExecutionPlanWire(_StrictWireModel):
    version: StrictInt
    run_mode: Literal["normal", "step", "single_node"]
    nodes: list[_ExecutionNodeWire]
    edges: list[_ExecutionEdgeWire]
    handles: list[_ExecutionHandleWire]
    capabilities: list[str] = Field(default_factory=list)
    resource_plan: dict[str, Any]
    inventory_resource_binding: Literal["pending", "bound"] | None = None
    target_node_uuid: str | None = None
    center_material_source_context: CenterMaterialSourceContext | None = None

    @model_validator(mode="after")
    def validate_graph_identity(self) -> "_InventoryExecutionPlanWire":
        if ("center_material_source_context" in self.model_fields_set
                and self.center_material_source_context is None):
            raise ValueError("中心物料来源上下文不得为 null")
        if self.version not in {1, 2}:
            raise ValueError("执行计划版本必须是 1 或 2")
        node_ids = [node.uuid for node in self.nodes]
        handle_ids = [handle.uuid for handle in self.handles]
        if any(not identity.strip() for identity in node_ids):
            raise ValueError("节点身份不能为空")
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("节点身份重复")
        if any(not identity.strip() for identity in handle_ids):
            raise ValueError("连接点身份不能为空")
        if len(handle_ids) != len(set(handle_ids)):
            raise ValueError("连接点身份重复")
        node_set = set(node_ids)
        handles = {handle.uuid: handle for handle in self.handles}
        template_handles: set[tuple[str, str]] = set()
        for handle in self.handles:
            if handle.node_uuid not in node_set:
                raise ValueError("连接点引用计划外节点")
            identity = (handle.node_uuid, handle.template_handle_uuid)
            if identity in template_handles:
                raise ValueError("节点模板连接点身份重复")
            template_handles.add(identity)
        for edge in self.edges:
            if (
                edge.source_node_uuid not in node_set
                or edge.target_node_uuid not in node_set
            ):
                raise ValueError("边引用计划外节点")
            if edge.source_handle_uuid:
                source = handles.get(edge.source_handle_uuid)
                if source is None or source.node_uuid != edge.source_node_uuid:
                    raise ValueError("边来源连接点与节点身份不一致")
            if edge.target_handle_uuid:
                target = handles.get(edge.target_handle_uuid)
                if target is None or target.node_uuid != edge.target_node_uuid:
                    raise ValueError("边目标连接点与节点身份不一致")
        if self.target_node_uuid and self.target_node_uuid not in node_set:
            raise ValueError("单节点目标不在计划内")
        return self


def _is_inventory_remount(node: _ExecutionNodeWire) -> bool:
    """``transfer_resource`` 只改挂已到位物料，计划阶段不要求机械臂合同。"""

    return (
        node.action_name == "transfer_resource"
        and node.action_resource_contract.transfer is None
    )


def _requires_location_keys(node: _ExecutionNodeWire) -> bool:
    """转运及原位动作需将库存确认的库位写入资源计划。"""
    return (
        node.kind == "material_transfer"
        or node.action_resource_contract.transfer is not None
        or node.action_resource_contract.operate_in_place is not None
    )


def _requires_inventory_binding(wire: _InventoryExecutionPlanWire) -> bool:
    """判断计划是否含必须由库存补齐资源身份的节点。"""

    return any(
        node.kind == "material_source"
        or (not _is_inventory_remount(node) and _requires_location_keys(node))
        for node in wire.nodes
    )


def _validate_bound_inventory_projection(
    wire: _InventoryExecutionPlanWire, resource_plan: ResourcePlan
) -> None:
    """证明 bound 判别字段与资源计划及转运节点投影一致。"""

    if resource_plan.binding_state != "bound":
        raise StoreConflict("持久化执行计划库存绑定状态与资源计划不一致")
    inventory_keys_by_node = {
        node.uuid: node.inventory_resource_lock_keys
        for node in wire.nodes
        if not _is_inventory_remount(node)
        and _requires_location_keys(node)
    }
    if resource_plan.metadata.get(_INVENTORY_RESOURCE_KEYS_METADATA) != (
        inventory_keys_by_node
    ):
        raise StoreConflict("持久化执行计划的库存资源清单与资源计划身份不一致")
    resources_by_key: dict[str, set[str]] = {}
    for resource in resource_plan.resources:
        resources_by_key.setdefault(resource.canonical_key, set()).add(
            resource.resource_id
        )
    for node in wire.nodes:
        if _is_inventory_remount(node):
            continue
        if not _requires_location_keys(node):
            continue
        inventory_keys = node.inventory_resource_lock_keys
        if not inventory_keys or len(inventory_keys) != len(set(inventory_keys)):
            raise StoreConflict("持久化执行计划的节点缺少完整库存资源清单")
        projected_resources = {
            interval.resource_id
            for interval in resource_plan.intervals
            if node.uuid in interval.node_uuids
        }
        for key in inventory_keys:
            resource_ids = resources_by_key.get(key, set())
            if (
                canonical_resource_lock_scope(key) is None
                or len(resource_ids) != 1
                or not resource_ids <= projected_resources
            ):
                raise StoreConflict("持久化执行计划的节点缺少完整库存资源投影")


def _validate_execution_plan(
    value: Mapping[str, Any],
) -> tuple[_InventoryExecutionPlanWire, ResourcePlan]:
    """在读取绑定判别字段前关闭式验证持久执行计划。"""

    try:
        wire = _InventoryExecutionPlanWire.model_validate(value)
        resource_plan = deserialize_resource_plan(wire.resource_plan)
        sites = reservation_sites(resource_plan)
    except (ValidationError, ResourcePlanError) as error:
        raise StoreConflict("持久化执行计划字段无效") from error
    if any(
        node.kind == "material_transfer"
        and not _is_inventory_remount(node)
        and node.action_resource_contract.transfer is None
        for node in wire.nodes
    ):
        raise StoreConflict("持久化执行计划的物料转移节点缺少完整 transfer 合同")
    binding_declared = "inventory_resource_binding" in wire.model_fields_set
    if wire.inventory_resource_binding is None:
        if binding_declared:
            raise StoreConflict("持久化执行计划库存绑定状态无效")
        if _requires_inventory_binding(wire) or sites:
            raise StoreConflict("持久化执行计划缺少库存绑定状态")
    if wire.inventory_resource_binding == "bound":
        _validate_bound_inventory_projection(wire, resource_plan)
    return wire, resource_plan


def _planned_transfer_resolver(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> Callable[[str], Callable[[str], Mapping[str, Any] | None]]:
    """查询物料唯一最近的因果搬运；父关系与原位库位投影共用同一证明。"""
    predecessors: dict[str, set[str]] = {str(node["uuid"]): set() for node in nodes}
    for edge in edges:
        predecessors[edge["target_node_uuid"]].add(edge["source_node_uuid"])
    ancestors: dict[str, set[str]] = {}

    def preceding(node_uuid: str) -> set[str]:
        if node_uuid not in ancestors:
            seen: set[str] = set()
            pending = list(predecessors[node_uuid])
            while pending:
                current = pending.pop()
                if current not in seen:
                    seen.add(current)
                    pending.extend(predecessors[current])
            ancestors[node_uuid] = seen
        return ancestors[node_uuid]

    moves: dict[str, dict[str, Mapping[str, Any]]] = {}
    for node in nodes:
        transfer = (node.get("action_resource_contract") or {}).get("transfer")
        if not transfer:
            continue
        material = _transfer_argument(node, "material_param")
        target = _transfer_argument(node, "target_owner_param")
        if material and target:
            moves.setdefault(material, {})[str(node["uuid"])] = node

    def for_node(node_uuid: str) -> Callable[[str], Mapping[str, Any] | None]:
        cache: dict[str, Mapping[str, Any] | None] = {}

        def parent(material_uuid: str) -> Mapping[str, Any] | None:
            if material_uuid not in cache:
                candidates = {
                    move: target
                    for move, target in moves.get(material_uuid, {}).items()
                    if move in preceding(node_uuid)
                }
                latest = [
                    move
                    for move in candidates
                    if not any(move in preceding(other) for other in candidates if other != move)
                ]
                if len(latest) > 1:
                    raise StoreConflict(f"物料 {material_uuid} 存在并行父位置变更，无法证明搬运设备祖先")
                cache[material_uuid] = candidates[latest[0]] if latest else None
            return cache[material_uuid]

        return parent

    return for_node


def _transfer_argument(node: Mapping[str, Any], key: str) -> str:
    descriptor = node["action_resource_contract"]["transfer"]
    value = (node.get("param") or {}).get(descriptor.get(key))
    if isinstance(value, Mapping):
        return str(value.get("uuid") or value.get("material_uuid") or value.get("site_uuid") or "")
    return str(value or "")


def _parent_from_transfer(
    resolve: Callable[[str], Mapping[str, Any] | None],
) -> Callable[[str], str | None]:
    def parent(material_uuid: str) -> str | None:
        move = resolve(material_uuid)
        return _transfer_argument(move, "target_owner_param") if move is not None else None

    return parent


def bind_inventory_resource_plan(
    task: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]], inventory: Any,
) -> dict[str, Any]:
    """仅用冻结透传合同传播物料身份，绝不执行动作或预填下游业务参数。"""
    if not isinstance(task, Mapping) or not isinstance(task.get("execution_plan"), Mapping):
        raise StoreConflict("持久化执行计划必须是对象")
    plan = deepcopy(task["execution_plan"])
    wire, _resource_plan = _validate_execution_plan(plan)
    from unilabos.workflow.resource_lock_plan import SCHEDULER_RESOURCE_ORDER_CAPABILITY, scheduler_resource_order

    with scheduler_resource_order(SCHEDULER_RESOURCE_ORDER_CAPABILITY in _resource_plan.capabilities):
        return _bind_inventory_resource_plan(plan, wire, _resource_plan, task, jobs, inventory)



def transfer_endpoint_site_keys(
    resolver: Any, transfer: Mapping[str, Any], params: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """把一次搬运的源/目标端点解析成库位锁键。

    参数：``resolver`` 提供 ``resolve_target_site``（库存库位权威）；``transfer`` 是动作
    合同的 transfer 段；``params`` 是动作参数。返回：``(库位锁键, 解析不出库位的所属物件 UUID)``。
    冻结计划绑定和派发期加锁都调用这里，保证两边对“哪一端整件锁”判断一致。
    """

    def ident(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("uuid") or value.get("material_uuid") or "")
        return str(value or "")

    moved = ident(params.get(transfer.get("material_param") or "", ""))
    site_keys: list[str] = []
    unresolved: list[str] = []
    for side in ("source", "target"):
        owner_param = transfer.get(side + "_owner_param")
        if not owner_param or owner_param not in params:
            continue
        owner = ident(params[owner_param])
        if not owner or owner == moved:
            continue
        uuid_param = transfer.get(side + "_site_uuid_param")
        name_param = transfer.get(side + "_site_name_param")
        site_uuid = ident(params[uuid_param]) if uuid_param and uuid_param in params else ""
        site_name = "" if site_uuid else (str(params[name_param]) if name_param and name_param in params else "")
        if not (site_uuid or site_name) or resolver is None:
            unresolved.append(owner)
            continue
        try:
            endpoint = resolver.resolve_target_site(TargetSiteRequest(
                owner_material_uuid=owner, site_uuid=site_uuid, site_name=site_name,
                occupant_material_uuid=moved, require_available=False))
        except Exception:
            unresolved.append(owner)
            continue
        site_keys.append(site_lock_key(endpoint.owner_material_uuid, endpoint.uuid))
    return site_keys, unresolved

def _bind_inventory_resource_plan(
    plan: dict[str, Any], wire: _InventoryExecutionPlanWire, resource_plan: ResourcePlan,
    task: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]], inventory: Any,
) -> dict[str, Any]:
    """在冻结计划声明的准入方式下绑定已校验的库存请求。"""
    if wire.inventory_resource_binding != "pending":
        return plan
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
        raise StoreConflict("持久化执行计划作业必须是数组")
    if any(not isinstance(job, Mapping) for job in jobs):
        raise StoreConflict("持久化执行计划作业成员必须是对象")
    job_node_ids = [str(job.get("workflow_node_uuid") or "") for job in jobs]
    if any(not identity for identity in job_node_ids) or len(job_node_ids) != len(
        set(job_node_ids)
    ):
        raise StoreConflict("持久化执行计划作业节点身份缺失或重复")
    projected = deepcopy(plan["nodes"])
    nodes = {node["uuid"]: node for node in projected}
    by_job = {job["workflow_node_uuid"]: job for job in jobs}
    if not set(by_job) <= set(nodes):
        raise StoreConflict("持久化执行计划作业引用计划外节点")
    if any(
        not isinstance(job.get("param", {}), Mapping)
        or not isinstance(job.get("return_info", {}), Mapping)
        for job in jobs
    ):
        raise StoreConflict("持久化执行计划作业参数或结果必须是对象")
    handles = {handle["uuid"]: handle for handle in plan["handles"]}
    templates = {(h["node_uuid"], h.get("template_handle_uuid")): h for h in handles.values()}
    values: dict[tuple[str, str], Any] = {}
    for node in projected:
        node["param"] = {**node.get("param", {}), **by_job.get(node["uuid"], {}).get("param", {})}
        if node.get("kind") == "material_source":
            material = by_job.get(node["uuid"], {}).get("return_info", {}).get("material")
            if material:
                for h in handles.values():
                    if h["node_uuid"] == node["uuid"] and h["io_type"] == "source":
                        values[(node["uuid"], h["uuid"])] = material
    # 固定点支持输入顺序与拓扑顺序不同的冻结图；只沿显式透传边传播。
    for _ in range(len(nodes) + 1):
        changed = False
        for edge in plan["edges"]:
            if edge.get("source_type") != "ResourceSlot" or edge.get("dependency_only"):
                continue
            value = values.get((edge["source_node_uuid"], edge["source_handle_uuid"]))
            if value is None:
                continue
            target = nodes[edge["target_node_uuid"]]["param"]
            key = final_target_data_key(edge["target_data_key"])
            existing = target.get(key)
            if isinstance(existing, Mapping) and isinstance(value, Mapping) and existing.get("uuid") != value.get("uuid"):
                raise ValueError("物料透传身份与冻结参数冲突")
            if target.get(key) != value:
                target[key] = deepcopy(value)
                changed = True
        for node in projected:
            passthrough = (node.get("meta_data") or {}).get("unilab", {}).get("material_passthrough_handles", {})
            for output_template, input_template in passthrough.items():
                source = templates.get((node["uuid"], output_template))
                target = templates.get((node["uuid"], input_template))
                if source is None or target is None:
                    continue
                value = node["param"].get(final_target_data_key(target["data_key"]))
                identity = (node["uuid"], source["uuid"])
                if value is not None and values.get(identity) != value:
                    values[identity] = deepcopy(value)
                    changed = True
        if not changed:
            break
    snapshot = task.get("workflow_snapshot") or {}
    if not isinstance(snapshot, Mapping):
        raise StoreConflict("持久化工作流快照必须是对象")
    graph = deepcopy(snapshot)
    bindings = graph.setdefault("resource_bindings", {})
    unilab_meta = graph.get("workflow", {}).get("meta_data", {}).get("unilab", {})
    persisted_bindings = unilab_meta.get("resource_bindings", {})
    for alias, selection in reservation_sites(resource_plan).items():
        if inventory is None:
            raise ExecutionPlanBuildError("invalid_resource_plan", "库位预留缺少库存权威")
        site = inventory.resolve_reservation_site(*selection)
        key = site_lock_key(site.owner_material_uuid, site.uuid)
        for source in (persisted_bindings, bindings):
            supplied = source.get(alias)
            if supplied is not None:
                probe = compile_template_resource_plan(
                    {"nodes": [{"uuid": "binding", "resources": [alias]}]}
                )
                actual = bind_station_resource_plan(probe, {alias: supplied}).resources[0].canonical_key
                if actual != key:
                    raise ExecutionPlanBuildError("invalid_resource_plan", "库位预留与冻结身份不一致")
        bindings[alias] = {"canonical_key": key, "kind": "material_site"}
    planned_transfer = None
    for node in projected:
        contract = node.get("action_resource_contract") or {}
        transfer = contract.get("transfer")
        operation = contract.get("operate_in_place") or {}
        if operation:
            if inventory is None:
                raise ExecutionPlanBuildError("invalid_resource_plan", "原位操作缺少库存权威")
            keys = set()
            materials = set()
            if planned_transfer is None:
                planned_transfer = _planned_transfer_resolver(projected, plan["edges"])
            previous_transfer = planned_transfer(node["uuid"])
            planned_parent = _parent_from_transfer(previous_transfer)
            for parameter in operate_in_place_parameters(operation):
                value = node["param"].get(parameter)
                material = (value.get("uuid") or value.get("material_uuid")) if isinstance(value, Mapping) else value
                if not isinstance(material, str) or not material or material in materials:
                    raise ExecutionPlanBuildError("invalid_resource_plan", "原位操作物料必须已解析且互不相同")
                materials.add(material)
                move = previous_transfer(material)
                location = None
                if move is not None:
                    location = {
                        "owner_material_uuid": _transfer_argument(move, "target_owner_param"),
                        "site_uuid": _transfer_argument(move, "target_site_uuid_param"),
                        "site_name": _transfer_argument(move, "target_site_name_param"),
                    }
                material_keys = inventory.plan_operate_in_place_resources(
                    material_uuid=material,
                    device_material_uuid=node.get("material_uuid", ""),
                    planned_location=location,
                    planned_parent=planned_parent,
                )
                keys.update(material_keys)
                # 记下每个库位锁是被哪件物料占着：连续持锁只沿同一件占位物料延续（见 _resource_chains）。
                occupants = node.setdefault("inventory_site_occupants", {})
                for key in material_keys:
                    if canonical_resource_lock_scope(key) == "material_site":
                        occupants["inventory:" + key] = material
        elif transfer:
            transfer_step = contract.get("transfer_step")
            if not transfer_step or transfer_step.get("operation") not in {"pick", "place"}:
                node["resource_default_boundary"] = True
            if inventory is None:
                raise ValueError("转运资源计划缺少库存权威")
            owners = [
                node["param"][transfer[key]]["uuid"]
                for key in ("source_owner_param", "target_owner_param")
                if transfer.get(key) and transfer[key] in node["param"]
            ]
            keys = inventory.plan_transfer_resources(
                owner_uuids=owners,
                executor_uuid=node.get("material_uuid", ""),
                gripper_role=transfer.get("gripper_site_role", ""),
            )
            if not (transfer_step and transfer_step.get("operation") in {"pick", "place"}):
                # 搬运两端只锁端点库位（所属物件不整件独占，见 execution_plan），这里把端点库位锁写进冻结计划，
                # 和派发时按库存解析出的库位锁一致；解析不出库位的一端仍整件锁住所属物件（旧语义）。
                site_keys, unresolved_owners = transfer_endpoint_site_keys(inventory, transfer, node["param"])
                keys = (*keys, *site_keys, *(material_lock_key(owner) for owner in unresolved_owners))
            if transfer_step and transfer_step.get("operation") in {"pick", "place"}:
                # 配对端点必须使用库存 Site 身份，名称不能退化为通用资源锁。
                # source/target 可有同名库位，别名包含 owner 和真实 Site UUID。
                params = node["param"]

                def identity(name: str) -> str:
                    value = params.get(name)
                    if not value:
                        raise ExecutionPlanBuildError("invalid_resource_plan", f"搬运节点 {node['uuid']} 的资源锁参数 {name} 未解析")
                    return (
                        str(value.get("uuid") or value.get("material_uuid"))
                        if isinstance(value, Mapping)
                        else str(value)
                    )

                owner = identity(transfer_step["owner_param"])
                site_value = params[transfer_step["site_param"]]
                side = "source" if transfer_step["operation"] == "pick" else "target"
                uses_uuid = transfer.get(side + "_site_uuid_param") == transfer_step["site_param"] or isinstance(
                    site_value, Mapping
                )
                target = inventory.resolve_target_site(
                    TargetSiteRequest(
                        owner_material_uuid=owner,
                        site_uuid=identity(transfer_step["site_param"]) if uses_uuid else "",
                        site_name="" if uses_uuid else str(site_value),
                        occupant_material_uuid=identity(transfer_step["material_param"]),
                        require_available=False,
                    )
                )
                endpoint_key = site_lock_key(owner, target.uuid)
                owner_key = material_lock_key(owner)
                carrier_keys = [device_lock_key(identity(name)) for name in transfer_step["carrier_params"]]
                # 端点只锁库位：所属物件（台面、转台、设备承载位）不整件独占，和派发时的锁一致。
                keys = (*keys, endpoint_key, *carrier_keys)
                node["transfer_step"] = {
                    "operation": transfer_step["operation"],
                    "material": identity(transfer_step["material_param"]),
                    "endpoint_resource": "inventory:" + endpoint_key,
                    "endpoint_site": "inventory:" + endpoint_key,
                    "carrier_resources": ["inventory:" + key for key in carrier_keys],
                }
        else:
            continue
        inventory_keys = sorted(set(keys))
        node["inventory_resource_lock_keys"] = inventory_keys
        for key in inventory_keys:
            scope = canonical_resource_lock_scope(key)
            if scope is None:
                raise ValueError(f"库存返回了非规范执行资源锁键：{key}")
            alias = f"inventory:{key}"
            bindings[alias] = {"canonical_key": key, "kind": scope}
            node.setdefault("resource_defaults", []).append(alias)
    bound = ExecutionPlanBuilder._resource_plan(
        graph=graph, planned_nodes=projected, planned_edges=plan["edges"], allow_deferred_inventory=False,
    )
    bound = with_hashed_resource_plan_metadata(
        bound,
        {
            _INVENTORY_RESOURCE_KEYS_METADATA: {
                node["uuid"]: list(node.get("inventory_resource_lock_keys", ()))
                for node in projected
                if node.get("inventory_resource_lock_keys")
            }
        },
    )
    plan["resource_plan"] = serialize_resource_plan(bound)
    plan["inventory_resource_binding"] = "bound"
    for node in plan["nodes"]:
        if node.get("kind") == "material_source" and node.get("runtime_material_source"):
            material = by_job.get(node["uuid"], {}).get("return_info", {}).get("material")
            if material:
                node["planned_material_uuid"] = material["uuid"]
        projected_node = nodes[node["uuid"]]
        node["inventory_resource_lock_keys"] = list(
            projected_node.get("inventory_resource_lock_keys", ())
        )
        projection = resource_plan_for_node(bound, node["uuid"])
        node["resource_plan_id"] = bound.plan_id
        node["resource_interval_ids"] = [item["interval_id"] for item in projection["intervals"]]
        node["resource_acquire_set_id"] = next(
            (item["acquire_set_id"] for item in projection["acquire_sets"]), "",
        )
    _validate_execution_plan(plan)
    return plan
