"""工站错误事实提交后唤醒中心快照，不把通知故障变成业务回滚。"""

from __future__ import annotations

import pytest

from tests.workflow.test_center_attention_snapshot import _bind_center_task
from tests.workflow.test_error_control import decision_request, seed_job
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.error_handling import ErrorHandlingService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


def test_error_decisions_and_session_transitions_notify_committed_snapshots(tmp_path):
    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    task, job = seed_job(store)
    _bind_center_task(store, task)
    observer = WorkflowStore(path)
    bridge = TaskSchedulerBridge(store, scheduler=EdgeScheduler(dispatcher=RecordingDispatcher()))
    snapshots = []
    bridge.set_attention_notifier(lambda: snapshots.append(observer.center_attention_snapshot()))
    service = ErrorHandlingService(store, bridge, None)
    control = bridge.error_control
    try:
        error = control.report_error(job, phase="execution", message="设备离线")
        assert len(snapshots) == 1
        assert snapshots[-1]["items"]["exceptions"][0]["stage"] == "DECISION_REQUIRED"
        entered = bridge.decide_error(error["decision_id"],
            decision_request(control, error, "enter_manual_handling"),
            idempotency_key="enter", operator_id="tester")
        assert snapshots[-1]["items"]["exceptions"][0]["stage"] == "MANUAL_HANDLING"
        session = entered["station"]["active_session"]
        completed = service.complete(session["session_id"], {
            "expected_session_version": session["version"],
            "expected_station_version": entered["station"]["station_version"],
            "summary": "已完成现场检查",
        }, idempotency_key="complete", operator_id="tester")
        assert snapshots[-1]["items"]["exceptions"][0]["stage"] == "DECISION_REQUIRED"
        error = completed["station"]["errors"][0]
        accepted = control.decide(error["decision_id"],
            decision_request(control, error, "cancel_task", session_id=session["session_id"],
                expected_session_version=completed["station"]["active_session"]["version"]),
            idempotency_key="cancel", operator_id="tester")
        assert snapshots[-1]["items"]["exceptions"][0]["stage"] == "APPLYING"
        assert snapshots[-1]["items"]["exceptions"][0]["status"] == "selected"
        control.mark_applied(accepted["command_uuid"])
        assert snapshots[-1]["exception_count"] == 0
        assert len(snapshots) == 5
        control.mark_applied(accepted["command_uuid"])
        assert len(snapshots) == 5
        bridge.set_attention_notifier(None)
        control.report_error(job, phase="cancel", message="新的设备异常", event_key="later")
        assert len(snapshots) == 5
    finally:
        bridge.close()
        observer.close()
        store.close()


def test_rolled_back_error_change_does_not_notify(tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path / "workflow.db")
    task, job = seed_job(store)
    _bind_center_task(store, task)
    bridge = TaskSchedulerBridge(store, scheduler=EdgeScheduler(dispatcher=RecordingDispatcher()))
    snapshots = []
    bridge.set_attention_notifier(lambda: snapshots.append(store.center_attention_snapshot()))
    original_emit = bridge.error_control._emit
    def fail_after_emit(connection, task_uuid=None):
        original_emit(connection, task_uuid)
        raise RuntimeError("提交前故障")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(bridge.error_control, "_emit", fail_after_emit)
            with pytest.raises(RuntimeError, match="提交前故障"):
                bridge.error_control.report_error(job, phase="execution", message="设备离线")
        assert snapshots == []
        assert bridge.error_control.snapshot()["errors"] == []
        bridge.error_control.report_error(job, phase="execution", message="设备离线")
        assert len(snapshots) == 1
        assert snapshots[0]["exception_count"] == 1
    finally:
        bridge.close()
        store.close()


def test_notification_failure_does_not_undo_committed_error(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    task, job = seed_job(store)
    _bind_center_task(store, task)
    bridge = TaskSchedulerBridge(store, scheduler=EdgeScheduler(dispatcher=RecordingDispatcher()))
    calls = []
    def unavailable():
        calls.append(True)
        raise RuntimeError("通知线程暂不可用")
    bridge.set_attention_notifier(unavailable)
    try:
        decision = bridge.error_control.report_error(job, phase="execution", message="设备离线")
        assert calls == [True]
        assert bridge.error_control.snapshot()["errors"][0]["decision_id"] == decision["decision_id"]
    finally:
        bridge.close()
        store.close()
