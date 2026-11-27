"""已结算墓碑不重复要求对账，原库存门禁和未完成责任仍须保留。"""
from __future__ import annotations

import copy
import json

import pytest

from tests.app.test_station_handoff import (
    add_second_item,
    frozen,
    mark_exception,
    package,
    receive,
    settlement_payload,
    setup,  # noqa: F401
    source_proof,
    uid,
)
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionRequest,
    DispatchResource,
    acquire_dispatch_permit,
)
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError
from unilabos.app.scheduler.inventory.handoff_attention import attention_handoffs, handoff_details
from unilabos.app.scheduler.resource_lock import material_lock_key
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.handoff import make_handoff_event


def acknowledge(s: dict, side: str, payload: dict, *, application: str = "applied") -> None:
    authority = s["auth"][side]
    numbered = s["stores"][side].query_one(
        "SELECT COUNT(*) n FROM station_handoff_outbox WHERE envelope_json IS NOT NULL"
    )["n"]
    for sequence, intent in enumerate(authority.pending_intents(), start=numbered + 1):
        event = make_handoff_event(intent, event_sequence=sequence)
        authority.remember_envelope(event["event_uuid"], event)
        authority.acknowledge_intent(event["event_uuid"], {
            "handoff": payload,
            "ack": {
                **{key: event[key] for key in ("event_uuid", "event_epoch_uuid", "event_sequence", "event_digest")},
                "application": application,
                "last_contiguous_event_sequence": sequence,
            },
        })


def canceled_snapshot(s: dict, *, reconciled: bool = False) -> dict:
    frozen(s)
    if reconciled:
        mark_exception(s)
        proof = source_proof(s)["payload"]["items"][0]
        s["payload"]["items"][0]["source_verification"] = proof
        final = settlement_payload(s, decision="source_present", proof=proof["verification_digest"])
    else:
        final = copy.deepcopy(s["payload"])
        final.update(status="canceled", revision=4)
    for authority in s["auth"].values():
        authority.apply_center_snapshot(package(final))
    return final


def assert_dispatch_blocked(s: dict, side: str, material: str) -> None:
    with s["stores"][side].transaction() as connection:
        decision = acquire_dispatch_permit(connection, DispatchAdmissionRequest(
            effect_uuid=uid(), task_uuid=uid(), job_uuid=uid(), attempt=1,
            parameter_hash="hash", expected_change_set={},
            resources=(DispatchResource(material_lock_key(material), "material", material),),
        ))
    assert not decision.acquired
    assert decision.wait_code == "station_handoff_protected"


@pytest.mark.parametrize("reconciled", [False, True], ids=["before-permit", "source-present"])
def test_completed_target_tombstone_stops_attention_without_releasing_dispatch_guard(setup, reconciled):
    s = setup
    final = canceled_snapshot(s, reconciled=reconciled)
    for side in ("source", "target"):
        acknowledge(s, side, final)
        before = handoff_details(s["auth"][side])
        assert attention_handoffs(s["auth"][side]) == []
        assert handoff_details(s["auth"][side]) == before
    item = before[0]["items"][0]
    assert item["protection_active"] is True
    assert all(resource["active"] == 1 for resource in item["resources"])
    assert item["reservation"]["state"] == "canceled"
    assert item["reservation"]["site_active"] == 0
    assert item["occupied_site_uuids"] == []
    assert_dispatch_blocked(s, "target", item["carrier_material_uuid"])


def test_completed_mixed_reconciliation_keeps_both_kinds_of_tombstone_protected(setup):
    s = setup
    _, second_item, _ = add_second_item(s)
    frozen(s)
    mark_exception(s)
    receipt = receive(s)
    s["payload"]["items"][0]["receipt"] = receipt
    proof = s["auth"]["source"].verify_source_presence(
        package(s["payload"]), handoff_item_uuid=second_item,
        barcode_namespace="lab", barcode="BKR-2", physical_present=True,
        evidence_refs=["scan:second-source-present"],
    )["payload"]["items"][0]
    s["payload"]["items"][1]["source_verification"] = proof
    final = settlement_payload(s, decision="target_received", proof=receipt["receipt_digest"])
    final.update(status="reconciled", attention_reasons=[])
    final["items"][1].update(current_state_version=3, settlement={
        **final["items"][0]["settlement"], "decision": "source_present",
        "actual_holder_station_uuid": s["ids"]["source"], "proof_digest": proof["verification_digest"],
    })
    for side, authority in s["auth"].items():
        authority.apply_center_snapshot(package(final))
        acknowledge(s, side, final)
        assert attention_handoffs(authority) == []
        protected = [item for item in handoff_details(authority)[0]["items"] if item["protection_active"]]
        assert len(protected) == 1
        assert protected[0]["state"] == ("settled" if side == "source" else "canceled")
        assert_dispatch_blocked(s, side, protected[0]["carrier_material_uuid"])


def test_unacknowledged_snapshot_stays_visible_until_exact_ack(setup):
    s = setup
    final = canceled_snapshot(s)
    assert len(attention_handoffs(s["auth"]["target"])) == 1
    acknowledge(s, "target", final)
    assert attention_handoffs(s["auth"]["target"]) == []


@pytest.mark.parametrize("reason", ["conflict", "recovery"])
def test_completed_tombstone_keeps_conflict_and_recovery_attention(setup, reason):
    s = setup
    final = canceled_snapshot(s)
    authority = s["auth"]["target"]
    intents = authority.pending_intents()
    acknowledge(s, "target", final, application="conflict" if reason == "conflict" else "applied")
    if reason == "recovery":
        authority.retain_recovery_intent(intents[-1]["event_uuid"], "恢复纪元仍待核实")
    assert len(attention_handoffs(authority)) == 1


def test_newer_failed_snapshot_cannot_be_hidden_by_previous_success(setup, monkeypatch):
    s = setup
    final = canceled_snapshot(s, reconciled=True)
    authority = s["auth"]["target"]
    acknowledge(s, "target", final)
    assert attention_handoffs(authority) == []
    newer = copy.deepcopy(final)
    newer["revision"] += 1
    original = authority._apply_settlement

    def fail_settlement(*args):
        raise HandoffInventoryError("test_conflict", "库存结算冲突")

    monkeypatch.setattr(authority, "_apply_settlement", fail_settlement)
    with pytest.raises(HandoffInventoryError, match="库存结算冲突"):
        authority.apply_center_snapshot(package(newer))
    acknowledge(s, "target", newer)
    attention = attention_handoffs(authority)
    assert len(attention) == 1
    assert "库存结算冲突" in attention[0]["meta_data"]["last_error"]
    monkeypatch.setattr(authority, "_apply_settlement", original)
    authority.apply_center_snapshot(package(newer))
    acknowledge(s, "target", newer)
    assert attention_handoffs(authority) == []


@pytest.mark.parametrize("defect", ["missing", "old-revision", "different-digest"])
def test_tombstone_requires_success_for_the_exact_current_snapshot(setup, defect):
    s = setup
    final = canceled_snapshot(s)
    acknowledge(s, "target", final)
    store = s["stores"]["target"]
    # 仅在隔离测试库模拟历史回执缺失或错配，不能把相邻修订的成功当成本次应用成功。
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT event_uuid,body_json FROM station_handoff_outbox WHERE purpose LIKE 'snapshot-applied:%'"
        ).fetchone()
        if defect == "missing":
            connection.execute("DELETE FROM station_handoff_outbox WHERE event_uuid=?", (row["event_uuid"],))
        else:
            body = json.loads(row["body_json"])
            if defect == "old-revision":
                body["payload"]["observed_revision"] -= 1
            else:
                body["payload"]["package_digest"] = digest({"other": "snapshot"})
            connection.execute("UPDATE station_handoff_outbox SET body_json=? WHERE event_uuid=?",
                               (json.dumps(body), row["event_uuid"]))
    assert len(attention_handoffs(s["auth"]["target"])) == 1


@pytest.mark.parametrize("defect", ["reserved-entry", "occupied-site", "missing-settlement"])
def test_incomplete_local_cleanup_is_not_a_completed_tombstone(setup, defect):
    s = setup
    final = canceled_snapshot(s, reconciled=True)
    acknowledge(s, "target", final)
    store = s["stores"]["target"]
    item = handoff_details(s["auth"]["target"])[0]["items"][0]
    # 模拟历史库存恢复不完整；保留完整成功回执也不能掩盖实际入口或结算责任。
    with store.transaction() as connection:
        if defect == "reserved-entry":
            connection.execute("UPDATE station_ingress_reservation SET state='reserved',site_active=1 WHERE uuid=?",
                               (item["reservation_uuid"],))
        elif defect == "occupied-site":
            connection.execute("UPDATE site SET occupied_material_uuid=? WHERE uuid=?",
                               (item["carrier_material_uuid"], s["sites"]["target"]))
        else:
            connection.execute("DELETE FROM station_handoff_settlement WHERE handoff_item_uuid=?",
                               (item["handoff_item_uuid"],))
    assert len(attention_handoffs(s["auth"]["target"])) == 1
    assert_dispatch_blocked(s, "target", item["carrier_material_uuid"])
