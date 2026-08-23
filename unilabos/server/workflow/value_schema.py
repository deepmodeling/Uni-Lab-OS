"""组合工作流边界使用的 JSON 值 Schema 工具。

该模块对应上游 ``unilabos.workflow.schema`` 的值合同部分。当前目录已有
SQLite ``schema.py``，因此按服务端命名约定单独放在 ``value_schema.py``。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from unilabos.server.workflow.handle_projection import resource_slot_schema
from unilabos.server.workflow.models import validate_uuid


class WorkflowValueSchemaError(ValueError):
    """值 Schema 或边界约束不能安全解释。"""

    def __init__(self, code: str, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(code)


def normalize_value_schema(raw: Any, *, path: str = "/schema") -> dict[str, Any]:
    """校验并复制组合合同支持的 JSON Schema 子集。"""

    if not isinstance(raw, Mapping):
        raise WorkflowValueSchemaError("workflow_schema_invalid", path)
    schema = _plain(raw)
    if "$slot" in schema:
        if schema.get("$slot") != "ResourceSlot":
            raise WorkflowValueSchemaError("workflow_schema_invalid", path)
        # 模板 UUID allowlist 只供前端提示，不参与后端 Schema 判定。
        return schema
    members = schema.get("anyOf")
    if members is not None:
        if not isinstance(members, list) or not members:
            raise WorkflowValueSchemaError("workflow_schema_invalid", path)
        schema["anyOf"] = [
            normalize_value_schema(item, path=f"{path}/anyOf/{index}")
            for index, item in enumerate(members)
        ]
        return schema
    value_type = schema.get("type")
    if value_type not in {
        "null",
        "boolean",
        "integer",
        "number",
        "string",
        "array",
        "object",
    }:
        raise WorkflowValueSchemaError("workflow_schema_invalid", f"{path}/type")
    if value_type == "array":
        if "items" not in schema:
            raise WorkflowValueSchemaError("workflow_schema_invalid", f"{path}/items")
        schema["items"] = normalize_value_schema(
            schema["items"],
            path=f"{path}/items",
        )
    if value_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise WorkflowValueSchemaError(
                "workflow_schema_invalid",
                f"{path}/properties",
            )
        schema["properties"] = {
            str(name): normalize_value_schema(
                child,
                path=f"{path}/properties/{name}",
            )
            for name, child in properties.items()
            if isinstance(name, str) and name
        }
        if len(schema["properties"]) != len(properties):
            raise WorkflowValueSchemaError(
                "workflow_schema_invalid",
                f"{path}/properties",
            )
        required = schema.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) or item not in schema["properties"]
            for item in required
        ):
            raise WorkflowValueSchemaError(
                "workflow_schema_invalid",
                f"{path}/required",
            )
        schema["required"] = list(dict.fromkeys(required))
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise WorkflowValueSchemaError("workflow_schema_invalid", f"{path}/enum")
        value_schema = dict(schema)
        value_schema.pop("enum", None)
        for index, value in enumerate(enum):
            validate_value(value_schema, value, path=f"{path}/enum/{index}")
    return schema


def validate_value(
    schema: Mapping[str, Any],
    value: Any,
    *,
    path: str = "/value",
    ignore_enum: bool = False,
) -> Any:
    """按组合合同支持的子集校验一个默认值。"""

    normalized = normalize_value_schema(schema)
    if not ignore_enum and "enum" in normalized and not any(
        type(value) is type(item) and value == item for item in normalized["enum"]
    ):
        raise WorkflowValueSchemaError("workflow_value_invalid", path)
    if normalized.get("$slot") == "ResourceSlot":
        if not isinstance(value, Mapping) or not isinstance(value.get("uuid"), str):
            raise WorkflowValueSchemaError("workflow_value_invalid", path)
        try:
            identity = validate_uuid(value["uuid"])
        except (TypeError, ValueError):
            raise WorkflowValueSchemaError("workflow_value_invalid", path) from None
        normalized_value = _plain(value)
        normalized_value["uuid"] = identity
        return normalized_value
    if "anyOf" in normalized:
        for member in normalized["anyOf"]:
            try:
                return validate_value(member, value, path=path)
            except WorkflowValueSchemaError:
                continue
        raise WorkflowValueSchemaError("workflow_value_invalid", path)
    kind = normalized["type"]
    valid = (
        (kind == "null" and value is None)
        or (kind == "boolean" and type(value) is bool)
        or (kind == "integer" and type(value) is int)
        or (
            kind == "number"
            and type(value) in {int, float}
            and (type(value) is int or math.isfinite(value))
        )
        or (kind == "string" and type(value) is str)
        or (kind == "array" and isinstance(value, list))
        or (kind == "object" and isinstance(value, Mapping))
    )
    if not valid:
        raise WorkflowValueSchemaError("workflow_value_invalid", path)
    if kind == "array":
        return [
            validate_value(normalized["items"], item, path=f"{path}/{index}")
            for index, item in enumerate(value)
        ]
    if kind == "object":
        required = set(normalized.get("required", []))
        if not required.issubset(value):
            raise WorkflowValueSchemaError("workflow_value_invalid", path)
        properties = normalized.get("properties", {})
        if normalized.get("additionalProperties") is False and set(value) - set(
            properties
        ):
            raise WorkflowValueSchemaError("workflow_value_invalid", path)
        return {
            str(key): (
                validate_value(properties[key], item, path=f"{path}/{key}")
                if key in properties
                else _plain(item)
            )
            for key, item in value.items()
        }
    return value


def schema_is_assignable(
    source_schema: Mapping[str, Any],
    target_schema: Mapping[str, Any],
) -> bool:
    """判断来源合同产生的值是否都可被目标合同接收。"""

    try:
        source = normalize_value_schema(source_schema)
        target = normalize_value_schema(target_schema)
    except WorkflowValueSchemaError:
        return False
    if source == target:
        return True
    source_members = source.get("anyOf")
    target_members = target.get("anyOf")
    if isinstance(source_members, list):
        return all(
            any(schema_is_assignable(item, candidate) for candidate in target_members)
            if isinstance(target_members, list)
            else schema_is_assignable(item, target)
            for item in source_members
        )
    if isinstance(target_members, list):
        return any(schema_is_assignable(source, item) for item in target_members)
    # ResourceSlot 只有在当前层级就是 slot 时才具有相同值形状。数组或对象中
    # “包含 slot”不能让整个容器与一个标量 slot 互相可赋值。
    source_slot = source.get("$slot") == "ResourceSlot"
    target_slot = target.get("$slot") == "ResourceSlot"
    if source_slot or target_slot:
        return source_slot and target_slot
    source_type = source.get("type")
    target_type = target.get("type")
    if source_type == "integer" and target_type == "number":
        return True
    if source_type != target_type:
        return False
    if source_type == "array":
        return schema_is_assignable(source["items"], target["items"])
    if source_type == "object":
        source_properties = source.get("properties", {})
        target_properties = target.get("properties", {})
        source_required = set(source.get("required", []))
        target_required = set(target.get("required", []))
        if not target_required <= source_required:
            return False
        if any(
            name in target_properties
            and not schema_is_assignable(child, target_properties[name])
            for name, child in source_properties.items()
        ):
            return False
        if target.get("additionalProperties") is False:
            if source.get("additionalProperties") is not False:
                return False
            if set(source_properties) - set(target_properties):
                return False
        return True
    if "enum" in target:
        if "enum" not in source:
            return False
        return all(
            any(type(item) is type(candidate) and item == candidate for candidate in target["enum"])
            for item in source["enum"]
        )
    return True


def intersect_resource_constraints(
    parent_schema: Mapping[str, Any],
    child_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """保留父 ResourceSlot 形状；模板 UUID allowlist 不参与后端收窄。"""

    parent = normalize_value_schema(parent_schema)
    child = normalize_value_schema(child_schema)
    parent_slot = resource_slot_schema(parent)
    child_slot = resource_slot_schema(child)
    if parent_slot is None or child_slot is None:
        if not schema_is_assignable(parent, child):
            raise WorkflowValueSchemaError(
                "composite_boundary_mapping_invalid",
                "/schema",
            )
        return parent
    return deepcopy(parent)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "WorkflowValueSchemaError",
    "intersect_resource_constraints",
    "normalize_value_schema",
    "schema_is_assignable",
    "validate_value",
]
