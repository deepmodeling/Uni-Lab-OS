"""把 dev 执行计划映射为按循环层级冻结的资源区间。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from unilabos.protocol.utils.workflow_hierarchy import lift_edge
from .resource_keys import device_lock_key
from .resource_plan import ResourcePlanError, bind_station_resource_plan, compile_template_resource_plan, serialize_resource_plan


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
    if not isinstance(declarations, list):
        raise ResourcePlanError("invalid_scope", "resource_scopes 必须是数组")
    for scope in declarations:
        if not isinstance(scope, dict):
            raise ResourcePlanError("invalid_scope", "资源作用域必须是对象")
        members = scope.get("node_uuids")
        if not isinstance(members, list) or not members or any(not isinstance(key, str) for key in members):
            raise ResourcePlanError("invalid_scope_boundary", "资源作用域必须明确列出 node_uuids")
        levels = {parents.get(key) for key in members}
        if len(levels) != 1 or any(key not in nodes or nodes[key]["kind"] == "loop" for key in members):
            raise ValueError("显式资源作用域跨循环边界，需要先展开对应层级的资源合同")
        scoped[next(iter(levels))].append({**scope, "node_uuids": members})
    result = []
    for parent, members in frames.items():
        template = compile_template_resource_plan({
            "workflow_uuid": str((graph.get("workflow") or {}).get("uuid") or "workflow:anonymous") + ":" + str(parent),
            "nodes": members, "edges": edges[parent], "resource_scopes": scoped[parent],
        })
        bound = bind_station_resource_plan(template, bindings)
        result.append({"parent_node_uuid": parent, "plan": serialize_resource_plan(bound)})
    return {"version": 1, "frames": result}
