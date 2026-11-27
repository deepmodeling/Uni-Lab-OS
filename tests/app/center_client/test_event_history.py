"""历史恢复不跳洞、不分配新身份、不恢复控制或创建执行事实。"""
from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INSTANCE, INVOCATION, STATION, FakeTransport, config, event_ack
from tests.app.center_client.test_event_envelope import draft, legacy_database, store_at
from tests.app.center_client.test_handoff_events import acknowledgement, intent
from unilabos.app.center_client.client import CenterClient, CenterClientError, ControlSession
from unilabos.app.center_client.event_history import EventHistoryRecovery, EventHistoryStore
from unilabos.center_protocol.event_history import validate_response
from unilabos.center_protocol.events import EventEnvelopeError
from unilabos.persistence.center_schema import CenterSchemaError
from unilabos.persistence.database import open_database


def response(event, *, prefix=0, status="recorded", handoff_response=None, application="applied"):
    return {"station_uuid": event["station_uuid"], "instance_uuid": event["instance_uuid"],
            "event_epoch_uuid": event["event_epoch_uuid"], "event_sequence": event["event_sequence"],
            "status": status, "last_contiguous_event_sequence": prefix,
            "highest_received_event_sequence": max(prefix, event["event_sequence"]) if status == "recorded" else prefix,
            "record": {"envelope": event, "acknowledgement": event_ack(event, prefix=prefix, application=application),
                       "handoff_response": handoff_response} if status == "recorded" else None}


def session(store):
    store.save_session(ControlSession(str(uuid4()), str(uuid4()), 1, STATION, INSTANCE))


def recovery(tmp_path, responses, path=None):
    store = EventHistoryStore(path or tmp_path / "events.db", station_uuid=STATION, instance_uuid=INSTANCE)
    transport = FakeTransport(responses)
    client = CenterClient(config(tmp_path), transport)
    return store, transport, EventHistoryRecovery(client, store)


def test_legacy_ack_is_preserved_and_original_envelope_backfilled(tmp_path):
    path = tmp_path / "events.db"
    legacy_database(path)
    source = store_at(path)
    session(source)
    row = source._db.execute("SELECT * FROM center_boundary_outbox WHERE event_sequence=1").fetchone()
    event = json.loads(row["event_json"])
    source.close()
    store, transport, recover = recovery(tmp_path, [response(event, status="missing"), event_ack(event), response(event, prefix=2)])
    assert recover.recover(1) == {"state": "recovered", "event_sequence": 1, "application": "applied", "last_contiguous_event_sequence": 2}
    after = store._db.execute("SELECT * FROM center_boundary_outbox WHERE event_sequence=1").fetchone()
    assert dict(after) == dict(row)
    assert transport.calls[1]["body"] == event
    assert all("sessions" not in call["path"] and "claim" not in call["path"] for call in transport.calls)
    store.close()


def test_missing_local_event_restores_from_center_without_task_or_job(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    first = source.record_event(INVOCATION, terminal=False, body=draft())
    second = source.record_event(INVOCATION, terminal=True, body=draft(terminal=True))
    with source._db.transaction():
        source._db.execute("DELETE FROM center_boundary_outbox WHERE event_sequence=1")
    source.close()
    with pytest.raises(CenterClientError, match="缺口"):
        store_at(path)
    store, transport, recover = recovery(tmp_path, [response(first, prefix=2)])
    assert store.gaps() == [1]
    assert recover.recover(1)["state"] == "recovered"
    assert store.gaps() == []
    assert transport.calls[0]["body"]["identity"] is None
    assert store._db.execute("SELECT COUNT(*) FROM center_invocation_inbox").fetchone()[0] == 0
    assert not store._db.execute("SELECT name FROM sqlite_master WHERE name IN ('task','job')").fetchall()
    store.close()
    normal = store_at(path)
    assert normal.events() == [(INVOCATION, second)]
    assert normal.record_event(str(uuid4()), terminal=False, body=draft())["event_sequence"] == 3
    normal.close()


def test_ack_does_not_fill_lower_holes_and_wrong_evidence_is_durable_attention(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    first = source.record_event(INVOCATION, terminal=False, body=draft())
    second = source.record_event(INVOCATION, terminal=True, body=draft(terminal=True))
    with source._db.transaction():
        source._db.execute("DELETE FROM center_boundary_outbox WHERE event_sequence=1")
    source.close()
    wrong = response(second)
    wrong["record"]["acknowledgement"]["event_digest"] = "sha256:" + "ff" * 32
    store, _, recover = recovery(tmp_path, [response(second), response(first, status="missing"), wrong])
    assert recover.recover(2)["last_contiguous_event_sequence"] == 0
    assert store.gaps() == [1]
    assert recover.recover(1)["state"] == "needs_review"
    assert recover.recover(2)["state"] == "needs_review"
    assert store.status(2)["state"] == "needs_review"
    assert store.gaps() == [1]
    assert store._db.execute("SELECT last_event_sequence FROM center_runtime_state").fetchone()[0] == 2
    store.close()


def test_response_loss_resumes_original_intent_after_restart(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    event = source.record_event(INVOCATION, terminal=False, body=draft())
    source.close()
    store, transport, recover = recovery(tmp_path, [response(event, status="missing"), TimeoutError("响应丢失")])
    with pytest.raises(TimeoutError):
        recover.recover(1)
    assert store.status(1)["state"] == "pending"
    original = transport.calls[1]["body"]
    store.close()
    store, transport, recover = recovery(tmp_path, [response(event, prefix=1)])
    assert recover.recover(1)["state"] == "recovered"
    assert original == event
    assert len(transport.calls) == 1
    store.close()


def test_handoff_original_ack_and_prefix_are_independent(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    event = source.remember_handoff_intent(intent(source))
    original = acknowledgement(event)
    original["ack"]["last_contiguous_event_sequence"] = 0
    source.acknowledge_handoff_event(event["event_uuid"], original)
    source.close()
    store, _, recover = recovery(tmp_path, [response(event, prefix=1, handoff_response=original)])
    assert recover.recover(1)["last_contiguous_event_sequence"] == 1
    assert json.loads(store._db.execute("SELECT ack_json FROM center_handoff_outbox").fetchone()[0]) == original
    store.close()


def test_missing_control_receipt_never_reissues_handoff_commitment(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    event = source.remember_handoff_intent(intent(source))
    source.close()
    store, transport, recover = recovery(tmp_path, [response(event, status="missing")])
    assert recover.recover(1)["state"] == "needs_review"
    assert len(transport.calls) == 1
    assert store._db.execute("SELECT ack_json FROM center_handoff_outbox").fetchone()[0] is None
    store.close()


def test_recovery_cannot_create_epoch_or_extend_high_water(tmp_path):
    path = tmp_path / "events.db"
    with pytest.raises(CenterSchemaError, match="原实例"):
        EventHistoryStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    source = store_at(path)
    session(source)
    source.close()
    store, transport, recover = recovery(tmp_path, [])
    with pytest.raises(CenterClientError, match="高水位"):
        recover.recover(1)
    assert not transport.calls
    store.close()


def test_recovery_rejects_unmigrated_legacy_without_reallocating_identity(tmp_path):
    path = tmp_path / "events.db"
    legacy_database(path)
    db = open_database(str(path), "workflow")
    before = db.execute("SELECT event_uuid,event_json,acknowledged FROM center_boundary_outbox ORDER BY event_uuid").fetchall()
    db.close()
    with pytest.raises(CenterSchemaError, match="原信封"):
        EventHistoryStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    db = open_database(str(path), "workflow")
    assert db.execute("SELECT event_uuid,event_json,acknowledged FROM center_boundary_outbox ORDER BY event_uuid").fetchall() == before
    assert "last_event_sequence" not in [row[1] for row in db.execute("PRAGMA table_info(center_runtime_state)")]
    db.close()


@pytest.mark.parametrize("field", ["station_uuid", "instance_uuid", "event_epoch_uuid", "event_sequence", "event_digest", "event_uuid"])
def test_history_rejects_identity_substitution(tmp_path, field):
    path = tmp_path / "events.db"
    source = store_at(path)
    event = source.record_event(INVOCATION, terminal=False, body=draft())
    source.close()
    request = {"event_epoch_uuid": event["event_epoch_uuid"], "event_sequence": 1,
               "identity": {key: event[key] for key in ("event_uuid", "event_digest")}}
    changed = deepcopy(response(event))
    changed["record"]["envelope"][field] = 2 if field == "event_sequence" else "sha256:" + "ff" * 32 if field == "event_digest" else str(uuid4())
    with pytest.raises((EventEnvelopeError, ValueError)):
        validate_response(changed, request, station_uuid=STATION, instance_uuid=INSTANCE)


def test_duplicate_mixed_stream_sequence_remains_invalid(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    first = source.record_event(INVOCATION, terminal=False, body=draft())
    event = source.remember_handoff_intent(intent(source))
    from unilabos.center_protocol.handoff import make_handoff_event
    event = make_handoff_event({key: value for key, value in event.items() if key not in ("protocol_version", "event_sequence", "event_digest")}, event_sequence=1)
    with source._db.transaction():
        source._db.execute("UPDATE center_handoff_outbox SET event_sequence=1,event_json=?", (json.dumps(event),))
    source.close()
    with pytest.raises(CenterSchemaError):
        EventHistoryStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    db = open_database(str(path), "workflow")
    assert json.loads(db.execute("SELECT event_json FROM center_boundary_outbox").fetchone()[0]) == first
    db.close()
