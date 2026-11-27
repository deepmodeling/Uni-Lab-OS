"""沿冻结组合图证明显式物料输出来自哪个调用输入。"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from unilabos.workflow.workflow_io import handle_value_schema, schema_contains_resource_slot

if TYPE_CHECKING:
    from unilabos.workflow.composite_expansion import CompositeExpansion


def explicit_material_input_parameter(
    expansion: "CompositeExpansion",
    boundary_handles: Sequence[Mapping[str, Any]],
    mapping: Mapping[str, Any],
) -> str | None:
    """沿声明的身份透传、数据边和祖先边界追溯；未知或歧义来源失败关闭。

    参数为同次冻结展开及其调用连接点；返回被证明的输入参数名。
    不执行动作，不根据连接点类型相同推断物料身份。
    """
    nodes = {str(node["uuid"]): node for node in expansion.nodes}
    handles = {str(handle["uuid"]): handle for handle in expansion.handle_templates}
    boundary = {str(handle["uuid"]): handle for handle in boundary_handles}
    incoming = {}
    for edge in expansion.edges:
        if not edge.get("dependency_only"):
            key = (edge.get("target_node_uuid"), edge.get("target_handle_uuid"))
            incoming.setdefault(key, []).append(edge)
    roots = {}
    for handle_id, destinations in expansion.target_mappings.items():
        for destination in destinations:
            key = (destination["workflow_node_uuid"], destination["target_handle_uuid"])
            roots.setdefault(key, set()).add(handle_id)
    seen: set[tuple[str, str]] = set()
    target_seen: set[tuple[str, str]] = set()

    def target(node_id: str, handle_id: str) -> str | None:
        key = (node_id, handle_id)
        if key in target_seen:
            return None
        target_seen.add(key)
        candidates = roots.get(key, set())
        edges = incoming.get(key, [])
        if candidates:
            if len(candidates) != 1 or edges:
                return None
            handle = boundary.get(next(iter(candidates)), {})
            return handle.get("handle_key") if handle.get("io_type") == "target" else None
        if len(edges) == 1:
            edge = edges[0]
            return source(edge.get("source_node_uuid"), edge.get("source_handle_uuid"))
        if edges:
            return None
        # 嵌套输入路由可能指向叶节点，而不是保留一条实体数据边。
        # 只有实际祖先能提供该路由，同级操作不能为此节点注入来源。
        routes = []
        parent_id = nodes.get(node_id, {}).get("parent_uuid")
        ancestors = set()
        while parent_id in nodes and parent_id not in ancestors:
            ancestors.add(parent_id)
            parent = nodes[parent_id]
            composite = parent.get("meta_data", {}).get("unilab", {}).get("composite", {})
            for boundary_id, destinations in composite.get("target_mappings", {}).items():
                if any(
                    (item.get("workflow_node_uuid"), item.get("target_handle_uuid")) == key
                    for item in destinations
                ):
                    routes.append((parent_id, boundary_id))
            parent_id = parent.get("parent_uuid")
        return target(*routes[0]) if len(routes) == 1 else None

    def source(node_id: str, handle_id: str) -> str | None:
        key = (node_id, handle_id)
        if key in seen:
            return None
        seen.add(key)
        node, handle = nodes.get(node_id), handles.get(handle_id)
        if node is None or handle is None or handle.get("io_type") != "source":
            return None
        template_id = node.get("workflow_node_template_uuid")
        if handle.get("workflow_node_template_uuid") != template_id:
            return None
        if not schema_contains_resource_slot(handle_value_schema(handle)):
            return None
        native = node.get("meta_data", {}).get("unilab", {})
        composite_source = native.get("composite", {}).get("source_mappings", {}).get(handle_id, {})
        if composite_source.get("kind") == "node_output":
            return source(
                composite_source.get("workflow_node_uuid"),
                composite_source.get("source_handle_uuid"),
            )
        if composite_source.get("kind") == "workflow_input":
            targets = [
                h for h in handles.values()
                if h.get("workflow_node_template_uuid") == template_id
                and h.get("io_type") == "target"
                and h.get("handle_key") == composite_source.get("parameter")
            ]
            return target(node_id, targets[0]["uuid"]) if len(targets) == 1 else None
        input_id = native.get("material_passthrough_handles", {}).get(handle_id)
        input_handle = handles.get(input_id, {})
        if (
            input_handle.get("workflow_node_template_uuid") != template_id
            or input_handle.get("io_type") != "target"
            or input_handle.get("handle_key") != handle.get("handle_key")
            or not schema_contains_resource_slot(handle_value_schema(input_handle))
        ):
            return None
        return target(node_id, input_id)

    return source(mapping.get("workflow_node_uuid"), mapping.get("source_handle_uuid"))
