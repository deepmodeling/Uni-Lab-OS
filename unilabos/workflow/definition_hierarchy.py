"""工作流定义按作者层级读写；发布和执行继续使用完整的冻结图。"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


def private_node_owners(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """返回私有后代到最外层子操作入口的映射，不跨越当前定义边界。"""
    index = {node["uuid"]: node for node in nodes}
    result = {}
    for node in nodes:
        current = node
        seen = {node["uuid"]}
        while current.get("parent_uuid"):
            parent = current["parent_uuid"]
            if parent not in index or parent in seen:
                raise ValueError("工作流定义的父节点不存在或循环")
            current = index[parent]
            seen.add(parent)
            if current.get("meta_data", {}).get("unilab", {}).get("composite"):
                result[node["uuid"]] = parent
    return result


def definition_graph(graph: Mapping[str, Any]) -> dict[str, Any]:
    """返回本层节点和边，保留子操作合同 pin、具名实参及外部输出接线。"""
    owners = private_node_owners(graph["nodes"])
    visible = {node["uuid"] for node in graph["nodes"]} - owners.keys()
    result = deepcopy(dict(graph))
    result["nodes"] = [node for node in result["nodes"] if node["uuid"] in visible]
    result["edges"] = [
        edge
        for edge in result["edges"]
        if edge["source_node_uuid"] in visible and edge["target_node_uuid"] in visible
    ]
    if "inventory_requirements" in result:
        # 子操作的数量需求在其定义中查看；本层只列自己声明的需求。
        result["inventory_requirements"] = [
            item
            for item in result.get("inventory_requirements") or []
            if str(item.get("consume_node_uuid") or "") in visible
            and not ((item.get("meta_data") or {}).get("unilab") or {}).get("composite_origin")
        ]
    templates = {node.get("workflow_node_template_uuid") for node in result["nodes"]}
    result["node_templates"] = [
        item for item in result.get("node_templates", []) if item["uuid"] in templates
    ]
    result["handle_templates"] = [
        item
        for item in result.get("handle_templates", [])
        if item["workflow_node_template_uuid"] in templates
    ]
    unilab = result["workflow"].get("meta_data", {}).get("unilab", {})
    if "resource_scopes" in unilab:
        # 子操作自己的锁区间在其定义中查看；外层锁仍保留操作入口的覆盖范围。
        scopes = []
        for scope in unilab["resource_scopes"]:
            if scope.get("composite_invocation_uuid"):
                continue
            scope["node_uuids"] = list(
                dict.fromkeys(
                    owners.get(uid, uid) for uid in scope.get("node_uuids", [])
                )
            )
            for key in ("entry_node_uuid", "exit_node_uuid"):
                if scope.get(key) in owners:
                    scope[key] = owners[scope[key]]
            scopes.append(scope)
        unilab["resource_scopes"] = scopes
    return result


def restore_private_subgraphs(
    *,
    applied: Mapping[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """公共定义图回写时恢复保留入口的冻结子图，再走完整校验和源码重编译。

    已提供内部节点的既有客户端走原路径；删除某入口会同时删除它的私有子图。
    新入口只能通过正常编译/插入流程建立，不能从其他父工作流复制私有执行身份。
    """
    owners = private_node_owners(applied["nodes"])
    supplied = {node["uuid"] for node in nodes}
    if not owners or supplied & owners.keys():
        return nodes, edges
    retained = {uid for uid, owner in owners.items() if owner in supplied}
    restored_nodes = [
        *nodes,
        *(deepcopy(node) for node in applied["nodes"] if node["uuid"] in retained),
    ]
    available = supplied | retained
    edge_ids = {edge["uuid"] for edge in edges}
    restored_edges = [
        *edges,
        *(
            deepcopy(edge)
            for edge in applied["edges"]
            if edge["uuid"] not in edge_ids
            and edge["source_node_uuid"] in available
            and edge["target_node_uuid"] in available
            and (
                edge["source_node_uuid"] in retained
                or edge["target_node_uuid"] in retained
            )
        ),
    ]
    return restored_nodes, restored_edges
