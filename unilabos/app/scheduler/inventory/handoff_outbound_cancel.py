"""未冻结源交接的取消只结算原全局任务、原容器及精确产物责任。"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid5

from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError
from unilabos.app.scheduler.inventory.outbound import PREFIX, settle_canceled_outbound
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.center_protocol.digest import digest
from unilabos.persistence.inspection import table_exists


def _eligible(authority, payload: Mapping[str, Any]) -> bool:
    return (payload["source_station_uuid"] == authority.station_uuid
            and payload.get("transfer_source_kind") == "node_output"
            and payload["released"] is None
            and authority._can_cancel(payload))


def _proof_uuid(payload) -> str:
    return str(uuid5(UUID(payload["handoff_uuid"]), "unfrozen-outbound-cancellation"))


def _rows(c, payload):
    """绑定只定位候选；释放授权另由原调用、正式边界和库存版本共同给出。"""
    result = []
    for item in payload["items"]:
        binding = c.execute("SELECT * FROM container_station_binding WHERE container_uuid=?", (item["container_uuid"],)).fetchone()
        if binding is None:
            continue
        rows = c.execute("SELECT * FROM inventory_reservation WHERE status='active' AND node_id=?",
                         (PREFIX + binding["carrier_material_uuid"],)).fetchall()
        result.extend((item, binding, row) for row in rows)
    return result


def cancellation_contexts(authority, payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any] | None]:
    """先释放库存读锁，再向工作流权威读取原永久身份，避免跨库锁顺序反转。"""
    if not _eligible(authority, payload) or authority.source_context_resolver is None:
        return {}
    with authority.store.read_connection() as c:
        if c.execute("SELECT 1 FROM inventory_ledger WHERE entry_uuid=?", (_proof_uuid(payload),)).fetchone():
            return {}
        invocations = {row["workflow_id"] for _, _, row in _rows(c, payload)}
    return {identity: authority.source_context_resolver(identity) for identity in invocations}


def _verify_origin(authority, c, payload, item, binding, row, context):
    if context is None or not table_exists(c, "station_material_export"):
        raise HandoffInventoryError("outbound_cancel_unverified", "原产物的冻结调用关联尚未核实，保留出站保护")
    export = c.execute("SELECT * FROM station_material_export WHERE invocation_uuid=?", (row["workflow_id"],)).fetchone()
    if export is None or not export["completed"] or not export["containers_json"] or not export["evidence_json"]:
        raise HandoffInventoryError("outbound_cancel_unverified", "原产物正式边界尚未完成，保留出站保护")
    source = json.loads(export["source_json"])["source"]
    package = context["package"]
    original = package["payload"]
    facts = json.loads(row["amounts_json"])
    if (digest(original) != package["payload_digest"]
            or original["global_task_uuid"] != payload["global_task_uuid"]
            or original["station_invocation_uuid"] != row["workflow_id"]
            or context["invocation_uuid"] != row["workflow_id"]
            or context["local_task_uuid"] != export["local_task_uuid"]
            or source["local_task_uuid"] != export["local_task_uuid"]
            or facts.get("outbound_task_uuid") != export["local_task_uuid"]
            or source["invocation_uuid"] != row["workflow_id"]
            or source["task_payload_digest"] != package["payload_digest"]
            or source["station_uuid"] != authority.station_uuid
            or source["owner_instance_uuid"] != authority.instance_uuid
            or source["event_epoch_uuid"] != authority.event_epoch_uuid
            or original["station_uuid"] != authority.station_uuid
            or context["instance_uuid"] != authority.instance_uuid
            or context["event_epoch_uuid"] != authority.event_epoch_uuid
            or binding["station_uuid"] != authority.station_uuid
            or facts.get("carrier_material_uuid") != binding["carrier_material_uuid"]):
        raise HandoffInventoryError("outbound_cancel_conflict", "取消交接与原全局任务、调用或产物责任不一致")
    entries = [entry for entry in json.loads(export["containers_json"]) if entry["container_uuid"] == item["container_uuid"]]
    if len(entries) != 1:
        raise HandoffInventoryError("outbound_cancel_conflict", "取消容器不属于原产物正式边界")
    entry = entries[0]
    state = entry["center_snapshot"]
    if (digest(entry["source_material_refs"]) != digest(item["source_material_refs"])
            or digest(entry["source_snapshot"]) != digest(item["source_snapshot"])
            or state["state_version"] != item["expected_state_version"]
            or state["current_station_uuid"] != authority.station_uuid
            or state["active_handoff_item_uuid"] is not None
            or state["custody_status"] != "available" or state["attention"]
            or state["barcode_namespace"] != binding["barcode_namespace"]
            or state["barcode"] != binding["barcode"]):
        raise HandoffInventoryError("outbound_cancel_conflict", "取消物料引用、完整快照或中心容器状态与原产物边界不一致")
    from unilabos.app.scheduler.inventory.handoff_adapter import validate_source

    if not validate_source(authority, c, item, binding["carrier_material_uuid"]):
        raise HandoffInventoryError("outbound_cancel_unverified", "原产物缺少完整库存快照，不能自动结算取消")
    return export["local_task_uuid"]


def settle_outbound_cancellation(authority, c, payload: Mapping[str, Any], contexts) -> None:
    """释放与稳定证明同事务；证明存在时只核对原交接身份，绝不再查询新占用。"""
    if not _eligible(authority, payload):
        return
    identity = _proof_uuid(payload)
    previous = c.execute("SELECT * FROM inventory_ledger WHERE entry_uuid=?", (identity,)).fetchone()
    if previous is not None:
        if previous["aggregate_type"] != "station_handoff" or previous["aggregate_id"] != payload["handoff_uuid"] or previous["op_type"] != "outbound_cancel_settled":
            raise HandoffInventoryError("outbound_cancel_conflict", "原出站取消结算证明身份不一致")
        authority._assert_same_items(json.loads(previous["delta_json"])["handoff_snapshot"], payload)
        return
    released = []
    for item, binding, row in _rows(c, payload):
        task = _verify_origin(authority, c, payload, item, binding, row, contexts.get(row["workflow_id"]))
        facts = settle_canceled_outbound(c, invocation_uuid=row["workflow_id"], task_uuid=task,
            evidence_refs=["inventory:handoff:" + payload["handoff_uuid"]], reservation_ids=[row["reservation_id"]])
        released.append({"handoff_item_uuid": item["handoff_item_uuid"], "invocation_uuid": row["workflow_id"],
                         "local_task_uuid": task, "reservations": facts})
    InventoryStore.tx_append_inventory_event(c, entry_uuid=identity, edge_id=authority.station_uuid,
        lab_id=authority.lab_uuid, occurred_at=authority._now_ms(), aggregate_type="station_handoff",
        aggregate_id=payload["handoff_uuid"], aggregate_version=payload["revision"], event_type="outbound_cancel_settled",
        payload={"handoff_snapshot": dict(payload), "released": released}, reason="center_pre_freeze_cancel",
        causation_id=payload["handoff_uuid"])
