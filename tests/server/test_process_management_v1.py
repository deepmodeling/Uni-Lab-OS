"""V1 进程管理的四个路由在 dev 进程管理上的合同与行为。

对应 V1 ``unilabos/workspace_host/host.py`` 的 ``/v1/snapshot``、``/v1/operations``、
``/v1/operations/{operation_id}``、``/v1/logs/{component}?maxBytes=``，以及 V1
``tests/workspace_host/test_process_management.py`` / ``test_scheduler_drain_lifecycle.py`` 中的幂等、
FIFO、排空后停止等用例。进程用替身（Host 子进程看护者、控制面、Host 访问口），重启走真实
``RestartCoordinator`` 路径，排空走真实 ``BackendScheduler`` + ``SchedulerLifecycle``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unilabos.server.backend.restart as restart_module
from unilabos.config.config import BasicConfig
from unilabos.config.control_token import TOKEN_HEADER
from unilabos.protocol.runtime.process_management import SCHEMA_VERSION, ProcessManagementError
from unilabos.server.api.app import ControlTokenMiddleware
from unilabos.server.api.process_management import install_process_management_api
from unilabos.server.backend.restart import RestartCoordinator
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.server.services.process_management import (
    HostGateway,
    ProcessManagementContext,
    ProcessManagementService,
    request_hash,
)

TOKEN = "process-management-test-token"


class FakeEdge:
    """调度权威侧的控制面视图：Host 是否在线、当前 Host 进程身份。"""

    def __init__(self) -> None:
        self.connected = True
        self.edge_process = "host-process-1"
        self.authority_epoch = "backend:test-epoch"

    def active_job_ids(self) -> list[str]:
        return []


class FakeSupervisor:
    """HostChildSupervisor 替身：start / stop 改变子进程与控制面连接。"""

    def __init__(self, edge: FakeEdge) -> None:
        self.edge = edge
        self.pid = 4321
        self.supervising = True
        self.stop_requested = False
        self.calls: list[str] = []

    def state(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "alive": self.pid is not None,
            "supervising": self.supervising,
            "stop_requested": self.stop_requested,
            "restart_count": 0,
            "crash_count": 0,
            "last_exit_code": None,
        }

    def start(self) -> None:
        self.calls.append("start")
        self.pid, self.supervising, self.stop_requested = 5432, True, False
        self.edge.connected = True
        self.edge.edge_process = f"host-process-{self.pid}"

    def stop(self) -> None:
        self.calls.append("stop")
        self.pid, self.supervising, self.stop_requested = None, False, True
        self.edge.connected = False


class FakeGateway(HostGateway):
    """Host 访问口替身：受管进程、驱动包操作台账与日志来源。"""

    def __init__(self) -> None:
        self.processes = [{
            "id": "proc-1", "name": "移液站", "status": "running", "pid": 777, "started_at_ms": 1000,
            "log_path": "/data/device_processes/proc-1/process.log", "device_ids": ["pipette"],
            "restart_policy": "on-failure", "restart_count": 0, "auto_start": True,
        }]
        self.actions: list[tuple[str, str]] = []
        self.package_states = ["running", "succeeded"]
        self.logs: dict[str, Any] = {}
        self.offline = False

    def remote(self) -> bool:
        return True

    def _check(self) -> None:
        if self.offline:
            raise ProcessManagementError("host_unreachable", "Host 未接入控制面或未在预算内响应")

    def list_processes(self, *, timeout: float = 5.0) -> list[dict[str, Any]]:
        self._check()
        return [dict(item) for item in self.processes]

    def process_action(self, process_id: str, action: str) -> dict[str, Any]:
        self._check()
        self.actions.append((process_id, action))
        if process_id != "proc-1":
            raise ProcessManagementError("host_resource_not_found", f"device process not found: {process_id}")
        return {**self.processes[0], "status": "running" if action != "stop" else "stopped"}

    def install_package(self, spec: str, enable: bool, upgrade: bool, name: str) -> dict[str, Any]:
        self.actions.append(("install", spec))
        return {"operation_id": "pkg-op-1", "kind": "install", "spec": spec, "status": "running"}

    def package_operation(self, operation_id: str) -> dict[str, Any]:
        status = self.package_states.pop(0) if self.package_states else "succeeded"
        record = {"operation_id": operation_id, "status": status, "log": "done"}
        if status == "failed":
            record["error"] = "pip 安装失败"
        return record

    def read_log(self, source_id: str, *, limit: int = 1000) -> dict[str, Any]:
        self._check()
        if source_id not in self.logs:
            raise ProcessManagementError("host_resource_not_found", "日志来源不存在，请刷新进程列表")
        return self.logs[source_id]


def _batch(source_id: str, lines: list[str], path: str) -> dict[str, Any]:
    offset, items = 0, []
    for text in lines:
        items.append({"offset": offset, "text": text})
        offset += len(text.encode()) + 1
    return {"source_id": source_id, "stream_id": "a" * 24, "cursor": f"{'a' * 24}:{offset}", "lines": items,
            "path": path}


class Stack:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(restart_module, "_POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(BasicConfig, "control_token", TOKEN)
        monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
        self.edge = FakeEdge()
        # 重启协调器经模块级入口判断「本进程是带控制面的调度权威」
        monkeypatch.setattr("unilabos.server.backend.edge_control.get_edge_control_service", lambda: self.edge)
        self.workflow = WorkflowService(":memory:")
        self.scheduler = BackendScheduler(self.workflow, MagicMock())
        self.active: list[str] = []
        self.backend = SimpleNamespace(
            device_manager=SimpleNamespace(get_active_jobs=lambda: [SimpleNamespace(job_id=j) for j in self.active]),
            wait_idle=lambda timeout=5.0: True,
        )
        self.coordinator = RestartCoordinator(lambda: self.backend, lambda: self.scheduler)
        self.supervisor = FakeSupervisor(self.edge)
        self.gateway = FakeGateway()
        self.materials = None
        self.context = ProcessManagementContext(
            management_port=8002, supervisor=self.supervisor, database_root=str(tmp_path), edge_database_root=str(tmp_path / "edge"),
        )
        self.app = FastAPI()
        self.app.add_middleware(ControlTokenMiddleware)
        self.service = self.install(self.app)
        self.client = TestClient(self.app)

    def install(self, app: FastAPI) -> ProcessManagementService:
        return install_process_management_api(
            app, self.workflow, context=self.context, gateway=self.gateway,
            get_scheduler=lambda: self.scheduler, get_execution_backend=lambda: self.backend,
            get_edge_control=lambda: self.edge, get_restart_coordinator=lambda: self.coordinator,
            get_materials=lambda: self.materials, poll_interval=0.01,
        )

    def submit(self, command: str, parameters: dict | None = None, *, operation_id: str | None = None,
               expected_revision: int | None = None, token: str | None = TOKEN):
        body: dict[str, Any] = {"operationId": operation_id or str(uuid4()), "command": command,
                                "parameters": parameters or {}}
        if expected_revision is not None:
            body["expectedRevision"] = expected_revision
        headers = {TOKEN_HEADER: token} if token else {}
        return self.client.post("/v1/operations", json=body, headers=headers)

    def wait(self, operation_id: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            document = self.client.get(f"/v1/operations/{operation_id}").json()
            if document.get("phase") in {"succeeded", "failed"}:
                return document
            time.sleep(0.02)
        raise AssertionError(f"操作未结束：{document}")

    def execute(self, command: str, parameters: dict | None = None, **options) -> dict[str, Any]:
        response = self.submit(command, parameters, **options)
        assert response.status_code == 202, response.text
        return self.wait(response.json()["operationId"])

    def close(self) -> None:
        self.service.close()
        self.coordinator.cancel()
        self.scheduler.stop()
        self.workflow.close()


@pytest.fixture
def stack(monkeypatch, tmp_path):
    value = Stack(monkeypatch, tmp_path)
    yield value
    value.close()


# ── 合同：/v1/snapshot ─────────────────────────────────────────────


def test_snapshot_reports_the_dev_process_tree(stack: Stack) -> None:
    response = stack.client.get("/v1/snapshot")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    snapshot = response.json()
    # 裸 JSON，不包信封
    assert "code" not in snapshot
    assert snapshot["schemaVersion"] == SCHEMA_VERSION
    assert snapshot["revision"] == snapshot["eventCursor"] >= 1
    assert snapshot["host"]["phase"] == "ready" and snapshot["host"]["endpoint"] == "http://127.0.0.1:8002"
    assert snapshot["configuration"]["topology"] == "authority+host-child"
    assert "os.restart" in snapshot["configuration"]["commands"]
    components = snapshot["components"]
    assert set(components) == {"backend", "edge", "plc", "renderer", "managed:proc-1"}
    backend, edge, managed = components["backend"], components["edge"], components["managed:proc-1"]
    assert backend["phase"] == "ready" and backend["generation"] == "backend:test-epoch"
    assert backend["metadata"]["scheduler"]["phase"] == "running"
    assert backend["capabilities"] == ["authoring", "inventory", "workflow-run", "station-scheduler"]
    assert (edge["phase"], edge["pid"], edge["generation"]) == ("ready", 4321, "host-process-1")
    assert edge["metadata"]["supervised"] is True and edge["capabilities"] == ["device-control"]
    assert (managed["phase"], managed["pid"], managed["logPath"]) == (
        "ready", 777, "/data/device_processes/proc-1/process.log")
    assert managed["metadata"]["deviceIds"] == ["pipette"]
    assert components["plc"]["phase"] == "idle" and components["plc"]["metadata"] == {"supported": False}
    for component in components.values():
        assert {"name", "phase", "pid", "address", "generation", "logPath", "diagnostic", "capabilities"} <= set(component)
    # 状态不变时修订号不变
    assert stack.client.get("/v1/snapshot").json()["revision"] == snapshot["revision"]


def test_snapshot_revision_follows_process_state(stack: Stack) -> None:
    first = stack.client.get("/v1/snapshot").json()
    stack.gateway.processes[0].update(status="crashed", pid=None, last_error="exit 1")
    crashed = stack.client.get("/v1/snapshot").json()
    assert crashed["revision"] == first["revision"] + 1
    assert crashed["components"]["managed:proc-1"]["phase"] == "failed"
    assert crashed["components"]["managed:proc-1"]["diagnostic"] == "exit 1"
    # Host 掉线：edge 进入 starting（子进程在、控制面断开），受管进程不可见
    stack.edge.connected = False
    offline = stack.client.get("/v1/snapshot").json()
    assert offline["revision"] == crashed["revision"] + 1
    assert offline["components"]["edge"]["phase"] == "starting"
    assert "managed:proc-1" not in offline["components"]


# ── 合同：/v1/operations ───────────────────────────────────────────


def test_operation_is_recoverable_idempotent_and_conflicts(stack: Stack) -> None:
    """V1 test_operation_is_recoverable_idempotent_and_audited 的 dev 版本。"""

    submitted = stack.submit("scheduler.drain", operation_id="drain-once")
    assert submitted.status_code == 202
    document = submitted.json()
    assert document["schemaVersion"] == SCHEMA_VERSION
    assert document["operationId"] == "drain-once" and document["command"] == "scheduler.drain"
    assert document["requestHash"] == request_hash("drain-once", "scheduler.drain", {}, None)
    assert document["phase"] in {"pending", "running", "succeeded"}
    duplicate = stack.submit("scheduler.drain", operation_id="drain-once")
    assert duplicate.status_code == 202 and duplicate.json()["requestHash"] == document["requestHash"]
    completed = stack.wait("drain-once")
    assert completed["phase"] == "succeeded" and completed["error"] is None
    assert completed["result"]["phase"] == "drained" and completed["result"]["requested"] is True
    assert completed["startedAt"] and completed["finishedAt"]
    # 重放不重复执行：仍是同一条记录
    assert stack.submit("scheduler.drain", operation_id="drain-once").json() == completed
    assert stack.client.get("/v1/operations/drain-once").json() == completed

    conflict = stack.submit("scheduler.resume", operation_id="drain-once")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "operation_conflict"
    # expectedRevision 参与请求指纹
    assert stack.submit("scheduler.drain", operation_id="drain-once", expected_revision=1).status_code == 409


def test_expected_revision_conflict_reports_actual(stack: Stack) -> None:
    revision = stack.client.get("/v1/snapshot").json()["revision"]
    stale = stack.submit("scheduler.resume", expected_revision=revision + 5)
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "revision_conflict", "message": "进程管理 revision 已变化",
        "details": {"expected": revision + 5, "actual": revision},
    }
    # 提交前先观测进程状态：Host 子进程换了 pid，旧修订号即失效
    stack.supervisor.pid = 9999
    moved = stack.submit("scheduler.resume", expected_revision=revision)
    assert moved.status_code == 409 and moved.json()["error"]["details"]["actual"] == revision + 1
    accepted = stack.submit("scheduler.resume", expected_revision=revision + 1)
    assert accepted.status_code == 202
    stack.wait(accepted.json()["operationId"])
    # 成功的写操作递增修订号
    assert stack.client.get("/v1/snapshot").json()["revision"] == revision + 2


@pytest.mark.parametrize(
    "body,message",
    [
        (b"not json", "请求体不是有效 JSON"),
        (b"[]", "操作请求必须是 JSON object"),
        (b'{"command": "os.restart"}', "operationId 必须是非空字符串"),
        (b'{"operationId": "a/b", "command": "os.restart"}', "operationId 无效"),
        (b'{"operationId": "x", "command": ""}', "command 必须是非空字符串"),
        # V1 用 ``parameters or {}``：空数组等同空对象，非空的非 object 才拒绝
        (b'{"operationId": "x", "command": "os.restart", "parameters": [1]}', "parameters 必须是 object"),
        (b'{"operationId": "x", "command": "os.restart", "expectedRevision": true}', "expectedRevision 必须是整数"),
    ],
)
def test_submit_validation_uses_v1_errors(stack: Stack, body: bytes, message: str) -> None:
    response = stack.client.post("/v1/operations", content=body, headers={TOKEN_HEADER: TOKEN})
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_request", "message": message}}


def test_writes_need_the_control_token_and_reads_do_not(stack: Stack) -> None:
    rejected = stack.submit("scheduler.drain", token=None)
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "unauthorized"
    assert stack.submit("scheduler.drain", token="wrong").status_code == 401
    assert stack.client.get("/v1/snapshot").status_code == 200
    missing = stack.client.get("/v1/operations/never-submitted")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "operation_not_found"
    assert stack.client.get("/v1/operations/a%5Cb").json()["error"]["code"] == "invalid_request"
    unknown = stack.client.get("/v1/unknown")
    assert (unknown.status_code, unknown.json()) == (404, {"error": {"code": "not_found"}})


def test_operations_run_in_submission_order(stack: Stack) -> None:
    """V1 test_os_restart_and_local_reset_state_are_distinct_commands：单一 FIFO 通道。"""

    calls: list[str] = []
    stack.service._dispatch = lambda operation_id, command, parameters: calls.append(command) or {"command": command}
    first = stack.submit("os.restart", operation_id="os").json()
    second = stack.submit("local.reset-state", operation_id="reset").json()
    assert stack.wait(first["operationId"])["phase"] == "succeeded"
    assert stack.wait(second["operationId"])["phase"] == "succeeded"
    assert calls == ["os.restart", "local.reset-state"]


def test_unsupported_and_resident_commands_fail_with_stable_codes(stack: Stack) -> None:
    for command, code in (
        ("backend.stop", "authority_resident"),
        ("backend.restart", "authority_resident"),
        ("plc.start", "command_unsupported"),
        ("local.reset-state", "command_unsupported"),
        ("renderer.attach", "command_unsupported"),
        ("no.such-command", "command_unknown"),
    ):
        failed = stack.execute(command)
        assert failed["phase"] == "failed" and failed["error"]["code"] == code, (command, failed)
    started = stack.execute("backend.start")
    assert started["phase"] == "succeeded" and started["result"]["components"]["backend"]["phase"] == "ready"


def test_interrupted_operations_are_failed_on_restart_of_the_authority(stack: Stack) -> None:
    with stack.workflow.transaction() as conn:
        conn.execute(
            "INSERT INTO process_management_operation(operation_id, command, parameters, request_hash, phase, submitted_at, "
            "started_at, progress) VALUES ('pkg', 'driver-package.install', '{}', 'h', 'running', 't', 't', ?)",
            (json.dumps({"packageOperationId": "pkg-op-9"}),),
        )
    reopened = stack.install(FastAPI())
    try:
        document = reopened.operation("pkg")
        assert document["phase"] == "failed" and document["error"]["code"] == "operation_interrupted"
        assert document["error"]["details"]["progress"] == {"packageOperationId": "pkg-op-9"}
    finally:
        reopened.close()


# ── 行为：Host 重启 / 启停 / 排空 ────────────────────────────────────


def test_os_restart_goes_through_the_restart_coordinator(stack: Stack, monkeypatch) -> None:
    posted: list[tuple[str, str, bytes]] = []

    def edge_http(method, path, *, headers=None, body=b"", timeout=60.0):
        posted.append((method, path, body))
        if method == "POST" and path == "/api/v1/restart":
            # Host 受理后以新进程身份重连
            stack.edge.edge_process = "host-process-2"
        return SimpleNamespace(status_code=200, body_bytes=lambda: b'{"code":0,"data":{"pending":true}}')

    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_http", edge_http)
    revision = stack.client.get("/v1/snapshot").json()["revision"]
    result = stack.execute("os.restart", {"timeoutSeconds": 10}, expected_revision=revision)
    assert result["phase"] == "succeeded", result
    assert posted == [("POST", "/api/v1/restart", b'{"mode": "immediate", "scope": "process"}')]
    assert result["result"]["restart"]["outcome"] == "succeeded"
    assert result["result"]["restart"]["edge_process"] == "host-process-2"
    assert result["result"]["components"]["edge"]["generation"] == "host-process-2"
    assert result["progress"]["restart"]["mode"] == "quiescent"
    status = stack.coordinator.status()
    assert not status["pending"] and not stack.scheduler.dispatch_paused


def test_os_restart_waits_for_quiescence_and_reports_cancel(stack: Stack, monkeypatch) -> None:
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_http", lambda *a, **k: pytest.fail("未安静前不得通知 Host"))
    stack.active[:] = ["job-1"]
    submitted = stack.submit("os.restart", {"timeoutSeconds": 10}).json()
    deadline = time.monotonic() + 5
    while not stack.coordinator.status()["pending"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stack.scheduler.dispatch_paused
    assert stack.client.get(f"/v1/operations/{submitted['operationId']}").json()["phase"] == "running"
    stack.coordinator.cancel()  # 等价于 DELETE /api/v1/restart
    failed = stack.wait(submitted["operationId"])
    assert failed["phase"] == "failed" and failed["error"]["code"] == "restart_canceled"
    assert not stack.scheduler.dispatch_paused


def test_os_restart_failure_is_reported(stack: Stack, monkeypatch) -> None:
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_http", lambda *a, **k: None)
    failed = stack.execute("workspace.restart", {"timeoutSeconds": 10})
    assert failed["phase"] == "failed" and failed["error"]["code"] == "restart_failed"
    assert "Edge 未在线" in failed["error"]["message"]
    # 失败后派发保持暂停（协调器语义），由运维确认后 DELETE /api/v1/restart 恢复
    assert stack.scheduler.dispatch_paused


def test_workspace_stop_drains_before_stopping_the_host(stack: Stack) -> None:
    stopped = stack.execute("workspace.stop")
    assert stopped["phase"] == "succeeded", stopped
    assert stack.supervisor.calls == ["stop"]
    assert stopped["result"]["drain"]["phase"] == "drained"
    assert stopped["result"]["components"]["edge"]["phase"] == "idle"
    assert list(stack.scheduler.dispatch_pause_reasons) == ["drain"]
    started = stack.execute("workspace.start", {"timeoutSeconds": 5})
    assert started["phase"] == "succeeded", started
    assert stack.supervisor.calls == ["stop", "start"]
    assert started["result"]["components"]["edge"]["phase"] == "ready"
    # workspace.start 恢复排空（V1 同口径）；os.start 不恢复
    assert started["result"]["drain"]["phase"] == "running" and not stack.scheduler.dispatch_paused


def test_workspace_stop_keeps_the_host_when_drain_times_out(stack: Stack) -> None:
    stack.active[:] = ["job-busy"]
    failed = stack.execute("workspace.stop", {"timeoutSeconds": 0.2})
    assert failed["phase"] == "failed" and failed["error"]["code"] == "workspace_drain_timeout"
    assert failed["error"]["details"]["active_device_job_ids"] == ["job-busy"]
    assert stack.supervisor.calls == [] and stack.supervisor.pid == 4321
    assert list(stack.scheduler.dispatch_pause_reasons) == ["drain"]


def test_host_lifecycle_needs_a_supervised_host_child(stack: Stack) -> None:
    stack.context.supervisor = None
    snapshot = stack.client.get("/v1/snapshot").json()
    assert snapshot["configuration"]["topology"] == "authority"
    assert snapshot["components"]["edge"]["metadata"]["supervised"] is False
    for command in ("os.start", "os.stop", "workspace.stop"):
        failed = stack.execute(command)
        assert failed["error"]["code"] == "host_not_supervised", command


# ── 行为：受管进程与驱动包 ─────────────────────────────────────────


def test_device_process_commands_reach_the_host(stack: Stack) -> None:
    restarted = stack.execute("device-process.restart", {"processId": "proc-1"})
    assert restarted["phase"] == "succeeded" and restarted["result"]["process"]["id"] == "proc-1"
    assert stack.gateway.actions == [("proc-1", "restart")]
    for parameters, code in (
        ({"processId": "../etc"}, "invalid_request"),
        ({}, "invalid_request"),
        ({"processId": "missing"}, "host_resource_not_found"),
    ):
        failed = stack.execute("device-process.stop", parameters)
        assert failed["error"]["code"] == code, parameters


def test_driver_package_install_follows_the_package_journal(stack: Stack) -> None:
    installed = stack.execute("driver-package.install", {"spec": "https://github.com/acme/driver"})
    assert installed["phase"] == "succeeded", installed
    assert installed["progress"] == {"packageOperationId": "pkg-op-1"}
    assert installed["result"]["packageOperation"]["status"] == "succeeded"
    stack.gateway.package_states[:] = ["failed"]
    failed = stack.execute("driver-package.install", {"spec": "https://github.com/acme/broken"})
    assert failed["error"]["code"] == "driver_package_failed"
    assert failed["error"]["message"] == "pip 安装失败"
    assert stack.execute("driver-package.install", {})["error"]["code"] == "invalid_request"


# ── 合同：/v1/logs/{component} ──────────────────────────────────────


def test_logs_are_bounded_and_never_take_paths(stack: Stack, monkeypatch, tmp_path: Path) -> None:
    log = tmp_path / "authority.log"
    log.write_text("第一行\nsecond line\n结尾行\n", encoding="utf-8")
    monkeypatch.setattr("unilabos.utils.runtime_logs.current_log_path", lambda: log)
    full = stack.client.get("/v1/logs/backend").json()
    assert full == {"component": "backend", "logPath": str(log), "content": "第一行\nsecond line\n结尾行\n",
                    "truncated": False, "sourceId": "backend"}
    # 按字节截尾，不切开多字节字符
    tail = stack.client.get("/v1/logs/backend", params={"maxBytes": 8}).json()
    # 最后 8 字节 = 「结」的最后一个续字节 + 「尾行\n」（7 字节）：丢掉残字节
    assert tail["content"] == "尾行\n" and tail["truncated"] is True
    assert stack.client.get("/v1/logs/backend", params={"maxBytes": 4 * 1024 * 1024}).status_code == 200
    for value in ("0", str(4 * 1024 * 1024 + 1), "-1"):
        rejected = stack.client.get("/v1/logs/backend", params={"maxBytes": value})
        assert rejected.status_code == 400
        assert rejected.json()["error"] == {"code": "invalid_request", "message": "maxBytes 超出范围"}
    assert stack.client.get("/v1/logs/backend", params={"maxBytes": "abc"}).json()["error"]["code"] == "invalid_request"

    stack.gateway.logs["host"] = _batch("host", ["host 启动", "HostLink ready"], "/data/edge.log")
    stack.gateway.logs["managed:proc-1"] = _batch("managed:proc-1", ["slave up"], "/data/proc-1.log")
    edge = stack.client.get("/v1/logs/edge").json()
    assert (edge["logPath"], edge["content"], edge["sourceId"]) == ("/data/edge.log", "host 启动\nHostLink ready\n", "host")
    assert stack.client.get("/v1/logs/managed:proc-1").json()["content"] == "slave up\n"
    assert stack.client.get("/v1/logs/plc").json() == {"component": "plc", "logPath": None, "content": ""}
    for component in ("nope", "managed:..", "managed:missing", "managed:a%20b"):
        response = stack.client.get(f"/v1/logs/{component}")
        assert response.status_code == 400 and response.json()["error"]["code"] == "component_invalid", component
    stack.gateway.offline = True
    unavailable = stack.client.get("/v1/logs/edge")
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "component_unavailable"


# ── 行为：布局 preview / apply 经操作通道 ───────────────────────────


def test_layout_preview_and_apply_compare_and_set(stack: Stack, tmp_path: Path) -> None:
    from tests.server.test_workspace_layout_v1 import _device, _mutation
    from unilabos.protocol.materials import ResourceTemplateWrite
    from unilabos.server.services.materials import MaterialsService

    materials = MaterialsService(tmp_path / "materials.db")
    stack.materials = materials
    try:
        materials.put_template(_mutation("put_template"), ResourceTemplateWrite(
            template_uuid="device-template", name="device", resource_type="device", class_name="Device"))
        device = _device(materials, "device-a", x=10, y=20, size=100)
        inspected = stack.execute("material.layout.inspect")["result"]
        revision = inspected["revision"]
        host_revision = stack.client.get("/v1/snapshot").json()["revision"]
        preview = stack.execute("material.layout.preview", {
            "expectedRevision": revision,
            "changeSet": {"nodes": [{"sourceNodeId": "device-a", "positionMm": [50, 60, 0]}]},
        })
        assert preview["phase"] == "succeeded", preview
        preview_id = preview["result"]["previewId"]
        assert preview["result"]["previewArtifact"]["operationId"] == preview["operationId"]
        # preview 只读：不改物料、不递增 Host 修订号
        assert stack.client.get("/v1/snapshot").json()["revision"] == host_revision
        assert materials.get_tree(device).nodes[0].position.position_x == 10

        stale = stack.execute("material.layout.apply", {"previewId": preview_id, "expectedRevision": "sha256:" + "0" * 64})
        assert stale["error"]["code"] == "layout_revision_conflict"
        assert stack.execute("material.layout.apply", {"previewId": "f" * 64, "expectedRevision": revision})[
            "error"]["code"] == "layout_preview_not_found"
        assert stack.execute("material.layout.apply", {"previewId": "../x", "expectedRevision": revision})[
            "error"]["code"] == "layout_preview_invalid"

        applied = stack.execute("material.layout.apply", {"previewId": preview_id, "expectedRevision": revision})
        assert applied["phase"] == "succeeded", applied
        assert applied["result"]["revision"] == preview["result"]["candidateRevision"]
        assert applied["result"]["authoringRevision"] == host_revision + 1
        assert materials.get_tree(device).nodes[0].position.position_x == 50
        # 同一 preview 不能再套用到已变化的布局源
        again = stack.execute("material.layout.apply", {"previewId": preview_id, "expectedRevision": revision})
        assert again["error"]["code"] == "layout_revision_conflict"
    finally:
        materials.close()


def test_layout_without_materials_authority_fails(stack: Stack) -> None:
    assert stack.execute("material.layout.inspect")["error"]["code"] == "layout_unavailable"


# ── MCP：目录登记的四个工具经进程内 ASGI 调到同一组路由 ────────────────


def test_mcp_tools_reach_the_process_management_routes(stack: Stack, monkeypatch) -> None:
    import asyncio

    from unilabos.server.mcp.server import ProtocolMCP
    from unilabos.server.openapi_export import export_openapi

    monkeypatch.setattr("unilabos.config.control_token.client_token", lambda: TOKEN)
    protocol = ProtocolMCP(stack.app, export_openapi())
    snapshot = asyncio.run(protocol.call_tool("process_management_snapshot", {}))
    assert not snapshot.isError and snapshot.structuredContent["body"]["schemaVersion"] == SCHEMA_VERSION
    submitted = asyncio.run(protocol.call_tool("process_management_operations_submit", {
        "body": {"operationId": "mcp-drain", "command": "scheduler.drain", "parameters": {}},
    }))
    assert not submitted.isError and submitted.structuredContent["http_status"] == 202
    assert stack.wait("mcp-drain")["phase"] == "succeeded"
    fetched = asyncio.run(protocol.call_tool("process_management_operations_get", {"path": {"operation_id": "mcp-drain"}}))
    assert fetched.structuredContent["body"]["phase"] == "succeeded"
    logs = asyncio.run(protocol.call_tool("process_management_logs_tail", {
        "path": {"component": "plc"}, "query": {"maxBytes": 1024},
    }))
    assert logs.structuredContent["body"] == {"component": "plc", "logPath": None, "content": ""}
    missing = asyncio.run(protocol.call_tool("process_management_operations_get", {"path": {"operation_id": "nope"}}))
    assert missing.isError and missing.structuredContent["http_status"] == 404


# ── SDK：HTTPLifecycleClient 的进程管理方法（V1 WorkspaceHostClient 用例） ──


def _lifecycle_client(stack: Stack):
    from unilabos.client.runtime.lifecycle import HTTPLifecycleClient

    client = HTTPLifecycleClient("http://testserver")
    client._http._client = stack.client  # 进程内 ASGI，同一组中间件与路由
    return client


def test_sdk_executes_and_normalizes_one_operation(stack: Stack) -> None:
    client = _lifecycle_client(stack)
    snapshot = client.workspace_snapshot()
    result = client.execute_workspace_operation(
        "scheduler.drain", operation_id="sdk-drain", expected_revision=snapshot["revision"], timeout=5
    )
    assert result["phase"] == "succeeded" and result["result"]["phase"] == "drained"
    assert client.workspace_operation("sdk-drain") == result
    assert client.workspace_logs("renderer", max_bytes=10) == {"component": "renderer", "logPath": None, "content": ""}
    with pytest.raises(ProcessManagementError) as caught:
        client.submit_workspace_operation("scheduler.resume", operation_id="sdk-drain")
    assert caught.value.code == "operation_conflict"
    with pytest.raises(ProcessManagementError) as caught:
        client.workspace_logs("backend", max_bytes=0)
    assert caught.value.code == "invalid_request"


def test_sdk_raises_the_operation_failure(stack: Stack) -> None:
    client = _lifecycle_client(stack)
    with pytest.raises(ProcessManagementError) as caught:
        client.execute_workspace_operation("backend.restart", timeout=5)
    assert caught.value.code == "authority_resident"
    stack.active[:] = ["job-busy"]
    with pytest.raises(ProcessManagementError) as caught:
        client.execute_workspace_operation("workspace.stop", parameters={"timeoutSeconds": 0.1}, timeout=5)
    assert caught.value.code == "workspace_drain_timeout"
    assert caught.value.details["active_device_job_ids"] == ["job-busy"]


def test_host_gateway_uses_the_control_plane_and_envelopes(monkeypatch) -> None:
    calls: list[tuple[str, str, bytes]] = []
    replies = {
        ("GET", "/api/v1/device-processes"): b'{"code":0,"data":{"hostlink":{},"processes":[{"id":"p"}]}}',
        ("POST", "/api/v1/device-processes/p%2Fx/stop"): b'{"code":4001,"error":{"msg":"device process not found"}}',
        ("GET", "/api/v1/hostlink/logs?source_id=managed%3Ap&limit=1000"): b'{"code":0,"data":{"lines":[]}}',
    }

    def edge_http(method, path, *, headers=None, body=b"", timeout=60.0):
        calls.append((method, path, body))
        reply = replies.get((method, path))
        return None if reply is None else SimpleNamespace(status_code=200, body_bytes=lambda: reply)

    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_http", edge_http)
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_proxy_enabled", lambda: True)
    gateway = HostGateway()
    assert gateway.remote() is True
    assert gateway.list_processes() == [{"id": "p"}]
    with pytest.raises(ProcessManagementError) as caught:
        gateway.process_action("p/x", "stop")
    assert caught.value.code == "host_resource_not_found" and caught.value.details["businessCode"] == 4001
    assert gateway.read_log("managed:p") == {"lines": []}
    with pytest.raises(ProcessManagementError) as caught:
        gateway.package_operation("missing")
    assert caught.value.code == "host_unreachable"
    assert calls[1] == ("POST", "/api/v1/device-processes/p%2Fx/stop", b"{}")
