"""动作与已发布工作流共用的 Handle 投影纯函数。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def structural_ready_handle(io_type: str) -> dict[str, Any]:
    if io_type not in {"target", "source"}:
        raise ValueError("ready Handle 方向必须是 target/source")
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
    base = _non_null_schema(schema)
    if base.get("type") == "array":
        return "array"
    if resource_slot_schema(base) is not None:
        return "ResourceSlot"
    value_type = base.get("type")
    return str(value_type) if isinstance(value_type, str) else "object"


def _non_null_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
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
