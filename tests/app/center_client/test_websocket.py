"""WSS 只增加可靠通知通路；不能跳序、丢回执或绕过持久 inbox。"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from tests.app.center_client.test_center_client import INSTANCE, STATION, config
from tests.app.center_client.test_runtime import Center
from unilabos.app.center_client.client import CenterClient, CenterClientError
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.websocket import (
    NotificationReceiver,
    decode_message,
    envelope,
    validate_attention_snapshot,
)


def setup_receiver(tmp_path):
    server = Center()
    client = CenterClient(config(tmp_path), server)
    store = DurableCenterStore(tmp_path / "wss.db", station_uuid=STATION, instance_uuid=INSTANCE)
    client.open_session(store.event_epoch_uuid)
    receiver = NotificationReceiver(client._config, client, store, lambda: None)
    return server, client, store, receiver


def hello(session):
    return envelope(session, "hello_ack", {"station_uuid": STATION, "instance_uuid": INSTANCE,
        "session_uuid": session.session_uuid, "session_epoch": session.session_epoch,
        "heartbeat_seconds": 15, "server_time": "2026-10-15T00:00:00Z", "replay_from_command_sequence": 1})


class Connection:
    def __init__(self, messages, sent=None):
        self.messages, self.sent = iter(messages), sent or (lambda _: None)

    async def send(self, value):
        self.sent(json.loads(value))

    async def recv(self):
        try:
            return next(self.messages)
        except StopIteration as error:
            raise EOFError from error


class LiveConnection:
    def __init__(self, first_message):
        self.messages = asyncio.Queue()
        self.messages.put_nowait(first_message)
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        value = await self.messages.get()
        if value is None:
            raise EOFError
        return value

    def close(self):
        self.messages.put_nowait(None)


def test_wss_commits_before_ack_and_lost_ack_survives_reopen(tmp_path):
    server, client, store, receiver = setup_receiver(tmp_path)
    notification = server.add()
    command = notification["command"]
    sent = []

    def observe(body):
        sent.append(body)
        if body["type"] == "command.ack":
            assert store.commands(unacknowledged=True) == [notification]
            assert store.cursor() == 0
            raise ConnectionError("提交 inbox 后丢失网络 ACK")

    try:
        with pytest.raises(ConnectionError):
            asyncio.run(receiver._exchange(Connection([hello(client.session), envelope(client.session, "task.available", notification)], observe), client.session))
        assert sent[-1]["payload"]["command_uuid"] == command["command_uuid"]
        store.close()
        store = DurableCenterStore(tmp_path / "wss.db", station_uuid=STATION, instance_uuid=INSTANCE)
        assert store.commands(unacknowledged=True) == [notification]
        store.receive(notification)
        assert len(store.commands()) == 1
    finally:
        store.close()


def test_wss_hello_advertises_station_http_base_url(tmp_path):
    server, client, store, _receiver = setup_receiver(tmp_path)
    client_config = config(tmp_path, http_base_url="http://station.example:8002")
    receiver = NotificationReceiver(client_config, client, store, lambda: None)
    sent = []
    try:
        with pytest.raises(EOFError):
            asyncio.run(
                receiver._exchange(
                    Connection([hello(client.session)], sent.append), client.session
                )
            )
        assert sent[0]["type"] == "hello"
        assert sent[0]["payload"]["http_base_url"] == "http://station.example:8002"
    finally:
        store.close()


@pytest.mark.parametrize("provider_kind", ["empty", "huge", "raises"])
def test_wss_attention_changed_never_reads_or_sends_snapshot(tmp_path, provider_kind):
    _server, client, store, _receiver = setup_receiver(tmp_path)
    snapshot = {"manual_confirmation_count": 0, "exception_count": 1,
        "items": {"manual_confirmations": [], "exceptions": [{
            "decision_kind": "handoff_reconciliation", "decision_id": "60000000-0000-4000-8000-000000000901",
            "handoff_uuid": "60000000-0000-4000-8000-000000000901", "revision": 1, "status": "required", "options": [],
            "opened_at": "2026-10-17T00:00:00Z",
            "meta_data": {"progress": {"handoff": {"details": "中文证明" * 30000}}},
        }]}}
    assert len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) > 100 * 1024

    def provider():
        if provider_kind == "raises":
            raise RuntimeError("完整事项读取失败不能断开通知连接")
        return snapshot if provider_kind == "huge" else {
            "manual_confirmation_count": 0, "exception_count": 0,
            "items": {"manual_confirmations": [], "exceptions": []}}

    receiver = NotificationReceiver(client._config, client, store, lambda: None, attention_provider=provider)
    try:
        for _ in range(2):
            sent = []
            with pytest.raises(EOFError):
                asyncio.run(receiver._exchange(Connection([hello(client.session),
                    envelope(client.session, "ping", {})], sent.append), client.session))
            assert [message["type"] for message in sent] == ["hello", "attention.changed", "pong"]
            assert sent[1]["payload"] == {}
            assert all(len(json.dumps(message, ensure_ascii=False).encode("utf-8")) < 16384 for message in sent)
    finally:
        store.close()


def test_wss_sends_attention_changes_even_when_change_happens_during_send(tmp_path):
    _server, client, store, _receiver = setup_receiver(tmp_path)
    def provider():
        pytest.fail("WSS 通知不能读取完整事项")
    receiver = NotificationReceiver(client._config, client, store, lambda: None, attention_provider=provider)
    class ChangingConnection(LiveConnection):
        async def send(self, value):
            await super().send(value)
            if len(self.sent) == 2:
                receiver.notify_attention_changed()
    connection = ChangingConnection(hello(client.session))
    async def scenario():
        exchange = asyncio.create_task(receiver._exchange(connection, client.session))
        try:
            for _ in range(200):
                if len(connection.sent) == 3:
                    break
                await asyncio.sleep(0)
            assert [message["type"] for message in connection.sent] == ["hello", "attention.changed", "attention.changed"]
            assert all(message["payload"] == {} for message in connection.sent[1:])
            connection.messages.put_nowait(envelope(client.session, "ping", {}))
            for _ in range(200):
                if len(connection.sent) == 4:
                    break
                await asyncio.sleep(0)
            assert [message["type"] for message in connection.sent].count("attention.changed") == 2
            assert connection.sent[-1]["type"] == "pong"
            connection.close()
            with pytest.raises(EOFError):
                await exchange
        finally:
            exchange.cancel()
            from contextlib import suppress
            with suppress(asyncio.CancelledError, EOFError):
                await exchange
    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_attention_snapshot_rejects_uncertain_and_center_only_aliases():
    uncertain = {
        "manual_confirmation_count": 0,
        "exception_count": 1,
        "items": {
            "manual_confirmations": [],
            "exceptions": [{
                "attention_uuid": "uncertain:job-1",
                "kind": "exception",
                "subtype": "uncertain",
                "status": "open",
                "job_uuid": "job-1",
                "reason": "执行结果未知",
            }],
        },
    }
    with pytest.raises(CenterClientError):
        validate_attention_snapshot(uncertain)


def test_exact_wss_ack_retires_only_persisted_command(tmp_path):
    server, client, store, receiver = setup_receiver(tmp_path)
    notification = server.add()
    command = notification["command"]
    ack = {"command_uuid": command["command_uuid"], "command_sequence": 1,
           "command_digest": notification["command_digest"], "acknowledged_sequence": 1}
    try:
        with pytest.raises(EOFError):
            asyncio.run(receiver._exchange(Connection([hello(client.session), envelope(client.session, "task.available", notification),
                                           envelope(client.session, "command.acknowledged", ack)]), client.session))
        assert store.cursor() == 1 and not store.commands(unacknowledged=True)
        assert store.commands() == [notification]  # 可靠送达不等于执行完成。
    finally:
        store.close()


def test_wss_accepts_task_cancel_and_acknowledges_it(tmp_path):
    server, client, store, receiver = setup_receiver(tmp_path)
    notification = server.add_cancel()
    command = notification["command"]
    ack = {
        "command_uuid": command["command_uuid"],
        "command_sequence": 1,
        "command_digest": notification["command_digest"],
        "acknowledged_sequence": 1,
    }
    try:
        with pytest.raises(EOFError):
            asyncio.run(
                receiver._exchange(
                    Connection([
                        hello(client.session),
                        envelope(client.session, "task.cancel", notification),
                        envelope(client.session, "command.acknowledged", ack),
                    ]),
                    client.session,
                )
            )
        assert store.cursor() == 1
        assert store.commands() == [notification]
    finally:
        store.close()


def test_ack_prefix_does_not_jump_when_http_and_wss_finish_out_of_order(tmp_path):
    server, _client, store, _receiver = setup_receiver(tmp_path)
    try:
        first, second = server.add(), server.add()
        store.receive(first)
        store.receive(second)
        store.acknowledge_command(second["command"]["command_uuid"])
        assert store.cursor() == 0
        store.acknowledge_command(first["command"]["command_uuid"])
        assert store.cursor() == 2
    finally:
        store.close()


def test_gap_uses_http_recovery_without_ack(tmp_path):
    server, client, store, receiver = setup_receiver(tmp_path)
    server.add()
    second = server.add()
    sent = []
    try:
        with pytest.raises(CenterClientError, match="序列不连续"):
            asyncio.run(receiver._exchange(Connection([hello(client.session), envelope(client.session, "task.available", second)], sent.append), client.session))
        assert not store.commands()
        assert [body["type"] for body in sent] == ["hello"]
    finally:
        store.close()


@pytest.mark.parametrize("change", ["duplicate", "trailing", "unknown", "session", "binary"])
def test_ws_rejects_invalid_envelopes(tmp_path, change):
    _server, client, store, _receiver = setup_receiver(tmp_path)
    try:
        raw = envelope(client.session, "ping", {})
        if change == "duplicate":
            raw = raw.replace('{', '{"protocol_version":"1.0",', 1)
        elif change == "trailing":
            raw += '{}'
        elif change == "unknown":
            body = json.loads(raw)
            body["extra"] = True
            raw = json.dumps(body)
        elif change == "session":
            other = deepcopy(client.session)
            other.session_epoch += 1
            raw = envelope(other, "ping", {})
        else:
            raw = raw.encode()
        with pytest.raises(CenterClientError):
            decode_message(raw, client.session)
    finally:
        store.close()


def test_fenced_message_stops_control_and_keeps_historical_evidence_session(tmp_path):
    from uuid import uuid4

    from unilabos.app.center_client.client import SessionFencedError

    _server, client, store, receiver = setup_receiver(tmp_path)
    old = deepcopy(client.session)
    fenced = {"reason": "STALE_SESSION", "new_session_uuid": str(uuid4()), "new_session_epoch": 2}
    try:
        with pytest.raises(SessionFencedError):
            asyncio.run(receiver._exchange(Connection([hello(old), envelope(old, "session.fenced", fenced)]), old))
        assert client.session is None
        with pytest.raises(SessionFencedError):
            client.open_session(store.event_epoch_uuid)
        assert client._with_session_retry(store.event_epoch_uuid, lambda session: session, evidence=True) == old
    finally:
        store.close()


def test_late_fenced_message_cannot_revoke_renewed_session(tmp_path):
    _server, client, store, _receiver = setup_receiver(tmp_path)
    try:
        old = deepcopy(client.session)
        # 模拟公开握手获得新的会话后，旧连接的提示才到达。
        from uuid import uuid4
        client._transport.session_uuid = str(uuid4())
        current = client.open_session(store.event_epoch_uuid)
        assert current != old
        assert client.fence_session(old) is False
        assert client.session == current
    finally:
        store.close()


@pytest.mark.parametrize("update", [{"extra": True}, {"new_session_epoch": True},
                                     {"new_session_epoch": 0}, {"reason": ""},
                                     {"new_session_uuid": "invalid"}])
def test_fenced_payload_strict_fields(tmp_path, update):
    from uuid import uuid4

    from unilabos.app.center_client.websocket import validate_fenced

    _server, client, store, _receiver = setup_receiver(tmp_path)
    try:
        payload = {"reason": "STALE_SESSION", "new_session_uuid": str(uuid4()), "new_session_epoch": 2}
        payload.update(update)
        with pytest.raises(CenterClientError):
            validate_fenced(payload, client.session)
    finally:
        store.close()


def test_catalog_hint_only_after_valid_committed_receipt(tmp_path):
    from uuid import uuid4

    from unilabos.app.center_client.digest import digest

    _server, client, store, receiver = setup_receiver(tmp_path)
    receipt = {"station_uuid": STATION, "catalog_version": str(uuid4()), "catalog_epoch_uuid": str(uuid4()),
               "catalog_sequence": 1, "catalog_digest": digest({"catalog": 1}), "contract_count": 0, "tombstone_count": 0}
    try:
        receiver.notify_catalog_changed(receipt)
        sent = []
        with pytest.raises(EOFError):
            asyncio.run(receiver._exchange(Connection([hello(client.session)], sent.append), client.session))
        assert sent[-1]["type"] == "catalog.changed"
        assert sent[-1]["payload"] == {key: receipt[key] for key in (
            "catalog_version", "catalog_epoch_uuid", "catalog_sequence", "catalog_digest")}
        with pytest.raises((CenterClientError, ValueError)):
            receiver.notify_catalog_changed({**receipt, "extra": True})
    finally:
        store.close()


@pytest.mark.parametrize("change_during_initial_send", [False, True])
@pytest.mark.parametrize("center_created", [False, True])
def test_native_error_lifecycle_notifies_wss_and_reads_http_without_manual_notifications(tmp_path, change_during_initial_send, center_created):
    from contextlib import suppress

    from tests.workflow.test_error_control import decision_request, seed_job
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.error_handling import ErrorHandlingService
    from unilabos.workflow.store import WorkflowStore
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    _server, client, durable, _receiver = setup_receiver(tmp_path)
    workflow = WorkflowStore(tmp_path / "wss.db")
    from tests.workflow.test_center_attention_snapshot import _bind_center_task
    task, job = seed_job(workflow)
    if center_created:
        _bind_center_task(workflow, task)
    bridge = TaskSchedulerBridge(workflow, scheduler=EdgeScheduler(dispatcher=RecordingDispatcher()))
    service = ErrorHandlingService(workflow, bridge, None)
    control = bridge.error_control
    import threading
    from types import SimpleNamespace

    from tests.app.center_client.test_attention_api import http_client
    from unilabos.app.center_client.runtime import CenterRuntime
    runtime = CenterRuntime.__new__(CenterRuntime)
    runtime.config, runtime.client, runtime.store = client._config, client, durable
    runtime._poll_lock = threading.RLock()
    native_reads = []
    def native_state():
        state = workflow.center_attention_state()
        native_reads.append(state)
        return state
    runtime.service = SimpleNamespace(center_attention_snapshot=workflow.center_attention_snapshot,
        center_attention_state=native_state)
    runtime._native_attention_lock = threading.Lock()
    runtime._native_attention_state = native_state()
    runtime.materials = runtime.handoffs = None
    http = http_client(runtime)
    def attention_provider():
        pytest.fail("通知线程不能读取完整事项")
    receiver = NotificationReceiver(client._config, client, durable, lambda: None,
        attention_provider=attention_provider)
    runtime._notifications = receiver
    bridge.set_attention_notifier(runtime._notify_native_attention_changes)
    decisions = []

    def report_error():
        decisions.append(control.report_error(job, phase="execution", message="设备离线"))

    class ChangingConnection(LiveConnection):
        async def send(self, value):
            await super().send(value)
            if (change_during_initial_send and self.sent[-1]["type"] == "attention.changed"
                    and not decisions):
                # 初始快照已经取出、发送尚未结束时发生真实状态变化。
                report_error()

    connection = ChangingConnection(hello(client.session))

    async def wait_snapshot(count, stage=None, after=0):
        for _ in range(200):
            notifications = [item for item in connection.sent if item["type"] == "attention.changed"]
            if len(notifications) > after:
                assert all(item["payload"] == {} for item in notifications)
                latest = http.get("/api/v1/center-attentions").json()["data"]["snapshot"]
                if latest["exception_count"] == count and (stage is None
                        or latest["items"]["exceptions"][0]["stage"] == stage):
                    return len(notifications)
            await asyncio.sleep(0)
        pytest.fail(f"没有收到异常数量为 {count}、阶段为 {stage} 的 HTTP 当前事项")

    async def scenario():
        exchange = asyncio.create_task(receiver._exchange(connection, client.session))
        try:
            if not change_during_initial_send:
                await wait_snapshot(0)
                report_error()
            if not center_created:
                for _ in range(200):
                    if len(native_reads) >= 2:
                        break
                    await asyncio.sleep(0)
                assert len(native_reads) >= 2
                assert len([item for item in connection.sent if item["type"] == "attention.changed"]) == 1
                assert http.get("/api/v1/center-attentions").json()["data"]["snapshot"]["exception_count"] == 0
                assert control.snapshot()["errors"][0]["decision_id"] == decisions[0]["decision_id"]
                return
            count = await wait_snapshot(1, "DECISION_REQUIRED", after=1)
            entered = bridge.decide_error(decisions[0]["decision_id"],
                decision_request(control, decisions[0], "enter_manual_handling"),
                idempotency_key="ws-enter", operator_id="tester")
            count = await wait_snapshot(1, "MANUAL_HANDLING", after=count)
            session = entered["station"]["active_session"]
            completed = service.complete(session["session_id"], {
                "expected_session_version": session["version"],
                "expected_station_version": entered["station"]["station_version"],
                "summary": "已完成现场检查",
            }, idempotency_key="ws-complete", operator_id="tester")
            count = await wait_snapshot(1, "DECISION_REQUIRED", after=count)
            error = completed["station"]["errors"][0]
            selected = control.decide(error["decision_id"],
                decision_request(control, error, "cancel_task", session_id=session["session_id"],
                    expected_session_version=completed["station"]["active_session"]["version"]),
                idempotency_key="ws-cancel", operator_id="tester")
            count = await wait_snapshot(1, "APPLYING", after=count)
            control.mark_applied(selected["command_uuid"])
            await wait_snapshot(0, after=count)
            connection.close()
            with pytest.raises(EOFError):
                await exchange
        finally:
            connection.close()
            exchange.cancel()
            with suppress(asyncio.CancelledError, EOFError):
                await exchange

    try:
        asyncio.run(scenario())
    finally:
        bridge.set_attention_notifier(None)
        bridge.close()
        workflow.close()
        durable.close()


def test_wss_attention_wakeup_does_not_drop_concurrent_ping(tmp_path):
    from contextlib import suppress
    _server, client, store, _receiver = setup_receiver(tmp_path)
    receiver = NotificationReceiver(client._config, client, store, lambda: None,
        attention_provider=lambda: {})
    connection = LiveConnection(hello(client.session))
    async def scenario():
        exchange = asyncio.create_task(receiver._exchange(connection, client.session))
        try:
            for _ in range(30):
                await asyncio.sleep(0)
            receiver.notify_attention_changed()
            connection.messages.put_nowait(envelope(client.session, 'ping', {}))
            for _ in range(200):
                if len(connection.sent) >= 4:
                    break
                await asyncio.sleep(0)
            assert sorted(message['type'] for message in connection.sent[2:]) == ['attention.changed', 'pong']
        finally:
            connection.close()
            exchange.cancel()
            with suppress(asyncio.CancelledError, EOFError):
                await exchange
    try:
        asyncio.run(scenario())
    finally:
        store.close()
