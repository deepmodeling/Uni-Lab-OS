"""库存声明与运行时需求分开：来源引用只在所选节点就绪后求值。"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from unilabos.protocol.materials import InventoryRequirement
from .input_binding import input_references
from .value_binding import remap_result_bindings


def normalize_inventory(items: Any, *, template: bool = False) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError("库存需求必须是列表")
    normalized = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("每项库存需求必须是对象")
        probe, bindings = dict(item), {}
        for key, value in item.items():
            if isinstance(value, Mapping):
                if key not in {"material_uuid", "template_uuid", "lot_uuid", "parent_material_uuid", "site_uuid", "quantity", "unit"}:
                    raise ValueError(f"库存字段 {key} 不能使用动态引用")
                if value.get("kind") == "workflow_input":
                    input_references(value)
                elif value.get("kind") == "node_output":
                    remap_result_bindings(value, lambda identity: identity, template=template)
                else:
                    raise ValueError(f"库存字段 {key} 必须是值或入口/结果引用")
                bindings[key] = deepcopy(value)
                probe[key] = 1.0 if key == "quantity" else "declared-reference"
        value = InventoryRequirement.model_validate(probe).model_dump(mode="json", exclude_none=False)
        value.update(bindings)
        normalized.append(value)
    keys = [item["key"] for item in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("同一步骤的库存需求 key 不能重复")
    return normalized


def inventory_deferred(requirements: list[dict[str, Any]], *, policy: Mapping[str, Any], mode: str = "task") -> bool:
    if mode not in {"task", "on_ready"}:
        raise ValueError("inventory_mode 必须是 task 或 on_ready")
    return bool(policy.get("condition_guards") or mode == "on_ready" or any(
        isinstance(value, Mapping) for item in requirements for value in item.values()
    ))
