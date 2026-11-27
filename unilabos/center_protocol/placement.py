"""跨站搬运的逐件位置计划；只验证协议，不创建库存或授权事实。"""
from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

from unilabos.center_protocol.digest import digest, is_digest

_SELECTION_FIELDS = frozenset({"selection_uuid", "source_site_uuid", "source_material_uuid", "source_material_revision", "container_uuid", "expected_container_state_version", "target_site_uuid", "target_resource_template_uuid"})

def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("位置计划包含重复 JSON 字段")
        out[key] = value
    return out

def _object(value: Any, fields: set[str] | frozenset[str]) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value, object_pairs_hook=_pairs)
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("位置计划字段缺失或包含未知字段")
    return deepcopy(dict(value))

def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("位置计划身份必须为非空规范 UUID") from error

def _version(value: Any) -> None:
    if type(value) is not int or not 1 <= value <= 2**53 - 1:
        raise ValueError("库存版本必须为正安全整数")

def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("搬运清单必须为 1 至 100 项")
    return value

def _unique(items, keys) -> None:
    for key in keys:
        if len({item[key] for item in items}) != len(items):
            raise ValueError("搬运清单的身份或位置重复：" + key)

def _selection_item(value: Any, *, require_source_site: bool = False) -> dict[str, Any]:
    fields = _SELECTION_FIELDS if require_source_site or "source_site_uuid" in value else _SELECTION_FIELDS - {"source_site_uuid"}
    value = _object(value, fields)
    for key in fields - {"source_material_revision", "expected_container_state_version"}:
        _uuid(value[key])
    _version(value["source_material_revision"])
    _version(value["expected_container_state_version"])
    return value

def validate_transfer_selection(value: Any, *, require_source_site: bool = True) -> dict[str, Any]:
    result = _object(value, {"schema_version", "items"})
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("不支持的搬运选择版本")
    result["items"] = [_selection_item(i, require_source_site=require_source_site) for i in _items(result["items"])]
    _unique(result["items"], ("selection_uuid", "source_material_uuid", "container_uuid", "target_site_uuid"))
    _unique([item for item in result["items"] if "source_site_uuid" in item], ("source_site_uuid",))
    return result

def selection_digest(value: Mapping[str, Any], transfer_definition_uuid: str) -> str:
    result = validate_transfer_selection(value, require_source_site=False)
    _uuid(transfer_definition_uuid)
    result["items"].sort(key=lambda item: item["selection_uuid"])
    return digest({"transfer_definition_uuid": transfer_definition_uuid, "selection": result})

def validate_transfer_destinations(value: Any) -> dict[str, Any]:
    result = _object(value, {"schema_version", "items"})
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("不支持的目标位计划版本")
    items = []
    for item in _items(result["items"]):
        item = _object(item, {"selection_uuid", "source_port", "target_site_uuid", "target_resource_template_uuid"})
        for key in ("selection_uuid", "target_site_uuid", "target_resource_template_uuid"):
            _uuid(item[key])
        if not isinstance(item["source_port"], str) or not item["source_port"].strip() or len(item["source_port"].encode()) > 200:
            raise ValueError("物料输出端口无效")
        items.append(item)
    _unique(items, ("selection_uuid", "source_port", "target_site_uuid"))
    result["items"] = items
    return result

def validate_item_placement(value: Any) -> dict[str, Any]:
    result = _object(value, {"transfer_definition_uuid", "selection_digest", "selection"})
    _uuid(result["transfer_definition_uuid"])
    if not is_digest(result["selection_digest"]):
        raise ValueError("选择摘要无效")
    result["selection"] = _selection_item(result["selection"])
    return result

def validate_prepared_placement(value: Any) -> dict[str, Any]:
    result = _object(value, {"selection_digest", "items"})
    if not is_digest(result["selection_digest"]):
        raise ValueError("目标预留的选择摘要无效")
    items = []
    fields = {"selection_uuid", "handoff_item_uuid", "container_uuid", "target_site_uuid", "reservation_uuid", "reservation_revision"}
    for item in _items(result["items"]):
        item = _object(item, fields)
        for key in fields - {"reservation_revision"}:
            _uuid(item[key])
        _version(item["reservation_revision"])
        items.append(item)
    _unique(items, tuple(fields - {"reservation_revision"}))
    result["items"] = items
    return result

def execution_plan_digest(value: Mapping[str, Any]) -> str:
    selection = validate_transfer_selection(value["selection"], require_source_site=False)
    selection["items"].sort(key=lambda item: item["selection_uuid"])
    prepared = validate_prepared_placement(value["prepared"])
    prepared["items"].sort(key=lambda item: item["selection_uuid"])
    return digest({"schema_version": value["schema_version"], "transfer_definition_uuid": value["transfer_definition_uuid"], "selection": selection, "prepared": prepared})

def validate_execution_placement(value: Any) -> dict[str, Any]:
    result = _object(value, {"schema_version", "transfer_definition_uuid", "selection", "prepared", "execution_plan_digest"})
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("执行位置计划版本无效")
    _uuid(result["transfer_definition_uuid"])
    result["selection"] = validate_transfer_selection(result["selection"], require_source_site=False)
    result["prepared"] = validate_prepared_placement(result["prepared"])
    if result["prepared"]["selection_digest"] != selection_digest(result["selection"], result["transfer_definition_uuid"]):
        raise ValueError("目标预留的原选择摘要不匹配")
    selected = {i["selection_uuid"]: i for i in result["selection"]["items"]}
    if len(result["prepared"]["items"]) != len(selected):
        raise ValueError("目标预留未覆盖整张清单")
    for reservation in result["prepared"]["items"]:
        item = selected.get(reservation["selection_uuid"])
        if item is None or any(item[key] != reservation[key] for key in ("container_uuid", "target_site_uuid")):
            raise ValueError("目标预留与搬运项不一致")
    if not is_digest(result["execution_plan_digest"]) or execution_plan_digest(result) != result["execution_plan_digest"]:
        raise ValueError("执行位置计划摘要不匹配")
    return result


def validate_handoff_placements(payload: Mapping[str, Any]) -> None:
    """明细位置计划必须覆盖整单，且绑定原载体和真实来源引用。"""
    plans = [i.get("placement_plan") for i in payload["items"]]
    if not any(plan is not None for plan in plans):
        return
    if any(plan is None for plan in plans):
        raise ValueError("精确位置计划必须覆盖全部交接明细")
    checked = [validate_item_placement(plan) for plan in plans]
    transfer = checked[0]["transfer_definition_uuid"]
    selection = {"schema_version": 1, "items": [plan["selection"] for plan in checked]}
    expected = selection_digest(selection, transfer)
    for item, plan in zip(payload["items"], checked):
        selected = plan["selection"]
        if payload.get("transfer_source_kind") != "node_output" and "source_site_uuid" not in selected:
            raise ValueError("直接库存搬运必须固定真实来源位")
        if plan["transfer_definition_uuid"] != transfer or plan["selection_digest"] != expected or (payload.get("transfer_definition_uuid") is not None and payload["transfer_definition_uuid"] != transfer):
            raise ValueError("交接明细引用了不同的位置计划")
        if item["container_uuid"] != selected["container_uuid"] or item["expected_state_version"] != selected["expected_container_state_version"]:
            raise ValueError("位置计划的载体身份或版本不符")
        if not any(ref["local_material_uuid"] == selected["source_material_uuid"] and ref["local_revision"] == selected["source_material_revision"] for ref in item["source_material_refs"]):
            raise ValueError("位置计划未绑定实际来源物料")
    prepared = payload.get("prepared")
    if prepared is not None:
        proof = validate_prepared_placement(prepared.get("placement_reservations"))
        validate_prepared_against_items(payload, proof)


def validate_prepared_against_items(payload: Mapping[str, Any], proof: Any) -> dict[str, Any]:
    proof = validate_prepared_placement(proof)
    expected = {i["handoff_item_uuid"]: i for i in payload["items"]}
    if len(proof["items"]) != len(expected):
        raise ValueError("真实预留必须覆盖全部交接明细")
    for reservation in proof["items"]:
        item = expected.get(reservation["handoff_item_uuid"])
        if item is None:
            raise ValueError("预留引用了不同交接明细")
        plan = validate_item_placement(item.get("placement_plan"))
        selected = plan["selection"]
        if proof["selection_digest"] != plan["selection_digest"] or any(reservation[key] != selected[key] for key in ("selection_uuid", "container_uuid", "target_site_uuid")):
            raise ValueError("预留的位置与原搬运选择不符")
    return proof


def execution_from_handoff(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """从原交接与真实 prepared 事实重建摘要，不采信取货消息自带摘要。"""
    validate_handoff_placements(payload)
    plans = [item.get('placement_plan') for item in payload['items']]
    if not any(plan is not None for plan in plans):
        return None
    if payload.get('prepared') is None:
        raise ValueError('位置计划尚未获得目标预留')
    value = {'schema_version': 1, 'transfer_definition_uuid': plans[0]['transfer_definition_uuid'],
        'selection': {'schema_version': 1, 'items': [plan['selection'] for plan in plans]},
        'prepared': payload['prepared']['placement_reservations']}
    value['execution_plan_digest'] = execution_plan_digest(value)
    return validate_execution_placement(value)
