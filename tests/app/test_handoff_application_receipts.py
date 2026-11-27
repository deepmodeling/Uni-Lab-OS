"""中心结算结论与本站实际库存应用回执分开。"""
import pytest

from tests.app.test_station_handoff import frozen, package, settlement_payload, setup, source_proof  # noqa: F401
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError
from unilabos.center_protocol.handoff import make_handoff_event, validate_handoff_event


def test_final_snapshot_application_ack_is_durable_and_idempotent(setup):
    s = setup
    frozen(s)
    s["payload"].update(attention=True, attention_reasons=["HANDOFF_REVIEW_REQUIRED"], revision=4)
    proof = source_proof(s)["payload"]["items"][0]
    s["payload"]["items"][0]["source_verification"] = proof
    final = settlement_payload(s, decision="source_present", proof=proof["verification_digest"])
    target = s["auth"]["target"]
    target.apply_center_snapshot(package(final))
    target.apply_center_snapshot(package(final))
    events = [v for v in target.pending_intents() if v["payload"]["kind"] == "snapshot_applied"]
    assert len(events) == 1
    assert events[0]["payload"]["applied"] is True
    assert events[0]["payload"]["failure_reason"] is None
    assert events[0]["payload"]["package_digest"] == package(final)["payload_digest"]
    assert validate_handoff_event(make_handoff_event(events[0], event_sequence=1))["payload"]["applied"] is True


def test_application_failure_keeps_resource_and_success_only_after_retry(setup, monkeypatch):
    s = setup
    frozen(s)
    s["payload"].update(attention=True, attention_reasons=["HANDOFF_REVIEW_REQUIRED"], revision=4)
    proof = source_proof(s)["payload"]["items"][0]
    s["payload"]["items"][0]["source_verification"] = proof
    final = settlement_payload(s, decision="source_present", proof=proof["verification_digest"])
    target = s["auth"]["target"]
    original = target._apply_settlement
    def failed(*args):
        raise HandoffInventoryError("test_conflict", "库存结算冲突")
    monkeypatch.setattr(target, "_apply_settlement", failed)
    with pytest.raises(HandoffInventoryError):
        target.apply_center_snapshot(package(final))
    events = [v for v in target.pending_intents() if v["payload"]["kind"] == "snapshot_applied"]
    assert len(events) == 1
    assert events[0]["payload"]["applied"] is False
    assert events[0]["payload"]["failure_reason"] == "库存结算冲突"
    assert s["stores"]["target"].query_one("SELECT COUNT(*) n FROM station_handoff_resource WHERE active=1")["n"] > 0
    monkeypatch.setattr(target, "_apply_settlement", original)
    target.apply_center_snapshot(package(final))
    assert any(v["payload"]["kind"] == "snapshot_applied" and v["payload"]["applied"] for v in target.pending_intents())
