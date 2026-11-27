"""结算后的新移动必须重新扫码当前库存，双端核实才可释放本轮保护。"""
from __future__ import annotations

import copy
import json

import pytest

from tests.app.test_station_handoff import frozen, mark_exception, package, settlement_payload, setup, source_proof, uid
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, StationHandoffAuthority
from unilabos.app.scheduler.inventory.handoff_adapter import ADAPTER
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.center_protocol.digest import digest


def settle_source(s):
    frozen(s)
    mark_exception(s)
    proof = source_proof(s)["payload"]["items"][0]
    s["payload"]["items"][0]["source_verification"] = proof
    s["payload"] = settlement_payload(s, decision="source_present", proof=proof["verification_digest"])
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))


def reopen(s):
    item = s["payload"]["items"][0]
    prior_review = item.get("movement_review")
    settlement = item.pop("settlement")
    s["payload"]["revision"] += 1
    s["payload"]["attention"] = True
    s["payload"]["attention_reasons"] = ["HANDOFF_TRANSPORT_AFTER_SETTLEMENT"]
    item["current_state_version"] += 1
    item["movement_review"] = {"review_revision": s["payload"]["revision"],
        "previous_review_revision": prior_review["review_revision"] if prior_review else None,
        "previous_settlement": settlement, "trigger_event_digest": digest({"movement": uid()}),
        "cause": "HANDOFF_TRANSPORT_AFTER_SETTLEMENT", "source_verification": None, "target_verification": None}
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))


def details(s):
    return {**s["details"][s["ids"]["container"]], "state_version": s["payload"]["items"][0]["current_state_version"],
            "custody_status": "quarantined", "attention": True}


def verify(s, side, present, *, plan=None):
    event = s["auth"][side].verify_movement(package(s["payload"]), handoff_item_uuid=s["ids"]["item"],
        physical_present=present, barcode_namespace="lab", barcode="BKR-1", evidence_refs=["scan:current:" + side],
        container_details=details(s), arrival_plan=plan)
    s["payload"]["items"][0]["movement_review"][side + "_verification"] = event["payload"]["items"][0]
    s["payload"]["revision"] += 1
    return event


def finish(s, holder):
    proof = s["payload"]["items"][0]["movement_review"][holder + "_verification"]
    s["payload"] = settlement_payload(s, decision="source_present" if holder == "source" else "target_received",
                                      proof=proof["verification_digest"])
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))


def arrival_plan(s, side):
    opposite = "target" if side == "source" else "source"
    proof = s["payload"]["items"][0]["movement_review"][opposite + "_verification"]
    return {"carrier_template_uuid": s["templates"][side]["test.carrier"], "carrier_lineage_uuid": s["ids"]["lineage"],
            "lineage_template_uuids": {s["ids"]["lineage"]: s["templates"][side]["test.carrier"]},
            "candidate_site_uuids": [s["sites"][side]], "ttl_seconds": 60,
            "adapter": {"kind": ADAPTER, "site_names": {n["local_material_uuid"]: {} for n in proof["inventory_snapshot"]["materials"]},
                        "reagent_info_uuids": {}}}


def test_current_source_scan_and_absent_target_recover_and_keep_immutable_history(setup):
    s = setup
    settle_source(s)
    original = copy.deepcopy(s["payload"])
    backend = BackendResourceService(s["stores"]["source"])
    current = backend.get_material(s["carrier"]["uuid"])
    backend.update_material(current["uuid"], {"name": "已完成本站处理后的载体"})
    reopen(s)
    event = verify(s, "source", True)
    proof = event["payload"]["items"][0]
    assert proof["material_refs"][0]["local_revision"] > original["items"][0]["source_material_refs"][0]["local_revision"]
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_quarantined"
    verify(s, "target", False)
    finish(s, "source")
    for side, store in s["stores"].items():
        history = [json.loads(r["settlement_json"]) for r in store.query_all("SELECT settlement_json FROM station_handoff_settlement_history ORDER BY handoff_revision")]
        assert history == [original["items"][0]["settlement"], s["payload"]["items"][0]["settlement"]]
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "warehouse"
    backend.update_material(s["carrier"]["uuid"], {"name": "新结算后继续处理"})
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(s["payload"]))
    assert backend.get_material(s["carrier"]["uuid"])["name"] == "新结算后继续处理"


def test_canceled_incoming_can_receive_new_movement_and_return_without_rewriting_old_receipt(setup):
    s = setup
    settle_source(s)
    original = copy.deepcopy(s["payload"])
    target_original = dict(s["stores"]["target"].query_one("SELECT * FROM station_handoff_item"))
    reopen(s)
    verify(s, "source", False)
    verify(s, "target", True, plan=arrival_plan(s, "target"))
    finish(s, "target")
    target = s["stores"]["target"].query_one("SELECT * FROM station_handoff_item")
    assert target["carrier_material_uuid"] == target_original["carrier_material_uuid"]
    assert target["receipt_json"] == target_original["receipt_json"] is None
    assert s["payload"]["items"][0]["receipt"] is None
    assert s["stores"]["target"].get_instance(target["carrier_material_uuid"])["status"] == "warehouse"
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "handoff_departed"
    reopen(s)
    verify(s, "target", False)
    verify(s, "source", True, plan=arrival_plan(s, "source"))
    finish(s, "source")
    assert s["stores"]["source"].get_instance(s["carrier"]["uuid"])["status"] == "warehouse"
    assert s["stores"]["target"].get_instance(target["carrier_material_uuid"])["status"] == "handoff_departed"
    assert len(s["stores"]["source"].query_all("SELECT * FROM station_handoff_settlement_history")) == 3
    assert json.loads(s["stores"]["source"].query_one("SELECT settlement_json FROM station_handoff_settlement_history ORDER BY handoff_revision LIMIT 1")["settlement_json"]) == original["items"][0]["settlement"]


def test_movement_proof_is_durable_exact_and_cannot_release_newer_transport(setup):
    s = setup
    settle_source(s)
    reopen(s)
    body = package(s["payload"])
    args = dict(handoff_item_uuid=s["ids"]["item"], physical_present=True, barcode_namespace="lab", barcode="BKR-1",
                evidence_refs=["scan:current"], container_details=details(s))
    event = s["auth"]["source"].verify_movement(body, **args)
    reopened = InventoryStore(str(s["path"] / "source.db"))
    try:
        authority = StationHandoffAuthority(reopened, station_uuid=s["ids"]["source"], instance_uuid=s["ids"]["source_instance"],
            event_epoch_uuid=s["ids"]["source_epoch"], lab_uuid=s["ids"]["lab"], now_ms=lambda: s["clock"][0])
        assert authority.verify_movement(body, **args) == event
        with pytest.raises(HandoffInventoryError, match="不可改写"):
            authority.verify_movement(body, **{**args, "evidence_refs": ["changed"]})
        changed = copy.deepcopy(args)
        changed["container_details"]["active_handoff_item_uuid"] = uid()
        with pytest.raises(HandoffInventoryError, match="占有"):
            authority.verify_movement(body, **changed)
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["sample", "reagent", "substance"])
def test_new_arrival_imports_fresh_nested_content_not_original_canceled_snapshot(setup, kind):
    from tests.app.test_station_handoff_adapter import add_nested_sample

    s = setup
    content = add_nested_sample(s, kind=kind)
    settle_source(s)
    update = getattr(content["service"], {"sample": "update_sample", "reagent": "update_reagent", "substance": "update_current_substance"}[kind])
    update(content["content"]["uuid"], {**content["content"], "quantity": 1.0})
    reopen(s)
    verify(s, "source", False)
    verify(s, "target", True, plan=s["plans"][s["ids"]["item"]])
    finish(s, "target")
    actual = s["stores"]["target"].query_all("SELECT * FROM container_content WHERE deleted_at IS NULL")
    assert len(actual) == 1
    assert actual[0]["quantity"] == 1.0
    assert not s["stores"]["source"].query_all("SELECT * FROM container_content WHERE deleted_at IS NULL")
    assert s["stores"]["target"].query_one("SELECT receipt_json FROM station_handoff_item")["receipt_json"] is None


def test_movement_arrival_emit_failure_rolls_back_inventory_reservation_and_proof(setup, monkeypatch):
    s = setup
    settle_source(s)
    reopen(s)
    verify(s, "source", False)
    store = s["stores"]["target"]
    before = {table: store.query_all("SELECT * FROM " + table) for table in
              ("material", "station_handoff_resource", "station_ingress_reservation", "station_handoff_movement")}
    original_emit = s["auth"]["target"]._emit

    def fail(*args, **kwargs):
        raise RuntimeError("模拟写发件箱失败")

    monkeypatch.setattr(s["auth"]["target"], "_emit", fail)
    with pytest.raises(RuntimeError, match="发件箱"):
        verify(s, "target", True, plan=arrival_plan(s, "target"))
    assert before == {table: store.query_all("SELECT * FROM " + table) for table in before}
    monkeypatch.setattr(s["auth"]["target"], "_emit", original_emit)
    verify(s, "target", True, plan=arrival_plan(s, "target"))
    finish(s, "target")


def test_current_proof_cannot_override_new_local_claim_or_inventory_drift(setup):
    from unilabos.app.scheduler.inventory.dispatch_admission import InventoryMutationConflict
    from unilabos.app.scheduler.resource_lock import material_lock_key

    s = setup
    settle_source(s)
    reopen(s)
    verify(s, "source", True)
    verify(s, "target", False)
    proof = s["payload"]["items"][0]["movement_review"]["source_verification"]
    final = settlement_payload(s, decision="source_present", proof=proof["verification_digest"])
    source = s["stores"]["source"]
    with source.transaction() as c:
        # 模拟恢复时发现当前版本已被另一运输认领，旧交接不可解除它。
        c.execute("INSERT INTO station_handoff VALUES (?,?,?,?,?,?,?,'protected',?,?)",
                  (uid(), "source", s["ids"]["source"], s["ids"]["source_instance"], 1, "{}", uid(), "later", s["clock"][0]))
        later = c.execute("SELECT handoff_uuid FROM station_handoff WHERE plan_digest='later'").fetchone()[0]
        other_item = uid()
        c.execute("INSERT INTO station_handoff_item(handoff_item_uuid,handoff_uuid,container_uuid,carrier_material_uuid,state) VALUES (?,?,?,?,'protected')",
                  (other_item, later, s["ids"]["container"], s["carrier"]["uuid"]))
        c.execute("INSERT INTO station_handoff_resource VALUES (?,?,?,?,?,1)",
                  (later, other_item, material_lock_key(s["carrier"]["uuid"]), s["carrier"]["uuid"], "warehouse"))
    with pytest.raises(InventoryMutationConflict):
        s["auth"]["source"].apply_center_snapshot(package(final))
    assert source.get_instance(s["carrier"]["uuid"])["status"] == "handoff_quarantined"
    with source.transaction() as c:
        c.execute("UPDATE station_handoff_resource SET active=0 WHERE handoff_uuid=?", (later,))
        c.execute("UPDATE material SET aggregate_version=aggregate_version+1 WHERE uuid=?", (s["carrier"]["uuid"],))
    with pytest.raises(HandoffInventoryError, match="修订"):
        s["auth"]["source"].apply_center_snapshot(package(final))
    assert source.get_instance(s["carrier"]["uuid"])["status"] == "handoff_quarantined"


def test_movement_api_uses_authenticated_latest_package_and_container(setup):
    from types import SimpleNamespace

    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    from unilabos.app.center_client.handoff import HandoffBridge
    from unilabos.app.scheduler.inventory.handoff_api import create_handoff_router

    s = setup
    settle_source(s)
    reopen(s)
    fetched = []

    class Client:
        def handoff_package(self, handoff, revision, epoch):
            fetched.append((handoff, revision, epoch))
            return package(s["payload"])

        def container(self, container, epoch, *, handoff_uuid):
            fetched.append((container, epoch, handoff_uuid))
            return details(s)

    def operator(authorization: str | None = Header(default=None)):
        if authorization != "Bearer operator":
            raise HTTPException(401)

    bridge = HandoffBridge(Client(), SimpleNamespace(station_uuid=s["ids"]["source"], instance_uuid=s["ids"]["source_instance"], event_epoch_uuid=s["ids"]["source_epoch"]), s["auth"]["source"])
    app = FastAPI()
    app.include_router(create_handoff_router(bridge, operator_dependency=operator))
    client = TestClient(app)
    path = f"/center-handoffs/{s['ids']['handoff']}/items/{s['ids']['item']}/verify-movement"
    request = {"physical_present": True, "barcode_namespace": "lab", "barcode": "BKR-1", "evidence_refs": ["operator:scan"]}
    assert client.post(path, json=request).status_code == 401
    assert fetched == []
    client.headers["authorization"] = "Bearer operator"
    assert client.post(path, json={**request, "container_details": details(s)}).status_code == 422
    response = client.post(path, json=request)
    assert response.status_code == 200, response.text
    assert response.json()["payload"]["kind"] == "movement_verified"
    assert len(fetched) == 2


def test_existing_sqlite_inventory_adds_movement_tables_on_restart(setup, tmp_path):
    s = setup
    original = s["stores"]["source"].get_instance(s["carrier"]["uuid"])
    with s["stores"]["source"].transaction() as c:
        # 仅测试临时库：模拟 v19 已有库存、尚无 T03b 的两张追加表。
        c.execute("DROP TABLE station_handoff_movement")
        c.execute("DROP TABLE station_handoff_settlement_history")
    s["stores"]["source"].close()
    restored = InventoryStore(str(tmp_path / "source.db"))
    try:
        assert restored.get_instance(s["carrier"]["uuid"]) == original
        assert restored.query_all("SELECT * FROM station_handoff_movement") == []
        assert restored.query_all("SELECT * FROM station_handoff_settlement_history") == []
        assert restored.query_one("SELECT * FROM container_station_binding")["carrier_material_uuid"] == s["carrier"]["uuid"]
    finally:
        restored.close()
