"""排空协议：来源隔离、执行态未知、角色边界与控制面鉴权。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.config.config import BasicConfig
from unilabos.server.api.app import ControlTokenMiddleware
from unilabos.server.api.runtime.diagnostics import create_backend_app
from unilabos.server.backend.lifecycle import SchedulerLifecycle
from unilabos.server.backend.restart import RestartCoordinator
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setattr("unilabos.server.backend.edge_control.get_edge_control_service", lambda: None)
    workflow = WorkflowService(":memory:")
    scheduler = BackendScheduler(workflow, MagicMock())
    backend = SimpleNamespace(device_manager=SimpleNamespace(get_active_jobs=lambda: []))
    lifecycle = SchedulerLifecycle(lambda: scheduler, lambda: backend)
    yield scheduler, backend, lifecycle
    scheduler.stop()
    workflow.close()


def test_canceling_restart_preserves_explicit_drain(stack):
    scheduler, backend, lifecycle = stack
    backend.device_manager.get_active_jobs = lambda: [SimpleNamespace(job_id="active")]
    coordinator = RestartCoordinator(lambda: backend, lambda: scheduler)
    assert lifecycle.status().phase == "running"
    assert lifecycle.begin_drain().phase == "draining"
    assert lifecycle.begin_drain().requested  # 重复请求不增加需要释放的次数
    try:
        coordinator.request()
        assert lifecycle.status().pause_reasons == ["drain", "restart"]
    finally:
        coordinator.cancel()
    assert lifecycle.status().pause_reasons == ["drain"]
    assert lifecycle.resume().phase == "running"


def test_resuming_drain_preserves_reset_and_restart_gates(stack):
    scheduler, _, lifecycle = stack
    lifecycle.begin_drain()
    scheduler.pause_dispatch(reason="reset")
    scheduler.pause_dispatch(reason="restart")
    status = lifecycle.resume()
    assert status.phase == "drained" and not status.requested
    assert status.pause_reasons == ["reset", "restart"]
    scheduler.resume_dispatch(reason="reset")
    assert scheduler.dispatch_paused
    scheduler.resume_dispatch(reason="restart")
    assert not scheduler.dispatch_paused


def test_runtime_reconnect_only_releases_restart_gate(stack, monkeypatch):
    scheduler, backend, lifecycle = stack
    lifecycle.begin_drain()
    scheduler.pause_dispatch(reason="restart")
    coordinator = RestartCoordinator(lambda: backend, lambda: scheduler)
    # Host 的 /restart 回统一信封
    accepted = SimpleNamespace(status_code=200, body_bytes=lambda: b'{"code":0,"data":{"pending":true}}')
    monkeypatch.setattr("unilabos.server.api.edge_proxy.edge_http", lambda *a, **kw: accepted)
    monkeypatch.setattr(coordinator, "_wait_edge_reconnected", lambda: True)
    coordinator._execute_edge_restart()
    assert lifecycle.status().pause_reasons == ["drain"]


def test_unknown_execution_blocks_drain_but_manual_confirmation_does_not(stack):
    scheduler, _, lifecycle = stack
    scheduler._dispatched_jobs.add("confirmation")
    scheduler._manual_confirmation_jobs.add("confirmation")
    assert lifecycle.begin_drain().phase == "drained"
    scheduler._reconciliation_decisions["decision"] = {"job_uuid": "unknown"}
    status = lifecycle.status()
    assert status.phase == "draining"
    assert status.active_device_job_ids == ["unknown"]
    assert status.active_device_job_count == 1


def test_remote_runtime_and_scheduler_reservations_are_combined(stack, monkeypatch):
    scheduler, _, _ = stack
    scheduler._dispatched_jobs.update({"reserved", "remote"})
    monkeypatch.setattr(
        "unilabos.server.backend.edge_control.get_edge_control_service",
        lambda: SimpleNamespace(active_job_ids=lambda: ["remote", "external"]),
    )
    lifecycle = SchedulerLifecycle(lambda: scheduler, lambda: None)
    status = lifecycle.begin_drain()
    assert status.active_device_job_ids == ["external", "remote", "reserved"]
    assert status.active_device_job_count == 3


def test_drain_api_authentication_readiness_and_idempotency(stack, monkeypatch):
    scheduler, backend, _ = stack
    monkeypatch.setattr(BasicConfig, "control_token", "test-lifecycle-token")
    app = create_backend_app(lambda: scheduler, lambda: backend)
    app.add_middleware(ControlTokenMiddleware)
    with TestClient(app) as client:
        assert ok_data(client.get("/api/v1/scheduler/drain"))["phase"] == "running"
        # 就绪 / 存活探针不包信封，仍按 HTTP 状态码判定
        assert client.get("/api/v1/readiness").status_code == 200
        error_of(client.post("/api/v1/scheduler/drain", json={}), 1002, status=401)
        assert not scheduler.dispatch_paused
        headers = {"X-UniLab-Token": "test-lifecycle-token"}
        # Bearer 留给 V1 工站 api_key，不能代替控制面 token
        error_of(
            client.post("/api/v1/scheduler/drain", json={}, headers={"Authorization": "Bearer test-lifecycle-token"}),
            1002,
            status=401,
        )
        assert not scheduler.dispatch_paused
        first = client.post("/api/v1/scheduler/drain", json={}, headers=headers)
        second = client.post("/api/v1/scheduler/drain", json={}, headers=headers)
        assert ok_data(first) == ok_data(second)
        assert client.get("/api/v1/readiness").status_code == 503
        assert client.get("/api/v1/health").status_code == 200
        error_of(client.post("/api/v1/scheduler/drain/resume", json={}), 1002, status=401)
        assert scheduler.dispatch_paused
        assert ok_data(client.post("/api/v1/scheduler/drain/resume", json={}, headers=headers))["phase"] == "running"
        assert client.get("/api/v1/readiness").status_code == 200


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/scheduler/drain"),
    ("POST", "/api/v1/scheduler/drain"),
    ("POST", "/api/v1/scheduler/drain/resume"),
])
def test_runtime_without_scheduler_does_not_claim_to_be_drained(method, path):
    with TestClient(create_backend_app()) as client:
        # 没有调度器：依赖暂不可用 5001（HTTP 200 信封），不伪装成已排空
        error_of(client.request(method, path), 5001)
