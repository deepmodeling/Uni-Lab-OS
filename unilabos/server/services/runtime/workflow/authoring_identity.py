"""组合工作流静态展开使用的确定性身份规则。"""

from __future__ import annotations

from uuid import UUID, uuid5

from unilabos.protocol.runtime.workflow import validate_uuid

_COMPOSITE_NODE_PREFIX = "unilabos:c1:node:v1:"


def expanded_node_uuid(invocation_uuid: str, child_node_uuid: str) -> str:
    """把子节点身份派生到一次组合调用的 UUIDv5 命名空间。"""

    namespace = UUID(validate_uuid(invocation_uuid))
    child = validate_uuid(child_node_uuid)
    return str(uuid5(namespace, _COMPOSITE_NODE_PREFIX + child))


def authoring_edge_uuid(
    *,
    workflow_uuid: str,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
) -> str:
    """按父工作流和完整端点生成稳定的边 UUIDv5。"""

    namespace = UUID(validate_uuid(workflow_uuid))
    source_node = validate_uuid(source_node_uuid)
    source_handle = validate_uuid(source_handle_uuid)
    target_node = validate_uuid(target_node_uuid)
    target_handle = validate_uuid(target_handle_uuid)
    name = (
        f"authoring-edge:{source_node}:{source_handle}:"
        f"{target_node}:{target_handle}"
    )
    return str(uuid5(namespace, name))


__all__ = ["authoring_edge_uuid", "expanded_node_uuid"]
