"""站端持久操作不从通知伪造实物；人工核对与实际落位进入库存权威。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.app.test_station_handoff import handoff_inventory_fixture, package
from unilabos.app.center_client.handoff_operations import HandoffOperations
from unilabos.center_protocol.digest import digest


def command(s, side, kind, details, *, actor=True):
    value = {
        "schema_version": 1,
        "operation_uuid": str(uuid4()),
        "handoff_uuid": s["ids"]["handoff"],
        "expected_revision": s["payload"]["revision"],
        "station_uuid": s["ids"][side],
        "event_epoch_uuid": s["ids"][side + "_epoch"],
        "actor_uuid": str(uuid4()) if actor else None,
        "details": details,
    }
    value["payload_digest"] = digest(value)
    return {"type": kind, "payload": value}


def verification(barcode="BKR-1"):
    return {"method": "manual_visual", "observed_identifiers": [barcode], "evidence_refs": ["operator:checked"]}


@pytest.fixture
def flow(tmp_path):
    fixture = handoff_inventory_fixture(tmp_path)
    s = next(fixture)
    client = SimpleNamespace(
        handoff_package=lambda *args: package(s["payload"]),
        container=lambda identity, *args, **kwargs: s["details"][identity],
    )
    managers = {
        side: HandoffOperations(
            SimpleNamespace(client=client, authority=s["auth"][side], incoming_plans=lambda payload: s["plans"])
        )
        for side in ("source", "target")
    }
    yield s, managers
    fixture.close()


def prepare(s, managers):
    cmd = command(s, "target", "handoff.target_prepare", {"include_transport_endpoints": True})
    assert managers["target"].handle(cmd)
    row = s["stores"]["target"].query_one("SELECT result_json FROM station_handoff_operation")
    import json

    intent = json.loads(row["result_json"])
    s["payload"].update(
        status="prepared",
        revision=2,
        prepared={
            "event_epoch_uuid": s["ids"]["target_epoch"],
            **{
                k: intent["payload"][k]
                for k in ("protection_uuid", "reservation_until_ms", "evidence_refs", "endpoint_snapshot")
            },
        },
    )
    assert managers["target"].handle(cmd)
    return cmd


def test_manual_two_confirmations_create_actual_source_and_target_inventory_facts(flow):
    s, managers = flow
    prepare(s, managers)
    ready = command(
        s,
        "source",
        "handoff.source_release",
        {"physical_ready": True, "verification": verification(), "include_transport_endpoints": True},
    )
    assert managers["source"].handle(ready)
    assert managers["source"].handle(ready)
    import json

    release = json.loads(
        s["stores"]["source"].query_one("SELECT result_json FROM station_handoff_operation")["result_json"]
    )
    assert release["payload"]["endpoint_snapshot"]["items"][0]["site_uuid"] == s["sites"]["source"]
    assert any(ref.startswith("manual_visual://") for ref in release["payload"]["evidence_refs"])
    s["payload"].update(
        status="dispatch_authorized",
        revision=4,
        released={
            "event_epoch_uuid": s["ids"]["source_epoch"],
            **{k: release["payload"][k] for k in ("release_uuid", "evidence_refs", "endpoint_snapshot")},
        },
        permit={"authorization_uuid": str(uuid4()), "logistics_subject_uuid": ready["payload"]["actor_uuid"]},
    )
    receive = command(
        s,
        "target",
        "handoff.target_receive",
        {
            "items": [
                {
                    "handoff_item_uuid": s["ids"]["item"],
                    "barcode_namespace": "lab",
                    "barcode": "BKR-1",
                    "actual_site_uuid": s["sites"]["target"],
                    "verification": verification(),
                }
            ]
        },
    )
    assert managers["target"].handle(receive)
    receipt = s["stores"]["target"].query_one("SELECT receipt_json FROM station_handoff_item")["receipt_json"]
    assert receipt
    assert managers["target"].handle(receive)
    assert s["stores"]["target"].query_one("SELECT receipt_json FROM station_handoff_item")["receipt_json"] == receipt
    changed = deepcopy(receive)
    changed["payload"]["details"]["items"][0]["barcode"] = "OTHER"
    changed["payload"]["payload_digest"] = digest(
        {k: v for k, v in changed["payload"].items() if k != "payload_digest"}
    )
    with pytest.raises(ValueError):
        managers["target"].handle(changed)


def test_automatic_source_waits_for_real_detector_and_foreign_epoch_is_rejected(flow):
    s, managers = flow
    prepare(s, managers)
    ready = command(s, "source", "handoff.source_release", {}, actor=False)
    assert managers["source"].handle(ready) is False
    assert not s["stores"]["source"].query_one("SELECT * FROM station_handoff_operation")
    ready["payload"]["event_epoch_uuid"] = str(uuid4())
    ready["payload"]["payload_digest"] = digest({k: v for k, v in ready["payload"].items() if k != "payload_digest"})
    with pytest.raises(ValueError):
        managers["source"].handle(ready)


def test_manual_wrong_source_identifier_does_not_freeze_inventory(flow):
    s, managers = flow
    prepare(s, managers)
    ready = command(
        s, "source", "handoff.source_release", {"physical_ready": True, "verification": verification("OTHER")}
    )
    with pytest.raises(ValueError):
        managers["source"].handle(ready)
    assert not s["stores"]["source"].query_one("SELECT * FROM station_handoff")


def test_renewal_keeps_same_site_and_identity_and_cannot_reacquire_expired_protection(flow):
    s, managers = flow
    prepare(s, managers)
    before = s["stores"]["target"].query_one("SELECT * FROM station_ingress_reservation")
    renewal = command(s, "target", "handoff.target_renew", {"ttl_seconds": 300})
    assert managers["target"].handle(renewal)
    after = s["stores"]["target"].query_one("SELECT * FROM station_ingress_reservation")
    assert (after["uuid"], after["site_uuid"]) == (before["uuid"], before["site_uuid"])
    assert after["revision"] == before["revision"] + 1
    assert managers["target"].handle(renewal)
    assert (
        s["stores"]["target"].query_one("SELECT revision FROM station_ingress_reservation")["revision"]
        == after["revision"]
    )
    s["payload"]["revision"] += 1
    s["clock"][0] += 600000
    with pytest.raises(ValueError):
        managers["target"].handle(command(s, "target", "handoff.target_renew", {"ttl_seconds": 600}))


def test_automatic_partial_arrivals_append_evidence_and_keep_command_pending(flow):
    import json

    from tests.app.test_station_handoff import add_second_item

    s, managers = flow
    _, second, _ = add_second_item(s)
    prepare(s, managers)
    s["payload"].update(
        status="dispatch_authorized",
        revision=4,
        permit={"authorization_uuid": str(uuid4()), "logistics_subject_uuid": str(uuid4())},
    )
    target = managers["target"]

    def observation(item, barcode, proof):
        reservation = s["stores"]["target"].query_one(
            "SELECT r.site_uuid FROM station_handoff_item i JOIN station_ingress_reservation r ON r.uuid=i.reservation_uuid WHERE i.handoff_item_uuid=?",
            (item,),
        )
        return {
            "handoff_item_uuid": item,
            "barcode_namespace": "lab",
            "barcode": barcode,
            "actual_site_uuid": reservation["site_uuid"],
            "evidence_refs": [proof],
        }

    first = observation(s["ids"]["item"], "BKR-1", "sensor:first")
    remaining = observation(second, "BKR-2", "sensor:second")
    target.arrival = lambda _: [first]
    cmd = command(s, "target", "handoff.target_receive", {}, actor=False)
    assert target.handle(cmd) is False
    original = s["stores"]["target"].query_one(
        "SELECT receipt_json FROM station_handoff_item WHERE handoff_item_uuid=?", (s["ids"]["item"],)
    )["receipt_json"]
    row = s["stores"]["target"].query_one(
        "SELECT result_json FROM station_handoff_operation WHERE kind='handoff.target_receive'"
    )
    assert row["result_json"] is None
    target.arrival = lambda _: [{**first, "evidence_refs": ["sensor:changed"]}, remaining]
    assert target.handle(cmd) is True
    assert (
        s["stores"]["target"].query_one(
            "SELECT receipt_json FROM station_handoff_item WHERE handoff_item_uuid=?", (s["ids"]["item"],)
        )["receipt_json"]
        == original
    )
    saved = json.loads(
        s["stores"]["target"].query_one(
            "SELECT parameters_json FROM station_handoff_operation WHERE kind='handoff.target_receive'"
        )["parameters_json"]
    )
    assert saved["items"][0]["evidence_refs"] == ["sensor:first"]
    assert target.handle(cmd) is True
