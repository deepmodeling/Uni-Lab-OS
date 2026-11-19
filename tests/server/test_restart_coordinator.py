"""安静点重启：派发闸门、安静检测与 API 行为。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import unilabos.server.backend.restart as restart_module
from tests.api_envelope import error_of, ok_data
from unilabos.server.api.runtime import create_backend_app
from unilabos.server.backend.restart import RestartCoordinator
from unilabos.server.backend.scheduler.service import BackendScheduler


class _FakeExecutionBackend:
    """可控 active job 集合的执行端替身。"""

    def __init__(self) -> None:
        self._jobs: list[Any] = []
        self.wait_idle_called = False
        self.device_manager = SimpleNamespace(get_active_jobs=lambda: list(self._jobs))

    def set_active(self, job_ids: list[str]) -> None:
        self._jobs = [SimpleNamespace(job_id=job_id) for job_id in job_ids]

    def wait_idle(self, timeout: float = 5.0) -> bool:
        self.wait_idle_called = True
        return True


class _FakeScheduler:
    def __init__(self) -> None:
        self.reasons: set[str] = set()

    @property
    def dispatch_paused(self) -> bool:
        return bool(self.reasons)

    def pause_dispatch(self, *, reason: str = "manual") -> None:
        self.reasons.add(reason)

    def resume_dispatch(self, *, reason: str = "manual") -> None:
        self.reasons.discard(reason)


@pytest.fixture()
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restart_module, "_POLL_INTERVAL_SECONDS", 0.01)


@pytest.fixture(autouse=True)
def clean_restart_flag() -> Any:
    restart_module._restart_requested.clear()
    restart_module.set_safe_restart_enabled(True)
    yield
    restart_module._restart_requested.clear()
    restart_module.set_safe_restart_enabled(True)


def _make_coordinator(
    backend: Optional[_FakeExecutionBackend] = None,
    scheduler: Optional[_FakeScheduler] = None,
) -> tuple[RestartCoordinator, _FakeExecutionBackend, _FakeScheduler]:
    backend = backend or _FakeExecutionBackend()
    scheduler = scheduler if scheduler is not None else _FakeScheduler()
    coordinator = RestartCoordinator(lambda: backend, lambda: scheduler)
    return coordinator, backend, scheduler


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _capture_shutdown(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    called = threading.Event()

    def fake_shutdown() -> bool:
        called.set()
        return True

    monkeypatch.setattr(
        "unilabos.server.api.app.request_server_shutdown",
        fake_shutdown,
    )
    return called


def test_restart_waits_for_active_jobs_then_triggers(
    fast_poll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown_called = _capture_shutdown(monkeypatch)
    coordinator, backend, scheduler = _make_coordinator()
    backend.set_active(["job-1"])

    status = coordinator.request()

    assert status["pending"] is True
    assert scheduler.dispatch_paused is True
    time.sleep(0.1)
    assert not shutdown_called.is_set(), "active job 存在时不得重启"

    backend.set_active([])

    assert _wait_until(shutdown_called.is_set)
    assert restart_module.is_restart_requested()
    assert backend.wait_idle_called


def test_cancel_resumes_dispatch_without_restart(
    fast_poll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown_called = _capture_shutdown(monkeypatch)
    coordinator, backend, scheduler = _make_coordinator()
    backend.set_active(["job-1"])

    coordinator.request()
    status = coordinator.cancel()

    assert status["pending"] is False
    assert scheduler.dispatch_paused is False
    backend.set_active([])
    time.sleep(0.1)
    assert not shutdown_called.is_set()
    assert not restart_module.is_restart_requested()


def test_immediate_mode_skips_quiescence_wait(
    fast_poll: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown_called = _capture_shutdown(monkeypatch)
    coordinator, backend, _ = _make_coordinator()
    backend.set_active(["job-1", "job-2"])

    coordinator.request(mode="immediate")

    assert _wait_until(shutdown_called.is_set)


def test_request_rejects_unknown_mode() -> None:
    coordinator, _, _ = _make_coordinator()
    with pytest.raises(ValueError, match="unsupported restart mode"):
        coordinator.request(mode="soft")


def test_authority_rejects_whole_process_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """调度权威是顶层常驻进程：auto 解析为 edge（只重启 Host），显式 process 被拒绝。"""

    monkeypatch.delenv("UNILABOS_SUPERVISOR_INNER", raising=False)
    edge_control = SimpleNamespace(active_job_ids=lambda: [], connected=True)
    monkeypatch.setattr(
        "unilabos.server.backend.edge_control.get_edge_control_service", lambda: edge_control
    )
    coordinator = RestartCoordinator(lambda: None, lambda: _FakeScheduler())
    assert coordinator.status()["effective_scope"] == "edge"
    with pytest.raises(ValueError, match="常驻"):
        coordinator.request(scope="process")
    # 有父进程（被监督）时才允许整进程重启
    monkeypatch.setenv("UNILABOS_SUPERVISOR_INNER", "1")
    status = coordinator.request(mode="quiescent", scope="process")
    assert status["pending"] is True
    coordinator.cancel()


def test_request_rejects_devices_scope() -> None:
    # 设备与 Host 共用进程，因此 devices 不是可独立重启的作用域。
    coordinator, _, _ = _make_coordinator()
    with pytest.raises(ValueError, match="unsupported restart scope"):
        coordinator.request(scope="devices")


def test_scheduler_dispatch_gate_keeps_waiting_jobs() -> None:
    executor = MagicMock()
    workflow = MagicMock()
    workflow.connection.execute.return_value.fetchone.return_value = None  # 无需库存的空权威
    scheduler = BackendScheduler(workflow=workflow, executor=executor)
    scheduler.resources = MagicMock()

    scheduler.pause_dispatch()
    assert scheduler.dispatch_paused is True

    # DAG 节点键是节点运行 uuid，等待集合/派发集合以当前 attempt 的 job uuid 为键
    node = SimpleNamespace(node_id="run-1", device_id="d", action="a", always_free=False)
    task = {"uuid": "task-1"}
    scheduler._run_specs["run-1"] = {"current_job_uuid": "job-1"}
    scheduler._waiting_resource_jobs["job-1"] = (task, node)

    scheduler._dispatch_held_node(task, node)

    # 闸门生效：未查询资源、未派发、job 保持等待
    scheduler.resources.request_for_owner.assert_not_called()
    assert "job-1" in scheduler._waiting_resource_jobs
    assert "job-1" not in scheduler._dispatched_jobs
    executor.dispatch.assert_not_called()

    # resume 触发 _reconcile_resources 重算等待集合
    reconciled = threading.Event()
    scheduler._reconcile_resources = lambda: reconciled.set()  # type: ignore[method-assign]
    scheduler.resume_dispatch()
    assert scheduler.dispatch_paused is False
    assert reconciled.is_set()


def test_restart_api_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, backend, scheduler = _make_coordinator()
    backend.set_active(["job-1"])
    monkeypatch.setattr(restart_module, "_coordinator", coordinator)

    client = TestClient(create_backend_app())

    response = client.post("/api/v1/restart", json={"mode": "quiescent"})
    assert response.status_code == 200
    assert ok_data(response)["pending"] is True
    assert ok_data(response)["active_jobs"] == ["job-1"]

    assert ok_data(client.get("/api/v1/restart"))["dispatch_paused"] is True

    response = client.delete("/api/v1/restart")
    assert response.status_code == 200
    assert ok_data(response)["pending"] is False

    response = client.post("/api/v1/restart", json={"mode": "bogus"})
    error_of(response, 1000)
    assert ok_data(client.get("/api/v1/restart"))["safe_restart"] is True
    coordinator.cancel()


def test_exit_if_restart_requested_uses_supervisor_code() -> None:
    restart_module._set_restart_requested()
    with pytest.raises(SystemExit) as exc:
        restart_module.exit_if_restart_requested()
    assert exc.value.code == restart_module.RESTART_EXIT_CODE


def test_exit_if_restart_requested_stays_down_when_safe_restart_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UNILABOS_SUPERVISOR_INNER", raising=False)
    restart_module.set_safe_restart_enabled(False)
    restart_module._set_restart_requested()
    with pytest.raises(SystemExit) as exc:
        restart_module.exit_if_restart_requested()
    assert exc.value.code == 0


def test_exit_if_restart_requested_is_noop_without_flag() -> None:
    restart_module.exit_if_restart_requested()
