"""从冻结工作流解析中心库位控件；不读取当前工作流或当前库存。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID


def validate_site_binding(value: Any) -> None:
    """校验对中心发布的闭合库位关系，拒绝扩展字段和非规范身份。"""
    if not isinstance(value, dict) or set(value) != {
        "version", "owner", "value_kind", "allowed_resource_template_uuids", "show_occupied", "allow_occupied"
    }:
        raise ValueError("库位选择合同字段无效")
    if type(value["version"]) is not int or value["version"] != 1 or value["value_kind"] != "uuid":
        raise ValueError("库位选择合同版本或值类型无效")
    if any(type(value[key]) is not bool for key in ("show_occupied", "allow_occupied")):
        raise ValueError("库位占用策略无效")
    owner = value["owner"]
    if not isinstance(owner, dict):
        raise ValueError("库位所属物料无效")
    if owner.get("kind") == "fixed" and set(owner) == {"kind", "material_uuid"}:
        _uuid(owner["material_uuid"])
    elif owner.get("kind") == "input" and set(owner) == {"kind", "parameter"}:
        if not isinstance(owner["parameter"], str) or not owner["parameter"] or owner["parameter"] != owner["parameter"].strip():
            raise ValueError("库位所属输入无效")
    else:
        raise ValueError("库位所属物料必须固定或来自工作流输入")
    templates = value["allowed_resource_template_uuids"]
    if not isinstance(templates, list) or len(templates) > 256 or len(set(map(str, templates))) != len(templates):
        raise ValueError("库位模板约束无效")
    for template in templates:
        _uuid(template)


def _uuid(value: Any) -> None:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("库位合同身份必须为规范 UUID")


def site_bindings_from_graph(graph: Mapping[str, Any]) -> dict[str, Any]:
    """显式启用后发布库位输入的所属载体、模板与占用策略。

    动作沿用 SiteSelector；物料来源及 MaterialIO 使用其已有库位绑定。旧合同
    没有能力声明时保持原投影，避免同一不可变合同在升级后改变摘要。
    """
    metadata = graph.get("workflow", {}).get("meta_data", {})
    if "center_site_selectors" not in metadata:
        return {}
    capability = metadata["center_site_selectors"]
    if (not isinstance(capability, dict) or set(capability) != {"schema_version"}
            or type(capability["schema_version"]) is not int or capability["schema_version"] != 1):
        raise ValueError("中心库位选择能力声明无效")
    handles = {h["uuid"]: h for h in graph.get("handle_templates", [])}
    result: dict[str, Any] = {}

    def add(parameter, owner, templates=(), *, show=True, allow=False):
        if not isinstance(parameter, str) or not parameter:
            raise ValueError("库位输入缺少工作流参数")
        if not isinstance(owner, Mapping):
            raise ValueError("中心库位选择器的所属物料必须固定或来自工作流输入")
        descriptor = {
            "version": 1, "owner": dict(owner), "value_kind": "uuid",
            "allowed_resource_template_uuids": sorted(set(templates)),
            "show_occupied": show, "allow_occupied": allow,
        }
        if parameter in result:
            previous = result[parameter]
            if previous["owner"] != descriptor["owner"]:
                raise ValueError(f"工作流库位输入 {parameter} 的多个用途具有不同所属物料")
            descriptor["allowed_resource_template_uuids"] = sorted(set(
                previous["allowed_resource_template_uuids"] + descriptor["allowed_resource_template_uuids"]))
            descriptor["allow_occupied"] = previous["allow_occupied"] and allow
            descriptor["show_occupied"] = previous["show_occupied"] or show
        result[parameter] = descriptor

    for node in graph.get("nodes", []):
        params = node.get("param") or {}
        unilab = (node.get("meta_data") or {}).get("unilab", {})
        bindings = unilab.get("input_bindings") or {}
        node_handles = {h.get("handle_key"): h for h in handles.values()
                        if h.get("workflow_node_template_uuid") == node.get("workflow_node_template_uuid")
                        and h.get("io_type") == "target"}

        def owner_of(name):
            literal = params.get(name)
            if isinstance(literal, Mapping) and literal.get("uuid"):
                return {"kind": "fixed", "material_uuid": literal["uuid"]}
            handle = node_handles.get(name, {})
            binding = bindings.get(handle.get("uuid"), {})
            if binding.get("parameter"):
                return {"kind": "input", "parameter": binding["parameter"]}
            return None

        for key, allow in (("material_source_site_binding", True), ("material_io_site_binding", False)):
            binding = unilab.get(key)
            if isinstance(binding, Mapping) and "parameter" in binding:
                template = params.get("resource_template_uuid")
                add(binding["parameter"], owner_of("mount"), [template] if template else [], allow=allow)
        for handle_uuid, binding in bindings.items():
            handle = handles.get(handle_uuid, {})
            selector = (handle.get("meta_data") or {}).get("unilab", {}).get("site_selector")
            if not isinstance(selector, Mapping):
                continue
            templates = []
            occupant = node_handles.get(selector.get("occupant"), {})
            schema = (occupant.get("meta_data") or {}).get("unilab", {}).get("value_schema", {})
            if "anyOf" in schema:
                schema = schema["anyOf"][0]
            templates = schema.get("allowed_resource_template_uuids", [])
            if unilab.get("local_command") == "unilabos.workflow.native_commands:InventoryCommands.record_produced":
                template = params.get("template_uuid")
                templates = [template] if template else []
            add(binding.get("parameter"), owner_of(selector["owner"]), templates,
                show=selector["show_occupied"], allow=selector["allow_occupied"])
    for descriptor in result.values():
        validate_site_binding(descriptor)
    return deepcopy(result)
