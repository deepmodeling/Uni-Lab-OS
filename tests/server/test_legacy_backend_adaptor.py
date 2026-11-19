"""旧云端 Backend 适配层：协议探测、WS 消息翻译、HTTP 数据面与物料镜像。"""

from __future__ import annotations

import asyncio
import gzip
import json
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.server.backend.execution_queue import QueueItem
from unilabos.server.backend.legacy_adaptor import probe
from unilabos.server.backend.legacy_adaptor.legacy.http import (
    LegacyBackendHTTPClient,
    LegacyBackendHTTPError,
)
from unilabos.server.backend.legacy_adaptor.legacy.sync import (
    LegacyMaterialMirror,
    legacy_material_edge,
    legacy_material_node,
    upload_registry_snapshot,
)
from unilabos.server.backend.legacy_adaptor.legacy.ws import (
    LegacyBackendWebSocketClient,
    _PriorityMessageQueue as LegacyPriorityMessageQueue,
)
from unilabos.server.backend.legacy_adaptor.session import BackendSessionFactory
from unilabos.server.backend.legacy_adaptor.websocket import (
    BackendWebSocketClient,
    _PriorityMessageQueue as RuntimePriorityMessageQueue,
)


# ── 测试替身 ─────────────────────────────────────────────────────


class _Response:
    def __init__(self, status: int, body: Any, content_type: str = "application/json"):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": content_type}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _Session:
    """按 (method, path) 返回预设响应，并记录全部调用。"""

    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def _dispatch(self, method: str, url: str, **kwargs):
        path = url.split("/api/v1", 1)[1] if "/api/v1" in url else url
        self.calls.append((method, path, kwargs))
        handler = self.routes.get((method, path))
        if handler is None:
            return _Response(404, "404 page not found", "text/plain")
        return handler(kwargs) if callable(handler) else handler

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._dispatch("PUT", url, **kwargs)


class _Backend:
    """JobExecutionBackend 的最小替身。"""

    def __init__(self):
        self.dispatched: list[dict] = []
        self.cancelled: list[str] = []
        self.busy: set[str] = set()
        self.device_manager = SimpleNamespace(get_job_info=lambda job_id: None)

    def dispatch(self, payload):
        self.dispatched.append(payload)

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return True

    def cancel_task(self, task_id):
        return ["j-a", "j-b"]

    def busy_device_action_keys(self):
        return set(self.busy)

    def resolve_error_decision(self, decision_id, decision):
        self.resolved = (decision_id, decision)
        return True


class _Adapter:
    devices_names = {"host_node": "/devices", "pump": "/devices"}
    _online_devices = {"/devices/host_node", "/devices/pump"}
    device_machine_names = {"host_node": "M1", "pump": "M1"}
    _action_value_mappings = {
        "host_node": {"test_latency": {}, "_execute_driver_command": {}},
        "pump": {"transfer": {"always_free": False}, "auto-status": {"always_free": True}},
    }

    def __init__(self):
        self.pongs: list[dict] = []

    def handle_pong_response(self, data):
        self.pongs.append(data)


def _drain(client: LegacyBackendWebSocketClient) -> list[dict]:
    out = []
    while not client._send_queue.empty():
        out.append(client._send_queue.get_nowait())
    return out


def _connected_client(backend: _Backend, adapter: _Adapter) -> LegacyBackendWebSocketClient:
    client = LegacyBackendWebSocketClient(
        "wss://legacy.example/api/v1/ws/schedule",
        execution_backend_getter=lambda: backend,
        adapter_getter=lambda: adapter,
    )
    client._connected = True
    return client


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    BackendSessionFactory.reset_client()
    probe.reset_probe_cache()
    monkeypatch.setattr(BasicConfig, "machine_name", "M1")
    monkeypatch.setattr(HTTPConfig, "backend_protocol", "")
    yield
    BackendSessionFactory.reset_client()
    probe.reset_probe_cache()


# ── 协议探测 ─────────────────────────────────────────────────────


def test_probe_detects_legacy_backend_by_http_routes(monkeypatch):
    session = _Session(
        {
            ("GET", "/edge/lab/info"): _Response(
                200, {"code": 0, "data": {"uuid": "lab", "name": "test_test"}}
            ),
        }
    )
    monkeypatch.setattr(HTTPConfig, "remote_addr", "https://legacy.example/api/v1")
    assert probe.detect_backend_protocol(session=session, force=True) == "legacy"


def test_edge_factory_never_switches_to_legacy_on_probe(monkeypatch):
    """Edge 的会话工厂固定 runtime.v1；旧云端兼容只能由 Backend 侧显式装配。"""

    monkeypatch.setattr(HTTPConfig, "remote_addr", "https://legacy.example/api/v1")
    monkeypatch.setattr(HTTPConfig, "backend_protocol", "legacy")
    assert probe.detect_backend_protocol(force=True) == "legacy"
    assert BackendSessionFactory.protocol() == "runtime.v1"
    assert not BackendSessionFactory.is_legacy()
    assert isinstance(BackendSessionFactory.create_client(), BackendWebSocketClient)
    legacy_client = BackendSessionFactory.create_legacy_client()
    assert isinstance(legacy_client, LegacyBackendWebSocketClient)
    # 显式 legacy 客户端沿用旧云端 ``+1`` 端口；runtime.v1 客户端同端口。
    monkeypatch.setattr(HTTPConfig, "remote_addr", "https://legacy.example:8002/api/v1")
    monkeypatch.setattr(HTTPConfig, "schedule_addr", "")
    assert BackendSessionFactory.create_legacy_client().websocket_url == (
        "wss://legacy.example:8003/api/v1/ws/schedule"
    )
    assert BackendSessionFactory.create_client().websocket_url == (
        "wss://legacy.example:8002/api/v1/ws/schedule"
    )


def test_probe_detects_runtime_v1_backend(monkeypatch):
    session = _Session(
        {
            ("GET", "/edge/commands/00000000-0000-0000-0000-000000000000"): _Response(
                404, {"detail": "command not found"}
            ),
        }
    )
    monkeypatch.setattr(HTTPConfig, "remote_addr", "http://backend:8081")
    assert probe.detect_backend_protocol(session=session, force=True) == "runtime.v1"
    assert isinstance(BackendSessionFactory.create_client(), BackendWebSocketClient)


def test_probe_honours_explicit_protocol(monkeypatch):
    monkeypatch.setattr(HTTPConfig, "remote_addr", "https://whatever.example")
    monkeypatch.setattr(HTTPConfig, "backend_protocol", "legacy")
    assert probe.detect_backend_protocol(force=True) == "legacy"


def test_probe_without_address_is_local_runtime(monkeypatch):
    monkeypatch.setattr(HTTPConfig, "remote_addr", "")
    assert probe.detect_backend_protocol(force=True) == "runtime.v1"


# ── 旧协议 WS ────────────────────────────────────────────────────


def test_job_start_is_dispatched_to_execution_backend_and_lock_reported():
    backend, adapter = _Backend(), _Adapter()
    client = _connected_client(backend, adapter)
    asyncio.run(
        client._process_message(
            "job_start",
            {
                "job_id": "job-1",
                "task_id": "task-1",
                "device_id": "pump",
                "action": "transfer",
                "action_type": "UniLabJsonCommand",
                "action_args": {"volume": 5},
                "server_info": {"send_timestamp": 12.5},
            },
        )
    )
    assert len(backend.dispatched) == 1
    payload = backend.dispatched[0]
    assert payload["job_id"] == "job-1"
    assert payload["device_id"] == "pump"
    assert payload["action"] == "transfer"
    assert payload["action_args"] == {"volume": 5}
    assert payload["server_info"] == {"send_timestamp": 12.5}
    assert payload["always_free"] is False
    assert "inventory_requirements" not in payload
    locks = [m for m in _drain(client) if m["action"] == "report_action_lock"]
    assert locks and locks[0]["data"]["locks"] == [
        {"device_id": "pump", "action_name": "transfer", "free": False}
    ]


def test_job_start_strips_workflow_device_selector_from_action_args():
    """旧后端工作流节点把 unilabos_device_id 放进 action_args；驱动签名里没有它。"""

    backend, adapter = _Backend(), _Adapter()
    client = _connected_client(backend, adapter)
    asyncio.run(
        client._process_message(
            "job_start",
            {
                "job_id": "job-2",
                "task_id": "task-2",
                "device_id": "host_node",
                "action": "test_latency",
                "action_type": "UniLabJsonCommand",
                "action_args": {"unilabos_device_id": "host_node"},
            },
        )
    )
    assert backend.dispatched[-1]["action_args"] == {}


def test_legacy_job_start_preserves_attempt_metadata_and_latency_special_field():
    """legacy 只负责字段兼容；attempt 链和 ping-pong 元数据交给微后端执行面。"""

    backend, adapter = _Backend(), _Adapter()
    client = _connected_client(backend, adapter)
    asyncio.run(
        client._process_message(
            "job_start",
            {
                "job_id": "job-retry",
                "task_id": "task-3",
                "workflow_id": "workflow-3",
                "node_id": "node-3",
                "node_run_uuid": "run-3",
                "attempt_no": "2",
                "retry_of_job_uuid": "job-first",
                "device_id": "pump",
                "action": "transfer",
                "action_args": {"volume": 5},
            },
        )
    )
    retry_payload = backend.dispatched[-1]
    assert retry_payload["workflow_id"] == "workflow-3"
    assert retry_payload["node_run_uuid"] == "run-3"
    assert retry_payload["attempt_no"] == 2
    assert retry_payload["retry_count"] == 1
    assert retry_payload["retry_of_job_uuid"] == "job-first"
    assert "server_info" not in retry_payload

    asyncio.run(
        client._process_message(
            "job_start",
            {
                "job_id": "job-latency",
                "task_id": "task-4",
                "node_id": "node-4",
                "device_id": "host_node",
                "action": "test_latency",
                "action_args": {},
            },
        )
    )
    latency_payload = backend.dispatched[-1]
    assert isinstance(latency_payload["server_info"]["send_timestamp"], float)
    assert latency_payload["server_info"]["send_timestamp"] > 0


def test_duplicate_job_start_replays_cached_terminal_result():
    backend, adapter = _Backend(), _Adapter()
    client = _connected_client(backend, adapter)
    start = {"job_id": "job-1", "task_id": "task-1", "device_id": "pump", "action": "transfer"}
    asyncio.run(client._process_message("job_start", start))
    item = QueueItem(
        task_type="job_call_back_status",
        device_id="pump",
        action_name="transfer",
        task_id="task-1",
        job_id="job-1",
        notebook_id="",
        device_action_key="/devices/pump/transfer",
    )
    client.publish_job_status({}, item, "success", {"suc": True, "return_value": {"ok": 1}})
    _drain(client)

    asyncio.run(client._process_message("job_start", start))
    assert len(backend.dispatched) == 1, "重复 job_start 不得再次派发"
    replayed = [m for m in _drain(client) if m["action"] == "job_status"]
    assert replayed and replayed[0]["data"]["status"] == "success"
    assert replayed[0]["data"]["return_info"]["return_value"] == {"ok": 1}


def test_terminal_job_status_frees_lock_and_canceled_maps_to_failed():
    client = _connected_client(_Backend(), _Adapter())
    item = QueueItem(
        task_type="job_call_back_status",
        device_id="pump",
        action_name="transfer",
        task_id="t",
        job_id="j",
        notebook_id="nb",
        device_action_key="/devices/pump/transfer",
    )
    client.publish_job_status({"pct": 50}, item, "running")
    client.publish_job_status({}, item, "canceled")
    messages = _drain(client)
    assert [m["action"] for m in messages] == [
        "job_status",
        "job_status",
        "report_action_lock",
    ]
    assert messages[0]["data"]["status"] == "running"
    assert messages[1]["data"]["status"] == "failed"
    assert messages[1]["data"]["return_info"]["suc"] is False
    assert messages[2]["data"]["locks"][0]["free"] is True


def test_host_node_ready_is_preceded_by_full_lock_snapshot():
    backend, adapter = _Backend(), _Adapter()
    backend.busy.add("/devices/pump/transfer")
    client = _connected_client(backend, adapter)
    client.publish_host_ready()
    messages = _drain(client)
    assert [m["action"] for m in messages] == ["report_action_lock", "host_node_ready"]
    locks = {
        (lock_item["device_id"], lock_item["action_name"]): lock_item["free"]
        for lock_item in messages[0]["data"]["locks"]
    }
    assert ("host_node", "_execute_driver_command") not in locks
    assert locks[("pump", "transfer")] is False
    assert locks[("pump", "auto-status")] is True
    ready = messages[1]["data"]
    assert ready["status"] == "ready" and ready["machine_name"] == "M1"
    assert {d["device_id"] for d in ready["devices"]} == {"host_node", "pump"}
    assert all(d["is_online"] for d in ready["devices"])


def test_pong_is_routed_to_adapter_and_ping_uses_legacy_fields():
    adapter = _Adapter()
    client = _connected_client(_Backend(), adapter)
    asyncio.run(
        client._process_message(
            "pong", {"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0}
        )
    )
    assert adapter.pongs == [{"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0}]
    assert client.send_ping("p2", 3.0) is True
    assert _drain(client) == [
        {"action": "ping", "data": {"ping_id": "p2", "client_timestamp": 3.0}}
    ]


def test_legacy_heartbeat_aliases_are_normalized_at_the_adaptor_boundary():
    """旧云端的 ``id/timestamp/server_time`` 只在 legacy 适配器内转换为 canonical 字段。"""

    adapter = _Adapter()
    client = _connected_client(_Backend(), adapter)
    asyncio.run(
        client._process_message(
            "pong", {"id": "p1", "timestamp": 1.0, "server_time": 2.0}
        )
    )
    assert adapter.pongs == [{"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0}]
    asyncio.run(client._process_message("ping", {"id": "b1", "timestamp": 4.0}))
    reply = _drain(client)[-1]
    assert reply["action"] == "pong"
    assert reply["data"]["ping_id"] == "b1" and reply["data"]["client_timestamp"] == 4.0
    assert isinstance(reply["data"]["server_timestamp"], float)
    # 未连接时同样快速失败。
    client._connected = False
    assert client.send_ping("p3", 5.0) is False


def test_cancel_and_query_action_state():
    backend = _Backend()
    client = _connected_client(backend, _Adapter())
    asyncio.run(client._process_message("cancel_action", {"job_id": "j1"}))
    asyncio.run(client._process_message("cancel_task", {"task_id": "t1"}))
    assert backend.cancelled == ["j1"]
    asyncio.run(
        client._process_message(
            "query_action_state",
            {"device_id": "pump", "action_name": "transfer", "task_id": "t", "job_id": "j"},
        )
    )
    reply = _drain(client)[-1]
    assert reply["action"] == "report_action_state"
    assert reply["data"]["free"] is True and reply["data"]["need_more"] == 1


def test_running_jobs_are_kept_alive_with_need_more():
    """旧后端 20 s 存活期限只能靠 report_action_state(need_more) 续期。"""
    backend = _Backend()
    running = SimpleNamespace(
        job_id="j-run", task_id="t-run", device_id="pump", action_name="transfer", notebook_id=""
    )
    local_only = SimpleNamespace(
        job_id="j-local", task_id="t-local", device_id="pump", action_name="transfer", notebook_id=""
    )
    backend.device_manager = SimpleNamespace(
        get_job_info=lambda job_id: None, get_active_jobs=lambda: [running, local_only]
    )
    client = _connected_client(backend, _Adapter())
    asyncio.run(
        client._process_message(
            "job_start",
            {"job_id": "j-run", "task_id": "t-run", "device_id": "pump", "action": "transfer"},
        )
    )
    _drain(client)

    assert client.report_running_jobs() == 1
    (msg,) = _drain(client)
    assert msg["action"] == "report_action_state"
    assert msg["data"]["type"] == "job_call_back_status"
    assert msg["data"]["job_id"] == "j-run" and msg["data"]["task_id"] == "t-run"
    assert msg["data"]["free"] is False and msg["data"]["need_more"] == 11

    client._connected = False
    assert client.report_running_jobs() == 0


def test_failure_decision_is_released_as_abort_on_behalf_of_legacy_backend():
    """旧后端没有决策闸门：适配层代 Backend 立即 abort，失败才能变成 job_status failed。"""

    backend = _Backend()
    client = _connected_client(backend, _Adapter())
    report = {
        "decision_id": "d-1",
        "job_id": "j-1",
        "device_id": "pump",
        "options": [{"action": "retry"}, {"action": "abort"}, {"action": "operator_intervention"}],
    }
    assert client.publish_job_error_decision_required(report) is True
    decision_id, decision = backend.resolved
    assert decision_id == "d-1"
    assert decision["action"] == "abort" and decision["scheduler_updated"] is True


def test_failure_decision_uses_real_execution_backend_release_path(tmp_path, monkeypatch):
    """端到端：JobExecutionBackend 的失败挂起 → legacy 桥接 abort → 终态 failed 释放。"""

    from unilabos.server.backend.execution import JobExecutionBackend

    monkeypatch.setattr(BasicConfig, "machine_name", "M1")
    adapter = _Adapter()
    finished: list[tuple] = []
    backend = JobExecutionBackend(host_node_getter=lambda: adapter)
    client = _connected_client(backend, adapter)
    backend.result_bridges.append(client)
    backend.add_job_finished_listener(lambda job_id, ok, ret, suc: finished.append((job_id, ok, suc)))
    backend.start()
    try:
        backend.dispatch(
            {"job_id": "j-fail", "task_id": "t", "device_id": "pump", "action": "transfer", "action_args": {}}
        )
        assert backend.wait_idle(5.0)
        item = QueueItem(
            task_type="job_call_back_status",
            device_id="pump",
            action_name="transfer",
            task_id="t",
            job_id="j-fail",
            notebook_id="",
            device_action_key="/devices/pump/transfer",
        )
        backend.publish_job_status(
            {}, item, "failed", {"suc": False, "error": "boom", "return_value": {}}
        )
        assert backend.wait_idle(5.0)
    finally:
        backend.stop()
    assert backend.list_error_decisions() == [], "失败不得停留在待决策状态"
    statuses = [m["data"]["status"] for m in _drain(client) if m["action"] == "job_status"]
    assert statuses == ["failed"]
    assert finished and finished[0][0] == "j-fail" and finished[0][1] is False


def test_unknown_or_malformed_messages_never_raise():
    client = _connected_client(_Backend(), _Adapter())
    asyncio.run(client._handle_raw_message("not json"))
    asyncio.run(client._handle_raw_message(json.dumps({"action": "job_start", "data": {}})))
    asyncio.run(client._handle_raw_message(json.dumps({"action": "mystery", "data": {}})))
    assert client._connected


def test_legacy_client_is_registered_as_execution_result_bridge(tmp_path, monkeypatch):
    """微后端释放的 job 结果必须能回到旧协议客户端，否则旧后端永远等不到 job_status。"""

    from unilabos.server.backend import composition
    from unilabos.server.database import ServerDatabasePaths

    composition.reset_for_test()
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    paths = ServerDatabasePaths.resolve(str(tmp_path), {})
    client = _connected_client(_Backend(), _Adapter())
    try:
        backend = composition.setup_execution_backend(
            control_client=client, host_node_getter=lambda: None, database_paths=paths
        )
        assert client in backend.result_bridges
        # runtime.v1 客户端只做通知回调，不进 result bridge
        assert not getattr(BackendWebSocketClient, "mirrors_job_results", False)
    finally:
        composition.reset_for_test()


# ── runtime.v1 WS 修复 ─────────────────────────────────────────────


def test_runtime_v1_ping_uses_pong_compatible_fields():
    client = BackendWebSocketClient("ws://backend/api/v1/ws/schedule", coordinator_getter=lambda: None)
    client._connected = True
    assert client.send_ping("p1", 5.0) is True
    assert client._send_queue.get_nowait() == {
        "action": "ping",
        "data": {"ping_id": "p1", "client_timestamp": 5.0},
    }


def test_runtime_v1_send_ping_fails_fast_when_not_connected():
    """未连接时 send_ping 立即返回 False，test_latency 不会盲等 10 秒。"""

    client = BackendWebSocketClient("ws://backend/api/v1/ws/schedule", coordinator_getter=lambda: None)
    assert client.send_ping("p1", 5.0) is False
    assert client._send_queue.empty()
    client._connected = True
    assert client.send_ping("", 5.0) is False
    assert client._send_queue.empty()


def test_runtime_v1_heartbeat_is_sent_before_queued_business_notices():
    client = BackendWebSocketClient("ws://backend/api/v1/ws/schedule", coordinator_getter=lambda: None)
    client._connected = True
    client._queue_message({"action": "edge_change", "data": {"event_sequence": 1}})
    client._queue_message({"action": "edge_change", "data": {"event_sequence": 2}})
    client.send_ping("p1", 5.0)
    drained = []
    while not client._send_queue.empty():
        drained.append(client._send_queue.get_nowait())
    assert [m["action"] for m in drained] == ["ping", "edge_change", "edge_change"]
    # 同优先级消息保持 FIFO。
    assert [m["data"]["event_sequence"] for m in drained[1:]] == [1, 2]


@pytest.mark.parametrize("queue_type", [RuntimePriorityMessageQueue, LegacyPriorityMessageQueue])
def test_heartbeat_queue_bypasses_business_capacity(queue_type):
    """业务通知填满有限队列时，控制面 ping 仍必须能入队并优先发送。"""

    queue = queue_type(maxsize=1)
    queue.put_nowait({"action": "edge_change", "data": {"event_sequence": 1}})
    queue.put_nowait(
        {
            "action": "ping",
            "data": {"ping_id": "p1", "client_timestamp": 1.0},
        }
    )
    assert queue.get_nowait()["action"] == "ping"
    assert queue.get_nowait()["action"] == "edge_change"


def test_runtime_v1_heartbeat_bypasses_business_queue(monkeypatch):
    """pong 在接收协程内直接投递给适配器；业务通知只入队、由 worker 串行消费。"""

    from unilabos.server.backend.legacy_adaptor import websocket as ws_module

    adapter = _Adapter()
    monkeypatch.setattr(ws_module, "get_execution_adapter", lambda _index: adapter)
    notices: list[dict] = []
    coordinator = SimpleNamespace(handle_backend_notice=notices.append)
    client = BackendWebSocketClient(
        "ws://backend/api/v1/ws/schedule", coordinator_getter=lambda: coordinator
    )

    async def scenario():
        business_queue: asyncio.Queue = asyncio.Queue()
        client._business_queue = business_queue
        await client._handle_raw_message(
            json.dumps({"action": "backend_change", "data": {"command_uuid": "c1"}})
        )
        await client._handle_raw_message(
            json.dumps(
                {
                    "action": "pong",
                    "data": {"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0},
                }
            )
        )
        # 业务通知还在队列里等 worker，pong 已经送达适配器。
        assert notices == []
        assert business_queue.qsize() == 1
        assert adapter.pongs == [
            {"ping_id": "p1", "client_timestamp": 1.0, "server_timestamp": 2.0}
        ]
        worker = asyncio.create_task(client._business_message_handler(business_queue))
        await business_queue.join()
        worker.cancel()
        assert notices == [{"command_uuid": "c1"}]

    asyncio.run(scenario())


def test_runtime_v1_answers_backend_ping_and_drops_malformed_pong(monkeypatch):
    from unilabos.server.backend.legacy_adaptor import websocket as ws_module

    adapter = _Adapter()
    monkeypatch.setattr(ws_module, "get_execution_adapter", lambda _index: adapter)
    client = BackendWebSocketClient("ws://backend/api/v1/ws/schedule", coordinator_getter=lambda: None)
    client._connected = True
    asyncio.run(
        client._handle_raw_message(
            json.dumps({"action": "ping", "data": {"ping_id": "b1", "client_timestamp": 7.0}})
        )
    )
    reply = client._send_queue.get_nowait()
    assert reply["action"] == "pong"
    assert reply["data"]["ping_id"] == "b1" and reply["data"]["client_timestamp"] == 7.0
    assert isinstance(reply["data"]["server_timestamp"], float)
    # 缺 server_timestamp 的 pong 不进入适配器，也不抛出到接收循环。
    asyncio.run(
        client._handle_raw_message(
            json.dumps({"action": "pong", "data": {"ping_id": "p1", "client_timestamp": 1.0}})
        )
    )
    assert adapter.pongs == []


def test_runtime_v1_poison_notice_does_not_propagate():
    class _Coordinator:
        def handle_backend_notice(self, value):
            raise ValueError("HTTP command identity does not match WS notice")

    client = BackendWebSocketClient(
        "ws://backend/api/v1/ws/schedule", coordinator_getter=lambda: _Coordinator()
    )
    asyncio.run(
        client._handle_raw_message(json.dumps({"action": "backend_change", "data": {"x": 1}}))
    )


# ── 旧 HTTP 数据面 ──────────────────────────────────────────────────


def test_http_client_registry_upload_is_gzip_and_decodes_envelope(monkeypatch):
    monkeypatch.setattr(BasicConfig, "ak", "ak")
    monkeypatch.setattr(BasicConfig, "sk", "sk")
    session = _Session(
        {("POST", "/lab/resource"): _Response(200, {"code": 0, "data": {"skipped": True}})}
    )
    client = LegacyBackendHTTPClient("https://legacy.example/api/v1", session=session)
    result = client.upload_registry([{"id": "dev", "class": {}}])
    assert result == {"skipped": True}
    method, path, kwargs = session.calls[0]
    assert (method, path) == ("POST", "/lab/resource")
    assert kwargs["headers"]["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(kwargs["data"])) == {"resources": [{"id": "dev", "class": {}}]}
    assert session.headers["Authorization"].startswith("Lab ")


def test_http_client_raises_on_business_error_and_text_404():
    session = _Session(
        {
            ("GET", "/edge/lab/info"): _Response(
                200, {"code": 5009, "error": {"msg": "login verification format error"}}
            )
        }
    )
    client = LegacyBackendHTTPClient("https://legacy.example", session=session)
    with pytest.raises(LegacyBackendHTTPError, match="5009"):
        client.lab_info()
    with pytest.raises(LegacyBackendHTTPError, match="non-JSON HTTP 404"):
        client.download_lab_graph()


def test_material_tree_upload_uses_post_then_put_and_returns_mapping():
    session = _Session(
        {
            ("POST", "/edge/material"): _Response(
                200, {"code": 0, "data": [{"uuid": "u1", "cloud_uuid": "u1"}]}
            ),
            ("PUT", "/edge/material"): _Response(200, {"code": 0, "data": []}),
        }
    )
    client = LegacyBackendHTTPClient("https://legacy.example", session=session)
    assert client.upload_material_tree([{"uuid": "u1"}], first_add=True) == {"u1": "u1"}
    assert client.upload_material_tree([{"uuid": "u1"}], first_add=False) == {}
    assert [c[0] for c in session.calls] == ["POST", "PUT"]
    assert json.loads(session.calls[0][2]["data"]) == {"nodes": [{"uuid": "u1"}], "mount_uuid": ""}


# ── 物料镜像 ─────────────────────────────────────────────────────


def test_legacy_material_node_strips_new_contract_fields_and_completes_pose():
    node = legacy_material_node(
        {
            "id": "c1",
            "uuid": "u1",
            "name": "c1",
            "type": "container",
            "class": "container",
            "template_name": "container",
            "display_name": "C1",
            "meta_data": {"k": 1},
            "sites": [],
            "sites_initialized": True,
            "substances": [["water", 1.0, "ul"]],
            "parent_uuid": None,
            "description": None,
            "pose": {"position": {"x": 1, "y": 2, "z": 3}},
            "config": {},
            "data": {},
            "extra": {},
        }
    )
    for removed in ("template_name", "display_name", "meta_data", "sites", "sites_initialized", "substances"):
        assert removed not in node
    assert node["parent_uuid"] == "" and node["description"] == ""
    assert node["pose"]["position3d"] == {"x": 1, "y": 2, "z": 3}
    assert node["pose"]["scale"] == {"x": 1.0, "y": 1.0, "z": 1.0}
    assert legacy_material_edge({"source_uuid": "a", "target_uuid": "b", "sourceHandle": "1"}) == {
        "source_uuid": "a",
        "target_uuid": "b",
        "source_handle": "1",
        "target_handle": "",
        "type": "",
    }
    assert legacy_material_edge({"source": "a", "target": "b"}) is None


class _Gateway:
    """只提供镜像所需接口的物料网关替身。"""

    def __init__(self):
        self.ledger: list[Any] = []
        self.trees = {"root-1": ["root-1", "child-1"]}

    def list_materials(self, *, roots_only=False):
        return [SimpleNamespace(material=SimpleNamespace(material_uuid=r)) for r in self.trees]

    def get_material(self, material_uuid):
        for root, members in self.trees.items():
            if material_uuid in members:
                parent = None if material_uuid == root else root
                return SimpleNamespace(material=SimpleNamespace(parent_material_uuid=parent))
        raise KeyError(material_uuid)

    def changes(self, *, after_sequence=0, limit=100):
        return [row for row in self.ledger if row.sequence > after_sequence][:limit]


def test_material_mirror_incremental_sync_groups_by_root_and_discards(monkeypatch):
    session = _Session(
        {
            ("POST", "/edge/material"): _Response(200, {"code": 0, "data": []}),
            ("PUT", "/edge/material"): _Response(200, {"code": 0, "data": []}),
            ("POST", "/edge/material/bench/discard"): _Response(200, {"code": 0}),
        }
    )
    client = LegacyBackendHTTPClient("https://legacy.example", session=session)
    gateway = _Gateway()
    mirror = LegacyMaterialMirror(client=client, gateway=gateway)
    monkeypatch.setattr(
        mirror,
        "_dump_root",
        lambda root: [{"uuid": m, "parent_uuid": "" if m == root else root} for m in gateway.trees[root]],
    )
    mirror.upload_full()
    assert session.calls[0][:2] == ("POST", "/edge/material")

    gateway.ledger = [
        SimpleNamespace(sequence=1, aggregate_type="material", aggregate_uuid="child-1", operation="update_data"),
        SimpleNamespace(sequence=2, aggregate_type="site", aggregate_uuid="site-x", operation="occupy"),
        SimpleNamespace(sequence=3, aggregate_type="material", aggregate_uuid="gone", operation="delete"),
    ]
    assert mirror.sync_once() == 3
    later = [c[:2] for c in session.calls[1:]]
    assert ("PUT", "/edge/material") in later
    assert ("POST", "/edge/material/bench/discard") in later
    assert later.count(("PUT", "/edge/material")) == 1, "同一根树的多条账本只重发一次"
    assert mirror.sync_once() == 0


def test_material_mirror_discards_tips_retired_by_device_snapshot(monkeypatch):
    """快照退役的枪头（账本 retire）与显式 delete 一样上报旧后端废弃，不按根树重发。"""

    session = _Session({("POST", "/edge/material/bench/discard"): _Response(200, {"code": 0})})
    client = LegacyBackendHTTPClient("https://legacy.example", session=session)
    gateway = _Gateway()
    mirror = LegacyMaterialMirror(client=client, gateway=gateway)
    gateway.ledger = [
        SimpleNamespace(sequence=1, aggregate_type="material", aggregate_uuid="tip-1", operation="retire"),
    ]
    assert mirror.sync_once() == 1
    assert [c[:2] for c in session.calls] == [("POST", "/edge/material/bench/discard")]


def test_material_mirror_backfills_authority_templates_and_uses_template_as_class(monkeypatch):
    """运行期在权威登记的模板不在 Edge Registry 里：先补报模板，节点 class 取模板名。"""

    captured: dict[str, Any] = {"registry": [], "material": []}

    def _registry(kwargs):
        captured["registry"].append(json.loads(gzip.decompress(kwargs["data"])))
        return _Response(200, {"code": 0, "data": {}})

    def _material(kwargs):
        captured["material"].append(json.loads(kwargs["data"]))
        return _Response(200, {"code": 0, "data": []})

    session = _Session(
        {
            ("POST", "/lab/resource"): _registry,
            ("POST", "/edge/material"): _material,
            ("PUT", "/edge/material"): _material,
        }
    )
    client = LegacyBackendHTTPClient("https://legacy.example", session=session)
    gateway = _Gateway()
    template_queries: list[dict[str, Any]] = []

    def _list_templates(*, name=None, include_definition=False):
        template_queries.append({"name": name, "include_definition": include_definition})
        if name != "virtual_heating_sample":
            return []
        return [
            SimpleNamespace(
                name="virtual_heating_sample",
                display_name="加热样品",
                resource_type="resource",
                module_name="",
                template_version="1",
                category=["heating_sample"],
                handles=[],
                definition={},
            )
        ]

    gateway.list_templates = _list_templates
    mirror = LegacyMaterialMirror(client=client, gateway=gateway, known_templates={"virtual_heating_platform"})
    monkeypatch.setattr(
        mirror,
        "_dump_root",
        lambda root: [
            legacy_material_node({"uuid": "root-1", "id": "heater", "type": "device", "class": "virtual_heating_platform", "template_name": "virtual_heating_platform", "config": {}, "data": {}, "extra": {}}),
            legacy_material_node({"uuid": "child-1", "id": "sample", "type": "resource", "class": "Resource", "template_name": "virtual_heating_sample", "parent_uuid": "root-1", "config": {}, "data": {}, "extra": {}}),
        ],
    )
    mirror.upload_full()
    # 1) 模板补报只包含缺失的那一个，且用旧字段名；旧后端要 config_info 正文，
    #    所以按缺失 name 逐个带 definition 取，不拉整个注册表
    assert template_queries == [{"name": "virtual_heating_sample", "include_definition": True}]
    assert len(captured["registry"]) == 1
    entry = captured["registry"][0]["resources"][0]
    assert entry["id"] == "virtual_heating_sample" and entry["displayname"] == "加热样品"
    assert entry["registry_type"] == "resource" and entry["class"]["type"] == "pylabrobot"
    # 2) 节点 class 以模板名为准（旧后端按 class 关联模板）
    nodes = captured["material"][0]["nodes"]
    assert {n["id"]: n["class"] for n in nodes} == {"heater": "virtual_heating_platform", "sample": "virtual_heating_sample"}
    # 3) 再次上报同一模板不重复补报
    mirror.upload_full()
    assert len(captured["registry"]) == 1


def test_material_mirror_skips_rejected_root_without_looping(monkeypatch):
    session = _Session(
        {
            ("POST", "/edge/material"): _Response(200, {"code": 0, "data": []}),
            ("PUT", "/edge/material"): _Response(
                200, {"code": 22020, "error": {"msg": "material resource template not exist"}}
            ),
        }
    )
    client = LegacyBackendHTTPClient("https://legacy.example", session=session)
    gateway = _Gateway()
    mirror = LegacyMaterialMirror(client=client, gateway=gateway)
    monkeypatch.setattr(mirror, "_dump_root", lambda root: [{"uuid": root, "parent_uuid": "", "class": ""}])
    mirror.upload_full()
    gateway.ledger = [
        SimpleNamespace(sequence=1, aggregate_type="material", aggregate_uuid="child-1", operation="update_data"),
    ]
    assert mirror.sync_once() == 1
    assert mirror.sync_once() == 0, "被拒绝的账本不得每轮重放"


def test_registry_snapshot_uses_legacy_field_names(monkeypatch):
    captured: list[Any] = []

    def _capture(kwargs):
        captured.append(json.loads(gzip.decompress(kwargs["data"])))
        return _Response(200, {"code": 0, "data": {}})

    session = _Session({("POST", "/lab/resource"): _capture})
    client = LegacyBackendHTTPClient("https://legacy.example", session=session)
    registry = SimpleNamespace(
        obtain_registry_device_info=lambda: [
            {
                "id": "pump",
                "display_name": "泵",
                "available_sites": [],
                "class": {
                    "module": "x:Pump",
                    "type": "python",
                    "supported_backends": ["ros2"],
                    "status_policies": {},
                    "action_value_mappings": {
                        "transfer": {"type": "UniLabJsonCommand", "error_policy": {}, "display_name": "转移"}
                    },
                },
            }
        ],
        obtain_registry_resource_info=lambda: [{"id": "plate", "display_name": "板"}],
    )
    report = upload_registry_snapshot(registry, client)
    assert report.device_count == 1 and report.resource_count == 1
    device = captured[0]["resources"][0]
    assert device["displayname"] == "泵" and "display_name" not in device
    assert "supported_backends" not in device["class"] and "status_policies" not in device["class"]
    action = device["class"]["action_value_mappings"]["transfer"]
    assert action["displayname"] == "转移" and "error_policy" not in action
    assert captured[1]["resources"][0]["displayname"] == "板"


# ── 启动期接线 ───────────────────────────────────────────────────


def test_start_legacy_uplink_reports_registry_then_mirrors_materials(monkeypatch, capsys):
    """开机上联顺序：注册表上报 → 全量物料镜像（失败不阻断）→ 增量线程启动。"""
    from unilabos.server.backend.legacy_adaptor.legacy import startup

    events: list[str] = []

    class _Mirror:
        def __init__(self, *, client, gateway, known_templates):
            self.client = client
            self.gateway = gateway
            self.known_templates = known_templates

        def upload_full(self, links=()):
            events.append(f"upload_full:{len(links)}")
            raise RuntimeError("cloud down")

        def start(self):
            events.append("start")

    report = SimpleNamespace(
        device_count=2, resource_count=3, device_skipped=False, resource_skipped=True,
        template_ids=frozenset({"pump", "plate"}),
    )
    monkeypatch.setattr(HTTPConfig, "remote_addr", "https://legacy.example/api/v1")
    monkeypatch.setattr(startup, "LegacyBackendHTTPClient", lambda: "http-client")
    monkeypatch.setattr(
        startup,
        "upload_registry_snapshot",
        lambda registry, client: events.append(f"registry:{client}") or report,
    )
    monkeypatch.setattr(startup, "LegacyMaterialMirror", _Mirror)

    mirror = startup.start_legacy_uplink(
        "registry", materials_gateway="gateway", resource_links=[{"a": 1}, {"b": 2}]
    )

    assert events == ["registry:http-client", "upload_full:2", "start"]
    assert mirror.known_templates == {"pump", "plate"} and mirror.gateway == "gateway"
    out = capsys.readouterr().out
    assert "显式接入旧协议 Backend" in out
    assert "设备 2 资源 3（未变化）" in out
    assert "物料全量镜像到旧 Backend 失败" in out
