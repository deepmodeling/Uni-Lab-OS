"""交接与执行共享序号、精确确认、跨库重传及协议边界。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INSTANCE, INVOCATION, STATION
from tests.app.center_client.test_event_envelope import draft, store_at
from unilabos.app.center_client.client import CenterClientError
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.handoff import (
    HandoffProtocolError,
    make_handoff_event,
    validate_handoff_ack,
    validate_handoff_event,
    validate_handoff_package,
)


def uid():
    return str(uuid4())


def intent(store):
    return {"event_uuid": uid(), "event_epoch_uuid": store.event_epoch_uuid,
            "station_uuid": STATION, "instance_uuid": INSTANCE, "handoff_uuid": uid(),
            "occurred_at_ms": 1800000000000,
            "payload": {"kind": "released", "expected_revision": 2,
                        "release_uuid": uid(), "evidence_refs": ["inventory:committed"]}}


def snapshot(event):
    return {
        "handoff_uuid": event["handoff_uuid"], "lab_uuid": uid(),
        "source_station_uuid": STATION, "target_station_uuid": uid(),
        "source_instance_uuid": INSTANCE, "target_instance_uuid": uid(),
        "status": "released", "revision": 3, "attention": False, "attention_reasons": [],
        "items": [{"handoff_item_uuid": uid(), "container_uuid": uid(), "expected_state_version": 1,
                   "reserved_state_version": 2, "current_state_version": 2,
                   "source_material_refs": [{"station_uuid": STATION, "local_material_uuid": uid(),
                                             "local_revision": 1, "material_lineage_uuid": uid()}],
                   "source_snapshot": {"label": "样品"}, "receipt": None}],
        "prepared": None, "released": {"event_epoch_uuid": event["event_epoch_uuid"],
            "release_uuid": event["payload"]["release_uuid"], "evidence_refs": ["inventory:committed"]},
        "permit": None, "cancel_acks": [], "created_at_ms": 1800000000000,
        "in_transit_deadline_ms": 1800000600000,
    }


def acknowledgement(event):
    return {"handoff": snapshot(event), "ack": {
        **{key: event[key] for key in ("event_uuid", "event_epoch_uuid", "event_sequence", "event_digest")},
        "application": "applied", "last_contiguous_event_sequence": event["event_sequence"],
    }}


def test_defined_handoff_requires_immutable_run_transfer_and_source_kind():
    from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, StationHandoffAuthority

    body = snapshot({"handoff_uuid": uid(), "event_epoch_uuid": uid(),
                     "payload": {"release_uuid": uid()}})
    body.update(global_task_uuid=uid(), transfer_definition_uuid=uid(), transfer_source_kind="node_output")
    validate_handoff_package({"payload": body, "payload_digest": digest(body)})
    StationHandoffAuthority._assert_same_items(body, deepcopy(body))
    for field in ("global_task_uuid", "transfer_definition_uuid", "transfer_source_kind"):
        partial = deepcopy(body)
        del partial[field]
        with pytest.raises(HandoffProtocolError):
            validate_handoff_package({"payload": partial, "payload_digest": digest(partial)})
        changed = deepcopy(body)
        changed[field] = "station_material" if field == "transfer_source_kind" else uid()
        with pytest.raises(HandoffInventoryError, match="固定身份"):
            StationHandoffAuthority._assert_same_items(body, changed)
        changed[field] = "invalid-uuid"
        with pytest.raises(HandoffProtocolError):
            validate_handoff_package({"payload": changed, "payload_digest": digest(changed)})


def test_legacy_material_edge_cannot_authorize_a_defined_handoff():
    body = snapshot({"handoff_uuid": uid(), "event_epoch_uuid": uid(),
                     "payload": {"release_uuid": uid()}})
    body.update(global_task_uuid=uid(), composite_edge_uuid=uid())
    with pytest.raises(HandoffProtocolError):
        validate_handoff_package({"payload": body, "payload_digest": digest(body)})


def test_transport_chain_identity_is_complete_monotonic_and_immutable():
    from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError, StationHandoffAuthority

    legacy = snapshot({"handoff_uuid": uid(), "event_epoch_uuid": uid(),
                       "payload": {"release_uuid": uid()}})
    first = {**legacy, "chain_uuid": uid(), "leg_no": 1}
    later = {**first, "leg_no": 2, "previous_handoff_uuid": uid()}
    for body in (legacy, first, later):
        assert validate_handoff_package({"payload": body, "payload_digest": digest(body)})["payload"] == body
        StationHandoffAuthority._assert_same_items(body, deepcopy(body))
    invalid = [
        {**legacy, "chain_uuid": uid()}, {**legacy, "leg_no": 1},
        {**first, "leg_no": 0}, {**first, "leg_no": True},
        {**first, "previous_handoff_uuid": uid()}, {**first, "previous_handoff_uuid": None},
        {**first, "leg_no": 2}, {**later, "previous_handoff_uuid": later["handoff_uuid"]},
    ]
    for body in invalid:
        with pytest.raises(HandoffProtocolError):
            validate_handoff_package({"payload": body, "payload_digest": digest(body)})
    for field, value in (("chain_uuid", uid()), ("leg_no", 3), ("previous_handoff_uuid", uid())):
        with pytest.raises(HandoffInventoryError, match="固定身份"):
            StationHandoffAuthority._assert_same_items(later, {**later, field: value})
    with pytest.raises(HandoffInventoryError, match="固定身份"):
        StationHandoffAuthority._assert_same_items(legacy, first)


def test_mixed_events_preserve_one_contiguous_sequence_after_restart(tmp_path):
    path = tmp_path / "shared.db"
    store = store_at(path)
    accepted = store.record_event(INVOCATION, terminal=False, body=draft())
    body = intent(store)
    handoff = store.remember_handoff_intent(body)
    outcome = store.record_event(INVOCATION, terminal=True, body=draft(terminal=True))
    assert [accepted["event_sequence"], handoff["event_sequence"], outcome["event_sequence"]] == [1, 2, 3]
    assert store.remember_handoff_intent(body) == handoff
    response = acknowledgement(handoff)
    store.acknowledge_handoff_event(handoff["event_uuid"], response)
    store.close()
    restored = store_at(path)
    try:
        assert restored.handoff_ack(handoff["event_uuid"]) == response
        assert restored.handoff_events() == []
        assert restored.remember_handoff_intent(body) == handoff
        assert restored.remember_handoff_intent(intent(restored))["event_sequence"] == 4
    finally:
        restored.close()


def test_mixed_writers_share_transaction_sequence(tmp_path):
    stores = [store_at(tmp_path / "shared.db"), store_at(tmp_path / "shared.db")]
    def write(index):
        store = stores[index % 2]
        event = (store.remember_handoff_intent(intent(store)) if index % 2 else
                 store.record_event(uid(), terminal=False, body=draft()))
        return event["event_sequence"]
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert sorted(workers.map(write, range(20))) == list(range(1, 21))
    finally:
        for store in stores:
            store.close()


def test_cross_stream_uuid_collision_and_changed_intent_are_rejected(tmp_path):
    store = store_at(tmp_path / "shared.db")
    try:
        original = store.record_event(INVOCATION, terminal=False, body=draft())
        body = intent(store)
        body["event_uuid"] = original["event_uuid"]
        with pytest.raises(CenterClientError, match="执行事件"):
            store.remember_handoff_intent(body)
        body["event_uuid"] = uid()
        saved = store.remember_handoff_intent(body)
        body["payload"]["evidence_refs"] = ["changed"]
        with pytest.raises(CenterClientError, match="变化"):
            store.remember_handoff_intent(body)
        execution = draft()
        execution["event_uuid"] = saved["event_uuid"]
        with pytest.raises(CenterClientError, match="交接事件"):
            store.record_event(uid(), terminal=False, body=execution)
        assert store.handoff_events() == [saved]
    finally:
        store.close()


def test_handoff_write_failure_rolls_back_body_and_shared_sequence(tmp_path, monkeypatch):
    store = store_at(tmp_path / "shared.db")
    execute = store._db.execute
    def fail(sql, params=()):
        if "UPDATE center_runtime_state SET last_event_sequence=?" in sql:
            raise RuntimeError("提交前故障")
        return execute(sql, params)
    body = intent(store)
    monkeypatch.setattr(store._db, "execute", fail)
    with pytest.raises(RuntimeError):
        store.remember_handoff_intent(body)
    assert store.handoff_events() == []
    monkeypatch.setattr(store._db, "execute", execute)
    try:
        assert store.remember_handoff_intent(body)["event_sequence"] == 1
    finally:
        store.close()


def test_ack_identity_and_original_response_are_exact(tmp_path):
    store = store_at(tmp_path / "shared.db")
    try:
        event = store.remember_handoff_intent(intent(store))
        response = acknowledgement(event)
        wrong = deepcopy(response)
        wrong["ack"]["event_sequence"] += 1
        with pytest.raises(CenterClientError):
            store.acknowledge_handoff_event(event["event_uuid"], wrong)
        assert store.handoff_events() == [event]
        store.acknowledge_handoff_event(event["event_uuid"], response)
        wrong = deepcopy(response)
        wrong["handoff"]["revision"] += 1
        with pytest.raises(CenterClientError, match="变化"):
            store.acknowledge_handoff_event(event["event_uuid"], wrong)
        assert store.handoff_ack(event["event_uuid"]) == response
    finally:
        store.close()


def test_foreign_epoch_and_missing_persisted_event_fail_closed(tmp_path):
    path = tmp_path / "shared.db"
    store = store_at(path)
    body = intent(store)
    body["event_epoch_uuid"] = uid()
    with pytest.raises(CenterClientError, match="纪元"):
        store.remember_handoff_intent(body)
    store.remember_handoff_intent(intent(store))
    with store._db.transaction():
        store._db.execute("DELETE FROM center_handoff_outbox")
    store.close()
    with pytest.raises(CenterClientError, match="缺口"):
        store_at(path)


@pytest.mark.parametrize("mutation", [
    lambda event: event.update(event_sequence=True),
    lambda event: event.update(extra="unknown"),
    lambda event: event["payload"].update(extra="unknown"),
    lambda event: event["payload"].update(evidence_refs=[]),
    lambda event: event.update(event_uuid=event["event_uuid"].upper()),
])
def test_strict_protocol_rejects_rehashed_invalid_events(tmp_path, mutation):
    store = store_at(tmp_path / "events.db")
    try:
        event = make_handoff_event(intent(store), event_sequence=1)
        mutation(event)
        event["event_digest"] = digest({key: value for key, value in event.items() if key != "event_digest"})
        with pytest.raises(HandoffProtocolError):
            validate_handoff_event(event)
    finally:
        store.close()


def test_package_digest_identity_and_target_binding_are_checked(tmp_path):
    store = store_at(tmp_path / "events.db")
    try:
        event = make_handoff_event(intent(store), event_sequence=1)
        payload = snapshot(event)
        body = {"payload": payload, "payload_digest": digest(payload)}
        assert validate_handoff_package(body, station_uuid=STATION, instance_uuid=INSTANCE) == body
        with pytest.raises(HandoffProtocolError):
            validate_handoff_package(body, station_uuid=uid(), instance_uuid=INSTANCE)
        body["payload"]["revision"] += 1
        with pytest.raises(HandoffProtocolError):
            validate_handoff_package(body)
        response = acknowledgement(event)
        response["handoff"]["handoff_uuid"] = uid()
        with pytest.raises(HandoffProtocolError):
            validate_handoff_ack(response, event)
    finally:
        store.close()


@pytest.mark.parametrize("number", [10**400, 2**53, float("inf"), float("nan")])
def test_handoff_snapshot_rejects_unsafe_numbers_as_protocol_error(tmp_path, number):
    from unilabos.center_protocol.handoff import validate_handoff_snapshot

    store = store_at(tmp_path / "events.db")
    try:
        payload = snapshot(make_handoff_event(intent(store), event_sequence=1))
        payload["items"][0]["source_snapshot"] = {"quantity": number}
        with pytest.raises(HandoffProtocolError, match="数值"):
            validate_handoff_snapshot(payload)
    finally:
        store.close()


def movement_snapshot(body):
    state = snapshot(body)
    state.update(revision=9, attention=True, attention_reasons=["HANDOFF_TRANSPORT_AFTER_SETTLEMENT"])
    item = state["items"][0]
    item["current_state_version"] = 6
    old = {"decision": "source_present", "actual_holder_station_uuid": state["source_station_uuid"],
           "proof_digest": digest({"old": "source-proof"}), "evidence_refs": ["old:scan"],
           "reason": "之前源端持有", "reconciled_at_ms": 1800000000001, "handoff_revision": 7}
    review = {"review_revision": 8, "previous_review_revision": None, "previous_settlement": old,
              "trigger_event_digest": digest({"new": "movement"}), "cause": "HANDOFF_TRANSPORT_AFTER_SETTLEMENT",
              "source_verification": None, "target_verification": None}
    for side in ("source", "target"):
        proof = {"handoff_item_uuid": item["handoff_item_uuid"], "verification_uuid": uid(),
                 "review_revision": 8, "expected_state_version": 6, "station_uuid": state[f"{side}_station_uuid"],
                 "physical_present": side == "target", "material_refs": [
                     {**item["source_material_refs"][0], "station_uuid": state[f"{side}_station_uuid"],
                      "local_material_uuid": uid(), "local_revision": 20}],
                 "inventory_snapshot": {"current_revision": 20}, "evidence_refs": ["current:scan"]}
        proof["verification_digest"] = digest(proof)
        review[f"{side}_verification"] = proof
    item["movement_review"] = review
    return state


def test_movement_evidence_uses_shared_ledger_and_survives_restart(tmp_path):
    path = tmp_path / "movement.db"
    store = store_at(path)
    body = intent(store)
    state = movement_snapshot(body)
    proof = state["items"][0]["movement_review"]["source_verification"]
    body["payload"] = {"kind": "movement_verified", "observed_revision": state["revision"],
                       "items": [proof], "evidence_refs": ["current:scan"]}
    saved = store.remember_handoff_intent(body)
    assert saved["payload"]["items"][0] == proof
    store.close()
    restored = store_at(path)
    try:
        assert restored.remember_handoff_intent(body) == saved
        assert restored.handoff_events() == [saved]
        changed = deepcopy(body)
        changed["payload"]["items"][0]["physical_present"] = True
        changed_proof = changed["payload"]["items"][0]
        changed_proof["verification_digest"] = digest({k: v for k, v in changed_proof.items() if k != "verification_digest"})
        with pytest.raises(CenterClientError, match="变化"):
            restored.remember_handoff_intent(changed)
    finally:
        restored.close()


def test_new_movement_settlement_accepts_current_target_identity_and_preserves_old_direction(tmp_path):
    store = store_at(tmp_path / "movement.db")
    try:
        state = movement_snapshot(intent(store))
    finally:
        store.close()
    item = state["items"][0]
    review = item["movement_review"]
    old_settlement = deepcopy(review["previous_settlement"])
    item["settlement"] = {"decision": "target_received", "actual_holder_station_uuid": state["target_station_uuid"],
                          "proof_digest": review["target_verification"]["verification_digest"],
                          "evidence_refs": ["current:scan"], "reason": "新移动已经到达目标",
                          "reconciled_at_ms": 1800000000010, "handoff_revision": 10}
    item["current_state_version"] += 1
    state.update(revision=10, attention=False, attention_reasons=[], status="received")
    valid = validate_handoff_package({"payload": state, "payload_digest": digest(state)})
    assert valid["payload"]["items"][0]["movement_review"]["previous_settlement"] == old_settlement
    assert item["receipt"] is None
    # 本轮结算不能换用历史源证明，即使重新计算包摘要。
    item["settlement"]["proof_digest"] = old_settlement["proof_digest"]
    with pytest.raises(HandoffProtocolError, match="双端证明"):
        validate_handoff_package({"payload": state, "payload_digest": digest(state)})


@pytest.mark.parametrize("fault", ["generation", "station", "lineage", "version", "both_present"])
def test_movement_proof_cannot_substitute_other_review_identity_or_location(tmp_path, fault):
    store = store_at(tmp_path / "movement.db")
    try:
        state = movement_snapshot(intent(store))
    finally:
        store.close()
    item = state["items"][0]
    review = item["movement_review"]
    proof = review["target_verification"]
    if fault == "generation":
        proof["review_revision"] -= 1
    elif fault == "station":
        proof["station_uuid"] = uid()
    elif fault == "lineage":
        proof["material_refs"][0]["material_lineage_uuid"] = uid()
    elif fault == "version":
        proof["expected_state_version"] += 5
    else:
        proof = review["source_verification"]
        proof["physical_present"] = True
        item["settlement"] = {"decision": "target_received", "actual_holder_station_uuid": state["target_station_uuid"],
                              "proof_digest": review["target_verification"]["verification_digest"],
                              "evidence_refs": ["scan:conflict"], "reason": "不能同时宣称双端持有",
                              "reconciled_at_ms": 1800000000010, "handoff_revision": 9}
        item["current_state_version"] += 1
    proof["verification_digest"] = digest({k: v for k, v in proof.items() if k != "verification_digest"})
    with pytest.raises(HandoffProtocolError):
        validate_handoff_package({"payload": state, "payload_digest": digest(state)})


def test_settlement_causal_coverage_is_preserved_and_strict(tmp_path):
    store = store_at(tmp_path / "coverage.db")
    try:
        state = movement_snapshot(intent(store))
    finally:
        store.close()
    old = state["items"][0]["movement_review"]["previous_settlement"]
    covered = [digest({"historical_transport": "original-envelope"})]
    old["covered_transport_digests"] = covered
    state["items"][0]["covered_transport_digests"] = covered
    body = {"payload": state, "payload_digest": digest(state)}
    assert validate_handoff_package(body) == body
    for invalid in (None, covered * 2, ["not-a-digest"], [{}], [digest(n) for n in range(1001)]):
        old["covered_transport_digests"] = invalid
        with pytest.raises(HandoffProtocolError, match="运输摘要"):
            validate_handoff_package({"payload": state, "payload_digest": digest(state)})
    del old["covered_transport_digests"]
    body = {"payload": state, "payload_digest": digest(state)}
    assert validate_handoff_package(body) == body
