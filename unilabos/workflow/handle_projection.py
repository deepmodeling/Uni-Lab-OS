"""动作与已发布工作流共用的连接点（Handle）投影纯函数。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def structural_ready_handle(io_type: str) -> dict[str, Any]:
    """投影结构性 ready 连接点（Handle）。

    参数：``io_type`` 只能是 ``target`` 或 ``source``。返回：与后端（Backend）
    动作模板一致的结构连接点候选，确保组合工作流调用
    （CompositeWorkflowInvocation）可与普通动作相连。异常：方向非法时抛出
    ``ValueError``。
    """

    if io_type not in {"target", "source"}:
        raise ValueError("ready 连接点方向必须是 target/source")
    return {
        "handle_key": "ready",
        "io_type": io_type,
        "display_name": "ready",
        "description": None,
        "type": "default",
        "required": False,
        "data_source": None,
        "data_key": None,
        "meta_data": {},
    }


def resource_slot_schema(schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """查找标量、数组或可空 Schema 内唯一可见的物料占位符（ResourceSlot）。

    参数：``schema`` 是已由工作流合同 parser 规范化的值 Schema。返回：最内层
    物料占位符 Schema，未包含时为 ``None``。异常：无；结构异常由上游 parser
    负责关闭失败。
    """

    if schema.get("$slot") == "ResourceSlot":
        return schema
    items = schema.get("items")
    if isinstance(items, Mapping):
        found = resource_slot_schema(items)
        if found is not None:
            return found
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping):
                found = resource_slot_schema(member)
                if found is not None:
                    return found
    return None


def workflow_handle_type(schema: Mapping[str, Any]) -> str:
    """把规范工作流值 Schema 投影为后端形态连接点类型。

    参数：``schema`` 是规范值 Schema。返回：数组保持 ``array``，物料引用使用
    ``ResourceSlot``，其他类型保留 JSON 类型，未知形状为 ``object``。
    异常：无；未知形状保守投影为 ``object``。
    """

    base = _non_null_schema(schema)
    if base.get("type") == "array":
        return "array"
    if resource_slot_schema(base) is not None:
        return "ResourceSlot"
    value_type = base.get("type")
    return str(value_type) if isinstance(value_type, str) else "object"


def _non_null_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """从规范可空 Schema 中返回非 null 成员，否则返回原 Schema。

    参数：``schema`` 是规范或可空值 Schema。返回：首个非 null 映射成员，找不到
    时返回原映射。异常：无；非列表 ``anyOf`` 按普通 Schema 处理。
    """

    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping) and member.get("type") != "null":
                return member
    return schema


__all__ = [
    "resource_slot_schema",
    "structural_ready_handle",
    "workflow_handle_type",
]
