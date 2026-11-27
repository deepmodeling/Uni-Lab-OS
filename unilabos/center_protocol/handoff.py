"""手工交接的严格线格式；与中心固定包、事件及精确确认共用摘要规则。"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Mapping
from uuid import UUID

from unilabos.center_protocol.digest import DigestError, canonical_json, digest, is_digest
from unilabos.center_protocol.events import MAX_EVENT_SEQUENCE, EventEnvelopeError, validate_ack


class HandoffProtocolError(ValueError):
    """交接身份、固定正文或摘要无法验证。"""


INTENT_FIELDS = frozenset({"event_uuid", "event_epoch_uuid", "station_uuid", "instance_uuid",
                           "handoff_uuid", "occurred_at_ms", "payload"})
EVENT_FIELDS = INTENT_FIELDS | {"protocol_version", "event_sequence", "event_digest"}
SNAPSHOT_FIELDS = frozenset({
    "handoff_uuid", "lab_uuid", "source_station_uuid", "target_station_uuid",
    "source_instance_uuid", "target_instance_uuid", "status", "revision", "attention",
    "attention_reasons", "items", "prepared", "released", "permit", "cancel_acks",
    "created_at_ms", "in_transit_deadline_ms",
})
STATUSES = frozenset({"requested", "prepared", "released", "dispatch_authorized", "in_transit",
                      "delivered", "partial_received", "received", "canceling", "canceled", "reconciled"})


def _object(value: Any, fields: set | frozenset) -> None:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise HandoffProtocolError("交接字段缺失或包含未知字段")


def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise HandoffProtocolError("交接身份必须为规范 UUID") from None


def _integer(value: Any, minimum: int = 1) -> None:
    if type(value) is not int or not minimum <= value <= MAX_EVENT_SEQUENCE:
        raise HandoffProtocolError("交接版本、序号或时间超出安全整数范围")


def _strings(values: Any, *, minimum: int = 1, maximum: int = 32, size: int = 2048) -> None:
    if (not isinstance(values, list) or not minimum <= len(values) <= maximum
            or any(not isinstance(v, str) or not v.strip() or len(v.encode()) > size for v in values)):
        raise HandoffProtocolError("交接证据或说明无效")


def _bounded(value: Any, limit: int = 1024 * 1024) -> None:
    try:
        if len(canonical_json(value)) > limit:
            raise HandoffProtocolError("交接正文超过大小限制")
    except (DigestError, RecursionError) as error:
        raise HandoffProtocolError("交接正文不能规范化") from error


def _snapshot(value: Any) -> None:
    if not isinstance(value, dict):
        raise HandoffProtocolError("物料快照必须为对象")
    remaining = 10000

    def visit(node: Any, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if depth > 32 or remaining < 0:
            raise HandoffProtocolError("物料快照嵌套或节点数超过限制")
        if ((type(node) is int and abs(node) > MAX_EVENT_SEQUENCE)
                or (type(node) is float and (not math.isfinite(node) or abs(node) > MAX_EVENT_SEQUENCE))):
            raise HandoffProtocolError("物料快照数值超出安全范围")
        if isinstance(node, dict):
            for item in node.values():
                visit(item, depth + 1)
        elif isinstance(node, list):
            for item in node:
                visit(item, depth + 1)

    visit(value, 0)
    _bounded(value, 65536)


def _refs(values: Any, station: str | None = None) -> None:
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise HandoffProtocolError("物料引用数量无效")
    local, lineage = set(), set()
    for ref in values:
        _object(ref, {"station_uuid", "local_material_uuid", "local_revision", "material_lineage_uuid"})
        for field in ("station_uuid", "local_material_uuid", "material_lineage_uuid"):
            _uuid(ref[field])
        _integer(ref["local_revision"])
        if ((station is not None and ref["station_uuid"] != station)
                or ref["local_material_uuid"] in local or ref["material_lineage_uuid"] in lineage):
            raise HandoffProtocolError("物料引用重复或工站归属错误")
        local.add(ref["local_material_uuid"])
        lineage.add(ref["material_lineage_uuid"])


def validate_handoff_receipt(value: Any, *, station_uuid: str | None = None) -> dict[str, Any]:
    _object(value, {"handoff_item_uuid", "target_material_refs", "target_snapshot", "receipt_digest"})
    _uuid(value["handoff_item_uuid"])
    _refs(value["target_material_refs"], station_uuid)
    _snapshot(value["target_snapshot"])
    unsigned = {key: item for key, item in value.items() if key != "receipt_digest"}
    if not is_digest(value["receipt_digest"]) or digest(unsigned) != value["receipt_digest"]:
        raise HandoffProtocolError("收货明细摘要不匹配")
    return deepcopy(dict(value))


def validate_source_verification(value: Any) -> dict[str, Any]:
    _object(value, {"handoff_item_uuid", "verification_uuid", "source_snapshot_digest", "evidence_refs", "verification_digest"})
    _uuid(value["handoff_item_uuid"])
    _uuid(value["verification_uuid"])
    _strings(value["evidence_refs"])
    if not is_digest(value["source_snapshot_digest"]):
        raise HandoffProtocolError("源快照摘要无效")
    unsigned = {k: v for k, v in value.items() if k != "verification_digest"}
    if not is_digest(value["verification_digest"]) or digest(unsigned) != value["verification_digest"]:
        raise HandoffProtocolError("源复核证明摘要不符")
    return deepcopy(dict(value))


def validate_movement_verification(value: Any) -> dict[str, Any]:
    _object(value, {"handoff_item_uuid", "verification_uuid", "review_revision", "expected_state_version",
                    "station_uuid", "physical_present", "material_refs", "inventory_snapshot",
                    "evidence_refs", "verification_digest"})
    for field in ("handoff_item_uuid", "verification_uuid", "station_uuid"):
        _uuid(value[field])
    for field in ("review_revision", "expected_state_version"):
        _integer(value[field])
    if type(value["physical_present"]) is not bool or not isinstance(value["material_refs"], list):
        raise HandoffProtocolError("新移动证明的在场标志或引用无效")
    if value["physical_present"] or value["material_refs"]:
        _refs(value["material_refs"], value["station_uuid"])
    _snapshot(value["inventory_snapshot"])
    _strings(value["evidence_refs"])
    unsigned = {k: v for k, v in value.items() if k != "verification_digest"}
    if not is_digest(value["verification_digest"]) or digest(unsigned) != value["verification_digest"]:
        raise HandoffProtocolError("新移动证明摘要不符")
    return deepcopy(dict(value))


def _coverage_fields(value: Any, fields: set[str]) -> None:
    if isinstance(value, Mapping) and "covered_transport_digests" in value:
        fields.add("covered_transport_digests")
    _object(value, fields)
    covered = value.get("covered_transport_digests", [])
    if (not isinstance(covered, list) or len(covered) > 1000
            or any(not is_digest(d) for d in covered) or len(set(covered)) != len(covered)):
        raise HandoffProtocolError("已核实运输摘要无效、重复或超过1000条")


def _settlement_fields(value: Any, handoff: Mapping[str, Any]) -> None:
    _coverage_fields(value, {"decision", "actual_holder_station_uuid", "proof_digest", "evidence_refs", "reason",
                             "reconciled_at_ms", "handoff_revision"})
    _strings(value["evidence_refs"])
    _strings([value["reason"]], size=1000)
    _integer(value["reconciled_at_ms"], 0)
    _integer(value["handoff_revision"])
    if value["handoff_revision"] > handoff["revision"]:
        raise HandoffProtocolError("核实修订超出交接修订")
    decision = value["decision"]
    if decision == "keep_quarantined":
        if value["actual_holder_station_uuid"] is not None or value["proof_digest"] is not None:
            raise HandoffProtocolError("未核实位置不能猜测持有者或证明")
    elif decision in {"source_present", "target_received"}:
        side = "source" if decision == "source_present" else "target"
        if (value["actual_holder_station_uuid"] != handoff[f"{side}_station_uuid"]
                or not is_digest(value["proof_digest"])):
            raise HandoffProtocolError("核实持有者或证明摘要无效")
    else:
        raise HandoffProtocolError("核实决策无效")


def _validate_movement_review(review: Any, item: Mapping[str, Any], handoff: Mapping[str, Any]) -> None:
    _object(review, {"review_revision", "previous_review_revision", "previous_settlement", "trigger_event_digest",
                              "cause", "source_verification", "target_verification"})
    _integer(review["review_revision"])
    if review["previous_review_revision"] is not None:
        _integer(review["previous_review_revision"])
        if review["previous_review_revision"] >= review["review_revision"]:
            raise HandoffProtocolError("新旧核实代次顺序无效")
    _strings([review["cause"]], size=1000)
    if review["trigger_event_digest"] is not None and not is_digest(review["trigger_event_digest"]):
        raise HandoffProtocolError("新移动触发事件摘要无效")
    previous = review["previous_settlement"]
    _settlement_fields(previous, handoff)
    if (not previous["handoff_revision"] < review["review_revision"] <= handoff["revision"]
            or previous["decision"] == "keep_quarantined"):
        raise HandoffProtocolError("新移动必须关联已经完成的历史结算")
    for side in ("source", "target"):
        proof = review[f"{side}_verification"]
        if proof is None:
            continue
        validate_movement_verification(proof)
        if (proof["handoff_item_uuid"] != item["handoff_item_uuid"]
                or proof["station_uuid"] != handoff[f"{side}_station_uuid"]
                or proof["review_revision"] != review["review_revision"]
                or proof["expected_state_version"] > item["current_state_version"]):
            raise HandoffProtocolError("新移动证明不属于当前明细或核实代次")
        if (proof["physical_present"] and {r["material_lineage_uuid"] for r in proof["material_refs"]}
                != {r["material_lineage_uuid"] for r in item["source_material_refs"]}):
            raise HandoffProtocolError("新移动在场证明的谱系与原明细不符")


def _validate_settlement(value: Any, item: Mapping[str, Any], handoff: Mapping[str, Any]) -> None:
    _settlement_fields(value, handoff)
    decision = value["decision"]
    if decision == "keep_quarantined":
        return
    if "movement_review" in item:
        review = item["movement_review"]
        side = "source" if decision == "source_present" else "target"
        present = review[f"{side}_verification"]
        absent = review["target_verification" if side == "source" else "source_verification"]
        if (present is None or absent is None or not present["physical_present"] or absent["physical_present"]
                or value["proof_digest"] != present["verification_digest"]
                or any(p["expected_state_version"] + 1 != item["current_state_version"] for p in (present, absent))):
            raise HandoffProtocolError("新移动结算必须匹配本轮当前版本双端证明")
    elif decision == "source_present":
        proof = item.get("source_verification")
        if (item["receipt"] is not None or proof is None or value["proof_digest"] != proof["verification_digest"]):
            raise HandoffProtocolError("回源结算缺少相符的源复核证明")
    elif decision == "target_received":
        receipt = item["receipt"]
        if receipt is None or value["proof_digest"] != receipt["receipt_digest"]:
            raise HandoffProtocolError("目标结算缺少相符的原收货证明")


def validate_handoff_snapshot(value: Any, *, station_uuid: str | None = None,
                              instance_uuid: str | None = None) -> dict[str, Any]:
    association = {"global_task_uuid", "transfer_definition_uuid", "transfer_source_kind"}
    chain = {"chain_uuid", "leg_no", "previous_handoff_uuid"}
    fields = SNAPSHOT_FIELDS
    associated = isinstance(value, Mapping) and bool(association.intersection(value))
    chained = isinstance(value, Mapping) and bool(chain.intersection(value))
    if associated:
        fields = fields | association
    if chained:
        fields = fields | {"chain_uuid", "leg_no"}
        if "previous_handoff_uuid" in value:
            fields = fields | {"previous_handoff_uuid"}
    if isinstance(value, Mapping) and "pickup" in value:
        fields = fields | {"pickup"}
    _object(value, fields)
    if associated:
        for field in ("global_task_uuid", "transfer_definition_uuid"):
            _uuid(value[field])
        if value["transfer_source_kind"] not in ("node_output", "station_material"):
            raise HandoffProtocolError("交接来源类型必须为工作流产物或本站库存")
    if chained:
        _uuid(value["chain_uuid"])
        _integer(value["leg_no"])
        previous = value.get("previous_handoff_uuid")
        if value["leg_no"] == 1:
            if "previous_handoff_uuid" in value:
                raise HandoffProtocolError("运输链首段不能指定前段")
        else:
            _uuid(previous)
            if previous == value["handoff_uuid"]:
                raise HandoffProtocolError("运输链前段不能指向自身")
    for field in ("handoff_uuid", "lab_uuid", "source_station_uuid", "target_station_uuid",
                  "source_instance_uuid", "target_instance_uuid"):
        _uuid(value[field])
    if value["source_station_uuid"] == value["target_station_uuid"]:
        raise HandoffProtocolError("交接两端不能为同一工站")
    if station_uuid is not None:
        side = next((side for side in ("source", "target") if value[f"{side}_station_uuid"] == station_uuid), None)
        if side is None or (instance_uuid is not None and value[f"{side}_instance_uuid"] != instance_uuid):
            raise HandoffProtocolError("交接包不属于本机固定工站实例")
    if not isinstance(value["status"], str) or value["status"] not in STATUSES or type(value["attention"]) is not bool:
        raise HandoffProtocolError("交接状态无效")
    _integer(value["revision"])
    for field in ("created_at_ms", "in_transit_deadline_ms"):
        _integer(value[field], 0)
    _strings(value["attention_reasons"], minimum=0, maximum=10000)
    items = value["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise HandoffProtocolError("交接明细数量无效")
    identities, containers = set(), set()
    for item in items:
        if not isinstance(item, Mapping):
            raise HandoffProtocolError("交接明细必须为对象")
        _coverage_fields({k: v for k, v in item.items() if k not in {"source_verification", "source_departure_verification", "settlement", "movement_review", "placement_plan"}}, {"handoff_item_uuid", "container_uuid", "expected_state_version", "reserved_state_version",
                       "current_state_version", "source_material_refs", "source_snapshot", "receipt"})
        for field in ("handoff_item_uuid", "container_uuid"):
            _uuid(item[field])
        if item["handoff_item_uuid"] in identities or item["container_uuid"] in containers:
            raise HandoffProtocolError("交接明细或容器身份重复")
        identities.add(item["handoff_item_uuid"])
        containers.add(item["container_uuid"])
        for field in ("expected_state_version", "reserved_state_version", "current_state_version"):
            _integer(item[field])
        if (item["reserved_state_version"] != item["expected_state_version"] + 1
                or item["current_state_version"] < item["reserved_state_version"]):
            raise HandoffProtocolError("容器版本关系无效")
        _refs(item["source_material_refs"], value["source_station_uuid"])
        _snapshot(item["source_snapshot"])
        if item["receipt"] is not None:
            receipt = validate_handoff_receipt(item["receipt"], station_uuid=value["target_station_uuid"])
            if (receipt["handoff_item_uuid"] != item["handoff_item_uuid"] or
                    {r["material_lineage_uuid"] for r in receipt["target_material_refs"]} !=
                    {r["material_lineage_uuid"] for r in item["source_material_refs"]}):
                raise HandoffProtocolError("收货映射与原明细或谱系不符")
        if "source_verification" in item:
            proof = validate_source_verification(item["source_verification"])
            if (proof["handoff_item_uuid"] != item["handoff_item_uuid"]
                    or proof["source_snapshot_digest"] != digest(item["source_snapshot"])):
                raise HandoffProtocolError("源复核证明与原冻结明细不符")
        if "source_departure_verification" in item:
            proof = validate_source_verification(item["source_departure_verification"])
            if (proof["handoff_item_uuid"] != item["handoff_item_uuid"]
                    or proof["source_snapshot_digest"] != digest(item["source_snapshot"])
                    or item["receipt"] is None):
                raise HandoffProtocolError("源离站复核证明与原冻结明细/目标收货不符")
        if "movement_review" in item:
            _validate_movement_review(item["movement_review"], item, value)
        if "settlement" in item:
            _validate_settlement(item["settlement"], item, value)
    from unilabos.center_protocol.placement import validate_handoff_placements
    try:
        validate_handoff_placements(value)
    except ValueError as error:
        raise HandoffProtocolError(str(error)) from error
    for field, identity in (("prepared", "protection_uuid"), ("released", "release_uuid")):
        proof = value[field]
        if proof is not None:
            fields = {"event_epoch_uuid", identity, "evidence_refs"}
            if field == "prepared":
                fields.add("reservation_until_ms")
            if field == "prepared" and "placement_reservations" in proof:
                fields.add("placement_reservations")
                from unilabos.center_protocol.placement import validate_prepared_against_items
                validate_prepared_against_items(value, proof["placement_reservations"])
            if "endpoint_snapshot" in proof:
                from .transport_endpoint import validate_endpoint

                validate_endpoint(proof["endpoint_snapshot"])
                endpoint = proof["endpoint_snapshot"]
                station = value["target_station_uuid" if field == "prepared" else "source_station_uuid"]
                if (endpoint["station_uuid"] != station or
                        {i["container_uuid"] for i in endpoint["items"]} != {i["container_uuid"] for i in value["items"]}):
                    raise HandoffProtocolError("位置快照与交接工站或容器不一致")
                fields.add("endpoint_snapshot")
            _object(proof, fields)
            _uuid(proof[identity])
            _uuid(proof["event_epoch_uuid"])
            _strings(proof["evidence_refs"])
            if field == "prepared":
                _integer(proof["reservation_until_ms"], 0)
    if value["permit"] is not None:
        _object(value["permit"], {"authorization_uuid", "logistics_subject_uuid"})
        for item in value["permit"].values():
            _uuid(item)
    if "pickup" in value:
        from unilabos.center_protocol.transport import validate_pickup_proof

        try:
            proof = validate_pickup_proof(value["pickup"])
            from unilabos.center_protocol.placement import execution_from_handoff
            plan = execution_from_handoff(value)
            if (proof["schema_version"] != (2 if plan else 1)
                    or proof.get("execution_plan_digest") != (plan["execution_plan_digest"] if plan else None)):
                raise ValueError("取货证明替换了目标实际预留的位置计划")
        except ValueError as error:
            raise HandoffProtocolError(str(error)) from error
        if (proof["handoff_uuid"] != value["handoff_uuid"] or value["permit"] is None
                or proof["authorization_uuid"] != value["permit"]["authorization_uuid"]
                or proof["logistics_station_uuid"] != value["permit"]["logistics_subject_uuid"]
                or set(proof["container_uuids"]) != containers):
            raise HandoffProtocolError("取货证明与原交接许可或容器不一致")
    acks = value["cancel_acks"]
    if (not isinstance(acks, list) or any(not isinstance(ack, str) for ack in acks)
            or len(set(acks)) != len(acks)
            or not set(acks) <= {value["source_station_uuid"], value["target_station_uuid"]}):
        raise HandoffProtocolError("撤销确认来源无效")
    _bounded(value)
    return deepcopy(dict(value))


def validate_handoff_package(value: Any, *, station_uuid: str | None = None,
                             instance_uuid: str | None = None, expected_digest: str | None = None,
                             expected_revision: int | None = None) -> dict[str, Any]:
    _object(value, {"payload", "payload_digest"})
    payload = validate_handoff_snapshot(value["payload"], station_uuid=station_uuid, instance_uuid=instance_uuid)
    if (not is_digest(value["payload_digest"]) or digest(payload) != value["payload_digest"]
            or (expected_digest is not None and expected_digest != value["payload_digest"])
            or (expected_revision is not None and expected_revision != payload["revision"])):
        raise HandoffProtocolError("交接包摘要或修订不匹配")
    return {"payload": payload, "payload_digest": value["payload_digest"]}


def validate_handoff_intent(value: Any) -> dict[str, Any]:
    _object(value, INTENT_FIELDS)
    for field in ("event_uuid", "event_epoch_uuid", "station_uuid", "instance_uuid", "handoff_uuid"):
        _uuid(value[field])
    _integer(value["occurred_at_ms"], 0)
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise HandoffProtocolError("交接事件正文必须为对象")
    kind = payload.get("kind")
    fields = {"kind", "evidence_refs"}
    if kind in {"prepared", "reservation_renewed"}:
        fields |= {"expected_revision", "reservation_until_ms", "protection_uuid"}
        if "placement_reservations" in payload:
            from unilabos.center_protocol.placement import validate_prepared_placement
            validate_prepared_placement(payload["placement_reservations"])
            fields.add("placement_reservations")
    elif kind == "released":
        fields |= {"expected_revision", "release_uuid"}
    elif kind in {"received", "source_verified", "source_departure_verified", "movement_verified"}:
        fields |= {"observed_revision", "items"}
    elif kind == "cancel_ack":
        fields |= {"observed_revision", "protection_released"}
    elif kind == "snapshot_applied":
        fields |= {"observed_revision", "package_digest", "applied", "failure_reason"}
    else:
        raise HandoffProtocolError("交接事件类型无效")
    if kind in {"prepared", "released", "reservation_renewed"} and "endpoint_snapshot" in payload:
        from .transport_endpoint import validate_endpoint

        validate_endpoint(payload["endpoint_snapshot"])
        if payload["endpoint_snapshot"]["station_uuid"] != value["station_uuid"]:
            raise HandoffProtocolError("位置事实不属于发送工站")
        fields.add("endpoint_snapshot")
    _object(payload, fields)
    _strings(payload["evidence_refs"])
    _integer(payload["expected_revision"] if kind in ("prepared", "released", "reservation_renewed") else payload["observed_revision"])
    if kind in {"prepared", "reservation_renewed"}:
        _uuid(payload["protection_uuid"])
        _integer(payload["reservation_until_ms"], 0)
    elif kind == "released":
        _uuid(payload["release_uuid"])
    elif kind in {"received", "source_verified", "source_departure_verified", "movement_verified"}:
        if not isinstance(payload["items"], list) or not 1 <= len(payload["items"]) <= 100:
            raise HandoffProtocolError("收货事件明细数量无效")
        ids = set()
        for receipt in payload["items"]:
            if kind == "received":
                validate_handoff_receipt(receipt, station_uuid=value["station_uuid"])
            elif kind == "movement_verified":
                validate_movement_verification(receipt)
                if receipt["station_uuid"] != value["station_uuid"]:
                    raise HandoffProtocolError("新移动证明工站与事件主体不同")
            else:
                validate_source_verification(receipt)
            if receipt["handoff_item_uuid"] in ids:
                raise HandoffProtocolError("收货事件明细重复")
            ids.add(receipt["handoff_item_uuid"])
    elif kind == "snapshot_applied":
        if (not is_digest(payload["package_digest"]) or type(payload["applied"]) is not bool
                or (payload["applied"] and payload["failure_reason"] is not None)
                or (not payload["applied"] and (not isinstance(payload["failure_reason"], str)
                    or not payload["failure_reason"].strip() or len(payload["failure_reason"].encode()) > 1000))):
            raise HandoffProtocolError("实际应用回执的摘要、结果或失败原因无效")
    elif type(payload["protection_released"]) is not bool:
        raise HandoffProtocolError("撤销保护确认必须为布尔值")
    _bounded(value)
    return deepcopy(dict(value))


def make_handoff_event(intent: Any, *, event_sequence: int) -> dict[str, Any]:
    body = validate_handoff_intent(intent)
    _integer(event_sequence)
    body.update(protocol_version="1.0", event_sequence=event_sequence)
    body["event_digest"] = digest(body)
    return validate_handoff_event(body)


def validate_handoff_event(value: Any) -> dict[str, Any]:
    _object(value, EVENT_FIELDS)
    validate_handoff_intent({k: value[k] for k in INTENT_FIELDS})
    _integer(value["event_sequence"])
    if value["protocol_version"] != "1.0":
        raise HandoffProtocolError("交接事件协议版本不兼容")
    unsigned = {k: v for k, v in value.items() if k != "event_digest"}
    if not is_digest(value["event_digest"]) or digest(unsigned) != value["event_digest"]:
        raise HandoffProtocolError("交接事件摘要不匹配")
    _bounded(value)
    return deepcopy(dict(value))


def validate_handoff_ack(response: Any, event: Any) -> dict[str, Any]:
    event = validate_handoff_event(event)
    _object(response, {"handoff", "ack"})
    handoff = validate_handoff_snapshot(response["handoff"], station_uuid=event["station_uuid"],
                                        instance_uuid=event["instance_uuid"])
    if handoff["handoff_uuid"] != event["handoff_uuid"]:
        raise HandoffProtocolError("交接回执属于其他交接")
    try:
        validate_ack(response["ack"], event)
    except EventEnvelopeError as error:
        raise HandoffProtocolError(str(error)) from error
    return deepcopy(dict(response))


def validate_container(value: Any, container_uuid: str) -> dict[str, Any]:
    _object(value, {"container_uuid", "lab_uuid", "barcode_namespace", "barcode", "current_station_uuid",
                    "state_version", "active_handoff_item_uuid", "custody_status", "attention"})
    for field in ("container_uuid", "lab_uuid", "current_station_uuid"):
        _uuid(value[field])
    if value["active_handoff_item_uuid"] is not None:
        _uuid(value["active_handoff_item_uuid"])
    _integer(value["state_version"])
    for field in ("barcode_namespace", "barcode"):
        item = value[field]
        if not isinstance(item, str) or not item.strip() or item.strip() != item or len(item.encode()) > 128:
            raise HandoffProtocolError("容器条码或命名空间无效")
    if (value["container_uuid"] != container_uuid or type(value["attention"]) is not bool
            or value["custody_status"] not in ("available", "reserved", "quarantined")):
        raise HandoffProtocolError("容器身份或保管状态无效")
    return deepcopy(dict(value))
