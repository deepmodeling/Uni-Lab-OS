"""执行面只在本进程计时的时长与期限：墙钟跳变不影响。

覆盖等待空闲的期限、超时报告里的已运行时长和已解决决策的幂等墓碑保留期。
只改写墙钟（``time.time``），单调时钟照常走。
"""

from __future__ import annotations

import itertools
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from unilabos.client.host import execution as execution_module
from unilabos.client.host.execution import JobExecutionBackend
from unilabos.server.execution_queue import JOB_ORIGIN_LOCAL_SCHEDULER, QueueItem
from unilabos.server.scheduler.payloads import build_job_start_payload


def _jump_wall_clock(monkeypatch, offset_s: float) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_s)


def _jumping_wall_clock(monkeypatch, step_s: float) -> None:
    """每读一次墙钟，它都比上一次再前跳 ``step_s`` 秒。"""

    real_time = time.time
    reads = itertools.count(1)
    monkeypatch.setattr(time, "time", lambda: real_time() + step_s * next(reads))


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


class _Adapter:
    def __init__(self, mapping: Dict[str, Any]) -> None:
        self._action_value_mappings = {"device-1": {"heat": mapping}}
        self.sent: List[QueueItem] = []

    def send_goal(self, item: QueueItem, **_kwargs: Any) -> None:
        self.sent.append(item)

    def cancel_goal(self, job_id: str) -> None:
        pass


class _Bridge:
    job_origins = frozenset({JOB_ORIGIN_LOCAL_SCHEDULER})

    def __init__(self) -> None:
        self.decisions: List[Dict[str, Any]] = []

    def publish_job_started(self, item: QueueItem) -> None:
        pass

    def publish_job_status(self, _data: dict, item: QueueItem, status: str, info=None) -> None:
        pass

    def publish_job_error_decision_required(self, report: Dict[str, Any]) -> bool:
        self.decisions.append(report)
        return True

    def publish_job_error_decision_resumed(self, report: Dict[str, Any]) -> bool:
        return True


def test_wait_idle_deadline_ignores_forward_wall_clock_jumps(monkeypatch) -> None:
    backend = JobExecutionBackend()
    with backend._pending_lock:
        backend._pending = 1

    def drain() -> None:
        time.sleep(0.2)
        with backend._pending_lock:
            backend._pending = 0

    threading.Thread(target=drain, daemon=True).start()
    _jumping_wall_clock(monkeypatch, 13.0)

    assert backend.wait_idle(timeout=5.0) is True


@pytest.mark.parametrize("timeout_key", ["timeout", "execution_timeout"])
def test_timeout_report_elapsed_ignores_forward_wall_clock_jump(monkeypatch, timeout_key: str) -> None:
    monkeypatch.setattr(execution_module, "HARD_TIMEOUT_GRACE_SECONDS", 0.0)
    adapter = _Adapter({timeout_key: 0.5})
    bridge = _Bridge()
    backend = JobExecutionBackend(host_node_getter=lambda: adapter, result_bridges=[bridge])
    backend.start()
    try:
        payload = build_job_start_payload(
            job_id="job-1",
            task_id="task-1",
            workflow_id="wf-1",
            node_id="node-1",
            device_id="device-1",
            action_name="heat",
            action_type="",
            action_args={},
            node_run_uuid="run-1",
        )
        payload["origin"] = JOB_ORIGIN_LOCAL_SCHEDULER
        backend.dispatch(payload)
        _wait_until(lambda: adapter.sent)
        # 动作执行期间墙钟前跳 13 s；超时看门狗本身按单调时钟到期
        _jump_wall_clock(monkeypatch, 13.0)
        _wait_until(lambda: bridge.decisions)

        (report,) = bridge.decisions
        pending = backend._pending_action_error_decisions[report["decision_id"]]
        elapsed = pending["return_info"]["error_info"]["elapsed_seconds"]
        assert 0.3 <= elapsed < 5.0
    finally:
        backend.stop()


def _retire_one_decision(backend: JobExecutionBackend) -> None:
    item = SimpleNamespace(
        task_id="task-1", node_id="node-1", node_run_uuid="run-1", device_id="device-1", action_name="heat"
    )
    pending = {"decision_id": "decision-1", "job_id": "job-1", "item": item}
    backend._pending_action_error_decisions["decision-1"] = pending
    assert backend._retire_pending_decision(pending, selected_action="abort", reason="operator") is not None


def test_resolved_decision_tombstone_survives_forward_wall_clock_jump(monkeypatch) -> None:
    backend = JobExecutionBackend()
    _retire_one_decision(backend)

    # 墙钟前跳超过 1 h 的保留期：幂等重放仍要找得到这次解决结果
    _jump_wall_clock(monkeypatch, 2 * 3600.0)

    report = backend.get_resolved_action_error_decision("decision-1", "job-1", "device-1")
    assert report is not None and report["selected_action"] == "abort"


def test_resolved_decision_tombstone_expires_on_monotonic_time(monkeypatch) -> None:
    monkeypatch.setattr(JobExecutionBackend, "_ACTION_ERROR_DECISION_TOMBSTONE_TTL_SECONDS", 0.2)
    backend = JobExecutionBackend()
    _retire_one_decision(backend)

    _jump_wall_clock(monkeypatch, -3600.0)
    time.sleep(0.3)

    assert backend.get_resolved_action_error_decision("decision-1", "job-1", "device-1") is None
