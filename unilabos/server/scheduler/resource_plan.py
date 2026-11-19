"""工作流资源占用区间与静态资源关系图。

这个模块是调度器资源语义的深模块：作者图只需要提供资源别名、节点边和作用域，
调用方即可取得不可变的区间/原子取得集合；资源实例绑定后再由同一个 Interface
完成整站关系合并与有向无环检查。模块不读取数据库、不调用设备、不依赖运行时
Scheduler，因此可以在发布、任务创建和恢复路径复用同一套安全规则。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from unilabos.server.backend.scheduler.resource_keys import (
    canonical_resource_lock_scope,
    device_lock_key,
    is_canonical_generic_resource_lock_key,
    material_lock_key,
    named_resource_lock_key,
)

RESOURCE_PLAN_VERSION = 2
_LEGACY_RESOURCE_PLAN_VERSION = 1
RESOURCE_PLAN_CAPABILITY = "resource_intervals_v1"
STATIC_RESOURCE_DAG_CAPABILITY = "static_resource_dag_v1"
_PLAN_NAMESPACE = uuid5(NAMESPACE_URL, "unilabos:workflow:resource-lock-plan:v1")
_DEVICE_RESOURCE_KINDS = frozenset({"device", "motion", "tool", "robot", "rail"})
_MATERIAL_RESOURCE_KINDS = frozenset({"material", "container", "sample"})


def _resource_kind_scope(kind: str) -> str | None:
    """把绑定角色收敛到规范锁键 scope；未知角色关闭式返回 ``None``。"""

    normalized = kind.strip().lower()
    if normalized in _DEVICE_RESOURCE_KINDS:
        return "device"
    if normalized in _MATERIAL_RESOURCE_KINDS:
        return "material"
    if normalized in {"site", "material_site"}:
        return "material_site"
    if normalized == "resource":
        return "resource"
    return None


def _validate_bound_resource_identity(resource: "CanonicalResource") -> None:
    """验证 kind、canonical_key 与可选实例 UUID 属于同一资源身份。"""

    key_scope = canonical_resource_lock_scope(resource.canonical_key)
    kind_scope = _resource_kind_scope(resource.kind)
    if key_scope is None:
        raise ResourcePlanError(
            "invalid_binding",
            f"资源锁键格式不受支持：{resource.alias}",
            path=f"/resources/{resource.alias}",
        )
    if kind_scope != key_scope:
        raise ResourcePlanError(
            "invalid_binding",
            f"资源绑定 kind 与 canonical_key 不匹配：{resource.alias}",
            path=f"/resources/{resource.alias}",
        )
    instance_uuid = resource.instance_uuid
    if not instance_uuid:
        return
    if key_scope == "device":
        key_identity = resource.canonical_key.removeprefix("/devices/")
    elif key_scope == "material":
        key_identity = resource.canonical_key.split("/")[1]
    elif key_scope == "resource":
        key_identity = resource.canonical_key.removeprefix("resource:")
    else:
        # Site 绑定同时包含 owner 与 site；当前 CanonicalResource 的单个
        # instance_uuid 没有足够字段表达两者，只验证规范键与 kind。
        return
    if key_identity != instance_uuid:
        raise ResourcePlanError(
            "invalid_binding",
            f"资源绑定实例 UUID 与 canonical_key 不匹配：{resource.alias}",
            path=f"/resources/{resource.alias}",
        )


class ResourcePlanError(ValueError):
    """资源计划无法安全生成、绑定或验证。"""

    def __init__(self, code: str, message: str, *, path: str = "/") -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CanonicalResource:
    """一项资源的稳定计划身份。

    ``resource_id`` 是计划内部身份；``canonical_key`` 在 template 阶段是
    ``symbol:<alias>``，在 bound 阶段必须是 Inventory 证明过的实例键。
    """

    resource_id: str
    canonical_key: str
    kind: str
    alias: str
    instance_uuid: str = ""


def _deserialize_canonical_resource(value: Mapping[str, Any]) -> CanonicalResource:
    """恢复资源，并仅规范化旧版 ``kind=resource`` 的严格物理锁键。"""

    canonical_key = str(value["canonical_key"])
    kind = str(value["kind"])
    key_scope = canonical_resource_lock_scope(canonical_key)
    if kind.strip().lower() == "resource" and key_scope in {
        "device",
        "material",
        "material_site",
    }:
        # 旧绑定器在只提供 canonical_key 时会把物理键标成 resource。键本身
        # 已通过严格 grammar，恢复时只规范角色，不改变 plan_id/资源身份。
        kind = key_scope
    return CanonicalResource(
        resource_id=str(value["resource_id"]),
        canonical_key=canonical_key,
        kind=kind,
        alias=str(value["alias"]),
        instance_uuid=str(value.get("instance_uuid") or ""),
    )


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """根或词法资源作用域的冻结边界。"""

    scope_id: str
    kind: str
    resource_ids: tuple[str, ...]
    parent_scope_id: str | None = None
    entry_node_uuid: str = ""
    exit_node_uuid: str = ""
    node_uuids: tuple[str, ...] = ()
    hard_boundary: bool = True
    branch_id: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class ResourceInterval:
    """一项资源从取得到安全释放的实际占用区间。"""

    interval_id: str
    resource_id: str
    acquire_node_uuid: str
    release_node_uuid: str
    node_uuids: tuple[str, ...]
    scope_id: str | None
    workflow_instance_id: str
    branch_id: str
    condition_ids: tuple[str, ...] = ()
    source: str = ""
    physical_state: str = ""
    safe_release: bool = False
    explicit_boundary: bool = False


@dataclass(frozen=True, slots=True)
class AcquireSet:
    """一个全有或全无的资源新增集合。"""

    acquire_set_id: str
    node_uuid: str
    resource_ids: tuple[str, ...]
    preheld_resource_ids: tuple[str, ...] = ()
    atomic: bool = True
    branch_id: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class ResourceRelation:
    """持有资源到后续新增资源的静态取得关系。"""

    relation_id: str
    from_resource_id: str
    to_resource_id: str
    source_interval_id: str = ""
    source_node_uuid: str = ""
    branch_id: str = ""
    possible_concurrency: bool = True
    reason: str = "hold_then_acquire"


@dataclass(frozen=True, slots=True)
class ResourcePlan:
    """可持久化的资源计划；所有集合均按稳定顺序冻结。"""

    plan_id: str
    version: int = RESOURCE_PLAN_VERSION
    binding_state: str = "template"
    capabilities: tuple[str, ...] = (RESOURCE_PLAN_CAPABILITY,)
    resources: tuple[CanonicalResource, ...] = ()
    scopes: tuple[ResourceScope, ...] = ()
    intervals: tuple[ResourceInterval, ...] = ()
    acquire_sets: tuple[AcquireSet, ...] = ()
    relations: tuple[ResourceRelation, ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


class _StrictWireModel(BaseModel):
    """持久化资源计划的关闭式 wire 基类。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class _CanonicalResourceWire(_StrictWireModel):
    resource_id: str
    canonical_key: str
    kind: str
    alias: str
    instance_uuid: str = ""


class _ResourceScopeWire(_StrictWireModel):
    scope_id: str
    kind: str
    resource_ids: list[str] = Field(default_factory=list)
    parent_scope_id: str | None = None
    entry_node_uuid: str = ""
    exit_node_uuid: str = ""
    node_uuids: list[str] = Field(default_factory=list)
    hard_boundary: bool = True
    branch_id: str = ""
    source: str = ""


class _ResourceIntervalWire(_StrictWireModel):
    interval_id: str
    resource_id: str
    acquire_node_uuid: str
    release_node_uuid: str
    node_uuids: list[str] = Field(default_factory=list)
    scope_id: str | None = None
    workflow_instance_id: str = ""
    branch_id: str = ""
    condition_ids: list[str] = Field(default_factory=list)
    source: str = ""
    physical_state: str = ""
    safe_release: bool = False
    explicit_boundary: bool = False


class _AcquireSetWire(_StrictWireModel):
    acquire_set_id: str
    node_uuid: str
    resource_ids: list[str] = Field(default_factory=list)
    preheld_resource_ids: list[str] = Field(default_factory=list)
    atomic: bool = True
    branch_id: str = ""
    source: str = ""


class _ResourceRelationWire(_StrictWireModel):
    relation_id: str
    from_resource_id: str
    to_resource_id: str
    source_interval_id: str = ""
    source_node_uuid: str = ""
    branch_id: str = ""
    possible_concurrency: bool = True
    reason: str = "hold_then_acquire"


class _ResourcePlanWire(_StrictWireModel):
    # 没有显式版本的历史计划只按 v1 读取，不能隐式升级为内容寻址 v2。
    version: int = _LEGACY_RESOURCE_PLAN_VERSION
    plan_id: str
    binding_state: str = "template"
    capabilities: list[str] = Field(
        default_factory=lambda: [RESOURCE_PLAN_CAPABILITY]
    )
    resources: list[_CanonicalResourceWire] = Field(default_factory=list)
    scopes: list[_ResourceScopeWire] = Field(default_factory=list)
    intervals: list[_ResourceIntervalWire] = Field(default_factory=list)
    acquire_sets: list[_AcquireSetWire] = Field(default_factory=list)
    relations: list[_ResourceRelationWire] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _is_verified_legacy_resource_plan_wire(wire: _ResourcePlanWire) -> bool:
    """仅识别旧编译器实际生成、可安全套用兼容归一化的 v1 wire。"""

    if wire.version != _LEGACY_RESOURCE_PLAN_VERSION:
        return False
    try:
        if UUID(wire.plan_id).version != 5:
            return False
    except ValueError:
        return False
    if set(wire.metadata) != {"workflow_instance_id"} or not isinstance(
        wire.metadata["workflow_instance_id"], str
    ):
        return False
    if not wire.metadata["workflow_instance_id"] or wire.diagnostics:
        return False
    expected_capabilities = {RESOURCE_PLAN_CAPABILITY}
    if wire.binding_state == "bound":
        expected_capabilities.add(STATIC_RESOURCE_DAG_CAPABILITY)
    if set(wire.capabilities) != expected_capabilities:
        return False
    if any(
        resource.resource_id
        != str(uuid5(_PLAN_NAMESPACE, f"resource:{resource.alias}"))
        for resource in wire.resources
    ):
        return False
    if any(
        interval.interval_id
        != str(
            uuid5(
                _PLAN_NAMESPACE,
                "interval:"
                f"{interval.resource_id}:{interval.acquire_node_uuid}:"
                f"{interval.release_node_uuid}:{interval.branch_id}:"
                f"{interval.scope_id or ''}",
            )
        )
        or interval.physical_state not in {"", "unknown"}
        or interval.safe_release
        for interval in wire.intervals
    ):
        return False
    if any(
        acquire_set.acquire_set_id
        != str(
            uuid5(
                _PLAN_NAMESPACE,
                f"acquire:{acquire_set.node_uuid}:{acquire_set.branch_id}",
            )
        )
        for acquire_set in wire.acquire_sets
    ):
        return False
    for relation in wire.relations:
        if relation.reason == "hold_then_acquire":
            identity = (
                f"relation:{relation.from_resource_id}:{relation.to_resource_id}:"
                f"{relation.source_node_uuid}:{relation.branch_id}"
            )
        elif relation.reason == "possible_concurrent_workflow":
            identity = (
                f"concurrent:{relation.from_resource_id}:{relation.to_resource_id}"
            )
        else:
            return False
        if relation.relation_id != str(uuid5(_PLAN_NAMESPACE, identity)):
            return False
    return True


def compile_template_resource_plan(
    graph: Mapping[str, Any],
    *,
    root_scopes: Sequence[Mapping[str, Any]] | None = None,
    _defer_cycle_validation: bool = False,
) -> ResourcePlan:
    """从冻结工作流图计算符号资源区间和取得关系。

    图可通过顶层 ``resource_scopes``、``resources`` 提供声明，也可放在
    ``workflow.meta_data.unilab``。节点资源支持 ``resource_defaults``、
    ``resources`` 或 ``meta_data.unilab.resource_defaults``。资源仍是别名，
    不在此处猜测设备/Site UUID；实例绑定必须经过
    :func:`bind_station_resource_plan`。
    """

    if not isinstance(graph, Mapping):
        raise ResourcePlanError("invalid_graph", "资源计划输入图必须是对象")
    raw_nodes = graph.get("nodes", [])
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
        raise ResourcePlanError("invalid_graph", "资源计划 nodes 必须是数组", path="/nodes")
    nodes: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("uuid"), str):
            raise ResourcePlanError("invalid_node", "资源计划节点缺少 uuid", path=f"/nodes/{index}")
        node_uuid = str(raw["uuid"])
        if node_uuid in nodes:
            raise ResourcePlanError("duplicate_node", f"节点 UUID 重复：{node_uuid}")
        nodes[node_uuid] = dict(raw)
        if (raw.get("action_resource_contract") or {}).get("order_sensitive"):
            nodes[node_uuid]["order_sensitive"] = True
    edges = _normalize_edges(graph.get("edges", []), nodes)
    order = _topological_order(nodes, edges)
    transfers = _compile_transfer_pairs(nodes, order, edges)
    workflow = graph.get("workflow")
    workflow_meta = workflow.get("meta_data") if isinstance(workflow, Mapping) else None
    unilab_meta = workflow_meta.get("unilab") if isinstance(workflow_meta, Mapping) else None
    if not isinstance(unilab_meta, Mapping):
        unilab_meta = {}
    declarations = root_scopes
    if declarations is None:
        declarations = graph.get("resource_scopes")
    if declarations is None:
        declarations = unilab_meta.get("resource_scopes")
    if declarations is None:
        declarations = []
    if not isinstance(declarations, Sequence) or isinstance(declarations, (str, bytes)):
        raise ResourcePlanError(
            "invalid_scope", "resource_scopes 必须是数组", path="/resource_scopes"
        )
    raw_root_resources = graph.get("resources")
    if raw_root_resources is None:
        raw_root_resources = unilab_meta.get("resources")
    if raw_root_resources is not None:
        raw_root_resources = _string_sequence(raw_root_resources, "/resources")
        # 根资源与词法 ``with resources(...)`` 是两类可叠加的声明：根作用域
        # 覆盖整个 Workflow，词法作用域在其内部建立更窄的硬边界。根声明统一
        # 放到列表首部，保证确定性的父级语义与序列化顺序。
        declarations = [
            {
                "scope_id": "root",
                "kind": "root",
                "resources": list(raw_root_resources),
                "node_uuids": list(order),
                "source": "workflow.root",
            },
            *list(declarations),
        ]

    resource_aliases: list[str] = []
    scope_inputs: list[dict[str, Any]] = []
    for index, raw_scope in enumerate(declarations):
        if not isinstance(raw_scope, Mapping):
            raise ResourcePlanError(
                "invalid_scope", "资源作用域必须是对象", path=f"/resource_scopes/{index}"
            )
        scope_kind = str(raw_scope.get("kind") or ("root" if index == 0 else "with"))
        if scope_kind not in {"root", "with", "action", "transfer"}:
            raise ResourcePlanError("invalid_scope_kind", f"不支持资源作用域类型：{scope_kind}")
        aliases = _string_sequence(
            raw_scope.get("resources", raw_scope.get("resource_aliases", [])),
            f"/resource_scopes/{index}/resources",
        )
        if not aliases:
            raise ResourcePlanError(
                "empty_scope", "资源作用域不能为空", path=f"/resource_scopes/{index}/resources"
            )
        resource_aliases.extend(aliases)
        node_uuids = tuple(
            _string_sequence(
                raw_scope.get("node_uuids", []), f"/resource_scopes/{index}/node_uuids"
            )
        )
        unknown_nodes = set(node_uuids) - set(nodes)
        if unknown_nodes:
            raise ResourcePlanError(
                "unknown_scope_node", f"作用域引用未知节点：{sorted(unknown_nodes)}"
            )
        scope_inputs.append(
            {
                "scope_id": str(raw_scope.get("scope_id") or f"scope-{index + 1}"),
                "kind": scope_kind,
                "aliases": aliases,
                "node_uuids": node_uuids,
                "entry_node_uuid": str(raw_scope.get("entry_node_uuid") or ""),
                "exit_node_uuid": str(raw_scope.get("exit_node_uuid") or ""),
                "parent_scope_id": (
                    str(raw_scope["parent_scope_id"])
                    if raw_scope.get("parent_scope_id") is not None
                    else None
                ),
                "branch_id": str(raw_scope.get("branch_id") or ""),
                "source": str(raw_scope.get("source") or f"resource_scopes[{index}]"),
            }
        )
    node_aliases = {
        node_uuid: _node_resource_aliases(node)
        for node_uuid, node in nodes.items()
    }
    for raw_scope in scope_inputs:
        members = _scope_node_members(raw_scope, order, nodes, edges)
        raw_scope["members"] = tuple(members)
        if raw_scope["kind"] in {"root", "with"}:
            # 连续作用域在入口前必须取得其中所有动作的完整资源集合；只把
            # 作者显式别名放进 Scope 会让后续设备、物料或 Site 在区间中途
            # 动态补锁，既不连续也会重新引入 hold-and-wait。
            raw_scope["aliases"] = tuple(
                dict.fromkeys(
                    [
                        *raw_scope["aliases"],
                        *(
                            alias
                            for node_uuid in members
                            for alias in node_aliases[node_uuid]
                        ),
                    ]
                )
            )

    # 子作用域可以声明没有直接附着到动作节点的命名资源。沿显式父链把这些
    # 资源提升到连续祖先；根作用域作为整图连续所有者，同时覆盖所有成员位于
    # 根内的词法作用域。固定点支持多层组合工作流嵌套。
    changed = True
    while changed:
        changed = False
        for scope in scope_inputs:
            if scope["kind"] not in {"root", "with"}:
                continue
            scope_members = set(scope["members"])
            merged = list(scope["aliases"])
            for child in scope_inputs:
                if child is scope:
                    continue
                direct_child = child.get("parent_scope_id") == scope["scope_id"]
                root_child = (
                    scope["kind"] == "root"
                    and set(child["members"]) <= scope_members
                )
                if not direct_child and not root_child:
                    continue
                merged.extend(child["aliases"])
            normalized = tuple(dict.fromkeys(merged))
            if normalized != scope["aliases"]:
                scope["aliases"] = normalized
                changed = True

    for aliases_for_node in node_aliases.values():
        resource_aliases.extend(aliases_for_node)
    for raw_scope in scope_inputs:
        resource_aliases.extend(raw_scope["aliases"])
    aliases = tuple(sorted(set(resource_aliases)))
    resource_by_alias = {
        alias: CanonicalResource(
            resource_id=str(uuid5(_PLAN_NAMESPACE, f"resource:{alias}")),
            canonical_key=f"symbol:{alias}",
            kind="symbolic",
            alias=alias,
        )
        for alias in aliases
    }
    scopes: list[ResourceScope] = []
    scope_membership: dict[str, list[tuple[str, bool, str | None]]] = defaultdict(list)
    for raw_scope in scope_inputs:
        members = list(raw_scope["members"])
        entry = raw_scope["entry_node_uuid"] or (members[0] if members else "")
        exit_node = raw_scope["exit_node_uuid"] or (members[-1] if members else "")
        if not entry or not exit_node:
            raise ResourcePlanError("empty_scope", f"作用域 {raw_scope['scope_id']} 没有可执行节点")
        scope = ResourceScope(
            scope_id=raw_scope["scope_id"],
            kind=raw_scope["kind"],
            resource_ids=tuple(
                sorted(resource_by_alias[a].resource_id for a in raw_scope["aliases"])
            ),
            parent_scope_id=raw_scope["parent_scope_id"],
            entry_node_uuid=entry,
            exit_node_uuid=exit_node,
            node_uuids=tuple(members),
            hard_boundary=True,
            branch_id=raw_scope["branch_id"],
            source=raw_scope["source"],
        )
        scopes.append(scope)
        for node_uuid in members:
            for resource_id in scope.resource_ids:
                scope_membership[node_uuid].append((resource_id, True, scope.scope_id))

    effective_by_node: dict[str, tuple[str, ...]] = {}
    source_by_node: dict[str, list[str]] = defaultdict(list)
    for node_uuid in order:
        node = nodes[node_uuid]
        resources = {resource_by_alias[a].resource_id for a in _node_resource_aliases(node)}
        for resource_id, _explicit, scope_id in scope_membership.get(node_uuid, []):
            resources.add(resource_id)
            source_by_node[node_uuid].append(scope_id or "scope")
        effective_by_node[node_uuid] = tuple(sorted(resources))
        if _node_resource_aliases(node):
            source_by_node[node_uuid].append("action.default")

    diagnostics: list[Mapping[str, Any]] = []
    reach = _reachability(order, edges)
    branch_by_node = {node_uuid: _branch_id(nodes[node_uuid]) for node_uuid in order}
    intervals: list[ResourceInterval] = []
    interval_by_node_resource: dict[tuple[str, str], ResourceInterval] = {}
    for resource_id in sorted({r for values in effective_by_node.values() for r in values}):
        uses = [node_uuid for node_uuid in order if resource_id in effective_by_node[node_uuid]]
        chains = _resource_chains(
            resource_id,
            uses,
            nodes,
            edges,
            scopes,
            reach,
            diagnostics,
            next(r.alias for r in resource_by_alias.values() if r.resource_id == resource_id),
        )
        for chain in chains:
            first, last = chain[0], chain[-1]
            scope_id = _common_scope_id(chain, scopes, resource_id)
            explicit_boundary = bool(scope_id)
            interval_id = str(
                uuid5(
                    _PLAN_NAMESPACE,
                    f"interval:{resource_id}:{first}:{last}:{branch_by_node[first]}:{scope_id or ''}",
                )
            )
            interval_sources = sorted(
                {source for node_uuid in chain for source in source_by_node[node_uuid]}
            )
            interval = ResourceInterval(
                interval_id=interval_id,
                resource_id=resource_id,
                acquire_node_uuid=first,
                release_node_uuid=last,
                node_uuids=tuple(chain),
                scope_id=scope_id,
                workflow_instance_id=_workflow_instance_id(graph),
                branch_id=branch_by_node[first],
                source=",".join(interval_sources),
                physical_state="unknown",
                safe_release=False,
                explicit_boundary=explicit_boundary,
            )
            intervals.append(interval)
            for node_uuid in chain:
                interval_by_node_resource[(node_uuid, resource_id)] = interval

    # 物理持料状态延续到配对 place，不能只验证 pick 的第一个后继。
    for transfer in transfers:
        pick, place = transfer["pick_node_uuid"], transfer["place_node_uuid"]
        for alias in [
            *transfer["carrier_resources"],
            transfer["target_resource"],
            transfer["target_site"],
        ]:
            resource_id = resource_by_alias[alias].resource_id
            if (
                interval_by_node_resource[(pick, resource_id)]
                is not interval_by_node_resource[(place, resource_id)]
            ):
                raise ResourcePlanError(
                    "unsafe_resource_handoff",
                    f"{pick} 到 {place} 的搬运资源 {alias} 在放料前不可安全交接；请扩大显式范围或先放到稳定 Site",
                )

    acquire_sets: list[AcquireSet] = []
    relations: list[ResourceRelation] = []
    # 对每个实际入口计算仍持有的区间；不能用拓扑排序的“上一个节点”
    # 代替因果关系，兄弟节点没有先后关系。
    for node_uuid in order:
        effective = set(effective_by_node[node_uuid])
        preheld_intervals = [
            item
            for item in intervals
            if item.acquire_node_uuid != node_uuid
            and (
                node_uuid in reach[item.acquire_node_uuid]
                or (
                    item.explicit_boundary
                    and any(
                        member != node_uuid and node_uuid in reach[member]
                        for member in item.node_uuids
                    )
                )
            )
            and (
                node_uuid in item.node_uuids
                or any(member in reach[node_uuid] for member in item.node_uuids)
            )
        ]
        preheld = tuple(sorted({item.resource_id for item in preheld_intervals}))
        new_resources = tuple(sorted(effective - set(preheld)))
        if new_resources:
            acquire_sets.append(
                AcquireSet(
                    acquire_set_id=str(uuid5(_PLAN_NAMESPACE, f"acquire:{node_uuid}")),
                    node_uuid=node_uuid,
                    resource_ids=new_resources,
                    preheld_resource_ids=preheld,
                    atomic=True,
                    branch_id=branch_by_node[node_uuid],
                    source=",".join(sorted(set(source_by_node[node_uuid]))),
                )
            )
            for held in preheld_intervals:
                for target in new_resources:
                    relations.append(
                        ResourceRelation(
                            relation_id=str(
                                uuid5(
                                    _PLAN_NAMESPACE,
                                    f"relation:{held.interval_id}:{target}:{node_uuid}",
                                )
                            ),
                            from_resource_id=held.resource_id,
                            to_resource_id=target,
                            source_interval_id=held.interval_id,
                            source_node_uuid=node_uuid,
                            branch_id=branch_by_node[node_uuid],
                            reason=held.source,
                        )
                    )

    # 任一兄弟可能先进入共同祖先；因此另一个分支的新增资源也必须进入
    # hold→acquire 图，不能因为两个入口没有直接依赖而漏掉反向取得环。
    scope_by_id = {scope.scope_id: scope for scope in scopes}
    for held in intervals:
        if not held.explicit_boundary:
            continue
        common = scope_by_id.get(held.scope_id or "")
        atomic_peers = set(common.resource_ids) if common is not None else {held.resource_id}
        # 子作用域开始时，其父链资源已经按词法作用域持有；这些资源不是子节点
        # 的“后续新增”目标。若仍生成 child→parent 关系，会与合法的
        # parent→child 取得次序构成假环，导致展开后的组合工作流无法编译。
        ancestor = scope_by_id.get(common.parent_scope_id or "") if common else None
        visited_ancestors: set[str] = set()
        while ancestor is not None and ancestor.scope_id not in visited_ancestors:
            visited_ancestors.add(ancestor.scope_id)
            atomic_peers.update(ancestor.resource_ids)
            ancestor = scope_by_id.get(ancestor.parent_scope_id or "")
        for node_uuid in held.node_uuids:
            if not any(
                other != node_uuid
                and other not in reach[node_uuid]
                and node_uuid not in reach[other]
                for other in held.node_uuids
            ):
                continue
            for target in set(effective_by_node[node_uuid]) - atomic_peers:
                target_interval = interval_by_node_resource[(node_uuid, target)]
                if (
                    target_interval.acquire_node_uuid != node_uuid
                    and node_uuid in reach[target_interval.acquire_node_uuid]
                ):
                    continue  # 共同祖先已原子取得的成员没有后续新增边。
                if any(
                    r.from_resource_id == held.resource_id
                    and r.to_resource_id == target
                    and r.source_node_uuid == node_uuid
                    for r in relations
                ):
                    continue
                relations.append(
                    ResourceRelation(
                        relation_id=str(
                            uuid5(
                                _PLAN_NAMESPACE,
                                f"scope-relation:{held.interval_id}:{node_uuid}:{target}",
                            )
                        ),
                        from_resource_id=held.resource_id,
                        to_resource_id=target,
                        source_interval_id=held.interval_id,
                        source_node_uuid=node_uuid,
                        branch_id=branch_by_node[node_uuid],
                        reason="shared_ancestor_scope",
                    )
                )

    workflow_id = _workflow_instance_id(graph)
    plan_id = str(uuid5(_PLAN_NAMESPACE, f"plan:{workflow_id}:{','.join(order)}"))
    plan = ResourcePlan(
        plan_id=plan_id,
        binding_state="template",
        capabilities=(RESOURCE_PLAN_CAPABILITY,),
        resources=tuple(resource_by_alias[a] for a in aliases),
        scopes=tuple(sorted(scopes, key=lambda item: item.scope_id)),
        intervals=tuple(sorted(intervals, key=lambda item: item.interval_id)),
        acquire_sets=tuple(sorted(acquire_sets, key=lambda item: item.acquire_set_id)),
        relations=tuple(sorted(relations, key=lambda item: item.relation_id)),
        diagnostics=tuple(diagnostics),
        metadata={
            "workflow_instance_id": workflow_id,
            "template_graph": dict(graph),
            "transfers": transfers,
            "active_resource_ids_by_node": {
                node_uuid: sorted(
                    resource_by_alias[alias].resource_id
                    for alias in _node_resource_aliases(nodes[node_uuid])
                )
                for node_uuid in order
                if _node_resource_aliases(nodes[node_uuid])
            },
            "physical_hold_nodes": {
                node_uuid: list(node.get("physical_hold_resources", ()))
                for node_uuid, node in nodes.items()
                if node.get("physical_hold_resources")
            },
            "dependency_edges": list(edges),
        },
    )
    plan = _identify_plan(plan)
    try:
        validate_resource_plan(plan)
    except ResourcePlanError as error:
        # 只允许即将绑定的内部调用推迟符号环检查；绑定后必须重新验证。
        if not _defer_cycle_validation or error.code != "resource_cycle":
            raise
    return plan


def bind_station_resource_plan(
    template_plan: ResourcePlan,
    resource_bindings: Mapping[str, Any],
    *,
    concurrency: Sequence[ResourcePlan | Mapping[str, Any]] = (),
) -> ResourcePlan:
    """把符号资源绑定为 Inventory 已证明的具体实例并检查整站 DAG。

    ``resource_bindings`` 的键是作者别名，值可以是 UUID 字符串，或包含
    ``instance_uuid``、``canonical_key``、``kind`` 的映射。模块不负责发现这些
    值；调用者必须从 ``StationResourceInventory`` 的公开 Interface 提供它们。
    """

    if not isinstance(template_plan, ResourcePlan):
        raise ResourcePlanError("invalid_plan", "绑定输入必须是 ResourcePlan")
    if template_plan.binding_state not in {"template", "bound"}:
        raise ResourcePlanError("invalid_binding_state", "资源计划绑定状态无效")
    if not isinstance(resource_bindings, Mapping):
        raise ResourcePlanError("invalid_binding", "resource_bindings 必须是对象")
    named_resource_ids = {
        resource_id
        for scope in template_plan.scopes
        if scope.kind in {"root", "with"}
        for resource_id in scope.resource_ids
    }
    bound_resources: list[CanonicalResource] = []
    for resource in template_plan.resources:
        raw = resource_bindings.get(resource.alias)
        if raw is None:
            if resource.resource_id not in named_resource_ids:
                raise ResourcePlanError(
                    "resource_unbound",
                    f"资源别名未绑定到具体实例：{resource.alias}",
                    path=f"/resources/{resource.alias}",
                )
            canonical_key = named_resource_lock_key(resource.alias)
            instance_uuid = canonical_key.removeprefix("resource:")
            kind = "resource"
        elif isinstance(raw, Mapping):
            instance_uuid = str(raw.get("instance_uuid") or raw.get("uuid") or "").strip()
            canonical_key = str(raw.get("canonical_key") or "").strip()
            raw_kind = raw.get("kind")
            kind = str(raw_kind).strip().lower() if raw_kind is not None else ""
        else:
            instance_uuid = str(raw).strip()
            canonical_key = ""
            kind = "resource"
        if not instance_uuid and not canonical_key:
            raise ResourcePlanError("invalid_binding", f"资源绑定缺少实例身份：{resource.alias}")
        if instance_uuid:
            try:
                instance_uuid = str(UUID(instance_uuid))
            except ValueError as error:
                raise ResourcePlanError(
                    "invalid_binding", f"资源 UUID 无效：{resource.alias}"
                ) from error
        if canonical_key and not kind:
            kind = canonical_resource_lock_scope(canonical_key) or ""
        if not canonical_key:
            kind = kind or "resource"
            canonical_key = f"{kind}:{instance_uuid}"
        # 与运行时物理互斥键一致；device/motion/tool 只是角色，不是不同实例。
        if not canonical_key.startswith("/") and instance_uuid:
            if kind.lower() in {"device", "motion", "tool", "robot", "rail"}:
                canonical_key = device_lock_key(instance_uuid)
            elif kind.lower() in {"material", "container", "sample"}:
                canonical_key = material_lock_key(instance_uuid)
        if canonical_key.startswith("resource:") and not is_canonical_generic_resource_lock_key(
            canonical_key
        ):
            raise ResourcePlanError(
                "invalid_binding",
                f"通用资源锁键不是规范 resource:<UUID>：{resource.alias}",
                path=f"/resources/{resource.alias}",
            )
        bound_resource = CanonicalResource(
            resource_id=resource.resource_id,
            canonical_key=canonical_key,
            kind=kind,
            alias=resource.alias,
            instance_uuid=instance_uuid,
        )
        _validate_bound_resource_identity(bound_resource)
        bound_resources.append(bound_resource)
    plan = ResourcePlan(
        plan_id=template_plan.plan_id,
        binding_state="bound",
        capabilities=tuple(
            sorted(set(template_plan.capabilities) | {STATIC_RESOURCE_DAG_CAPABILITY})
        ),
        resources=tuple(bound_resources),
        scopes=template_plan.scopes,
        intervals=template_plan.intervals,
        acquire_sets=template_plan.acquire_sets,
        relations=template_plan.relations,
        diagnostics=template_plan.diagnostics,
        metadata={
            key: value for key, value in template_plan.metadata.items() if key != "template_graph"
        },
    )
    # 别名必须在实例绑定后合并，再重新计算区间与关系，避免同一物理设备
    # 以两个符号身份绕过重入和无环检查。
    keys = [item.canonical_key for item in bound_resources]
    if len(keys) != len(set(keys)):
        raw_graph = template_plan.metadata.get("template_graph")
        if not isinstance(raw_graph, Mapping):
            raise ResourcePlanError("alias_recompile_required", "同实例别名合并需要原始模板图")
        aliases = {}
        first_by_key = {}
        for item in bound_resources:
            aliases[item.alias] = first_by_key.setdefault(item.canonical_key, item.alias)
        from copy import deepcopy

        graph = deepcopy(dict(raw_graph))
        graph_nodes = {str(node["uuid"]): node for node in graph.get("nodes", [])}
        graph_edges = _normalize_edges(graph.get("edges", []), graph_nodes)
        _compile_transfer_pairs(
            graph_nodes, _topological_order(graph_nodes, graph_edges), graph_edges
        )
        # 将合同资源投影成统一别名后再合并；参数名本身不是资源身份。
        for node in graph.get("nodes", []):
            node["resource_defaults"] = list(_node_resource_aliases(node))
            contract = node.get("action_resource_contract")
            if isinstance(contract, dict):
                contract.pop("resource_params", None)
                contract.pop("required_device_params", None)
                contract.pop("resource_aliases", None)
                transfer = contract.get("transfer")
                if isinstance(transfer, dict):
                    transfer.pop("motion_resource_roles", None)
                    transfer.pop("tool_resource_roles", None)

        def remap(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in list(value.items()):
                    if key in {
                        "resources",
                        "resource_defaults",
                        "resource_aliases",
                        "physical_hold_resources",
                        "carrier_resources",
                    } and isinstance(child, (list, tuple)):
                        value[key] = list(dict.fromkeys(aliases.get(x, x) for x in child))
                    elif key in {"endpoint_resource", "endpoint_site"} and isinstance(child, str):
                        value[key] = aliases.get(child, child)
                    else:
                        remap(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    remap(child)

        remap(graph)
        return bind_station_resource_plan(
            compile_template_resource_plan(graph, _defer_cycle_validation=True),
            resource_bindings,
            concurrency=concurrency,
        )
    material_aliases = {
        item.alias
        for item in bound_resources
        if _resource_kind_scope(item.kind) == "material"
    }
    if material_aliases:
        raw_graph = template_plan.metadata.get("template_graph")
        if not isinstance(raw_graph, Mapping):
            raise ResourcePlanError(
                "material_boundary_recompile_required",
                "物料释放边界需要原始模板图",
            )
        from copy import deepcopy

        graph = deepcopy(dict(raw_graph))
        physical_hold_nodes = template_plan.metadata.get("physical_hold_nodes") or {}
        protected_by_node: dict[str, set[str]] = defaultdict(set)
        transfer_aliases: set[str] = set()

        def bound_aliases_for(reference: Any) -> set[str]:
            """把 transfer 的参数值/实例 UUID 还原到模板资源别名。"""

            text = str(reference or "")
            if not text:
                return set()
            aliases = {text}
            aliases.update(
                resource.alias
                for resource in bound_resources
                if text
                in {
                    resource.alias,
                    resource.instance_uuid,
                    resource.canonical_key,
                }
            )
            return aliases

        for transfer in template_plan.metadata.get("transfers", ()):
            if not isinstance(transfer, Mapping):
                continue
            aliases = set().union(
                *(
                    bound_aliases_for(reference)
                    for reference in (
                        transfer.get("material"),
                        transfer.get("target_resource"),
                        transfer.get("target_site"),
                        *(transfer.get("carrier_resources") or ()),
                    )
                )
            )
            transfer_aliases.update(aliases)
            for field in ("pick_node_uuid", "place_node_uuid"):
                node_uuid = str(transfer.get(field) or "")
                if node_uuid:
                    protected_by_node[node_uuid].update(aliases)
        # 没有成对 transfer 证明的 physical hold 无法推断安全释放点，继续全图
        # 保守续持；已配对搬运则只保护 pick/place，之后的普通动作恢复单 Job 边界。
        globally_protected_aliases: set[str] = set()
        if isinstance(physical_hold_nodes, Mapping):
            for node_uuid, raw_aliases in physical_hold_nodes.items():
                if not isinstance(raw_aliases, Sequence) or isinstance(
                    raw_aliases,
                    (str, bytes),
                ):
                    continue
                aliases = set().union(
                    *(bound_aliases_for(alias) for alias in raw_aliases)
                )
                protected_by_node[str(node_uuid)].update(aliases)
                globally_protected_aliases.update(aliases - transfer_aliases)
        changed = False
        for node in graph.get("nodes", ()):
            if not isinstance(node, MutableMapping):
                continue
            node_uuid = str(node.get("uuid") or "")
            releasable = (
                set(_node_resource_aliases(node))
                & material_aliases
                - protected_by_node.get(node_uuid, set())
                - globally_protected_aliases
            )
            if not releasable:
                continue
            existing = node.get("resource_default_boundaries", ())
            existing_aliases = set(
                _string_sequence(
                    existing,
                    f"/nodes/{node.get('uuid', '')}/resource_default_boundaries",
                )
            )
            combined = existing_aliases | releasable
            if combined != existing_aliases:
                node["resource_default_boundaries"] = sorted(combined)
                changed = True
        if changed:
            return bind_station_resource_plan(
                compile_template_resource_plan(graph),
                resource_bindings,
                concurrency=concurrency,
            )
    if concurrency:
        plan = _merge_concurrent_relations(plan, concurrency)
    plan = _identify_plan(plan)
    validate_resource_plan(plan)
    return plan


def validate_resource_plan(plan: ResourcePlan) -> None:
    """验证资源计划引用完整、取得原子且关系图无环。"""

    if not isinstance(plan, ResourcePlan):
        raise ResourcePlanError("invalid_plan", "资源计划必须是 ResourcePlan")
    if plan.version not in {
        _LEGACY_RESOURCE_PLAN_VERSION,
        RESOURCE_PLAN_VERSION,
    }:
        raise ResourcePlanError("unsupported_plan_version", "资源计划版本不支持")
    if plan.binding_state not in {"template", "bound"}:
        raise ResourcePlanError("invalid_binding_state", "资源计划 binding_state 无效")
    if RESOURCE_PLAN_CAPABILITY not in plan.capabilities:
        raise ResourcePlanError("unsupported_plan_capability", "资源计划缺少资源区间能力")
    if plan.binding_state == "bound" and STATIC_RESOURCE_DAG_CAPABILITY not in plan.capabilities:
        raise ResourcePlanError("unsupported_plan_capability", "bound 资源计划缺少静态无环证明能力")
    resource_ids = [item.resource_id for item in plan.resources]
    if len(resource_ids) != len(set(resource_ids)):
        raise ResourcePlanError("duplicate_resource", "资源计划包含重复 resource_id")
    if plan.version == _LEGACY_RESOURCE_PLAN_VERSION and any(
        canonical_resource_lock_scope(resource.canonical_key) == "resource"
        for resource in plan.resources
    ):
        raise ResourcePlanError(
            "unsupported_legacy_resource",
            "v1 资源计划不支持通用命名资源键",
            path="/resources",
        )
    if plan.binding_state == "bound":
        canonical_keys = [item.canonical_key for item in plan.resources]
        if any(not key or key.startswith("symbol:") for key in canonical_keys):
            raise ResourcePlanError("resource_unbound", "bound 资源计划仍含符号资源")
        for resource in plan.resources:
            _validate_bound_resource_identity(resource)
    valid_resources = set(resource_ids)
    scope_ids = {scope.scope_id for scope in plan.scopes}
    if len(scope_ids) != len(plan.scopes):
        raise ResourcePlanError("duplicate_scope", "资源计划包含重复 scope_id")
    for scope in plan.scopes:
        if not set(scope.resource_ids) <= valid_resources:
            raise ResourcePlanError(
                "unknown_scope_resource", f"作用域 {scope.scope_id} 引用未知资源"
            )
        if scope.parent_scope_id and scope.parent_scope_id not in scope_ids:
            raise ResourcePlanError(
                "unknown_parent_scope", f"作用域 {scope.scope_id} 的父作用域不存在"
            )
    for scope_id in scope_ids:
        seen_scopes: set[str] = set()
        current = scope_id
        while current:
            if current in seen_scopes:
                raise ResourcePlanError("scope_cycle", f"资源作用域父级关系成环：{scope_id}")
            seen_scopes.add(current)
            parent = next(
                (scope.parent_scope_id for scope in plan.scopes if scope.scope_id == current),
                None,
            )
            current = parent or ""
    interval_ids = {interval.interval_id for interval in plan.intervals}
    if len(interval_ids) != len(plan.intervals):
        raise ResourcePlanError("duplicate_interval", "资源计划包含重复 interval_id")
    for interval in plan.intervals:
        if interval.resource_id not in valid_resources:
            raise ResourcePlanError(
                "unknown_interval_resource", f"区间 {interval.interval_id} 引用未知资源"
            )
        if (
            not interval.acquire_node_uuid
            or not interval.release_node_uuid
            or not interval.node_uuids
        ):
            raise ResourcePlanError("invalid_interval", f"区间 {interval.interval_id} 缺少节点边界")
        if interval.scope_id and interval.scope_id not in scope_ids:
            raise ResourcePlanError(
                "unknown_interval_scope", f"区间 {interval.interval_id} 引用未知作用域"
            )
    acquire_ids = {item.acquire_set_id for item in plan.acquire_sets}
    if len(acquire_ids) != len(plan.acquire_sets):
        raise ResourcePlanError("duplicate_acquire_set", "资源计划包含重复 acquire_set_id")
    for acquire_set in plan.acquire_sets:
        if not acquire_set.atomic:
            raise ResourcePlanError(
                "non_atomic_acquire", f"取得集合 {acquire_set.acquire_set_id} 不是原子集合"
            )
        resources = set(acquire_set.resource_ids)
        preheld = set(acquire_set.preheld_resource_ids)
        if not resources <= valid_resources or not preheld <= valid_resources:
            raise ResourcePlanError(
                "unknown_acquire_resource", f"取得集合 {acquire_set.acquire_set_id} 引用未知资源"
            )
        if resources & preheld:
            raise ResourcePlanError(
                "overlapping_acquire_set",
                f"取得集合 {acquire_set.acquire_set_id} 同时新增并预持有资源",
            )
    relations_by_id = {item.relation_id: item for item in plan.relations}
    if len(relations_by_id) != len(plan.relations):
        raise ResourcePlanError("duplicate_relation", "资源计划包含重复 relation_id")
    adjacency: dict[str, list[str]] = defaultdict(list)
    for relation in plan.relations:
        if (
            relation.from_resource_id not in valid_resources
            or relation.to_resource_id not in valid_resources
        ):
            raise ResourcePlanError(
                "unknown_relation_resource", f"关系 {relation.relation_id} 引用未知资源"
            )
        if relation.from_resource_id == relation.to_resource_id:
            raise ResourcePlanError(
                "resource_self_cycle", f"资源关系 {relation.relation_id} 形成自环"
            )
        if relation.possible_concurrency:
            adjacency[relation.from_resource_id].append(relation.to_resource_id)
    cycle = next(
        (cycle for cycle in _resource_cycles(adjacency) if _cycle_may_overlap(plan, cycle)), []
    )
    if cycle:
        raise ResourcePlanError(
            "resource_cycle",
            "静态资源取得关系成环："
            + " -> ".join(
                next(r.canonical_key for r in plan.resources if r.resource_id == item)
                for item in cycle
            )
            + "; 资源身份："
            + " -> ".join(cycle)
            + "; 来源："
            + "; ".join(
                f"{r.source_node_uuid}[{r.branch_id}] {r.reason}"
                for r in plan.relations
                if r.from_resource_id in cycle and r.to_resource_id in cycle
            ),
            path="/relations",
        )


def serialize_resource_plan(plan: ResourcePlan) -> dict[str, Any]:
    """返回可嵌入 ``execution_plan`` JSON 的确定性字典。"""

    validate_resource_plan(plan)
    return {
        "version": plan.version,
        "plan_id": plan.plan_id,
        "binding_state": plan.binding_state,
        "capabilities": list(plan.capabilities),
        "resources": [_serialize_dataclass(item) for item in plan.resources],
        "scopes": [_serialize_dataclass(item) for item in plan.scopes],
        "intervals": [_serialize_dataclass(item) for item in plan.intervals],
        "acquire_sets": [_serialize_dataclass(item) for item in plan.acquire_sets],
        "relations": [_serialize_dataclass(item) for item in plan.relations],
        "diagnostics": [dict(item) for item in plan.diagnostics],
        "metadata": _sort_json(dict(plan.metadata)),
    }


def with_hashed_resource_plan_metadata(
    plan: ResourcePlan,
    updates: Mapping[str, Any],
) -> ResourcePlan:
    """返回加入稳定元数据并重新计算内容身份的不可变资源计划。

    ``template_graph`` 是编译期临时输入，不参与内容身份；本接口只接受必须由
    ``plan_id`` 覆盖的持久事实，因此明确拒绝更新该保留键。
    """

    if not isinstance(plan, ResourcePlan):
        raise ResourcePlanError("invalid_plan", "资源计划元数据输入必须是 ResourcePlan")
    if not isinstance(updates, Mapping):
        raise ResourcePlanError("invalid_plan", "资源计划元数据更新必须是对象")
    normalized_updates = _sort_json(dict(updates))
    if "template_graph" in normalized_updates:
        raise ResourcePlanError("invalid_plan", "持久资源计划元数据不能更新 template_graph")
    updated = replace(
        plan,
        metadata={**dict(plan.metadata), **normalized_updates},
    )
    identified = _identify_plan(updated)
    validate_resource_plan(identified)
    return identified


def deserialize_resource_plan(value: Mapping[str, Any]) -> ResourcePlan:
    """从持久化 JSON 恢复并验证资源计划。

    反序列化只负责恢复冻结事实，不解析实时 Inventory；因此 bound 计划的实例
    身份仍必须已经存在，任何缺字段或未知引用都会在 WorkflowSpec seam 失败。
    """

    if not isinstance(value, Mapping):
        raise ResourcePlanError("invalid_plan", "持久化资源计划必须是对象")
    try:
        wire = _ResourcePlanWire.model_validate(value)
    except ValidationError as error:
        first = error.errors(include_url=False)[0] if error.error_count() else {}
        location = first.get("loc", ())
        path = "/" + "/".join(str(part) for part in location) if location else "/"
        raise ResourcePlanError(
            "invalid_plan",
            "持久化资源计划字段无效",
            path=path,
        ) from error
    verified_legacy = _is_verified_legacy_resource_plan_wire(wire)
    try:
        resources = tuple(
            (
                _deserialize_canonical_resource(item.model_dump())
                if verified_legacy
                else CanonicalResource(**item.model_dump())
            )
            for item in wire.resources
        )
        scopes = tuple(
            ResourceScope(
                scope_id=item.scope_id,
                kind=item.kind,
                resource_ids=tuple(item.resource_ids),
                parent_scope_id=item.parent_scope_id,
                entry_node_uuid=item.entry_node_uuid,
                exit_node_uuid=item.exit_node_uuid,
                node_uuids=tuple(item.node_uuids),
                hard_boundary=item.hard_boundary,
                branch_id=item.branch_id,
                source=item.source,
            )
            for item in wire.scopes
        )
        intervals = tuple(
            ResourceInterval(
                interval_id=item.interval_id,
                resource_id=item.resource_id,
                acquire_node_uuid=item.acquire_node_uuid,
                release_node_uuid=item.release_node_uuid,
                node_uuids=tuple(item.node_uuids),
                scope_id=item.scope_id,
                workflow_instance_id=item.workflow_instance_id,
                branch_id=item.branch_id,
                condition_ids=tuple(item.condition_ids),
                source=item.source,
                physical_state=item.physical_state,
                safe_release=item.safe_release,
                explicit_boundary=item.explicit_boundary,
            )
            for item in wire.intervals
        )
        acquire_sets = tuple(
            AcquireSet(
                acquire_set_id=item.acquire_set_id,
                node_uuid=item.node_uuid,
                resource_ids=tuple(item.resource_ids),
                preheld_resource_ids=tuple(item.preheld_resource_ids),
                atomic=item.atomic,
                branch_id=item.branch_id,
                source=item.source,
            )
            for item in wire.acquire_sets
        )
        relations = tuple(
            ResourceRelation(
                relation_id=item.relation_id,
                from_resource_id=item.from_resource_id,
                to_resource_id=item.to_resource_id,
                source_interval_id=item.source_interval_id,
                source_node_uuid=item.source_node_uuid,
                branch_id=item.branch_id,
                possible_concurrency=item.possible_concurrency,
                reason=item.reason,
            )
            for item in wire.relations
        )
        plan = ResourcePlan(
            plan_id=wire.plan_id,
            version=wire.version,
            binding_state=wire.binding_state,
            capabilities=tuple(wire.capabilities),
            resources=resources,
            scopes=scopes,
            intervals=intervals,
            acquire_sets=acquire_sets,
            relations=relations,
            diagnostics=tuple(dict(item) for item in wire.diagnostics),
            metadata=dict(wire.metadata),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ResourcePlanError("invalid_plan", "持久化资源计划字段无效") from error
    identified = _identify_plan(plan)
    if plan.version == RESOURCE_PLAN_VERSION and identified.plan_id != plan.plan_id:
        raise ResourcePlanError(
            "plan_identity_mismatch",
            "持久化资源计划内容与 plan_id 不一致",
            path="/plan_id",
        )
    validate_resource_plan(plan)
    if plan.version == _LEGACY_RESOURCE_PLAN_VERSION:
        if not verified_legacy or identified.plan_id == plan.plan_id:
            raise ResourcePlanError(
                "plan_version_downgrade",
                "内容寻址资源计划不能降级为 v1",
                path="/version",
            )
    return plan


def resource_plan_for_node(plan: ResourcePlan, node_uuid: str) -> dict[str, Any]:
    """取得一个节点的区间和新增集合只读投影。"""

    validate_resource_plan(plan)
    node_uuid = str(node_uuid)
    return {
        "plan_id": plan.plan_id,
        "node_uuid": node_uuid,
        "intervals": [
            _serialize_dataclass(item)
            for item in plan.intervals
            if node_uuid in item.node_uuids
        ],
        "acquire_sets": [
            _serialize_dataclass(item)
            for item in plan.acquire_sets
            if item.node_uuid == node_uuid
        ],
    }


def _serialize_dataclass(value: Any) -> dict[str, Any]:
    raw = asdict(value)
    return _sort_json(raw)


def _sort_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sort_json(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, tuple):
        return [_sort_json(item) for item in value]
    if isinstance(value, list):
        return [_sort_json(item) for item in value]
    return value


def _string_sequence(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ResourcePlanError("invalid_resource_list", "资源声明必须是字符串数组", path=path)
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ResourcePlanError("invalid_resource_alias", "资源别名必须是非空字符串", path=f"{path}/{index}")
        text = item.strip()
        if text in result:
            raise ResourcePlanError("duplicate_resource_alias", f"资源别名重复：{text}", path=path)
        result.append(text)
    return tuple(result)


def _normalize_edges(value: Any, nodes: Mapping[str, Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ResourcePlanError("invalid_edges", "资源计划 edges 必须是数组", path="/edges")
    result: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ResourcePlanError("invalid_edge", "资源计划边必须是对象", path=f"/edges/{index}")
        source = str(raw.get("source_node_uuid") or raw.get("source_node_id") or "")
        target = str(raw.get("target_node_uuid") or raw.get("target_node_id") or "")
        if source not in nodes or target not in nodes:
            raise ResourcePlanError("unknown_edge_node", "资源计划边引用未知节点", path=f"/edges/{index}")
        if source == target:
            raise ResourcePlanError("graph_cycle", "资源计划节点不能自依赖", path=f"/edges/{index}")
        result.add((source, target))
    return tuple(sorted(result))


def _topological_order(nodes: Mapping[str, Mapping[str, Any]], edges: Sequence[tuple[str, str]]) -> list[str]:
    incoming = {node_uuid: 0 for node_uuid in nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        incoming[target] += 1
        outgoing[source].append(target)
    queue = deque(sorted(node_uuid for node_uuid, count in incoming.items() if count == 0))
    order: list[str] = []
    while queue:
        node_uuid = queue.popleft()
        order.append(node_uuid)
        for target in sorted(outgoing[node_uuid]):
            incoming[target] -= 1
            if incoming[target] == 0:
                queue.append(target)
    if len(order) != len(nodes):
        raise ResourcePlanError("graph_cycle", "工作流依赖图本身成环")
    return order


def _node_resource_aliases(node: Mapping[str, Any]) -> tuple[str, ...]:
    """合并动作的全部资源声明，执行设备默认值不能遮蔽辅助资源。"""
    declarations = [node.get("resource_defaults"), node.get("resources")]
    metadata = node.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    if isinstance(unilab, Mapping):
        declarations.append(unilab.get("resource_defaults"))
    contract = node.get("action_resource_contract")
    if isinstance(contract, Mapping):
        declarations.append(contract.get("resource_aliases"))
        declarations.append(contract.get("required_device_params"))
        resource_params = contract.get("resource_params")
        if isinstance(resource_params, Sequence) and not isinstance(resource_params, (str, bytes)):
            declarations.append(
                [item.get("param") for item in resource_params if isinstance(item, Mapping)]
            )
        transfer = contract.get("transfer")
        if isinstance(transfer, Mapping):
            declarations.extend(
                transfer.get(field) for field in ("motion_resource_roles", "tool_resource_roles")
            )
    aliases = []
    for raw in declarations:
        if raw is not None:
            aliases.extend(_string_sequence(raw, f"/nodes/{node.get('uuid')}/resources"))
    return tuple(dict.fromkeys(aliases))


def _branch_id(node: Mapping[str, Any]) -> str:
    explicit = node.get("branch_id")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    metadata = node.get("meta_data")
    unilab = metadata.get("unilab") if isinstance(metadata, Mapping) else None
    if isinstance(unilab, Mapping):
        scope = unilab.get("parallel_scope")
        order = unilab.get("parallel_order")
        if scope is not None and order is not None:
            return f"parallel:{scope}:{order}"
    return "main"


def _scope_node_members(
    raw_scope: Mapping[str, Any],
    order: Sequence[str],
    nodes: Mapping[str, Mapping[str, Any]],
    edges: Sequence[tuple[str, str]],
) -> list[str]:
    explicit = list(raw_scope.get("node_uuids") or ())
    if explicit:
        return [node_uuid for node_uuid in order if node_uuid in explicit]
    entry = str(raw_scope.get("entry_node_uuid") or "")
    exit_node = str(raw_scope.get("exit_node_uuid") or "")
    if entry or exit_node:
        if not entry or not exit_node or entry not in nodes or exit_node not in nodes:
            raise ResourcePlanError("invalid_scope_boundary", "资源作用域入口/出口节点无效")
        reach = _reachability(order, edges)
        if entry != exit_node and exit_node not in reach[entry]:
            raise ResourcePlanError("invalid_scope_boundary", "资源作用域出口必须依赖入口")
        return [
            node
            for node in order
            if (node == entry or node in reach[entry])
            and (node == exit_node or exit_node in reach[node])
        ]
    # 根作用域没有显式边界时覆盖所有启用节点；with 作用域必须给边界或成员，
    # 否则扩大锁周期不可见。
    if raw_scope.get("kind") == "root":
        return list(order)
    raise ResourcePlanError(
        "missing_scope_boundary", "词法资源作用域必须声明 node_uuids 或入口/出口"
    )


def _common_scope_id(chain: Sequence[str], scopes: Sequence[ResourceScope], resource_id: str) -> str | None:
    candidates = [
        scope
        for scope in scopes
        if resource_id in scope.resource_ids
        and all(node_uuid in scope.node_uuids for node_uuid in chain)
    ]
    if not candidates:
        return None
    scope_by_id = {scope.scope_id: scope for scope in scopes}

    def depth(scope: ResourceScope) -> int:
        current = scope
        visited: set[str] = set()
        result = 0
        while current.parent_scope_id and current.parent_scope_id not in visited:
            visited.add(current.scope_id)
            parent = scope_by_id.get(current.parent_scope_id)
            if parent is None:
                break
            result += 1
            current = parent
        return result

    return max(candidates, key=lambda scope: (depth(scope), scope.scope_id)).scope_id


def _workflow_instance_id(graph: Mapping[str, Any]) -> str:
    for key in ("workflow_instance_id", "workflow_uuid", "uuid"):
        value = graph.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    workflow = graph.get("workflow")
    if isinstance(workflow, Mapping):
        value = workflow.get("uuid")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "workflow:anonymous"


def _merge_concurrent_relations(
    plan: ResourcePlan,
    concurrency: Sequence[ResourcePlan | Mapping[str, Any]],
) -> ResourcePlan:
    """联合全部实例资源；没有出现在当前流程的中间端点也必须保留。"""
    resources = {r.canonical_key: r for r in plan.resources}
    relations = list(plan.relations)
    for index, candidate in enumerate(concurrency):
        other = (
            candidate
            if isinstance(candidate, ResourcePlan)
            else deserialize_resource_plan(candidate)
        )
        if other.binding_state != "bound":
            raise ResourcePlanError("resource_unbound", "并发计划必须绑定到具体资源实例")
        remap = {}
        for resource in other.resources:
            if resource.canonical_key not in resources:
                resources[resource.canonical_key] = replace(
                    resource, resource_id=str(uuid5(_PLAN_NAMESPACE, resource.canonical_key))
                )
            remap[resource.resource_id] = resources[resource.canonical_key].resource_id
        for relation in other.relations:
            relations.append(
                replace(
                    relation,
                    relation_id=str(
                        uuid5(
                            _PLAN_NAMESPACE,
                            f"concurrent:{index}:{other.plan_id}:{relation.relation_id}",
                        )
                    ),
                    from_resource_id=remap[relation.from_resource_id],
                    to_resource_id=remap[relation.to_resource_id],
                    reason=f"Workflow {other.metadata.get('workflow_instance_id', other.plan_id)}: {relation.reason}",
                )
            )
    return replace(plan, resources=tuple(resources.values()), relations=tuple(relations))


def validate_station_resource_plans(plans: Sequence[ResourcePlan]) -> None:
    """在任务准入/恢复时联合所有可能并发的实例，只验证而不改变冻结计划。"""
    if plans:
        validate_resource_plan(_merge_concurrent_relations(plans[0], plans[1:]))


def _reachability(order: Sequence[str], edges: Sequence[tuple[str, str]]) -> dict[str, set[str]]:
    outgoing = defaultdict(set)
    for source, target in edges:
        outgoing[source].add(target)
    reach = {node: set() for node in order}
    for node in reversed(order):
        for child in outgoing[node]:
            reach[node].add(child)
            reach[node].update(reach[child])
    return reach


def _resource_chains(
    resource_id: str,
    uses: Sequence[str],
    nodes: Mapping[str, Mapping[str, Any]],
    edges: Sequence[tuple[str, str]],
    scopes: Sequence[ResourceScope],
    reach: Mapping[str, set[str]],
    diagnostics: list[Mapping[str, Any]],
    alias: str,
) -> list[list[str]]:
    """显式词法所有权先合并，默认连续关系只跨安全控制边界。"""
    parent = {node: node for node in uses}

    def root(node: str) -> str:
        while parent[node] != node:
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        parent[root(b)] = root(a)

    explicit = {node: set() for node in uses}
    for scope in scopes:
        if resource_id not in scope.resource_ids:
            continue
        members = [n for n in uses if n in scope.node_uuids]
        for member in members:
            explicit[member].add(scope.scope_id)
            union(members[0], member)
    outgoing = defaultdict(list)
    for source, target in edges:
        outgoing[source].append(target)
    control_boundaries = {
        node_uuid for node_uuid, node in nodes.items()
        if node.get("kind") in {"condition", "repeat_until"}
    }
    for start in uses:
        queue = list(outgoing[start])
        seen = set()
        while queue:
            end = queue.pop()
            if end in seen:
                continue
            seen.add(end)
            if end not in parent:
                if nodes[end].get("kind") in {"group", "join", "parallel", "structure"}:
                    queue.extend(outgoing[end])
                continue
            # 原子搬运完成物理结算后是默认锁的安全释放边界。显式作用域已在
            # 上方合并，不受此边界影响；不能因下一工站复用设备而推断持续持锁。
            start_boundaries = nodes[start].get("resource_default_boundaries", ())
            end_boundaries = nodes[end].get("resource_default_boundaries", ())
            if (
                nodes[start].get("resource_default_boundary")
                or nodes[end].get("resource_default_boundary")
                or (
                    isinstance(start_boundaries, Sequence)
                    and not isinstance(start_boundaries, (str, bytes))
                    and alias in start_boundaries
                )
                or (
                    isinstance(end_boundaries, Sequence)
                    and not isinstance(end_boundaries, (str, bytes))
                    and alias in end_boundaries
                )
            ):
                continue
            if root(start) == root(end):
                continue
            if explicit[start] or explicit[end]:
                continue  # 显式出口与入口不扩展默认连续边界。
            # 组合展开可能同时留下 start→end 和 start→控制节点→end。
            # 直接完成边不能绕过控制边界，否则 start 持锁等 end，而循环
            # body 为了让 end 就绪又必须取得同一资源，形成自等待。
            if any(end in reach[boundary] for boundary in control_boundaries & reach[start]):
                continue
            competitors = [
                n
                for n in uses
                if n != start
                and n != end
                and end in reach[n]
                and n not in reach[start]
                and start not in reach[n]
            ]
            if competitors:
                diagnostics.append(
                    {
                        "code": "join_continuity_released",
                        "resource_id": resource_id,
                        "source_node_uuid": start,
                        "target_node_uuid": end,
                        "competing_nodes": competitors,
                    }
                )
                continue
            union(start, end)
    for first in uses:
        for second in uses:
            if first == second or second in reach[first] or first in reach[second]:
                continue
            if nodes[first].get("order_sensitive") or nodes[second].get("order_sensitive"):
                raise ResourcePlanError(
                    "unordered_resource_actions",
                    f"资源 {alias} 的兄弟动作 {first}、{second} 顺序影响语义，必须显式排序",
                )
        if alias in nodes[first].get("physical_hold_resources", ()):
            if not any(other in reach[first] and root(first) == root(other) for other in uses):
                raise ResourcePlanError(
                    "unsafe_resource_handoff",
                    f"{first} 仍承载物料/设备状态，资源 {alias} 不可安全交接；请扩大显式范围或先放到稳定 Site",
                )
    groups = defaultdict(list)
    for node in uses:
        groups[root(node)].append(node)
    return list(groups.values())


__all__ = [
    "AcquireSet",
    "CanonicalResource",
    "RESOURCE_PLAN_CAPABILITY",
    "RESOURCE_PLAN_VERSION",
    "ResourceInterval",
    "ResourcePlan",
    "ResourcePlanError",
    "ResourceRelation",
    "ResourceScope",
    "STATIC_RESOURCE_DAG_CAPABILITY",
    "bind_station_resource_plan",
    "compile_template_resource_plan",
    "failed_explicit_resource_interval_ids",
    "node_has_explicit_resource_interval",
    "retained_resource_interval_ids",
    "deserialize_resource_plan",
    "resource_plan_for_node",
    "serialize_resource_plan",
    "validate_resource_plan",
    "with_hashed_resource_plan_metadata",
]


def normalize_execution_resource_plan(execution_plan: Mapping[str, Any]) -> dict[str, Any]:
    """旧占用区间只在入口迁移为同一个已验证计划，不信任 static_acyclic 标记。

    现代执行计划已经包含规范 ``resource_plan``，热路径只读其内容。此时仅复制
    顶层映射，避免每个 Job 结果投影为每个历史区间拥有者反复深复制整份大图。
    只有确实携带旧 ``resource_occupancy_plan`` 时才执行迁移所需的深复制。
    """

    result = dict(execution_plan)
    if "resource_occupancy_plan" not in result:
        return result

    from copy import deepcopy

    result = deepcopy(result)
    legacy = result.pop("resource_occupancy_plan", None)
    if legacy is None:
        return result
    if result.get("resource_plan") is not None:
        raise ResourcePlanError("ambiguous_resource_plan", "不能同时提供两种资源计划")
    if not isinstance(legacy, Mapping) or legacy.get("version") != 1:
        raise ResourcePlanError("invalid_resource_plan", "旧资源占用计划版本无效")
    bindings = {}
    scopes = []
    for item in legacy.get("intervals", []):
        aliases = []
        for lock in item.get("resource_locks", []):
            key = str(lock["lock_key"])
            aliases.append(key)
            bindings[key] = {"canonical_key": key, "kind": lock.get("scope", "resource")}
        scopes.append(
            {
                "scope_id": item["uuid"],
                "kind": "with",
                "resources": aliases,
                "node_uuids": item["member_node_uuids"],
                "source": item.get("source", "legacy_interval"),
            }
        )
    template = compile_template_resource_plan(
        {
            "nodes": result.get("nodes", []),
            "edges": result.get("edges", []),
            "resource_scopes": scopes,
        }
    )
    plan = bind_station_resource_plan(template, bindings)
    result["resource_plan"] = serialize_resource_plan(plan)
    result["capabilities"] = sorted(set(result.get("capabilities", [])) | set(plan.capabilities))
    for node in result.get("nodes", []):
        projection = resource_plan_for_node(plan, node["uuid"])
        node["resource_plan_id"] = plan.plan_id
        node["resource_interval_ids"] = [i["interval_id"] for i in projection["intervals"]]
        node["resource_acquire_set_id"] = next(
            (i["acquire_set_id"] for i in projection["acquire_sets"]), ""
        )
    return result


def _execution_resource_plan_view(execution_plan: Mapping[str, Any]) -> Mapping[str, Any]:
    """资源终态判定只读计划；只有旧格式需要复制、迁移。"""
    if execution_plan.get("resource_occupancy_plan") is not None:
        return normalize_execution_resource_plan(execution_plan).get(
            "resource_plan", execution_plan
        )
    return execution_plan.get("resource_plan", execution_plan)


def completed_resource_nodes_for_job(
    job: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    execution_plan: Mapping[str, Any] | None = None,
) -> set[str]:
    """以本轮确定终态关闭区间；未完成的物理交接仍保留所有权。"""

    def iteration_scope(candidate: Mapping[str, Any]) -> tuple[Any, Any] | None:
        metadata = candidate.get("meta_data") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        unilab = metadata.get("unilab") or {}
        if "iteration_index" not in unilab:
            return None
        return unilab.get("control_path"), unilab["iteration_index"]

    current_scope = iteration_scope(job)
    aborted = any(
        candidate.get("status") in {"failed", "canceled", "timeout"} for candidate in jobs if candidate.get("current_attempt") is not False
    )
    stopped_states = {"succeeded", "skipped", "failed", "canceled", "timeout"}
    if aborted:
        stopped_states.add("pending")  # fail-fast 后未派发的后继不再进入资源区间。
    parent_scope_by_runtime: dict[str, tuple[Any, Any] | None] = {}
    for candidate in jobs:
        metadata = candidate.get("meta_data") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        runtime_id = (metadata.get("unilab") or {}).get("runtime_node_id")
        if runtime_id:
            parent_scope_by_runtime[str(runtime_id)] = iteration_scope(candidate)

    def belongs_to_current(candidate: Mapping[str, Any]) -> bool:
        scope = iteration_scope(candidate)
        if current_scope is None or scope is None:
            return True
        visited: set[tuple[Any, Any]] = set()
        while scope is not None and scope not in visited:
            if scope == current_scope:
                return True
            visited.add(scope)
            scope = parent_scope_by_runtime.get(str(scope[0]))
        return False

    relevant = [candidate for candidate in jobs if candidate.get("current_attempt") is not False and belongs_to_current(candidate)]
    by_template: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for candidate in relevant:
        by_template[str(candidate["workflow_node_uuid"])].append(candidate)
    successful = {
        template
        for template, instances in by_template.items()
        if all(candidate.get("status") == "succeeded" for candidate in instances)
    }
    succeeded_instances = {
        (str(candidate["workflow_node_uuid"]), iteration_scope(candidate))
        for candidate in relevant
        if candidate.get("status") == "succeeded"
    }
    protected: set[str] = set()
    if execution_plan:
        plan = _execution_resource_plan_view(execution_plan)
        metadata = plan.get("metadata") or {}
        for transfer in metadata.get("transfers", ()):
            if any(
                template == transfer["pick_node_uuid"]
                and (transfer["place_node_uuid"], scope) not in succeeded_instances
                for template, scope in succeeded_instances
            ):
                protected.add(transfer["place_node_uuid"])
        # 自定义持料合同没有成功的区间出口，同样不能把跳过/失败当成交接。
        holds = {
            key: set(value) for key, value in (metadata.get("physical_hold_nodes") or {}).items()
        }
        for transfer in metadata.get("transfers", ()):
            if transfer["place_node_uuid"] in successful:
                holds.get(transfer["pick_node_uuid"], set()).difference_update(
                    [
                        *transfer["carrier_resources"],
                        transfer["target_resource"],
                        transfer["target_site"],
                    ]
                )
        aliases = {
            resource["resource_id"]: resource["alias"] for resource in plan.get("resources", ())
        }
        for interval in plan.get("intervals", ()):
            members = set(interval["node_uuids"])
            alias = aliases.get(interval["resource_id"])
            if any(
                member in successful and alias in holds.get(member, set()) for member in members
            ):
                protected.update(members - successful)
    completed = {
        template
        for template, instances in by_template.items()
        if template not in protected
        and all(
            candidate.get("status") in stopped_states
            and not str(candidate.get("uncertainty_reason") or "").strip()
            for candidate in instances
        )
    }
    if execution_plan:
        # Skipped repeat regions never materialize body Jobs. Their terminal
        # controller is the witness for those absent templates, including nested
        # repeats in an unselected branch. Do not infer completion from absence alone.
        # 只读遍历：跳过非映射条目（只读策略测试用不可复制哨兵占位 nodes）。
        planned_nodes = {
            str(node["uuid"]): node
            for node in execution_plan.get("nodes", ())
            if isinstance(node, Mapping) and node.get("uuid")
        }
        for template, node in planned_nodes.items():
            if template in by_template or template in protected:
                continue
            parent = node.get("parent_uuid")
            seen = set()
            while parent in planned_nodes and parent not in seen:
                seen.add(parent)
                if planned_nodes[parent].get("kind") == "repeat_until" and parent in completed:
                    completed.add(template)
                    break
                parent = planned_nodes[parent].get("parent_uuid")
    if aborted and execution_plan:
        represented = {str(candidate["workflow_node_uuid"]) for candidate in jobs}
        planned_members = {
            member for interval in plan.get("intervals", ()) for member in interval["node_uuids"]
        }
        completed.update(planned_members - represented - protected)
    return completed


def failed_explicit_resource_interval_ids(
    execution_plan: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """返回被异常结果永久闩住、只能由操作员释放的显式连续区间。"""

    plan = _execution_resource_plan_view(execution_plan)
    raw_intervals = plan.get("intervals", ()) if isinstance(plan, Mapping) else ()
    if not isinstance(raw_intervals, Sequence) or isinstance(
        raw_intervals, (str, bytes)
    ):
        return ()
    abnormal_nodes = {
        str(job.get("workflow_node_uuid") or "")
        for job in jobs
        if isinstance(job, Mapping)
        and job.get("current_attempt") is not False
        and (
            job.get("status") in {"failed", "canceled", "timeout"}
            or bool(str(job.get("uncertainty_reason") or "").strip())
        )
    }
    return tuple(
        sorted(
            interval_id
            for interval in raw_intervals
            if isinstance(interval, Mapping)
            and bool(interval.get("explicit_boundary"))
            and (
                interval_id := str(interval.get("interval_id") or "")
            )
            and abnormal_nodes.intersection(
                str(value) for value in interval.get("node_uuids", ())
            )
        )
    )


def node_has_explicit_resource_interval(
    execution_plan: Mapping[str, Any],
    node_uuid: str,
) -> bool:
    """判断节点是否属于必须按 Task 整组释放的显式连续区间。"""

    plan = _execution_resource_plan_view(execution_plan)
    raw_intervals = plan.get("intervals", ()) if isinstance(plan, Mapping) else ()
    if not isinstance(raw_intervals, Sequence) or isinstance(
        raw_intervals, (str, bytes)
    ):
        return False
    return any(
        isinstance(interval, Mapping)
        and bool(interval.get("explicit_boundary"))
        and node_uuid in {str(value) for value in interval.get("node_uuids", ())}
        for interval in raw_intervals
    )


def continuing_resource_interval_ids(
    execution_plan: Mapping[str, Any],
    interval_ids: Sequence[str] | set[str],
    current_node: str,
    completed_nodes: Sequence[str] | set[str],
    *,
    current_completed: bool = True,
) -> tuple[str, ...]:
    """释放以全部区间成员完成为准；物理交接失败不能伪装成完成。"""
    plan = _execution_resource_plan_view(execution_plan)
    completed = set(completed_nodes)
    if current_completed:
        completed.add(current_node)
    return tuple(
        sorted(
            str(item["interval_id"])
            for item in plan.get("intervals", ())
            if item["interval_id"] in interval_ids
            and current_node in item["node_uuids"]
            and not set(item["node_uuids"]) <= completed
        )
    )


def retained_resource_interval_ids(
    execution_plan: Mapping[str, Any],
    interval_ids: Sequence[str] | set[str],
    current_job: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """统一计算正常连续持有与异常失败闩锁要求保留的区间。"""

    normalized_ids = {str(value) for value in interval_ids}
    retained = set(
        continuing_resource_interval_ids(
            execution_plan,
            normalized_ids,
            str(current_job.get("workflow_node_uuid") or ""),
            completed_resource_nodes_for_job(current_job, jobs, execution_plan),
            current_completed=current_job.get("status") == "succeeded",
        )
    )
    retained.update(
        set(failed_explicit_resource_interval_ids(execution_plan, jobs))
        & normalized_ids
    )
    return tuple(sorted(retained))


def _compile_transfer_pairs(
    nodes: Mapping[str, MutableMapping[str, Any]],
    order: Sequence[str],
    edges: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    """按物料身份配对独立 pick/place；缺少唯一目标时拒绝编译。"""
    reach = _reachability(order, edges)
    steps = {}
    for node_id, node in nodes.items():
        raw = node.get("transfer_step")
        contract = node.get("action_resource_contract") or {}
        if raw is None and isinstance(contract.get("transfer_step"), Mapping):
            descriptor = contract["transfer_step"]
            params = node.get("param") or {}

            def argument(name: str) -> str:
                value = params.get(name)
                if isinstance(value, Mapping):
                    value = value.get("material_uuid") or value.get("uuid")
                if not isinstance(value, str) or not value:
                    raise ResourcePlanError(
                        "transfer_unbound", f"{node_id} 的搬运参数 {name} 必须在 pick 前绑定"
                    )
                return value

            raw = {
                "operation": descriptor["operation"],
                "material": argument(descriptor["material_param"]),
                "endpoint_resource": argument(descriptor["owner_param"]),
                "endpoint_site": argument(descriptor["site_param"]),
                "carrier_resources": [argument(name) for name in descriptor["carrier_params"]],
            }
        if raw is None:
            continue
        if not isinstance(raw, Mapping) or raw.get("operation") not in {"pick", "place"}:
            raise ResourcePlanError("invalid_transfer_step", f"{node_id} 搬运步骤声明无效")
        for key in ("material", "endpoint_resource", "endpoint_site"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ResourcePlanError("transfer_unbound", f"{node_id} 缺少静态 {key}")
        carriers = _string_sequence(
            raw.get("carrier_resources"), f"/nodes/{node_id}/transfer_step/carrier_resources"
        )
        if not carriers:
            raise ResourcePlanError("transfer_unbound", f"{node_id} 缺少搬运器/运动资源")
        steps[node_id] = dict(raw)
        node["transfer_step"] = dict(raw)
        node["resource_defaults"] = list(
            dict.fromkeys(
                [
                    *_node_resource_aliases(node),
                    *carriers,
                    raw["endpoint_resource"],
                    raw["endpoint_site"],
                ]
            )
        )
    matched = set()
    transfers = []
    for pick, step in steps.items():
        if step["operation"] != "pick":
            continue
        candidates = [
            other
            for other, end in steps.items()
            if end["operation"] == "place"
            and end["material"] == step["material"]
            and other in reach[pick]
        ]
        candidates = [
            other
            for other in candidates
            if not any(other in reach[earlier] for earlier in candidates if earlier != other)
        ]
        if len(candidates) != 1 or candidates[0] in matched:
            raise ResourcePlanError(
                "transfer_pair_ambiguous", f"{pick} 无法配对唯一 place；目标必须在取料前确定"
            )
        place = candidates[0]
        target = steps[place]
        if set(step["carrier_resources"]) != set(target["carrier_resources"]):
            raise ResourcePlanError(
                "transfer_carrier_mismatch", f"{pick}/{place} 搬运器或运动资源不一致"
            )
        matched.add(place)
        held = [*step["carrier_resources"], target["endpoint_resource"], target["endpoint_site"]]
        nodes[pick]["resource_defaults"] = list(
            dict.fromkeys([*nodes[pick]["resource_defaults"], *held])
        )
        nodes[pick]["physical_hold_resources"] = list(
            dict.fromkeys([*nodes[pick].get("physical_hold_resources", []), *held])
        )
        transfers.append(
            {
                "pick_node_uuid": pick,
                "place_node_uuid": place,
                "material": step["material"],
                "target_resource": target["endpoint_resource"],
                "target_site": target["endpoint_site"],
                "carrier_resources": step["carrier_resources"],
            }
        )
    if any(step["operation"] == "place" and node not in matched for node, step in steps.items()):
        raise ResourcePlanError("transfer_pair_missing", "place 缺少同物料的先行 pick")
    return transfers


def _identify_plan(plan: ResourcePlan) -> ResourcePlan:
    """边界、资源绑定或并发关系变化均产生新的计划身份。"""
    payload = {
        "capabilities": list(plan.capabilities),
        "resources": [asdict(r) for r in plan.resources],
        "scopes": [asdict(scope) for scope in plan.scopes],
        "intervals": [asdict(i) for i in plan.intervals],
        "acquire_sets": [asdict(a) for a in plan.acquire_sets],
        "relations": [asdict(r) for r in plan.relations],
        "metadata": _sort_json(
            {key: value for key, value in plan.metadata.items() if key != "template_graph"}
        ),
        "binding_state": plan.binding_state,
        "workflow_instance_id": plan.metadata.get("workflow_instance_id"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return replace(plan, plan_id=str(uuid5(_PLAN_NAMESPACE, digest)))


def _resource_cycles(adjacency: Mapping[str, Sequence[str]]) -> Iterator[list[str]]:
    """枚举简单环，避免第一个不可能重叠的环掩盖另一个真实环。"""
    for start in sorted(adjacency):

        def visit(path: list[str]) -> Iterator[list[str]]:
            for target in sorted(set(adjacency.get(path[-1], ()))):
                if target == start:
                    yield path + [start]
                elif target > start and target not in path:
                    yield from visit(path + [target])

        yield from visit([start])


def _cycle_may_overlap(plan: ResourcePlan, cycle: Sequence[str]) -> bool:
    """仅用显式依赖排除本实例内绝不重叠的关系，不根据时长推测。"""
    edges = plan.metadata.get("dependency_edges", ())
    if not edges:
        return True
    nodes = {node for edge in edges for node in edge}
    order = _topological_order({node: {} for node in nodes}, edges)
    reach = _reachability(order, edges)
    intervals = {i.interval_id: i for i in plan.intervals}
    choices = [
        [
            r
            for r in plan.relations
            if r.from_resource_id == source
            and r.to_resource_id == target
            and r.possible_concurrency
        ]
        for source, target in zip(cycle, cycle[1:])
    ]
    from itertools import product

    for combination in product(*choices):
        possible = True
        for first in combination:
            for second in combination:
                left = intervals.get(first.source_interval_id)
                right = intervals.get(second.source_interval_id)
                # 外部流程节点即使名称相同也没有本地依赖证明。
                if (
                    left is None
                    or right is None
                    or first.reason.startswith("Workflow ")
                    or second.reason.startswith("Workflow ")
                ):
                    continue
                if left is right:
                    continue
                # 并行区间的单个拓扑首尾不是实际取得/释放屏障。
                # 只有全部左侧成员都先于全部右侧成员，才能证明不会重叠。
                if all(
                    later in reach.get(earlier, set())
                    for earlier in left.node_uuids
                    for later in right.node_uuids
                ):
                    possible = False
        if possible:
            return True
    return False
