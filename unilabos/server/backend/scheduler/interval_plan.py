"""把 dev 执行计划映射为按循环层级冻结的资源区间。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from unilabos.protocol.utils.workflow_hierarchy import lift_edge
from .resource_keys import device_lock_key
from .resource_plan import ResourcePlanError, bind_station_resource_plan, compile_template_resource_plan, serialize_resource_plan


def validate_graph_resource_scopes(graph: dict[str, Any]) -> None:
    """保存作者图时检查作用域，组框不改变循环边界。"""
    nodes = {node["uuid"]: node for node in graph["nodes"]}

    def loop_parent(node):
        parent = node.get("parent_uuid")
        seen = set()
        while parent in nodes and nodes[parent].get("type") != "loop":
            if parent in seen:
                raise ValueError("节点父级关系成环")
            seen.add(parent)
            parent = nodes[parent].get("parent_uuid")
        return parent

    plan = {"nodes": [{**node, "kind": node["type"], "parent_uuid": loop_parent(node)}
                      for node in nodes.values() if node["type"] != "group"], "edges": graph["edges"]}
    compile_interval_plan(plan, graph)


def compile_interval_plan(plan: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    """资源身份使用实际设备 ID；组框已由 dev 展开，循环体每轮有独立运行代次。"""
    nodes = {node["uuid"]: node for node in plan["nodes"]}
    sources = {node["uuid"]: node for node in graph["nodes"]}
    parents = {key: node.get("parent_uuid") if node.get("parent_uuid") in nodes else None for key, node in nodes.items()}
    frames: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    bindings = {}
    for key, node in nodes.items():
        source = sources.get(key, {})
        policy = node.get("execution_policy") or {}
        aliases = []
        if node["kind"] == "device_action":
            device = str((source.get("meta_data") or {}).get("target_device_id") or node.get("material_uuid")
                         or source.get("material_uuid") or node.get("param", {}).get("device_id") or "")
            if device:
                if not policy.get("always_free"):
                    aliases.append(device)
                bindings[device] = {"canonical_key": device_lock_key(device), "kind": "device"}
        frames[parents[key]].append({"uuid": key, "kind": "repeat_until" if node["kind"] == "loop" else node["kind"],
                                    "resource_defaults": aliases})
    pairs = {(edge["source_node_uuid"], edge["target_node_uuid"]) for edge in plan.get("edges", [])}
    pairs.update((dep, key) for key, node in nodes.items() for dep in (node.get("execution_policy") or {}).get("depends_on", []))
    edges: dict[str | None, list[dict[str, str]]] = defaultdict(list)
    seen = set()
    for source, target in sorted(pairs):
        if source not in parents or target not in parents:
            continue
        source, target = lift_edge(parents, source, target)
        if source == target or (source, target) in seen:
            continue
        seen.add((source, target))
        edges[parents[source]].append({"source_node_uuid": source, "target_node_uuid": target})
    metadata = (graph.get("workflow") or {}).get("meta_data") or {}
    declarations = (metadata.get("unilab") or {}).get("resource_scopes", [])
    scoped: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    scope_frames: dict[str, str | None] = {}
    scope_members: dict[str, list[str]] = {}

    def ancestors(key: str) -> list[str | None]:
        result: list[str | None] = [key]
        while result[-1] is not None:
            parent = parents[result[-1]]
            if parent in result:
                raise ResourcePlanError("invalid_scope_boundary", "循环父级成环")
            result.append(parent)
        return result

    def child_in_frame(key: str, frame: str | None) -> str:
        chain = ancestors(key)
        return chain[chain.index(frame) - 1]
    if not isinstance(declarations, list):
        raise ResourcePlanError("invalid_scope", "resource_scopes 必须是数组")
    for scope in declarations:
        if not isinstance(scope, dict):
            raise ResourcePlanError("invalid_scope", "资源作用域必须是对象")
        members = scope.get("node_uuids")
        if not isinstance(members, list) or not members or any(not isinstance(key, str) for key in members):
            raise ResourcePlanError("invalid_scope_boundary", "资源作用域必须明确列出 node_uuids")
        if any(key not in nodes for key in members):
            raise ResourcePlanError("invalid_scope_boundary", "资源作用域引用未知节点")
        scope_id = str(scope.get("scope_id") or "")
        if not scope_id or scope_id in scope_frames:
            raise ResourcePlanError("invalid_scope", "资源作用域身份必须非空且唯一")
        chains = [ancestors(key)[1:] for key in members]
        frame = next(parent for parent in chains[0] if all(parent in chain for chain in chains))
        boundary = sorted({child_in_frame(key, frame) for key in members})
        # 跨入循环即声明完整循环的所有权边界，真实动作逐次准入；零轮循环也能正常收口。
        expanded = sorted(key for key in nodes if any(member in ancestors(key) for member in boundary))
        scope_frames[scope_id] = frame
        scope_members[scope_id] = expanded
        scoped[frame].append({**scope, "node_uuids": boundary})
    for frame, scopes in scoped.items():
        for scope in scopes:
            parent_id = scope.get("parent_scope_id")
            if parent_id:
                if parent_id not in scope_frames or not set(scope_members[scope["scope_id"]]) <= set(scope_members[parent_id]):
                    raise ResourcePlanError("invalid_scope_boundary", "子资源作用域必须完整位于父作用域内")
                if scope_frames[parent_id] != frame:
                    # 不同循环层级由运行时继承外层所有权；单层规划器只处理本层词法父级。
                    scope.pop("parent_scope_id", None)
    result = []
    for parent, members in frames.items():
        template = compile_template_resource_plan({
            "workflow_uuid": str((graph.get("workflow") or {}).get("uuid") or "workflow:anonymous") + ":" + str(parent),
            "nodes": members, "edges": edges[parent], "resource_scopes": scoped[parent],
        })
        bound = bind_station_resource_plan(template, bindings)
        inherited = {interval.interval_id: scope_members[interval.scope_id]
                     for interval in bound.intervals if interval.explicit_boundary and interval.scope_id in scope_members}
        result.append({"parent_node_uuid": parent, "plan": serialize_resource_plan(bound),
                       "admission_members": inherited})
    return {"version": 2, "frames": result}
