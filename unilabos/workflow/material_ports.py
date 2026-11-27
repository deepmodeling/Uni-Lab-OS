"""冻结工作流公开声明的物料边界；不从设备图推测物料语义。"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Mapping
from uuid import UUID


class MaterialPortError(ValueError):
    """显式物料端口与边界合同不一致。"""


def _path(value: Any) -> list[str | int]:
    if not isinstance(value, str) or len(value) > 1024 or not value.startswith("$"):
        raise MaterialPortError("物料端口必须使用固定值路径")
    parts: list[str | int] = []
    rest = value[1:]
    while rest:
        match = re.match(r"(?:\.([A-Za-z0-9_]+)|\[([0-9]+)\])", rest)
        if match is None:
            raise MaterialPortError("物料端口禁止通配符或表达式路径")
        part = match[1] if match[1] is not None else int(match[2])
        if isinstance(part, int) and part > 10000:
            raise MaterialPortError("物料端口数组下标超过限制")
        parts.append(part)
        rest = rest[match.end():]
    if not 1 <= len(parts) <= 32:
        raise MaterialPortError("物料端口路径深度无效")
    return parts


def validate_material_ports(raw: Any, input_contract: Mapping[str, Any],
                            output_contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    """只接受显式、闭合、与公开参数类型及 ResourceSlot 模板约束一致的声明。"""
    if not isinstance(raw, list) or len(raw) > 100:
        raise MaterialPortError("物料端口必须为最多 100 项的数组")
    names: set[tuple[str, str]] = set()
    paths: set[tuple[str, tuple[str | int, ...]]] = set()
    for port in raw:
        if not isinstance(port, dict) or set(port) != {
            "name", "direction", "value_path", "local_value_kind", "cardinality", "resource_template_uuid",
        }:
            raise MaterialPortError("物料端口字段缺失或包含未知字段")
        if (not isinstance(port["name"], str) or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", port["name"])
                or port["direction"] not in ("input", "output")
                or port["local_value_kind"] not in ("material_uuid", "material_ref")
                or port["cardinality"] not in ("one", "many")):
            raise MaterialPortError("物料端口名称、方向或类型无效")
        template = port["resource_template_uuid"]
        try:
            if not isinstance(template, str) or str(UUID(template)) != template:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise MaterialPortError("物料端口资源模板必须为规范 UUID") from None
        name = (port["direction"], port["name"])
        segments = _path(port["value_path"])
        path = (port["direction"], tuple(segments))
        if name in names or path in paths:
            raise MaterialPortError("同方向物料端口名称或路径重复")
        names.add(name)
        paths.add(path)
        descriptors = input_contract["parameters"] if port["direction"] == "input" else output_contract["outputs"]
        schema: dict[str, Any] = {"type": "object", "properties": {item["name"]: item["schema"] for item in descriptors}}
        allowed: list[str] = []
        try:
            for segment in segments:
                if schema.get("$slot") == "ResourceSlot":
                    allowed = schema.get("allowed_resource_template_uuids", [])
                    schema = {"type": "object", "properties": {"uuid": {"type": "string", "format": "uuid"}},
                              "allowed_resource_template_uuids": schema.get("allowed_resource_template_uuids", [])}
                schema = schema["properties"][segment] if isinstance(segment, str) else schema["items"]
            if port["cardinality"] == "many":
                if schema.get("type") != "array":
                    raise KeyError("items")
                schema = schema["items"]
            if port["local_value_kind"] == "material_uuid":
                valid = schema.get("type") == "string"
            else:
                valid = schema.get("$slot") == "ResourceSlot"
            allowed = schema.get("allowed_resource_template_uuids", allowed)
            if not valid or (allowed and template not in allowed):
                raise KeyError("type")
        except (KeyError, TypeError):
            raise MaterialPortError("物料端口路径、形态、基数或资源模板与公开边界不一致") from None
    return deepcopy(raw)


def constrain_projected_material_schema(ports: list[dict[str, Any]], input_schema: dict[str, Any],
                                       output_schema: dict[str, Any]) -> None:
    """显式 UUID 声明收紧中心投影；本地普通 string 合同和旧摘要保持原样。"""
    for port in ports:
        if port["local_value_kind"] != "material_uuid":
            continue
        schema = input_schema if port["direction"] == "input" else output_schema
        for segment in _path(port["value_path"]):
            schema = schema["properties"][segment] if isinstance(segment, str) else schema["items"]
        if port["cardinality"] == "many":
            schema = schema["items"]
        schema["format"] = "uuid"


def ports_from_graph(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    """冻结图中的原声明是唯一来源，缺省为空，不修改旧图及其指纹。"""
    public_metadata = graph["workflow"]["meta_data"]
    metadata = public_metadata.get("unilab", {})
    # 作者通过公开元数据声明边界，不允许覆盖系统保留的 unilab 命名空间。
    ports = public_metadata.get("center_material_ports", metadata.get("material_ports", []))
    if "center_material_ports" in public_metadata and "material_ports" in metadata and ports != metadata["material_ports"]:
        raise MaterialPortError("公开物料端口与遗留系统声明不一致")
    if not ports:
        return []
    if "input_contract" not in metadata or "output_contract" not in metadata:
        raise MaterialPortError("声明物料端口时必须同时冻结输入输出合同")
    return validate_material_ports(ports,
                                   metadata["input_contract"], metadata["output_contract"])


def output_material_ids(ports: list[dict[str, Any]], output: dict[str, Any]) -> set[str]:
    """按已批准固定路径解析真实输出，ResourceSlot 仅取公开 uuid 字段。"""
    identities: set[str] = set()
    for port in ports:
        if port["direction"] != "output":
            continue
        try:
            value: Any = output
            for segment in _path(port["value_path"]):
                value = value[segment]
            values = value if port["cardinality"] == "many" else [value]
            if not isinstance(values, list):
                raise ValueError
            for item in values:
                identity = item["uuid"] if port["local_value_kind"] == "material_ref" else item
                if not isinstance(identity, str) or str(UUID(identity)) != identity:
                    raise ValueError
                identities.add(identity)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise MaterialPortError("真实工作流输出不满足物料端口声明") from None
    return identities


def output_material_items(ports: list[dict[str, Any]], output: dict[str, Any]) -> list[dict[str, str]]:
    """稳定 item_key 使用真实本地 UUID；不使用可能重排的数组下标。"""
    output_material_ids(ports, output)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for port in ports:
        if port["direction"] != "output":
            continue
        value: Any = output
        for segment in _path(port["value_path"]):
            value = value[segment]
        for item in value if port["cardinality"] == "many" else [value]:
            identity = item["uuid"] if port["local_value_kind"] == "material_ref" else item
            if identity in seen:
                raise MaterialPortError("同一本地物料不能占用多个稳定输出身份")
            seen.add(identity)
            result.append({"material_port": port["name"], "item_key": identity,
                           "local_material_uuid": identity, "resource_template_uuid": port["resource_template_uuid"]})
    return result


def input_material_items(ports: list[dict[str, Any]], value: dict[str, Any]) -> list[dict[str, str]]:
    """从冻结公开入口取实际目标 UUID 和模板；当前物料 DAG 只接受单值入口。"""
    result = []
    seen: set[str] = set()
    for port in ports:
        if port["direction"] != "input":
            continue
        try:
            item: Any = value
            for segment in _path(port["value_path"]):
                item = item[segment]
            identity = item["uuid"] if port["local_value_kind"] == "material_ref" else item
            if (port["cardinality"] != "one" or not isinstance(identity, str)
                    or str(UUID(identity)) != identity or identity in seen):
                raise ValueError
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise MaterialPortError("目标输入不满足唯一单值物料入口声明") from None
        seen.add(identity)
        result.append({"local_material_uuid": identity, "resource_template_uuid": port["resource_template_uuid"]})
    return result
