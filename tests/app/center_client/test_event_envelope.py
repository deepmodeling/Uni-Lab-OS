"""完整事件信封、事务序号、旧账本升级及精确确认的恢复合同。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import (
    DIGEST,
    EPOCH,
    EVENT,
    INSTANCE,
    INVOCATION,
    LOCAL_TASK,
    STATION,
    FakeTransport,
    boundary_event,
    config,
    event_ack,
    session_response,
)
from tests.app.center_client.test_runtime import Center, Service, runtime
from unilabos.app.center_client.client import CenterClient, CenterClientError
from unilabos.app.center_client.digest import canonical_json, digest
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.events import (
    MAX_EVENT_SEQUENCE,
    EventEnvelopeError,
    utc_timestamp_from_ms,
    validate_event,
)
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.persistence.database import open_database

# 负例用的固定身份：不能在 parametrize 里调用 uuid4()，否则每个 xdist worker
# import 模块时拿到不同 UUID，node id 不一致会让 pytest-xdist 拒绝收集。
FOREIGN_EVENT_UUID = "11111111-1111-4111-8111-111111111111"
FOREIGN_EPOCH_UUID = "22222222-2222-4222-8222-222222222222"


def draft(*, terminal=False):
    return {"event_uuid": str(uuid4()), "local_task_uuid": LOCAL_TASK,
            "event_type": "outcome" if terminal else "accepted", "occurred_at": "2023-11-14T22:13:20.000Z",
            "payload": ({"task_payload_digest": DIGEST, "status": "succeeded", "output": {"value": 2.5}}
                        if terminal else {"task_payload_digest": DIGEST, "accepted_at": "2023-11-14T22:13:20.000Z",
                                          "local_task_uuid": LOCAL_TASK})}


def test_transport_pickup_matches_the_rust_wire_fixture():
    event = json.loads((Path(__file__).parent / "fixtures" / "station-transport-pickup-v1.json").read_text())
    assert validate_event(event) == event
    assert event["event_digest"] == "sha256:91dd4eeeb2c8d1f437fa4274481984ff866fd5ebb05e3cffcb6d558421b42009"
    assert digest({key:value for key,value in event.items() if key != "event_digest"}) == event["event_digest"]
    assert event["payload"]["task_payload_digest"] == "sha256:" + "a"*64


def store_at(path):
    return DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)


def test_sequence_atomic_and_replayed_phase_keeps_identity(tmp_path):
    path = tmp_path / "events.db"
    store = store_at(path)
    first = store.record_event(INVOCATION, terminal=False, body=draft())
    assert first["event_sequence"] == 1
    assert store.record_event(INVOCATION, terminal=False, body=draft()) == first
    terminal = store.record_event(INVOCATION, terminal=True, body=draft(terminal=True))
    assert terminal["event_sequence"] == 2
    original = canonical_json(terminal)
    store.close()
    restored = store_at(path)
    assert canonical_json(restored.events()[1][1]) == original
    assert restored.record_event(INVOCATION, terminal=True, body=draft(terminal=True)) == terminal
    third = restored.record_event(str(uuid4()), terminal=False, body=draft())
    assert third["event_sequence"] == 3
    restored.close()


def test_event_transaction_rolls_back_sequence_and_body(tmp_path, monkeypatch):
    store = store_at(tmp_path / "events.db")
    execute = store._db.execute
    def fail_after_sequence(sql, params=()):
        if "UPDATE center_invocation_inbox" in sql:
            raise RuntimeError("模拟整个事件事务提交前失败")
        return execute(sql, params)
    monkeypatch.setattr(store._db, "execute", fail_after_sequence)
    with pytest.raises(RuntimeError):
        store.record_event(INVOCATION, terminal=False, body=draft())
    assert store.events() == []
    assert execute("SELECT last_event_sequence FROM center_runtime_state").fetchone()[0] == 0
    monkeypatch.setattr(store._db, "execute", execute)
    assert store.record_event(INVOCATION, terminal=False, body=draft())["event_sequence"] == 1
    store.close()


def test_two_connections_serialize_event_numbers(tmp_path):
    path = tmp_path / "events.db"
    stores = [store_at(path), store_at(path)]
    def write(index):
        return stores[index % 2].record_event(str(uuid4()), terminal=False, body=draft())["event_sequence"]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            sequences = list(pool.map(write, range(12)))
        assert sorted(sequences) == list(range(1, 13))
    finally:
        for store in stores:
            store.close()


def test_sequence_exhaustion_cannot_wrap_or_replace_epoch(tmp_path):
    store = store_at(tmp_path / "events.db")
    epoch = store.event_epoch_uuid
    with store._db.transaction():
        store._db.execute("UPDATE center_runtime_state SET last_event_sequence=?", (MAX_EVENT_SEQUENCE,))
    with pytest.raises(CenterClientError, match="耗尽"):
        store.record_event(INVOCATION, terminal=False, body=draft())
    assert store.event_epoch_uuid == epoch and store.events() == []
    store.close()


def legacy_database(path, *, epoch=EPOCH, damaged=False):
    """原版本原样 schema；故意逆序插入以验证排序不依赖 SQL 扫描顺序。"""
    database = open_database(str(path), "workflow", create_parent=True)
    with database.transaction():
        # database_mode 的一次性 PG 库会复用表结构；此处还原指定的旧版本结构。
        database.execute("DROP TABLE IF EXISTS center_boundary_outbox")
        database.execute("DROP TABLE IF EXISTS center_runtime_state")
        database.execute("""CREATE TABLE center_runtime_state (
            singleton TEXT PRIMARY KEY,station_uuid TEXT NOT NULL,instance_uuid TEXT NOT NULL,
            event_epoch_uuid TEXT NOT NULL,session_json TEXT)""")
        database.execute("""CREATE TABLE center_boundary_outbox (
            event_uuid TEXT PRIMARY KEY,invocation_uuid TEXT NOT NULL,phase INTEGER NOT NULL,
            event_json TEXT NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0,
            UNIQUE(invocation_uuid,phase))""")
        database.execute("INSERT INTO center_runtime_state VALUES ('runtime',?,?,?,NULL)", (STATION, INSTANCE, epoch))
        old = []
        for phase in (1, 0):
            body = {"event_uuid": str(uuid4()), "event_epoch_uuid": str(uuid4()) if damaged and phase == 1 else epoch,
                    "local_task_uuid": LOCAL_TASK, "event_type": "succeeded" if phase else "accepted",
                    "payload_digest": DIGEST, "output": {"measurement": "历史真实值"} if phase else {},
                    "occurred_at_ms": 100 + phase}
            database.execute("INSERT INTO center_boundary_outbox VALUES (?,?,?,?,?)",
                             (body["event_uuid"], INVOCATION, phase, json.dumps(body), int(phase == 0)))
            old.append(body)
    database.close()
    return old


def test_legacy_outbox_migration_keeps_evidence_and_acknowledgement(tmp_path):
    path = tmp_path / "events.db"
    old = legacy_database(path)
    store = store_at(path)
    assert store.event_epoch_uuid == EPOCH
    rows = store._db.execute("SELECT * FROM center_boundary_outbox ORDER BY event_sequence").fetchall()
    assert [row["acknowledged"] for row in rows] == [1, 0]
    assert rows[0]["application"] == "legacy_acknowledged"
    events = [validate_event(json.loads(row["event_json"])) for row in rows]
    assert [event["event_sequence"] for event in events] == [1, 2]
    assert [event["event_uuid"] for event in events] == [old[1]["event_uuid"], old[0]["event_uuid"]]
    assert events[1]["payload"]["output"] == old[0]["output"]
    assert events[1]["occurred_at"] == utc_timestamp_from_ms(old[0]["occurred_at_ms"])
    assert events[0]["payload"]["accepted_at"] == "1970-01-01T00:00:00.100Z"
    assert events[0]["payload"]["local_task_uuid"] == events[0]["local_task_uuid"]
    assert [event["event_sequence"] for _, event in store.events()] == [2]
    originals = [row["event_json"] for row in rows]
    store.close()
    restarted = store_at(path)
    assert [row[0] for row in restarted._db.execute("SELECT event_json FROM center_boundary_outbox ORDER BY event_sequence")] == originals
    assert restarted.record_event(str(uuid4()), terminal=False, body=draft())["event_sequence"] == 3
    restarted.close()


@pytest.mark.parametrize("value", [1e16, 1e20])
def test_legacy_jcs_float_output_migration_preserves_digest_and_identity(tmp_path, value):
    path = tmp_path / "events.db"
    old = legacy_database(path)
    outcome = old[0]
    outcome["output"] = {"value": value, "nested": [{"negative": -value}, True, 42]}
    original_output = canonical_json(outcome["output"])
    historical_json = canonical_json(outcome).decode()
    assert type(json.loads(historical_json)["output"]["value"]) is int
    db = open_database(str(path), "workflow")
    with db.transaction():
        db.execute("UPDATE center_boundary_outbox SET event_json=? WHERE event_uuid=?",
                   (historical_json, outcome["event_uuid"]))
    db.close()

    store = store_at(path)
    invocation, event = store.events()[0]
    assert invocation == INVOCATION and event["event_sequence"] == 2
    assert event["event_uuid"] == outcome["event_uuid"]
    assert event["event_epoch_uuid"] == EPOCH and event["local_task_uuid"] == LOCAL_TASK
    output = event["payload"]["output"]
    assert type(output["value"]) is float and type(output["nested"][0]["negative"]) is float
    assert type(output["nested"][1]) is bool and type(output["nested"][2]) is int
    assert canonical_json(output) == original_output
    assert digest(output) == digest(outcome["output"])
    original_event = deepcopy(event)
    store.close()

    restarted = store_at(path)
    restored = restarted.events()[0][1]
    assert validate_event(restored) == original_event
    assert type(restored["payload"]["output"]["value"]) is float
    assert canonical_json(restored["payload"]["output"]) == original_output
    client = CenterClient(config(tmp_path), FakeTransport([session_response(), event_ack(restored)]))
    restarted.acknowledge_event(restored["event_uuid"], client.report(restored))
    assert restarted.events() == []
    assert restarted._db.execute("SELECT last_event_sequence FROM center_runtime_state").fetchone()[0] == 2
    restarted.close()


@pytest.mark.parametrize("value", [2**53 + 1, 10**20 + 1, 10**400])
def test_legacy_jcs_output_rejects_inexact_integer_without_rewriting(tmp_path, value):
    path = tmp_path / "events.db"
    old = legacy_database(path)
    old[0]["output"] = {"nested": [{"unsafe": value}]}
    historical_json = json.dumps(old[0])
    db = open_database(str(path), "workflow")
    with db.transaction():
        db.execute("UPDATE center_boundary_outbox SET event_json=? WHERE event_uuid=?",
                   (historical_json, old[0]["event_uuid"]))
    before = [tuple(row[index] for index in range(3)) for row in db.execute(
        "SELECT event_uuid,event_json,acknowledged FROM center_boundary_outbox ORDER BY phase")]
    db.close()
    with pytest.raises(CenterClientError, match="无法等价恢复"):
        store_at(path)
    db = open_database(str(path), "workflow")
    assert [tuple(row[index] for index in range(3)) for row in db.execute(
        "SELECT event_uuid,event_json,acknowledged FROM center_boundary_outbox ORDER BY phase")] == before
    assert "event_sequence" not in {row[1] for row in db.execute("PRAGMA table_info(center_boundary_outbox)")}
    assert db.execute("SELECT event_epoch_uuid FROM center_runtime_state").fetchone()[0] == EPOCH
    db.close()


def test_migration_epoch_mismatch_rolls_back_without_rewriting_history(tmp_path):
    path = tmp_path / "events.db"
    old = legacy_database(path, damaged=True)
    with pytest.raises(CenterClientError, match="纪元"):
        store_at(path)
    db = open_database(str(path), "workflow")
    assert {json.loads(row[0])["event_uuid"] for row in db.execute("SELECT event_json FROM center_boundary_outbox")} == {body["event_uuid"] for body in old}
    assert db.execute("SELECT event_epoch_uuid FROM center_runtime_state").fetchone()[0] == EPOCH
    assert "event_sequence" not in {row[1] for row in db.execute("PRAGMA table_info(center_boundary_outbox)")}
    db.close()


def test_missing_epoch_ledger_cannot_recreate_identity(tmp_path):
    path = tmp_path / "events.db"
    legacy_database(path)
    db = open_database(str(path), "workflow")
    with db.transaction():
        db.execute("DELETE FROM center_runtime_state")
    db.close()
    with pytest.raises(CenterClientError, match="账本缺失"):
        store_at(path)


@pytest.mark.parametrize("field,replacement", [
    ("event_uuid", FOREIGN_EVENT_UUID), ("event_epoch_uuid", FOREIGN_EPOCH_UUID), ("event_sequence", 2),
    ("event_sequence", True), ("event_digest", "sha256:" + "0" * 64),
    ("last_contiguous_event_sequence", True), ("last_contiguous_event_sequence", -1),
    ("last_contiguous_event_sequence", MAX_EVENT_SEQUENCE + 1), ("application", "unknown"),
])
def test_ack_rejects_every_identity_mismatch(tmp_path, field, replacement):
    envelope = boundary_event()
    ack = {**event_ack(envelope), field: replacement}
    client = CenterClient(config(tmp_path), FakeTransport([session_response(), ack]))
    with pytest.raises(CenterClientError):
        client.report(envelope)


@pytest.mark.parametrize("prefix", [0, 1, 100])
def test_exact_ack_does_not_require_or_consume_a_contiguous_prefix(tmp_path, prefix):
    envelope = boundary_event()
    client = CenterClient(config(tmp_path), FakeTransport([session_response(), event_ack(envelope, prefix=prefix)]))
    assert client.report(envelope) == "applied"


@pytest.mark.parametrize("change", ["payload", "sequence", "time", "station", "unknown", "epoch", "boolean"])
def test_digest_covers_complete_event_identity_and_body(change):
    envelope = boundary_event()
    if change == "payload":
        envelope["payload"]["output"] = {"changed": True}
    elif change == "sequence":
        envelope["event_sequence"] = 2
    elif change == "time":
        envelope["occurred_at"] = "1970-01-01T00:00:00.002Z"
    elif change == "station":
        envelope["station_uuid"] = str(uuid4())
    elif change == "unknown":
        envelope["session_uuid"] = str(uuid4())
    elif change == "epoch":
        envelope["event_epoch_uuid"] = str(uuid4())
    else:
        envelope["event_sequence"] = True
    with pytest.raises(EventEnvelopeError):
        validate_event(envelope)


def test_conflict_ack_is_quarantined_durably_without_claiming_success(tmp_path):
    center, service = Center(), Service()
    center.add()
    original = center.request
    def conflict(method, path, **kwargs):
        if path.endswith("/events"):
            center.reports.append(deepcopy(kwargs["json_body"]))
            return event_ack(kwargs["json_body"], application="conflict")
        return original(method, path, **kwargs)
    center.request = conflict
    run = runtime(tmp_path, center, service)
    run.poll_once()
    first = deepcopy(center.reports)
    row = run.store._db.execute("SELECT acknowledged,application,event_json FROM center_boundary_outbox").fetchone()
    assert row["acknowledged"] == 0 and row["application"] == "conflict"
    assert json.loads(row["event_json"]) == first[0]
    run.close()
    restarted = runtime(tmp_path, center, service)
    restarted.poll_once()
    assert center.reports == first and restarted.store.events() == []
    restarted.close()


def test_runtime_keeps_outbox_after_inexact_ack(tmp_path):
    center, service = Center(), Service()
    center.add()
    original = center.request
    def wrong_ack(method, path, **kwargs):
        response = original(method, path, **kwargs)
        if path.endswith("/events"):
            response["event_digest"] = "sha256:" + "0" * 64
        return response
    center.request = wrong_ack
    run = runtime(tmp_path, center, service)
    with pytest.raises(CenterClientError):
        run.poll_once()
    pending = deepcopy(run.store.events())
    run.close()
    center.request = original
    restarted = runtime(tmp_path, center, service)
    assert restarted.store.events() == pending
    restarted.poll_once()
    assert restarted.store.events() == []
    assert center.reports[0] == center.reports[1]
    restarted.close()


def test_shared_rust_python_event_golden_vector():
    event = json.loads((Path(__file__).parent / "fixtures" / "station-event-v1.json").read_text())
    assert validate_event(event) == event
    expected = event.pop("event_digest")
    assert digest(event) == expected == "sha256:dad7b617a50d0036202302768de8a09081e49bf8d1e2073b6d9b73475e06cc7f"


@pytest.mark.parametrize("kind,payload", [
    ("accepted", {"task_payload_digest": DIGEST, "accepted_at": "1970-01-01T00:00:00Z",
                  "local_task_uuid": LOCAL_TASK}),
    ("running", {"task_payload_digest": DIGEST}),
    ("attention_changed", {"task_payload_digest": DIGEST, "required": True, "reason": "需检查"}),
    ("attention_changed", {"task_payload_digest": DIGEST, "required": False, "reason": "核实完成"}),
])
def test_event_payload_variants(kind, payload):
    event = boundary_event()
    event.update(event_type=kind, payload=payload)
    event.pop("event_digest")
    event["event_digest"] = digest(event)
    assert validate_event(event) == event


@pytest.mark.parametrize("payload", [
    {"task_payload_digest": DIGEST, "required": True, "reason": " "},
    {"task_payload_digest": DIGEST, "required": 1, "reason": "需检查"},
    {"task_payload_digest": DIGEST, "required": True, "reason": "汉" * 167},
])
def test_attention_reason_and_boolean_contract(payload):
    event = boundary_event()
    event.update(event_type="attention_changed", payload=payload)
    event.pop("event_digest")
    event["event_digest"] = digest(event)
    with pytest.raises(EventEnvelopeError):
        validate_event(event)


def redigest(event):
    event.pop("event_digest", None)
    event["event_digest"] = digest(event)
    return event


@pytest.mark.parametrize("timestamp", ["1970-01-01T00:00:00Z", "2023-11-14T22:13:20.123Z"])
def test_utc_timestamp_original_text_is_frozen(timestamp):
    event = boundary_event(event_type="accepted")
    event["occurred_at"] = event["payload"]["accepted_at"] = timestamp
    restored = validate_event(redigest(event))
    assert restored["occurred_at"] == restored["payload"]["accepted_at"] == timestamp


@pytest.mark.parametrize("timestamp", [
    1700000000000, -1, None, True, "1969-12-31T23:59:59.999Z",
    "2023-11-14T22:13:20+00:00", "2023-11-14T22:13:20-08:00", "2023-11-14T22:13:20",
    "2023-11-14T22:13:20.1Z", "2023-11-14T22:13:20.12Z", "2023-11-14T22:13:20.1234Z",
    "2023-11-14T22:13:20.123456Z", "2023-11-14T22:13:60Z", "2023-02-29T00:00:00Z",
    "2023-11-14T22:13:20z", "２０２３-11-14T22:13:20Z", "2023-11-14T22:13:20Z\n",
])
@pytest.mark.parametrize("field", ["occurred_at", "accepted_at"])
def test_utc_timestamp_rejects_non_contract_values(timestamp, field):
    event = boundary_event(event_type="accepted")
    (event if field == "occurred_at" else event["payload"])[field] = timestamp
    with pytest.raises(EventEnvelopeError):
        validate_event(redigest(event))


@pytest.mark.parametrize("change", ["missing_time", "missing_local", "wrong_local", "old_digest"])
def test_accepted_requires_time_and_same_local_task(change):
    event = boundary_event(event_type="accepted")
    payload = event["payload"]
    if change == "missing_time":
        payload.pop("accepted_at")
    elif change == "missing_local":
        payload.pop("local_task_uuid")
    elif change == "wrong_local":
        payload["local_task_uuid"] = EVENT
    else:
        payload["payload_digest"] = payload.pop("task_payload_digest")
    with pytest.raises(EventEnvelopeError):
        validate_event(redigest(event))


@pytest.mark.parametrize("value", [1e16, 1e20])
def test_binary64_result_restarts_and_receives_exact_ack(tmp_path, value):
    center, service = Center(), Service()
    center.add()
    first = runtime(tmp_path, center, service)
    first.poll_once()
    service.complete("succeeded", result={"value": value}, occurred_at="2023-11-14T22:13:20.123Z")
    center.report_lost = True
    with pytest.raises(CenterTransportError):
        first.poll_once()
    original = deepcopy(center.reports[-1])
    assert original["occurred_at"] == "2023-11-14T22:13:20.123Z"
    assert type(first.store.events()[0][1]["payload"]["output"]["value"]) is float
    assert validate_event(original) == original
    first.close()
    restarted = runtime(tmp_path, center, service)
    assert restarted.store.events()[0][1] == original
    assert type(restarted.store.events()[0][1]["payload"]["output"]["value"]) is float
    restarted.poll_once()
    assert center.reports[-1] == original and restarted.store.events() == []
    assert restarted.store._db.execute(
        "SELECT acknowledged,application FROM center_boundary_outbox WHERE event_uuid=?", (original["event_uuid"],),
    ).fetchone()["acknowledged"] == 1
    restarted.close()


def test_unsafe_integer_historical_json_is_rejected_without_rewriting(tmp_path):
    path = tmp_path / "events.db"
    store = store_at(path)
    body = draft(terminal=True)
    body["payload"]["output"] = {"value": 1e20}
    event = store.record_event(INVOCATION, terminal=True, body=body)
    previous_json = canonical_json(event).decode()
    with store._db.transaction():
        store._db.execute("UPDATE center_boundary_outbox SET event_json=?", (previous_json,))
    store.close()
    with pytest.raises(EventEnvelopeError, match="不能规范化"):
        store_at(path)
    database = open_database(str(path), "workflow")
    assert database.execute("SELECT event_json FROM center_boundary_outbox").fetchone()[0] == previous_json
    database.close()


def test_unreleased_complete_envelope_cannot_be_silently_upgraded(tmp_path):
    path = tmp_path / "events.db"
    store = store_at(path)
    event = store.record_event(INVOCATION, terminal=True, body=draft(terminal=True))
    event["occurred_at"] = 1700000000000
    event["payload"]["payload_digest"] = event["payload"].pop("task_payload_digest")
    previous_json = json.dumps(redigest(event))
    with store._db.transaction():
        store._db.execute("UPDATE center_boundary_outbox SET event_json=?", (previous_json,))
    store.close()
    with pytest.raises(EventEnvelopeError):
        store_at(path)
    database = open_database(str(path), "workflow")
    assert database.execute("SELECT event_json FROM center_boundary_outbox").fetchone()[0] == previous_json
    database.close()
