"""执行面 ``@action(timeout / execution_timeout)`` 看门狗与决策链合同。

- 硬超时：协作式取消动作，attempt 以 ``TimeoutException`` 进入决策链，设备迟到的结果被忽略；
- 软超时：动作继续执行，决策多一个 ``wait``；``wait`` 重新计时并把节点运行收回 running；
  动作在等待期间真实完成时真实结果优先；
- Backend-controlled（bridge 不支持恢复）：不给 ``wait``，迟到结果附在决策上供 operator_intervention 放行。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from unilabos.server.backend import execution as execution_module
from unilabos.server.backend.execution import JobExecutionBackend
from unilabos.server.backend.execution_queue import (
    JOB_ORIGIN_BACKEND_CONTROL,
    JOB_ORIGIN_LOCAL_SCHEDULER,
    QueueItem,
)
from unilabos.server.backend.scheduler.payloads import build_job_start_payload


class _Adapter:
    """最小执行适配器：记录 send_goal / cancel_goal，动作结果由测试手动回报。"""

    def __init__(self, mappings: Dict[str, Dict[str, Any]]) -> None:
        self._action_value_mappings = {"device-1": mappings}
        self.sent: List[QueueItem] = []
        self.cancelled: List[str] = []

    def send_goal(self, item: QueueItem, **_kwargs: Any) -> None:
        self.sent.append(item)

    def cancel_goal(self, job_id: str) -> None:
        self.cancelled.append(job_id)


class _Bridge:
    def __init__(self, origins: frozenset[str], *, resumable: bool) -> None:
        self.job_origins = origins
        self.started: List[str] = []
        self.statuses: List[tuple[str, str, Optional[dict]]] = []
        self.decisions: List[Dict[str, Any]] = []
        self.resumed: List[Dict[str, Any]] = []
        if resumable:
            self.publish_job_error_decision_resumed = self._resumed  # type: ignore[assignment]

    def publish_job_started(self, item: QueueItem) -> None:
        self.started.append(item.job_id)

    def publish_job_status(self, _data: dict, item: QueueItem, status: str, info=None) -> None:
        self.statuses.append((item.job_id, status, info))

    def publish_job_error_decision_required(self, report: Dict[str, Any]) -> bool:
        self.decisions.append(report)
        return True

    def _resumed(self, report: Dict[str, Any]) -> bool:
        self.resumed.append(report)
        return True


def _payload(job_id: str, origin: str, args: Optional[dict] = None, **extra: Any):
    payload = build_job_start_payload(
        job_id=job_id,
        task_id="task-1",
        workflow_id="wf-1",
        node_id="node-1",
        device_id="device-1",
        action_name="heat",
        action_type="",
        action_args=dict(args or {}),
        node_run_uuid="run-1",
    )
    payload["origin"] = origin
    payload.update(extra)
    return payload


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def fast_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_module, "HARD_TIMEOUT_GRACE_SECONDS", 0.0)


def _make_backend(mapping: Dict[str, Any], bridge: _Bridge):
    adapter = _Adapter({"heat": mapping})
    backend = JobExecutionBackend(host_node_getter=lambda: adapter, result_bridges=[bridge])
    backend.start()
    return backend, adapter


def _finished_events(backend: JobExecutionBackend) -> List[tuple]:
    events: List[tuple] = []
    backend.add_job_finished_listener(
        lambda job_id, success, ret, suc_type, return_info: events.append(
            (job_id, success, suc_type, return_info)
        )
    )
    return events


# ── 解析 ────────────────────────────────────────────────────────


def test_resolve_action_timeouts_evaluates_expression_with_goal_defaults() -> None:
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, _adapter = _make_backend(
        {
            "timeout": 600,
            "execution_timeout": "duration * 1.5 + 30",
            "goal_default": {"duration": 60},
        },
        bridge,
    )
    try:
        resolved = backend.resolve_action_timeouts("device-1", "heat", {})
        assert resolved["timeout"] == 600.0
        assert resolved["execution_timeout"] == 120.0  # goal_default 补齐 duration
        assert resolved["execution_timeout_spec"] == "duration * 1.5 + 30"
        resolved = backend.resolve_action_timeouts("device-1", "heat", {"duration": "10"})
        assert resolved["execution_timeout"] == 45.0
        # 参数不可运算：放弃软超时但不报错，硬超时保留
        resolved = backend.resolve_action_timeouts("device-1", "heat", {"duration": "fast"})
        assert resolved["execution_timeout"] is None
        assert resolved["timeout"] == 600.0
        assert "不是数字" in resolved["error"]
    finally:
        backend.stop()


# ── 硬超时 ──────────────────────────────────────────────────────


def test_hard_timeout_cancels_action_and_opens_timeout_decision(fast_grace: None) -> None:
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, adapter = _make_backend({"timeout": 0.05}, bridge)
    events = _finished_events(backend)
    try:
        backend.dispatch(_payload("job-hard", JOB_ORIGIN_LOCAL_SCHEDULER))
        _wait_until(lambda: bridge.decisions)
        assert adapter.cancelled == ["job-hard"]
        (report,) = bridge.decisions
        assert report["exception_type"] == "TimeoutException"
        assert report["category"] == "timeout"
        assert report["timeout_kind"] == "timeout"
        assert report["timeout_seconds"] == 0.05
        assert "wait" not in {option["action"] for option in report["options"]}
        assert events == []  # attempt 挂起等待决策，尚未终态

        # 设备迟到的成功结果被忽略：终态已由超时闸门决定
        item = adapter.sent[0]
        backend.publish_job_status({}, item, "success", {"suc": True, "return_value": 1})
        time.sleep(0.05)
        assert events == []
        assert [status for _, status, _ in bridge.statuses if status == "success"] == []

        assert backend.resolve_error_decision(
            report["decision_id"], {"action": "abort", "scheduler_updated": True}
        )
        _wait_until(lambda: events)
        job_id, success, _suc_type, return_info = events[0]
        assert (job_id, success) is not None and job_id == "job-hard" and success is False
        assert return_info["error_info"]["exception_type"] == "TimeoutException"
        assert return_info["error_resolution"]["selected_action"] == "abort"
    finally:
        backend.stop()


def test_normal_completion_before_hard_timeout_clears_watchdog(fast_grace: None) -> None:
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, adapter = _make_backend({"timeout": 0.2}, bridge)
    events = _finished_events(backend)
    try:
        backend.dispatch(_payload("job-ok", JOB_ORIGIN_LOCAL_SCHEDULER))
        _wait_until(lambda: adapter.sent)
        backend.publish_job_status({}, adapter.sent[0], "success", {"suc": True, "return_value": 7})
        _wait_until(lambda: events)
        time.sleep(0.3)  # 超时点过去后没有第二个终态、没有取消
        assert len(events) == 1 and events[0][1] is True
        assert adapter.cancelled == []
        assert bridge.decisions == []
    finally:
        backend.stop()


# ── 软超时（本机调度：bridge 支持恢复） ─────────────────────────


def test_execution_timeout_offers_wait_and_rearms_without_cancelling(fast_grace: None) -> None:
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, adapter = _make_backend({"execution_timeout": 0.05}, bridge)
    events = _finished_events(backend)
    try:
        backend.dispatch(_payload("job-soft", JOB_ORIGIN_LOCAL_SCHEDULER))
        _wait_until(lambda: bridge.decisions)
        (report,) = bridge.decisions
        assert report["exception_type"] == "ExecutionTimeoutException"
        assert report["severity"] == "warning"
        assert report["action_still_running"] is True
        assert report["timeout_kind"] == "execution_timeout"
        options = [option["action"] for option in report["options"]]
        assert options[0] == "wait" and {"retry", "abort", "operator_intervention"} <= set(options)
        assert adapter.cancelled == []  # 动作没有被取消

        # wait：决策收回、节点运行恢复 running、重新计时后再次超时
        assert backend.resolve_error_decision(
            report["decision_id"], {"action": "wait", "scheduler_updated": True}
        )
        assert bridge.resumed and bridge.resumed[0]["selected_action"] == "wait"
        assert backend.list_error_decisions() == []
        _wait_until(lambda: len(bridge.decisions) >= 2)
        assert bridge.decisions[1]["decision_id"] != report["decision_id"]
        assert adapter.cancelled == []
        assert events == []

        # abort：这时才取消仍在执行的动作，并以失败收口
        second = bridge.decisions[1]
        assert backend.resolve_error_decision(
            second["decision_id"], {"action": "abort", "scheduler_updated": True}
        )
        _wait_until(lambda: events)
        assert adapter.cancelled == ["job-soft"]
        assert events[0][1] is False
        assert events[0][3]["error_info"]["exception_type"] == "ExecutionTimeoutException"
    finally:
        backend.stop()


def test_real_result_supersedes_pending_execution_timeout_decision(fast_grace: None) -> None:
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, adapter = _make_backend({"execution_timeout": 0.05}, bridge)
    events = _finished_events(backend)
    try:
        backend.dispatch(_payload("job-late", JOB_ORIGIN_LOCAL_SCHEDULER))
        _wait_until(lambda: bridge.decisions)
        decision_id = bridge.decisions[0]["decision_id"]

        backend.publish_job_status(
            {}, adapter.sent[0], "success", {"suc": True, "return_value": {"ok": 1}}
        )
        _wait_until(lambda: events)
        assert events[0][1] is True and events[0][3]["return_value"] == {"ok": 1}
        assert backend.list_error_decisions() == []
        assert bridge.resumed[0]["decision_id"] == decision_id
        assert bridge.resumed[0]["selected_action"] == "superseded"
        # 决策墓碑保留，重复放行幂等地被识别
        assert backend.get_resolved_action_error_decision(decision_id, "job-late", "device-1")
        assert adapter.cancelled == []
    finally:
        backend.stop()


# ── 软超时（Backend-controlled：bridge 不支持恢复） ───────────────


def test_execution_timeout_without_resume_support_attaches_late_result(fast_grace: None) -> None:
    bridge = _Bridge(frozenset({JOB_ORIGIN_BACKEND_CONTROL}), resumable=False)
    backend, adapter = _make_backend({"execution_timeout": 0.05}, bridge)
    events = _finished_events(backend)
    try:
        backend.dispatch(_payload("job-bc", JOB_ORIGIN_BACKEND_CONTROL))
        _wait_until(lambda: bridge.decisions)
        (report,) = bridge.decisions
        assert "wait" not in {option["action"] for option in report["options"]}

        # 动作在等待期间真实完成：结果附在决策上，不直接放行
        backend.publish_job_status(
            {}, adapter.sent[0], "success", {"suc": True, "return_value": {"temp": 60}}
        )
        time.sleep(0.05)
        assert events == []
        assert backend.list_error_decisions()[0]["decision_id"] == report["decision_id"]

        # operator_intervention 不带结果：缺省替换结果就是设备真实返回值
        assert backend.resolve_error_decision(
            report["decision_id"],
            {"action": "operator_intervention", "scheduler_updated": True},
        )
        _wait_until(lambda: events)
        _job, success, suc_type, return_info = events[0]
        assert success is True and suc_type == "operator_intervention"
        assert return_info["return_value"] == {"temp": 60}
        assert adapter.cancelled == []
    finally:
        backend.stop()


# ── 调度器下发的覆盖值优先 ──────────────────────────────────────


def test_dispatch_payload_timeouts_override_registry(fast_grace: None) -> None:
    bridge = _Bridge(frozenset({JOB_ORIGIN_LOCAL_SCHEDULER}), resumable=True)
    backend, adapter = _make_backend({"timeout": 600, "execution_timeout": 600}, bridge)
    try:
        backend.dispatch(
            _payload(
                "job-override",
                JOB_ORIGIN_LOCAL_SCHEDULER,
                execution_timeout_seconds=0.05,
            )
        )
        _wait_until(lambda: bridge.decisions)
        assert bridge.decisions[0]["exception_type"] == "ExecutionTimeoutException"
        assert bridge.decisions[0]["timeout_seconds"] == 0.05
        assert adapter.cancelled == []
    finally:
        backend.stop()
