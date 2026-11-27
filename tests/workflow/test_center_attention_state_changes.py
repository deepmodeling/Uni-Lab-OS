"""已提交的人工事项变化必须唤醒中心当前快照。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.scheduler_core.conftest import build_core_runtime, stable_uuid
from tests.workflow.test_center_attention_snapshot import _bind_center_task
from tests.workflow.test_workflow_intervention import (
    TASK_UUID as INTERVENTION_TASK,
)
from tests.workflow.test_workflow_intervention import (
    _InterventionDelivery,
    _report,
    _seed_running_job,
)
from unilabos.workflow.service import WorkflowConflict, WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore


@pytest.mark.parametrize("entry", ["bridge", "center_control"])
@pytest.mark.parametrize("notifier_raises", [False, True])
def test_external_cancel_notifies_committed_manual_confirmation_closure(
    tmp_path: Path, entry: str, notifier_raises: bool,
) -> None:
    """外部取消直接关闭未发送人工确认时，第二连接已能看到通知中的终态。"""

    runtime = build_core_runtime(tmp_path / "cancel")
    observer = WorkflowStore(tmp_path / "cancel" / "workflow.db")
    try:
        service = WorkflowService(
            runtime.workflow_store, task_scheduler_bridge=runtime.bridge,
        )
        snapshots = []
        service.set_attention_notifier(
            lambda: snapshots.append(observer.center_attention_snapshot())
        )
        aggregate, job_uuid = runtime.submit_manual(task_name="cancel-attention")
        _bind_center_task(runtime.workflow_store, aggregate["task"]["uuid"])
        task_uuid = aggregate["task"]["uuid"]
        assert [item["manual_confirmation_count"] for item in snapshots] == [0]
        assert observer.center_attention_snapshot()["manual_confirmation_count"] == 1
        snapshots.clear()
        committed_states = []

        def notify() -> None:
            snapshots.append(observer.center_attention_snapshot())
            committed_states.append((
                observer.get_task(task_uuid)["status"],
                WorkflowService(observer).get_manual_confirmation(job_uuid)["status"],
            ))
            if notifier_raises:
                raise RuntimeError("测试通知失败")

        service.set_attention_notifier(notify)
        if entry == "center_control":
            # 中心 task.cancel 通过此既有服务入口委托同一调度桥。
            command = service.command_workflow_task(
                task_uuid, command_type="cancel", target_node_uuid=None,
                idempotency_key=stable_uuid("command:cancel-attention"),
                description="中心取消", meta_data={},
            )
            assert command["status"] == "succeeded"
            canceled = command["result"]
        else:
            canceled = runtime.bridge.cancel(
                task_uuid,
                command_uuid=stable_uuid("command:cancel-attention"),
            )

        assert canceled["task"]["status"] == "canceled"
        assert snapshots
        assert {item["manual_confirmation_count"] for item in snapshots} == {0}
        assert set(committed_states) == {("canceled", "canceled")}
    finally:
        observer.close()
        runtime.close()


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("notifier_raises", [False, True])
def test_intervention_selection_notifies_committed_state_after_delivery(
    tmp_path: Path, accepted: bool, notifier_raises: bool,
) -> None:
    """设备拒绝也已关闭 open 事项；通知故障不得替换业务冲突或成功结果。"""

    path = tmp_path / "selection.db"
    store = WorkflowStore(path)
    observer = WorkflowStore(path)
    try:
        _seed_running_job(store)
        _bind_center_task(store, INTERVENTION_TASK)
        delivery = _InterventionDelivery(accepted=accepted)
        service = WorkflowService(store)
        service.bind_intervention_delivery(delivery)
        opened = service.open_workflow_intervention_from_report(
            _report("50000000-0000-4000-8000-000000000991")
        )
        assert observer.center_attention_snapshot()["exception_count"] == 1
        observed = []

        def notify() -> None:
            snapshot = observer.center_attention_snapshot()
            intervention = WorkflowService(observer).get_workflow_intervention(opened["uuid"])
            observed.append((
                snapshot["exception_count"],
                intervention["status"],
                intervention["delivery_status"],
            ))
            if notifier_raises:
                raise RuntimeError("测试通知失败")

        service.set_attention_notifier(notify)
        if accepted:
            result = service.select_workflow_intervention(
                opened["uuid"], revision=1, option_id="retry", idempotency_key="decision",
            )
            assert result["created"] is True
            assert result["intervention"]["delivery_status"] == "accepted"
        else:
            with pytest.raises(WorkflowConflict):
                service.select_workflow_intervention(
                    opened["uuid"], revision=1, option_id="retry", idempotency_key="decision",
                )

        assert observed == [(0, "selected", "accepted" if accepted else "unknown")]
    finally:
        observer.close()
        store.close()


@pytest.mark.parametrize("revision, option_id", [(2, "retry"), (1, "missing")])
def test_invalid_intervention_selection_does_not_notify(
    tmp_path: Path, revision: int, option_id: str,
) -> None:
    """修订或选项错误未提交任何变化，不产生事项通知。"""

    store = WorkflowStore(tmp_path / "invalid.db")
    try:
        _seed_running_job(store)
        _bind_center_task(store, INTERVENTION_TASK)
        service = WorkflowService(store)
        opened = service.open_workflow_intervention_from_report(
            _report("50000000-0000-4000-8000-000000000992")
        )
        notifications = []
        service.set_attention_notifier(lambda: notifications.append(True))

        with pytest.raises(WorkflowConflict):
            service.select_workflow_intervention(
                opened["uuid"], revision=revision, option_id=option_id,
                idempotency_key="invalid-decision",
            )

        assert notifications == []
        assert service.get_workflow_intervention(opened["uuid"])["status"] == "open"
        assert store.center_attention_snapshot()["exception_count"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("action, failure_point", [
    ("approve", "timer"), ("approve", "continuation"),
    ("reject", "timer"), ("reject", "continuation"),
])
@pytest.mark.parametrize("notifier_raises", [False, True])
def test_manual_decision_notifies_committed_closure_before_side_effect_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    action: str, failure_point: str, notifier_raises: bool,
) -> None:
    """决定已提交后，即使计时器、调度或取消失败，也先通知且仍可重试。"""

    runtime = build_core_runtime(tmp_path / "decision-failure")
    observer = WorkflowStore(tmp_path / "decision-failure" / "workflow.db")
    try:
        service = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge)
        aggregate, job_uuid = runtime.submit_manual(task_name="decision-failure")
        _bind_center_task(runtime.workflow_store, aggregate["task"]["uuid"])
        observed = []

        def notify() -> None:
            observed.append((
                observer.center_attention_snapshot()["manual_confirmation_count"],
                WorkflowService(observer).get_manual_confirmation(job_uuid)["status"],
                observer.get_task(aggregate["task"]["uuid"])["status"],
            ))
            if notifier_raises:
                raise RuntimeError("测试通知失败")

        service.set_attention_notifier(notify)
        if failure_point == "timer":
            target, method = runtime.bridge, "_cancel_manual_confirmation_timer"
        elif action == "approve":
            target, method = runtime.scheduler, "resolve_manual_confirmation"
        else:
            target, method = runtime.bridge, "cancel"
        original = getattr(target, method)

        def fail(*_args: object, **_kwargs: object) -> None:
            raise ValueError("测试后续动作失败")

        monkeypatch.setattr(target, method, fail)
        with pytest.raises(WorkflowConflict):
            service.decide_manual_confirmation(job_uuid, action=action)

        expected_status = "approved" if action == "approve" else "rejected"
        assert WorkflowService(observer).get_manual_confirmation(job_uuid)["status"] == expected_status
        assert observer.center_attention_snapshot()["manual_confirmation_count"] == 0
        assert observed == [(0, expected_status, "running")]
        assert runtime.dispatcher.dispatched == []

        monkeypatch.setattr(target, method, original)
        recovered = service.decide_manual_confirmation(job_uuid, action=action)
        if action == "approve":
            assert recovered["jobs"][0]["status"] == "running"
            assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [job_uuid]
        else:
            assert recovered["task"]["status"] == "canceled"
            assert runtime.dispatcher.dispatched == []
        after_recovery = list(observed)
        service.decide_manual_confirmation(job_uuid, action=action)
        assert observed == after_recovery
    finally:
        observer.close()
        runtime.close()


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("notifier_raises", [False, True])
def test_manual_decision_success_notifies_before_continuation_without_replay_wake(
    tmp_path: Path, action: str, notifier_raises: bool,
) -> None:
    """正常决定也在继续调度前通知；同一决定重放不产生决定通知。"""

    runtime = build_core_runtime(tmp_path / "decision-success")
    observer = WorkflowStore(tmp_path / "decision-success" / "workflow.db")
    try:
        service = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge)
        _aggregate, job_uuid = runtime.submit_manual(task_name="decision-success")
        _bind_center_task(runtime.workflow_store, _aggregate["task"]["uuid"])
        observed = []

        def notify() -> None:
            job = WorkflowService(observer).get_workflow_node_job(job_uuid)
            observed.append((
                observer.center_attention_snapshot()["manual_confirmation_count"],
                job["status"], job["manual_confirmation"]["status"],
            ))
            if notifier_raises:
                raise RuntimeError("测试通知失败")

        service.set_attention_notifier(notify)
        decided = service.decide_manual_confirmation(job_uuid, action=action)
        assert observed[0] == (
            0, "pending" if action == "approve" else "running",
            "approved" if action == "approve" else "rejected",
        )
        assert decided["task"]["status"] == ("running" if action == "approve" else "canceled")
        after_decision = list(observed)
        service.decide_manual_confirmation(job_uuid, action=action)
        assert observed == after_decision
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == (
            [job_uuid] if action == "approve" else []
        )
    finally:
        observer.close()
        runtime.close()


@pytest.mark.parametrize("notifier_raises", [False, True])
def test_manual_timeout_notifies_committed_closure_before_cancel_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notifier_raises: bool,
) -> None:
    """超时关闭已提交，取消失败或通知失败仍能读取已关闭的中心快照。"""

    runtime = build_core_runtime(tmp_path / "timeout-failure")
    observer = WorkflowStore(tmp_path / "timeout-failure" / "workflow.db")
    try:
        service = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge)
        aggregate, job_uuid = runtime.submit_manual(task_name="timeout-failure", timeout_seconds=5)
        _bind_center_task(runtime.workflow_store, aggregate["task"]["uuid"])
        observed = []
        cancel_requests = []

        def notify() -> None:
            observed.append((
                observer.center_attention_snapshot()["manual_confirmation_count"],
                WorkflowService(observer).get_manual_confirmation(job_uuid)["status"],
            ))
            if notifier_raises:
                raise RuntimeError("测试通知失败")

        def fail_cancel(task_uuid: str, *, reason: str) -> None:
            cancel_requests.append((task_uuid, reason))
            raise ValueError("测试取消失败")

        service.set_attention_notifier(notify)
        monkeypatch.setattr(runtime.bridge, "cancel", fail_cancel)
        runtime.clock.advance(5)

        assert WorkflowService(observer).get_manual_confirmation(job_uuid)["status"] == "timed_out"
        assert observer.center_attention_snapshot()["manual_confirmation_count"] == 0
        assert observed == [(0, "timed_out")]
        assert cancel_requests == [(aggregate["task"]["uuid"], "manual_confirmation_timeout")]
        runtime.bridge._on_manual_confirmation_timeout()
        assert observed == [(0, "timed_out")]
        assert len(cancel_requests) == 1
    finally:
        observer.close()
        runtime.close()


@pytest.mark.parametrize("invalid", ["action", "job_uuid"])
def test_invalid_manual_decision_does_not_notify(tmp_path: Path, invalid: str) -> None:
    """非法选项或 UUID 未提交决定，不唤醒中心事项快照。"""

    runtime = build_core_runtime(tmp_path / "invalid-decision")
    try:
        service = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge)
        _aggregate, job_uuid = runtime.submit_manual(task_name="invalid-decision")
        _bind_center_task(runtime.workflow_store, _aggregate["task"]["uuid"])
        notifications = []
        service.set_attention_notifier(lambda: notifications.append(True))

        with pytest.raises(WorkflowError):
            service.decide_manual_confirmation(
                "invalid-uuid" if invalid == "job_uuid" else job_uuid,
                action="missing" if invalid == "action" else "approve",
            )

        assert notifications == []
        assert service.get_manual_confirmation(job_uuid)["status"] == "pending"
        assert service.center_attention_snapshot()["manual_confirmation_count"] == 1
    finally:
        runtime.close()
