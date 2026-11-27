"""结算后新移动的库存事务：当前实物证明、显式到站适配及逐代结算历史。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from unilabos.app.scheduler.inventory.dispatch_admission import assert_resource_keys_unclaimed
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, _evidence, _json, _uuid
from unilabos.app.scheduler.inventory.handoff_adapter import (
    export_matching_snapshot,
    export_snapshot,
    prepare_structure,
    validate_snapshot,
    validate_source,
)
from unilabos.app.scheduler.inventory.store import clear_site_occupancy
from unilabos.app.scheduler.resource_lock import device_lock_key, material_lock_key
from unilabos.center_protocol.digest import digest

_LIVE = {"warehouse", "handoff_quarantined", "handoff_outgoing", "handoff_received"}
_ABSENT = {"incoming_canceled", "handoff_departed"}


def _fail(code: str, message: str) -> None:
    raise HandoffInventoryError(code, message)


def _unclaimed(a: Any, c: Any, payload: Mapping[str, Any], identities: set[str], reservation: str = "") -> None:
    keys = [key for identity in identities for key in (material_lock_key(identity), device_lock_key(identity))]
    assert_resource_keys_unclaimed(c, lock_keys=keys, ignore_handoff_uuid=payload["handoff_uuid"],
                                   ignore_ingress_reservation_uuid=reservation)
    for identity in identities:
        if c.execute("SELECT 1 FROM inventory_material_source_binding WHERE material_uuid=? AND status='active'", (identity,)).fetchone():
            _fail("task_claimed", "新移动核实的当前物料仍被本地任务占用")
        if c.execute("SELECT 1 FROM inventory_reservation,json_each(amounts_json,'$.instances') AS i WHERE status='active' AND i.value=?", (identity,)).fetchone():
            _fail("task_reserved", "新移动核实的当前物料仍有本地预留")


def _known_mappings(c: Any, item: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in c.execute("SELECT b.* FROM material_station_binding b JOIN station_handoff_item i USING(handoff_item_uuid) WHERE i.container_uuid=?", (item["container_uuid"],)).fetchall():
        result.setdefault(row["material_lineage_uuid"], set()).add(row["local_material_uuid"])
    for row in c.execute("SELECT proof_json FROM station_handoff_movement WHERE handoff_item_uuid=?", (item["handoff_item_uuid"],)).fetchall():
        for ref in json.loads(row[0])["material_refs"]:
            result.setdefault(ref["material_lineage_uuid"], set()).add(ref["local_material_uuid"])
    return result


def _current_refs(a: Any, c: Any, item: Mapping[str, Any], carrier: str) -> list[dict[str, Any]]:
    tree = a._subtree(c, carrier)
    known = _known_mappings(c, item)
    refs = []
    for source in item["source_material_refs"]:
        candidates = known.get(source["material_lineage_uuid"], set()) & tree
        if len(candidates) != 1:
            _fail("movement_mapping_required", "当前实物谱系没有唯一有效本站映射，不能沿用旧身份猜测")
        local = next(iter(candidates))
        material = a._material(c, local)
        refs.append({"station_uuid": a.station_uuid, "local_material_uuid": local,
                     "local_revision": material["aggregate_version"], "material_lineage_uuid": source["material_lineage_uuid"]})
    return refs


def _protect(a: Any, c: Any, payload: Mapping[str, Any], item: Mapping[str, Any], identities: set[str]) -> None:
    for identity in identities:
        material = a._material(c, identity)
        if material["inventory_status"] not in _LIVE or material["disposition"] != "active":
            _fail("movement_inventory_unavailable", "当前物料不是可核实库存，不能将历史墓碑当作实物在场")
        for key in (material_lock_key(identity), device_lock_key(identity)):
            c.execute("INSERT INTO station_handoff_resource VALUES (?,?,?,?,?,1) ON CONFLICT(handoff_item_uuid,lock_key) DO UPDATE SET active=1",
                      (payload["handoff_uuid"], item["handoff_item_uuid"], key, identity, "warehouse"))
        c.execute("UPDATE material SET inventory_status='handoff_quarantined' WHERE uuid=?", (identity,))


def _arrive(a: Any, c: Any, payload: Mapping[str, Any], item: Mapping[str, Any], role: str,
            details: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    review = item["movement_review"]
    opposite = review["target_verification" if role == "source" else "source_verification"]
    if (opposite is None or opposite["physical_present"] or not opposite["material_refs"]
            or opposite["expected_state_version"] != item["current_state_version"]):
        _fail("movement_departure_required", "新到站必须先有本轮当前版本离站证明及完整实物快照")
    if set(plan) != {"carrier_template_uuid", "carrier_lineage_uuid", "lineage_template_uuids",
                     "candidate_site_uuids", "ttl_seconds", "adapter"}:
        _fail("local_plan_required", "新移动到站需要完整显式模板、入口、内容及库位映射")
    incoming = {**item, "source_material_refs": opposite["material_refs"], "source_snapshot": opposite["inventory_snapshot"]}
    validate_snapshot(incoming["source_snapshot"], incoming["source_material_refs"], plan)
    lineages = {r["material_lineage_uuid"] for r in incoming["source_material_refs"]}
    if set(plan["lineage_template_uuids"]) != lineages or plan["carrier_lineage_uuid"] not in lineages:
        _fail("lineage_plan_mismatch", "到站模板必须精确覆盖当前实物谱系")
    binding = c.execute("SELECT * FROM container_station_binding WHERE container_uuid=?", (item["container_uuid"],)).fetchone()
    known = _known_mappings(c, item)
    local_by_source: dict[str, str] = {}
    carrier = ""
    for ref in incoming["source_material_refs"]:
        lineage = ref["material_lineage_uuid"]
        template = plan["lineage_template_uuids"][lineage]
        is_carrier = lineage == plan["carrier_lineage_uuid"]
        if is_carrier and template != plan["carrier_template_uuid"]:
            _fail("template_mismatch", "当前载体模板和载体谱系模板不同")
        node = next(n for n in incoming["source_snapshot"]["materials"] if n["local_material_uuid"] == ref["local_material_uuid"])
        if is_carrier and node["barcode"] != details["barcode"]:
            _fail("barcode_mismatch", "离站实物快照与可信容器条码不同")
        candidates = {binding["carrier_material_uuid"]} if is_carrier and binding else known.get(lineage, set())
        existing = [a._material(c, identity) for identity in candidates]
        if len(existing) > 1:
            _fail("movement_mapping_required", "历史本站谱系存在多个候选，需明确核实有效映射")
        if existing:
            material = existing[0]
            local = material["uuid"]
            _unclaimed(a, c, payload, {local})
            if material["inventory_status"] not in _ABSENT or material["resource_template_uuid"] != template:
                _fail("movement_inventory_present", "不能覆盖现存库存、后续运输或改变本站模板")
            if c.execute("SELECT 1 FROM container_content WHERE material_uuid=? AND deleted_at IS NULL", (local,)).fetchone():
                _fail("movement_content_present", "本站仍有活动内容，不能覆盖为新到站快照")
            c.execute("UPDATE station_handoff_resource SET active=0 WHERE handoff_uuid=? AND material_uuid=?", (payload["handoff_uuid"], local))
            c.execute("UPDATE material SET inventory_status='incoming',disposition='active',aggregate_version=aggregate_version+1 WHERE uuid=?", (local,))
        else:
            local = a._create_incoming(c, template, barcode=node["barcode"], adapted=True)
        local_by_source[ref["local_material_uuid"]] = local
        if is_carrier:
            carrier = local
    if binding is None:
        c.execute("INSERT INTO container_station_binding(container_uuid,station_uuid,carrier_material_uuid,barcode_namespace,barcode) VALUES (?,?,?,?,?)",
                  (item["container_uuid"], a.station_uuid, carrier, details["barcode_namespace"], details["barcode"]))
    local_item = c.execute("SELECT 1 FROM station_handoff_item WHERE handoff_item_uuid=?", (item["handoff_item_uuid"],)).fetchone()
    if local_item is None:
        a._insert_item(c, payload, item, carrier)
    prepare_structure(a, c, incoming, plan, local_by_source)
    reservation = a.ingress.reserve_in_transaction(c, idempotency_key=f"movement:{item['handoff_item_uuid']}:{review['review_revision']}",
        carrier_material_uuid=carrier, candidate_site_uuids=plan["candidate_site_uuids"], ttl_seconds=plan["ttl_seconds"])
    a._validate_site_template(c, reservation["site_uuid"], plan["carrier_template_uuid"])
    a.ingress.protect_prepared_in_transaction(c, reservation["uuid"])
    # 新入口独立保留身份，不改原交接接收记录或 receipt。
    a.ingress.receive_in_transaction(c, reservation["uuid"], handoff_uuid=payload["handoff_uuid"], physical_receipt=True)
    refs = []
    for ref in incoming["source_material_refs"]:
        local = local_by_source[ref["local_material_uuid"]]
        c.execute("UPDATE material SET inventory_status='handoff_quarantined',aggregate_version=aggregate_version+1 WHERE uuid=?", (local,))
        refs.append({"station_uuid": a.station_uuid, "local_material_uuid": local,
                     "local_revision": a._material(c, local)["aggregate_version"], "material_lineage_uuid": ref["material_lineage_uuid"]})
    return carrier, refs, reservation["uuid"]


def verify_movement(a: Any, package: Mapping[str, Any], *, handoff_item_uuid: str,
                    physical_present: bool, barcode_namespace: str, barcode: str,
                    evidence_refs: Sequence[str], container_details: Mapping[str, Any],
                    arrival_plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload, identity, evidence = a._package(package), _uuid(handoff_item_uuid), _evidence(evidence_refs)
    item = next((i for i in payload["items"] if i["handoff_item_uuid"] == identity), None)
    if item is None or not item.get("movement_review") or not payload["attention"] or type(physical_present) is not bool:
        _fail("movement_not_pending", "需要当前待核实的新移动代次及明确在场/离站确认")
    if item.get("settlement") and item["settlement"]["decision"] != "keep_quarantined":
        _fail("movement_already_settled", "本件本轮已经结算")
    details = container_details
    if (details.get("container_uuid") != item["container_uuid"] or details.get("lab_uuid") != payload["lab_uuid"]
            or details.get("state_version") != item["current_state_version"]
            or details.get("active_handoff_item_uuid") not in (None, identity)
            or not details.get("attention") or details.get("custody_status") != "quarantined"
            or (barcode_namespace, barcode) != (details.get("barcode_namespace"), details.get("barcode"))):
        _fail("movement_container_changed", "实物条码、中心当前版本或占有不属于本轮核实")
    role = "source" if payload["source_station_uuid"] == a.station_uuid else "target"
    review = item["movement_review"]
    request_digest = digest({"physical_present": physical_present, "barcode_namespace": barcode_namespace, "barcode": barcode,
                             "evidence_refs": evidence, "arrival_plan": arrival_plan, "state_version": item["current_state_version"]})
    purpose = f"movement_verified:{identity}:{review['review_revision']}"
    with a.store.transaction() as c:
        local_handoff = c.execute("SELECT * FROM station_handoff WHERE handoff_uuid=?", (payload["handoff_uuid"],)).fetchone()
        if local_handoff:
            a._assert_same_items(json.loads(local_handoff["package_json"]), payload)
            if payload["revision"] < local_handoff["revision"]:
                _fail("stale_package", "新移动核实必须使用最新中心包")
        else:
            a._record_new(c, payload, role, digest({"movement_review": review["review_revision"]}))
        old = c.execute("SELECT request_digest FROM station_handoff_movement WHERE handoff_item_uuid=? AND review_revision=?", (identity, review["review_revision"])).fetchone()
        if old:
            if old[0] != request_digest:
                _fail("movement_verification_conflict", "同一核实代次的物理证明或接收计划不可改写")
            return a._intent(c, payload["handoff_uuid"], purpose)
        binding = c.execute("SELECT * FROM container_station_binding WHERE container_uuid=?", (item["container_uuid"],)).fetchone()
        if binding and (binding["barcode_namespace"], binding["barcode"]) != (barcode_namespace, barcode):
            _fail("barcode_mismatch", "当前容器条码与本站绑定不同")
        reservation, refs, snapshot = "", [], {}
        if arrival_plan is not None:
            if not physical_present:
                _fail("movement_plan_without_arrival", "离站确认不能执行接收计划")
            carrier, refs, reservation = _arrive(a, c, payload, item, role, details, arrival_plan)
        elif binding and a._material(c, binding["carrier_material_uuid"])["inventory_status"] in _LIVE:
            carrier = binding["carrier_material_uuid"]
            refs = _current_refs(a, c, item, carrier)
        elif physical_present or review["previous_settlement"]["actual_holder_station_uuid"] == a.station_uuid:
            _fail("movement_arrival_plan_required", "本站没有当前实物库存，需要核实离站快照和显式新到站计划")
        if refs:
            identities = {r["local_material_uuid"] for r in refs}
            _unclaimed(a, c, payload, identities, reservation)
            snapshot = export_snapshot(a, c, carrier, refs, template_evidence=True)
            validate_source(a, c, {"source_snapshot": snapshot, "source_material_refs": refs}, carrier)
            _protect(a, c, payload, item, identities)
        proof = {"handoff_item_uuid": identity, "verification_uuid": str(uuid4()), "review_revision": review["review_revision"],
                 "expected_state_version": item["current_state_version"], "station_uuid": a.station_uuid,
                 "physical_present": physical_present, "material_refs": refs, "inventory_snapshot": snapshot, "evidence_refs": evidence}
        proof["verification_digest"] = digest(proof)
        c.execute("INSERT INTO station_handoff_movement VALUES (?,?,?,?,?,?,NULL)",
                  (payload["handoff_uuid"], identity, review["review_revision"], request_digest, _json(proof), reservation or None))
        c.execute("UPDATE station_handoff SET revision=?,package_json=? WHERE handoff_uuid=?",
                  (payload["revision"], _json(payload), payload["handoff_uuid"]))
        return a._emit(c, payload, purpose, {"kind": "movement_verified", "observed_revision": payload["revision"],
                                           "items": [proof], "evidence_refs": evidence})


def apply_settlement(a: Any, c: Any, payload: Mapping[str, Any], role: str, item: Mapping[str, Any]) -> None:
    review, settlement = item["movement_review"], item["settlement"]
    row = c.execute("SELECT * FROM station_handoff_movement WHERE handoff_item_uuid=? AND review_revision=?",
                    (item["handoff_item_uuid"], review["review_revision"])).fetchone()
    if row is None:
        _fail("movement_local_proof_missing", "新移动结算缺少本站本轮持久证明")
    if row["settlement_json"] is not None:
        if json.loads(row["settlement_json"]) != settlement:
            _fail("movement_settlement_conflict", "本轮已应用结算不可覆盖")
        return
    proof = json.loads(row["proof_json"])
    if proof != review[f"{role}_verification"]:
        _fail("movement_local_proof_mismatch", "中心新移动结算与本站本轮证明不同")
    refs = proof["material_refs"]
    identities = {r["local_material_uuid"] for r in refs}
    _unclaimed(a, c, payload, identities, row["reservation_uuid"] or "")
    if refs:
        carrier = proof["inventory_snapshot"]["carrier_material_uuid"]
        if digest(export_matching_snapshot(a, c, carrier, refs, proof["inventory_snapshot"])) != digest(proof["inventory_snapshot"]):
            _fail("movement_inventory_changed", "本轮复核后库存版本、结构或内容已变化")
        if any(a._material(c, identity)["inventory_status"] != "handoff_quarantined" for identity in identities):
            _fail("movement_protection_changed", "本轮库存保护已变化，不能套用历史证明释放")
        for identity in identities:
            if not proof["physical_present"]:
                clear_site_occupancy(c, material_uuid=identity)
                now = datetime.fromtimestamp(a._now_ms() / 1000, timezone.utc).isoformat()
                c.execute("UPDATE container_content SET deleted_at=?,update_time=? WHERE material_uuid=? AND deleted_at IS NULL", (now, now, identity))
                c.execute("UPDATE material SET parent_uuid=NULL WHERE uuid=?", (identity,))
            c.execute("UPDATE material SET inventory_status=? WHERE uuid=?", ("warehouse" if proof["physical_present"] else "handoff_departed", identity))
            c.execute("UPDATE station_handoff_resource SET active=? WHERE handoff_item_uuid=? AND material_uuid=?",
                      (0 if proof["physical_present"] else 1, item["handoff_item_uuid"], identity))
    for historical in (review["previous_settlement"], settlement):
        prior = c.execute("SELECT settlement_json FROM station_handoff_settlement_history WHERE handoff_item_uuid=? AND handoff_revision=?",
                          (item["handoff_item_uuid"], historical["handoff_revision"])).fetchone()
        if prior is not None and json.loads(prior[0]) != historical:
            _fail("movement_history_conflict", "历史结算同修订正文不一致")
        c.execute("INSERT INTO station_handoff_settlement_history VALUES (?,?,?) ON CONFLICT DO NOTHING",
                  (item["handoff_item_uuid"], historical["handoff_revision"], _json(historical)))
    c.execute("UPDATE station_handoff_movement SET settlement_json=? WHERE handoff_item_uuid=? AND review_revision=?",
              (_json(settlement), item["handoff_item_uuid"], review["review_revision"]))
    if c.execute("SELECT 1 FROM station_handoff_item WHERE handoff_item_uuid=?", (item["handoff_item_uuid"],)).fetchone():
        c.execute("INSERT INTO station_handoff_settlement VALUES (?,?) ON CONFLICT(handoff_item_uuid) DO UPDATE SET settlement_json=excluded.settlement_json",
                  (item["handoff_item_uuid"], _json(settlement)))
        c.execute("UPDATE station_handoff_item SET state=? WHERE handoff_item_uuid=?",
                  ("canceled" if settlement["decision"] == "source_present" else "settled", item["handoff_item_uuid"]))
