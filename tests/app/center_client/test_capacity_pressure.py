"""容量只暂停新领取；永久拒绝跨重启保留，原身份经核实后可重放。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests

from tests.app.center_client.test_center_client import INSTANCE, INVOCATION, STATION, config, event_ack
from tests.app.center_client.test_event_envelope import draft, store_at
from tests.app.center_client.test_handoff_events import intent
from tests.app.center_client.test_runtime import Center, Service, runtime
from unilabos.app.center_client import capacity
from unilabos.app.center_client.client import CenterClient, CenterClientError, ControlSession
from unilabos.app.center_client.config import CapacityLimits, CenterConfigError
from unilabos.app.center_client.digest import digest
from unilabos.app.center_client.runtime import CenterRuntime
from unilabos.app.center_client.transport import CenterRequestHeldError, CenterTransport


def test_disk_pressure_preserves_notification_then_recovers_same_task_and_result(tmp_path, monkeypatch):
    center, service = Center(), Service()
    command = center.add()
    run = runtime(tmp_path, center, service)
    run.store.receive(command)
    free = [0]
    monkeypatch.setattr(capacity, "disk_free_bytes", lambda _path: free[0])
    try:
        run.poll_once()
        assert center.claims == 0
        assert service.task is None
        assert run.store.commands() == [command]
        assert run.capacity_status()["pause_reasons"] == ["DISK_FREE_RESERVE"]
        free[0] = 2**40
        service.crash_after_commit = True
        with pytest.raises(RuntimeError, match="模拟公共提交"):
            run.poll_once()
        original = deepcopy(service.calls[0])
        task_uuid = service.task["uuid"]
        free[0] = 0
        run.poll_once()
        assert service.calls == [original]
        assert service.task["uuid"] == task_uuid
        assert run.store.commands() == []
        service.complete("succeeded", result={"value": "原结果"})
        run.poll_once()
        assert center.reports[-1]["event_type"] == "outcome"
        assert center.reports[-1]["local_task_uuid"] == task_uuid
        assert run.store.events() == []
        assert center.claims == 1
    finally:
        run.close()


def test_pressure_counts_utf8_bytes_both_outboxes_and_result_reserve(tmp_path, monkeypatch):
    store = store_at(tmp_path / "pressure.db")
    monkeypatch.setattr(capacity, "disk_free_bytes", lambda _path: 2**40)
    try:
        body = draft(terminal=True)
        body["payload"]["output"] = {"sample": "样品"}
        store.record_event(INVOCATION, terminal=True, body=body)
        store.remember_handoff_intent(intent(store))
        status = capacity.snapshot(store, CapacityLimits(outbox_high_water_events=3))
        assert status["boundary_pending_events"] == 1
        assert status["handoff_pending_events"] == 1
        assert status["next_claim_reserved_events"] == 2
        assert status["pause_reasons"] == ["OUTBOX_EVENT_HIGH_WATER"]
        original_bytes = sum(len(row[0].encode("utf-8")) for row in store._db.execute(
            "SELECT event_json FROM center_boundary_outbox UNION ALL SELECT event_json FROM center_handoff_outbox"
        ))
        assert status["pending_bytes"] == original_bytes
        limits = CapacityLimits(outbox_high_water_bytes=original_bytes + 7, result_reserve_bytes=8)
        assert capacity.snapshot(store, limits)["pause_reasons"] == ["OUTBOX_BYTE_HIGH_WATER"]
        assert len(store.events()) == 1 and len(store.handoff_events()) == 1
        monkeypatch.setattr(capacity, "disk_free_bytes", lambda _path: None)
        assert "DISK_CAPACITY_UNAVAILABLE" in capacity.snapshot(store, CapacityLimits())["pause_reasons"]
    finally:
        store.close()


@pytest.mark.parametrize("status", [413, 422])
def test_permanent_event_rejection_survives_restart_and_explicit_retry_keeps_identity(tmp_path, monkeypatch, status):
    path = tmp_path / "events.db"
    store = store_at(path)
    event = store.record_event(INVOCATION, terminal=False, body=draft())
    cfg = config(tmp_path)
    requests_seen = []
    rejected = [True]

    def send(_method, _url, **kwargs):
        requests_seen.append(deepcopy(kwargs["json"]))
        response = requests.Response()
        response.status_code = status if rejected[0] else 202
        body = {"code": "PAYLOAD_TOO_LARGE"} if rejected[0] else event_ack(kwargs["json"])
        response._content = json.dumps(body).encode()
        return response

    def build(current_store):
        transport = CenterTransport(cfg)
        monkeypatch.setattr(transport, "_session", lambda: SimpleNamespace(request=send))
        client = CenterClient(cfg, transport)
        client.restore_evidence_session(ControlSession(str(uuid4()), str(uuid4()), 1, STATION, INSTANCE))
        return CenterRuntime(cfg, client, current_store, Service())

    run = build(store)
    run._flush()
    held = store.request_holds()[0]
    assert requests_seen == [event]
    assert store.events() == []
    assert store.capacity_backlog()["pending_events"] == 1
    run.close()
    store = store_at(path)
    run = build(store)
    try:
        run._flush()
        with pytest.raises(CenterRequestHeldError):
            run.client.report(event)
        assert requests_seen == [event]
        with pytest.raises(CenterClientError, match="已变化"):
            store.request_retry(held["request_digest"], expected_attempts=2, reason="已修正限制")
        store.request_retry(held["request_digest"], expected_attempts=1, reason="已核实并调整中心限制")
        rejected[0] = False
        run._flush()
        assert requests_seen == [event, event]
        assert store.capacity_backlog()["pending_events"] == 0
        row = store._db.execute("SELECT * FROM center_boundary_outbox").fetchone()
        assert json.loads(row["event_json"]) == event
        assert row["acknowledged"] == 1
        assert row["application"] == "applied"
    finally:
        run.close()


def test_held_events_do_not_starve_later_bounded_outbox_pages(tmp_path):
    store = store_at(tmp_path / "batch.db")
    try:
        events = [store.record_event(str(uuid4()), terminal=False, body=draft()) for _ in range(101)]
        assert len(store.events()) == 100
        for event in events[:100]:
            key = digest(event)
            store.hold_request(key, "POST", "/events", event["event_uuid"], 413, "PAYLOAD_TOO_LARGE")
        assert store.events() == [(events[-1]["station_invocation_uuid"], events[-1])]
        assert store.capacity_backlog()["pending_events"] == 101
        store.request_retry(digest(events[0]), expected_attempts=1, reason="原请求限制已修复")
        assert [event["event_uuid"] for _, event in store.events()] == [events[0]["event_uuid"], events[-1]["event_uuid"]]
    finally:
        store.close()


def test_pending_command_pages_do_not_starve_an_independent_task(tmp_path):
    center, service = Center(), Service()
    for _ in range(100):
        pending = center.add()
        pending["command"]["type"] = "handoff.available"
        pending["command_digest"] = digest(pending["command"])
    center.add()
    original_request = center.request

    def bounded_pull(method, path, **kwargs):
        response = original_request(method, path, **kwargs)
        if "/commands?" in path and len(response["commands"]) > 100:
            response["commands"] = response["commands"][:100]
            response["next_sequence"] = response["commands"][-1]["command"]["command_sequence"]
            response["has_more"] = True
        return response

    center.request = bounded_pull
    run = runtime(tmp_path, center, service)
    try:
        run.poll_once()
        assert center.claims == 0
        run.poll_once()
        assert center.claims == 1
        assert len(service.calls) == 1
        assert len(run.store.commands()) == 100
    finally:
        run.close()


@pytest.mark.parametrize("already_acknowledged", [False, True])
def test_held_ack_page_preserves_original_commands_and_allows_independent_work(tmp_path, already_acknowledged):
    center, service = Center(), Service()
    commands = [center.add() for _ in range(101)]
    run = runtime(tmp_path, center, service)
    for command in commands:
        run.store.receive(command)
    if already_acknowledged:
        run.store.acknowledge_command(commands[-1]["command"]["command_uuid"])
    request = center.request
    attempts = []

    def reject_ack(method, path, **kwargs):
        if path.endswith("/commands/ack"):
            body = kwargs["json_body"]
            attempts.append(body["command_sequence"])
            if body["command_sequence"] <= 100:
                raise CenterRequestHeldError(digest(body), 413)
        result = request(method, path, **kwargs)
        if "/commands?" in path and len(result["commands"]) > 100:
            result["commands"] = result["commands"][:100]
            result["next_sequence"] = result["commands"][-1]["command"]["command_sequence"]
            result["has_more"] = True
        return result

    center.request = reject_ack
    try:
        run.poll_once()
        assert center.claims == int(already_acknowledged)
        run.poll_once()
        assert center.claims == 1
        assert len(service.calls) == 1
        assert run.store.commands(unacknowledged=True) == commands[:100]
        assert (101 in attempts) is not already_acknowledged
    finally:
        run.close()


def test_reviewed_recovery_releases_only_future_result_budget(tmp_path, monkeypatch):
    from tests.app.center_client.test_center_client import FakeTransport, package_response
    from tests.app.center_client.test_event_history import response, session
    from tests.app.center_client.test_recovery_mode import resume_request, retained
    from unilabos.app.center_client.client import TaskPackage
    from unilabos.app.center_client.event_history import EventHistoryRecovery, EventHistoryStore
    from unilabos.persistence.recovery_mode import enter, resume, review

    source = store_at(tmp_path / "recovered.db")
    monkeypatch.setattr(capacity, "disk_free_bytes", lambda _path: 2**40)
    packages = []
    try:
        session(source)
        for _ in range(32):
            body = package_response()
            body["payload"]["station_invocation_uuid"] = str(uuid4())
            body["payload"]["global_task_uuid"] = str(uuid4())
            body["payload_digest"] = digest(body["payload"])
            package = source.remember_package(TaskPackage.from_response(body))
            source.confirm_claim(package.station_invocation_uuid)
            packages.append(package)
        event = source.record_event(packages[0].station_invocation_uuid, terminal=False, body=draft())
        before = source.capacity_backlog()
        assert before["unfinished_claims"] == 32
        assert capacity.snapshot(source, CapacityLimits())["new_claim_paused"]
        with source._db.transaction():
            mode = enter(source._db, "旧备份全部调用永久隔离")
        history = EventHistoryStore(source.path, station_uuid=STATION, instance_uuid=INSTANCE)
        try:
            client = CenterClient(config(tmp_path), FakeTransport([response(event, status="missing"), retained(event)]))
            assert EventHistoryRecovery(client, history, retain_missing=True).recover(1)["state"] == "retained_needs_review"
        finally:
            history.close()
        with source._db.transaction():
            checkpoint = {
                "checkpoint_uuid": str(uuid4()), "recovery_uuid": mode["recovery_uuid"],
                "station_uuid": source.station_uuid, "instance_uuid": source.instance_uuid,
                "event_epoch_uuid": source.event_epoch_uuid, "command_high_watermark": 0,
                "event_high_watermark": 1, "invocations": [], "reason": "原调用保留隔离", "created_at_ms": 1234,
            }
            resume(source._db, resume_request(review(source._db), checkpoint["checkpoint_uuid"]), checkpoint=checkpoint)
        after = capacity.snapshot(source, CapacityLimits())
        assert after["unfinished_claims"] == 0
        assert not after["new_claim_paused"]
        assert after["pending_events"] == before["pending_events"] == 1
        assert after["pending_bytes"] == before["pending_bytes"]
        assert source.invocation_held(packages[0].station_invocation_uuid)
        assert json.loads(source._db.execute("SELECT event_json FROM center_boundary_outbox").fetchone()[0]) == event
        assert source._db.execute("SELECT COUNT(*) FROM center_invocation_inbox WHERE terminal_recorded=0").fetchone()[0] == 32
    finally:
        source.close()


def test_capacity_cli_inspects_and_releases_current_failure_without_credentials(tmp_path, capsys):
    path = tmp_path / "diagnostics.db"
    store = store_at(path)
    key = digest({"original": 1})
    store.hold_request(key, "PUT", "/workflow-catalog/original/chunks/0", None, 413, "PAYLOAD_TOO_LARGE")
    store.close()
    capacity.main(["--database", str(path), "--database-type", "sqlite", "status"])
    status = json.loads(capsys.readouterr().out)
    assert status["capacity"]["held_requests"] == 1
    assert status["request_holds"][0]["request_digest"] == key
    capacity.main(["--database", str(path), "--database-type", "sqlite", "retry-request",
                   "--request-digest", key, "--expected-attempts", "1", "--reason", "已核实块上限"])
    assert json.loads(capsys.readouterr().out)["retry_requested"] is True
    restored = store_at(path)
    try:
        assert restored.request_holds() == []
        restored.hold_request(key, "PUT", "/workflow-catalog/original/chunks/0", None, 413, "PAYLOAD_TOO_LARGE")
        with pytest.raises(CenterClientError, match="已变化"):
            restored.request_retry(key, expected_attempts=1, reason="旧核实不能清除新的拒绝")
        assert restored.request_hold(key)["attempts"] == 2
    finally:
        restored.close()


@pytest.mark.parametrize("changes", [{"outbox_high_water_events": 1}, {"disk_free_reserve_bytes": 0},
                                    {"result_reserve_bytes": True}, {"outbox_high_water_bytes": 1}])
def test_invalid_capacity_limits_are_rejected(changes):
    with pytest.raises(CenterConfigError):
        replace(CapacityLimits(), **changes)


def test_capacity_limits_can_be_loaded_without_machine_secrets():
    limits = CapacityLimits.from_env({"CENTER_OUTBOX_HIGH_WATER_EVENTS": "20", "CENTER_RESULT_RESERVE_BYTES": "4096"})
    assert limits.outbox_high_water_events == 20
    assert limits.result_reserve_bytes == 4096


def test_http_pressure_stops_new_inbox_growth_until_space_returns(tmp_path, monkeypatch):
    center, service = Center(), Service()
    center.add()
    run = runtime(tmp_path, center, service)
    free = [0]
    monkeypatch.setattr(capacity, "disk_free_bytes", lambda _path: free[0])
    try:
        run.poll_once()
        assert run.store.commands() == []
        assert run.store.cursor() == 0
        assert center.claims == 0
        free[0] = 2**40
        run.poll_once()
        assert run.store.cursor() == 1
        assert center.claims == 1
    finally:
        run.close()


def test_wss_pressure_does_not_ack_a_new_notification_and_replay_keeps_identity(tmp_path, monkeypatch):
    import asyncio

    from tests.app.center_client.test_websocket import Connection, hello, setup_receiver
    from unilabos.app.center_client.websocket import envelope

    server, client, store, receiver = setup_receiver(tmp_path)
    notification = server.add()
    sent = []
    free = [0]
    monkeypatch.setattr(capacity, "disk_free_bytes", lambda _path: free[0])

    def exchange():
        return receiver._exchange(Connection([
            hello(client.session), envelope(client.session, "task.available", notification),
        ], sent.append), client.session)

    try:
        with pytest.raises(CenterClientError, match="容量不足"):
            asyncio.run(exchange())
        assert store.commands() == []
        assert [message["type"] for message in sent] == ["hello"]
        free[0] = 2**40
        with pytest.raises(EOFError):
            asyncio.run(exchange())
        assert store.commands() == [notification]
        assert sent[-1]["type"] == "command.ack"
        free[0] = 0
        with pytest.raises(EOFError):
            asyncio.run(exchange())
        assert store.commands() == [notification]
        assert sent[-1]["payload"]["command_uuid"] == notification["command"]["command_uuid"]
    finally:
        store.close()


def test_oldest_event_time_orders_seconds_and_milliseconds_correctly(tmp_path):
    store = store_at(tmp_path / "time.db")
    try:
        for occurred in ["2026-10-16T00:00:00.500Z", "2026-10-16T00:00:00Z"]:
            body = draft()
            body["occurred_at"] = body["payload"]["accepted_at"] = occurred
            store.record_event(str(uuid4()), terminal=False, body=body)
        assert store.capacity_backlog()["oldest_event_occurred_at"] == "2026-10-16T00:00:00.000Z"
    finally:
        store.close()
