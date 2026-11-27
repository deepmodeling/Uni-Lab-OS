"""搬运工作流的冻结许可与送达回执；不把导航完成推断为物料收货。"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Mapping
from uuid import UUID

from unilabos.center_protocol.digest import canonical_json, is_digest

_FIELDS = {
    "schema_version",
    "handoff_uuid",
    "authorization_uuid",
    "logistics_station_uuid",
    "source_station_uuid",
    "target_station_uuid",
    "vehicle_id",
    "container_uuids",
}


def validate_transport_context(value: Any) -> dict[str, Any]:
    """验证中心许可的完整身份，返回独立副本。"""
    if isinstance(value, str):
        from unilabos.center_protocol.placement import _pairs
        value = json.loads(value, object_pairs_hook=_pairs)
    fields = _FIELDS | ({"placement_plan"} if isinstance(value, Mapping) and value.get("schema_version") == 2 else set())
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("搬运许可字段不完整或含未知字段")
    if type(value["schema_version"]) is not int or value["schema_version"] not in {1, 2}:
        raise ValueError("搬运许可 schema version 不兼容")
    for key in _FIELDS - {"schema_version", "vehicle_id", "container_uuids"}:
        _uuid(value[key])
    if not isinstance(value["vehicle_id"], str) or re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", value["vehicle_id"]) is None:
        raise ValueError("搬运车辆身份无效")
    containers = value["container_uuids"]
    if not isinstance(containers, list) or not 1 <= len(containers) <= 100:
        raise ValueError("搬运容器列表无效")
    for identity in containers:
        _uuid(identity)
    if len(set(containers)) != len(containers):
        raise ValueError("搬运容器重复")
    if value["source_station_uuid"] == value["target_station_uuid"]:
        raise ValueError("搬运源与目标工站相同")
    if value["schema_version"] == 2:
        from unilabos.center_protocol.placement import validate_execution_placement
        plan = validate_execution_placement(value["placement_plan"])
        if set(containers) != {item["container_uuid"] for item in plan["selection"]["items"]}:
            raise ValueError("搬运许可未精确覆盖位置计划中的载体")
    return deepcopy(dict(value))


def delivery_receipt(context: Mapping[str, Any] | str, *, delivered: bool, evidence_refs: list[str]) -> str:
    """在卸载动作提供实际送达证明后构造工作流输出，调用方仍须提供证据。"""
    context = validate_transport_context(context)
    if delivered is not True or not isinstance(evidence_refs, list) or not 1 <= len(evidence_refs) <= 32:
        raise ValueError("送达回执须明确 delivered=True，并提供 1 至 32 条实际卸载证据")
    if any(not isinstance(item, str) or not item.strip() or len(item.encode()) > 2048 for item in evidence_refs):
        raise ValueError("送达证据引用无效")
    return canonical_json(
        {
            "schema_version": context["schema_version"],
            **({"execution_plan_digest": context["placement_plan"]["execution_plan_digest"]} if context["schema_version"] == 2 else {}),
            "handoff_uuid": context["handoff_uuid"],
            "authorization_uuid": context["authorization_uuid"],
            "vehicle_id": context["vehicle_id"],
            "delivered": True,
            "evidence_refs": list(evidence_refs),
        }
    ).decode("utf-8")


def validate_pickup_proof(value: Any) -> dict[str, Any]:
    """验证可信中心快照内的原取货事实；缺省字段不改变旧交接摘要。"""
    identities = {"event_uuid", "station_invocation_uuid", "logistics_station_uuid", "instance_uuid",
                  "local_task_uuid", "handoff_uuid", "authorization_uuid", "job_uuid", "node_uuid"}
    fields = identities | {"schema_version", "event_digest", "task_payload_digest", "vehicle_id",
                           "container_uuids", "picked_up_at_ms", "evidence_refs"}
    if isinstance(value, Mapping) and value.get("schema_version") == 2:
        fields.add("execution_plan_digest")
        if not is_digest(value.get("execution_plan_digest")):
            raise ValueError("取货证明缺少位置计划摘要")
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("取货证明字段缺失或含未知字段")
    if type(value["schema_version"]) is not int or value["schema_version"] not in {1, 2}:
        raise ValueError("取货证明 schema version 不兼容")
    for identity in identities:
        _uuid(value[identity])
    if (not is_digest(value["event_digest"]) or not is_digest(value["task_payload_digest"])
            or type(value["picked_up_at_ms"]) is not int or not 0 <= value["picked_up_at_ms"] <= 2**53 - 1):
        raise ValueError("取货证明摘要或时间无效")
    _pickup_fields(value)
    return deepcopy(dict(value))


def _pickup_fields(value: Mapping[str, Any]) -> None:
    if not isinstance(value["vehicle_id"], str) or re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", value["vehicle_id"]) is None:
        raise ValueError("取货车辆身份无效")
    containers = value["container_uuids"]
    if not isinstance(containers, list) or not 1 <= len(containers) <= 100:
        raise ValueError("取货容器列表无效")
    for identity in containers:
        _uuid(identity)
    if len(set(containers)) != len(containers):
        raise ValueError("取货容器重复")
    refs = value["evidence_refs"]
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 32
            or any(not isinstance(ref, str) or not ref.strip() or len(ref.encode()) > 2048 for ref in refs)):
        raise ValueError("取货必须提供实际装载证据引用")


def pickup_receipt(context: Mapping[str, Any] | str, *, picked_up: bool, evidence_refs: list[str]) -> str:
    """设备完成实际装载后返回明确回执；不把开始运行或导航完成当作取货。"""
    context = validate_transport_context(context)
    value = {key: context[key] for key in ("schema_version", "handoff_uuid", "authorization_uuid", "vehicle_id", "container_uuids")}
    value.update(picked_up=picked_up, evidence_refs=evidence_refs)
    if context["schema_version"] == 2:
        value["execution_plan_digest"] = context["placement_plan"]["execution_plan_digest"]
    validate_pickup_receipt(value, context)
    return canonical_json(value).decode()


def validate_pickup_receipt(value: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    """设备回执须精确覆盖原冻结许可中的整批容器。"""
    if isinstance(value, str):
        from unilabos.center_protocol.placement import _pairs
        value = json.loads(value, object_pairs_hook=_pairs)
    fields = {"schema_version", "handoff_uuid", "authorization_uuid", "vehicle_id", "container_uuids", "picked_up", "evidence_refs"}
    context = validate_transport_context(context)
    if context["schema_version"] == 2:
        fields.add("execution_plan_digest")
        if not isinstance(value, Mapping) or value.get("execution_plan_digest") != context["placement_plan"]["execution_plan_digest"]:
            raise ValueError("取货回执替换了位置计划摘要")
    if not isinstance(value, Mapping) or set(value) != fields or value["picked_up"] is not True:
        raise ValueError("取货回执必须明确 picked_up=True 且字段完整")
    context = validate_transport_context(context)
    if (type(value["schema_version"]) is not int or value["schema_version"] != context["schema_version"]
            or any(value[key] != context[key] for key in ("handoff_uuid", "authorization_uuid", "vehicle_id"))):
        raise ValueError("取货回执与冻结许可不一致")
    _pickup_fields(value)
    if set(value["container_uuids"]) != set(context["container_uuids"]):
        raise ValueError("取货回执必须覆盖冻结许可的全部容器")
    return deepcopy(dict(value))


def validate_transport_progress(value: Any) -> dict[str, Any]:
    """验证中间取货事件；节点与作业身份由本站不可变执行结果补齐。"""
    fields = {"schema_version", "stage", "handoff_uuid", "authorization_uuid", "vehicle_id",
              "container_uuids", "job_uuid", "node_uuid", "evidence_refs"}
    if isinstance(value, Mapping) and value.get("schema_version") == 2:
        fields.add("execution_plan_digest")
        if not is_digest(value.get("execution_plan_digest")):
            raise ValueError("取货事件缺少执行位置摘要")
    if (not isinstance(value, Mapping) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] not in {1, 2}
            or value["stage"] != "picked_up"):
        raise ValueError("运输阶段事件只接受完整 picked_up 事实")
    for key in ("handoff_uuid", "authorization_uuid", "job_uuid", "node_uuid"):
        _uuid(value[key])
    _pickup_fields(value)
    return deepcopy(dict(value))


def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("搬运身份须为规范 UUID") from error
