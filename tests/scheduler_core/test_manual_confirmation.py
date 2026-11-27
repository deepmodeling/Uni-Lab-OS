"""人工确认包装设备动作的调度核心验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.scheduler_core.conftest import CoreRuntime, build_core_runtime, stable_uuid
from tests.scheduler_core.test_failure_safety import outcome
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


def _manual_node_job(
    runtime: CoreRuntime,
    *,
    task_name: str,
    index: int,
    device_id: str,
) -> tuple[dict, dict]:
    node_uuid = stable_uuid(f"manual-node:{task_name}:{index}")
    return (
        {
            "uuid": node_uuid,
            "kind": "manual_confirm",
            "device_id": device_id,
            "material_uuid": runtime.device_materials[device_id],
            "action_name": "run",
            "action_type": "UniLabJsonCommand",
            "param": {},
            "param_schema": {
                "type": "object",
                "properties": {"goal": {"type": "object"}},
            },
            "manual_confirmation": {"timeout_seconds": 3600},
            "execution_policy": {},
            "action_resource_contract": {},
            "material_requirements": [],
        },
        {
            "uuid": stable_uuid(f"manual-job:{task_name}:{index}"),
            "workflow_node_uuid": node_uuid,
            "topological_index": index,
            "executor_kind": "manual_confirm",
            "execution_policy": {},
            "execution_timeout_seconds": 0,
            "param": {},
        },
    )


def test_manual_confirmation_holds_device_before_prompt_and_approve_reuses_job(
    core_runtime: CoreRuntime,
) -> None:
    """完整准入后才提示；等待占设备，批准复用同一 Job 下发一次。"""

    aggregate, job_uuid = core_runtime.submit_manual(task_name="manual-approve")
    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )

    assert aggregate["task"]["status"] == "running"
    assert aggregate["jobs"][0]["status"] == "running"
    assert aggregate["jobs"][0]["executor_kind"] == "manual_confirm"
    assert aggregate["jobs"][0]["manual_confirmation"]["status"] == "pending"
    assert aggregate["jobs"][0]["manual_confirmation"]["actions"] == [
        "approve",
        "reject",
    ]
    assert core_runtime.dispatcher.dispatched == []

    blocked = core_runtime.submit(task_name="blocked-by-human", devices=["reactor-a"])
    assert blocked["jobs"][0]["status"] == "pending"
    assert core_runtime.dispatcher.dispatched == []

    decided = service.decide_manual_confirmation(job_uuid, action="approve")

    assert decided["task"]["status"] == "running"
    decided_job = next(job for job in decided["jobs"] if job["uuid"] == job_uuid)
    assert decided_job["executor_kind"] == "device_action"
    assert decided_job["status"] == "running"
    assert decided_job["manual_confirmation"]["status"] == "approved"
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [job_uuid]

    replay = service.decide_manual_confirmation(job_uuid, action="approve")
    assert replay["task"]["uuid"] == aggregate["task"]["uuid"]
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [job_uuid]


@pytest.mark.parametrize("resume_immediately", [False, True])
@pytest.mark.parametrize("resolution", ["approve", "reject", "timeout"])
def test_failed_manual_action_retry_requires_fresh_confirmation(
    core_runtime: CoreRuntime, resume_immediately: bool, resolution: str,
) -> None:
    """已批准动作失败后的新尝试重新提示确认，不能沿用旧批准或无入口地等待。"""
    runtime = core_runtime
    aggregate, first_job = runtime.submit_manual(task_name="manual-failed-retry")
    service = WorkflowService(runtime.workflow_store, task_scheduler_bridge=runtime.bridge)

    def pending_confirmations() -> list[dict]:
        return [item for item in service.list_task_manual_confirmations(aggregate["task"]["uuid"])
                if item["status"] == "pending"]

    service.decide_manual_confirmation(first_job, action="approve")
    runtime.clock.advance(1)
    runtime.scheduler.on_job_outcome(first_job, outcome("failed"))
    assert runtime.workflow_store.get_job(first_job)["status"] == "failed"

    snapshot = runtime.bridge.error_control.snapshot()
    error = snapshot["errors"][0]
    request = {
        "action": "retry_current_node",
        "expected_decision_version": error["decision_version"],
        "expected_station_version": snapshot["station_version"],
        "observed_error_epoch": snapshot["error_epoch"],
        "resume_requested": resume_immediately,
        "operator_confirmed": resume_immediately,
        "confirm_snapshot_id": snapshot["snapshot_id"],
    }
    accepted = runtime.bridge.decide_error(
        error["decision_id"], request,
        idempotency_key="retry-manual-action", operator_id="tester",
    )
    assert accepted["status"] == "APPLIED", accepted
    retry_job = accepted["result"]["retry_job_uuid"]
    assert retry_job != first_job
    if not resume_immediately:
        paused = runtime.bridge.error_control.snapshot()
        assert paused["mode"] == "PAUSED"
        assert pending_confirmations() == []
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first_job]
        runtime.bridge.error_control.resume({
            "expected_station_version": paused["station_version"],
            "observed_error_epoch": paused["error_epoch"],
            "operator_confirmed": True,
            "confirm_snapshot_id": paused["snapshot_id"],
        }, idempotency_key="resume-manual-action", operator_id="tester")
    runtime.scheduler.reschedule()

    retry = service.get_workflow_node_job(retry_job)
    assert retry["attempt"] == 2
    assert retry["executor_kind"] == "manual_confirm"
    assert retry.get("manual_confirmation", {}).get("status") == "pending", retry
    assert retry["manual_confirmation"]["actions"] == ["approve", "reject"]
    assert service.get_manual_confirmation(first_job)["status"] == "approved"
    assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first_job]
    replay = runtime.bridge.decide_error(
        error["decision_id"], request,
        idempotency_key="retry-manual-action", operator_id="tester",
    )
    assert replay["result"]["retry_job_uuid"] == retry_job
    runtime.scheduler.reschedule()
    assert len(pending_confirmations()) == 1
    required = [item["data"]["job_uuid"]
                for item in service.list_events(after_sequence=0, limit=100)["items"]
                if item["event"] == "manual_confirmation.required"]
    assert required == [first_job, retry_job]
    service.decide_manual_confirmation(first_job, action="approve")
    assert service.get_manual_confirmation(retry_job)["status"] == "pending"
    assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first_job]

    if resolution != "approve":
        if resolution == "reject":
            service.decide_manual_confirmation(retry_job, action="reject")
        else:
            runtime.clock.advance(3600)
        expected = "rejected" if resolution == "reject" else "timed_out"
        assert service.get_manual_confirmation(retry_job)["status"] == expected
        assert service.get_workflow_task(aggregate["task"]["uuid"])["status"] == "canceled"
        assert pending_confirmations() == []
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first_job]
        return

    service.decide_manual_confirmation(retry_job, action="approve")
    service.decide_manual_confirmation(retry_job, action="approve")
    assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first_job, retry_job]
    runtime.scheduler.on_job_outcome(retry_job, outcome("succeeded"))
    assert service.get_workflow_task(aggregate["task"]["uuid"])["status"] == "succeeded"
    assert pending_confirmations() == []


def test_manual_confirmation_reject_uses_task_cancel_without_device_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    """拒绝赢得 CAS 后走 Task Cancel，未下发人工节点的设备取消请求。"""

    aggregate, job_uuid = core_runtime.submit_manual(task_name="manual-reject")
    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )

    decided = service.decide_manual_confirmation(job_uuid, action="reject")

    assert aggregate["task"]["status"] == "running"
    assert decided["task"]["status"] == "canceled"
    decided_job = next(job for job in decided["jobs"] if job["uuid"] == job_uuid)
    assert decided_job["status"] == "canceled"
    assert decided_job["manual_confirmation"]["status"] == "rejected"
    assert decided_job["error_info"][0]["code"] == "manual_confirmation_rejected"
    assert core_runtime.dispatcher.dispatched == []
    assert core_runtime.dispatcher.cancel_requests == []
    assert core_runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim"
    ) == [{"state": "released"}]
    assert core_runtime.inventory_store.query_all(
        "SELECT DISTINCT state FROM station_execution_lock_lease"
    ) == [{"state": "released"}]


def test_external_task_cancel_closes_confirmation_and_releases_resources(
    core_runtime: CoreRuntime,
) -> None:
    """外部取消关闭待确认事实，且无需设备取消证明即可释放未发送资源。"""

    aggregate, job_uuid = core_runtime.submit_manual(task_name="manual-canceled")
    canceled = core_runtime.bridge.cancel(aggregate["task"]["uuid"])

    assert canceled["task"]["status"] == "canceled"
    canceled_job = next(job for job in canceled["jobs"] if job["uuid"] == job_uuid)
    assert canceled_job["manual_confirmation"]["status"] == "canceled"
    assert core_runtime.dispatcher.cancel_requests == []
    claim = TaskRuntimeProjection(core_runtime.workflow_store).get_execution_claim(
        job_uuid
    )
    assert claim is not None
    assert claim["state"] == "released"


def test_same_approve_retries_continuation_after_first_side_effect_failure(
    core_runtime: CoreRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准事实已提交但继续调度失败时，同一 approve 可幂等补偿。"""

    _aggregate, job_uuid = core_runtime.submit_manual(task_name="approve-retry")
    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    original = core_runtime.scheduler.resolve_manual_confirmation

    def fail_once(_job_uuid: str, *, approved: bool) -> dict[str, object]:
        del approved
        raise ValueError("injected continuation failure")

    monkeypatch.setattr(
        core_runtime.scheduler,
        "resolve_manual_confirmation",
        fail_once,
    )
    with pytest.raises(WorkflowConflict):
        service.decide_manual_confirmation(job_uuid, action="approve")
    assert service.get_workflow_node_job(job_uuid)["status"] == "pending"

    monkeypatch.setattr(
        core_runtime.scheduler,
        "resolve_manual_confirmation",
        original,
    )
    recovered = service.decide_manual_confirmation(job_uuid, action="approve")
    assert next(job for job in recovered["jobs"] if job["uuid"] == job_uuid)[
        "status"
    ] == "running"
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [job_uuid]


def test_same_reject_retries_task_cancel_after_first_side_effect_failure(
    core_runtime: CoreRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拒绝事实已提交但取消失败时，同一 reject 可幂等补偿整个 Task。"""

    aggregate, job_uuid = core_runtime.submit_manual(task_name="reject-retry")
    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    original = core_runtime.bridge.cancel

    def fail_once(
        _task_uuid: str,
        *,
        command_uuid: str | None = None,
        reason: str = "task_canceled",
    ) -> dict[str, object]:
        del command_uuid, reason
        raise ValueError("injected cancel failure")

    monkeypatch.setattr(core_runtime.bridge, "cancel", fail_once)
    with pytest.raises(WorkflowConflict):
        service.decide_manual_confirmation(job_uuid, action="reject")
    assert service.get_workflow_task(aggregate["task"]["uuid"])["status"] == "running"

    monkeypatch.setattr(core_runtime.bridge, "cancel", original)
    recovered = service.decide_manual_confirmation(job_uuid, action="reject")
    assert recovered["task"]["status"] == "canceled"
    assert core_runtime.dispatcher.dispatched == []


def test_manual_confirmation_timeout_uses_manual_clock_and_task_cancel(
    core_runtime: CoreRuntime,
) -> None:
    """持久截止时间到期后关闭确认并取消整个 Task。"""

    _aggregate, job_uuid = core_runtime.submit_manual(
        task_name="manual-timeout",
        timeout_seconds=5,
    )
    service = WorkflowService(core_runtime.workflow_store)

    core_runtime.clock.advance(5)

    task_uuid = stable_uuid("task:manual-timeout")
    task = service.get_workflow_task(task_uuid)
    job = service.get_workflow_node_job(job_uuid)
    assert task["status"] == "canceled"
    assert job["status"] == "canceled"
    assert job["manual_confirmation"]["status"] == "timed_out"
    assert job["error_info"][0]["code"] == "manual_confirmation_timeout"
    assert core_runtime.dispatcher.dispatched == []


def test_manual_confirmation_only_opens_after_all_resources_are_available(
    core_runtime: CoreRuntime,
) -> None:
    """设备资源尚被占用时保持普通 pending，不提前出现确认按钮。"""

    running = core_runtime.submit(task_name="physical-first", devices=["reactor-a"])
    _manual, manual_job_uuid = core_runtime.submit_manual(
        task_name="manual-waits-for-device"
    )
    service = WorkflowService(core_runtime.workflow_store)

    assert running["jobs"][0]["status"] == "running"
    waiting_job = service.get_workflow_node_job(manual_job_uuid)
    assert waiting_job["status"] == "pending"
    assert "manual_confirmation" not in waiting_job


def test_manual_confirmation_holds_action_material_lock_until_device_finishes(
    core_runtime: CoreRuntime,
) -> None:
    """等待人工批准时取得动作物料锁，并保持到真实设备动作结束。"""

    sample_uuid = core_runtime.device_materials["warehouse-a"]
    manual_node, manual_job = _manual_node_job(
        core_runtime,
        task_name="manual-material-owner",
        index=0,
        device_id="reactor-a",
    )
    manual_node["param"] = {"sample": {"uuid": sample_uuid}}
    manual_node["param_schema"] = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "sample": {
                        "type": "object",
                        "x-unilabos-material-lock": True,
                        "properties": {
                            "uuid": {"type": "string", "format": "uuid"}
                        },
                        "required": ["uuid"],
                        "additionalProperties": False,
                    }
                },
                "required": ["sample"],
                "additionalProperties": False,
            }
        },
        "required": ["goal"],
    }
    pending = core_runtime.submit_frozen(
        task_name="manual-material-owner",
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": [manual_node],
            "handles": [],
            "edges": [],
        },
        jobs=[manual_job],
    )
    assert pending["jobs"][0]["manual_confirmation"]["status"] == "pending"

    ordinary_node = {
        **manual_node,
        "uuid": stable_uuid("node:material-contender"),
        "kind": "device_action",
        "device_id": "reactor-b",
        "material_uuid": core_runtime.device_materials["reactor-b"],
    }
    ordinary_node.pop("manual_confirmation")
    ordinary_job = {
        **manual_job,
        "uuid": stable_uuid("job:material-contender"),
        "workflow_node_uuid": ordinary_node["uuid"],
        "executor_kind": "device_action",
    }
    blocked = core_runtime.submit_frozen(
        task_name="material-contender",
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": [ordinary_node],
            "handles": [],
            "edges": [],
        },
        jobs=[ordinary_job],
    )
    assert blocked["jobs"][0]["status"] == "pending"

    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    service.decide_manual_confirmation(manual_job["uuid"], action="approve")
    assert core_runtime.workflow_store.get_job(ordinary_job["uuid"])["status"] == (
        "pending"
    )
    core_runtime.scheduler.on_job_finished(
        manual_job["uuid"],
        True,
        {},
        "normal",
    )
    assert core_runtime.workflow_store.get_job(ordinary_job["uuid"])["status"] == (
        "running"
    )


def test_approved_manual_confirmation_waits_offline_then_dispatches_automatically(
    tmp_path: Path,
) -> None:
    """批准事实持久化后，设备离线只延迟物理下发，不回滚决定或资源。"""

    online = True
    runtime = build_core_runtime(
        tmp_path,
        device_available=lambda _device_id: online,
    )
    try:
        _pending, job_uuid = runtime.submit_manual(task_name="manual-offline")
        service = WorkflowService(
            runtime.workflow_store,
            task_scheduler_bridge=runtime.bridge,
        )
        online = False

        approved = service.decide_manual_confirmation(job_uuid, action="approve")
        job = next(item for item in approved["jobs"] if item["uuid"] == job_uuid)
        assert job["status"] == "pending"
        assert job["manual_confirmation"]["status"] == "approved"
        assert runtime.dispatcher.dispatched == []

        online = True
        runtime.scheduler.reschedule()
        assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [job_uuid]
        assert service.get_workflow_node_job(job_uuid)["status"] == "running"
    finally:
        runtime.close()


def test_manual_approval_is_an_inflight_continuation_allowed_during_drain(
    core_runtime: CoreRuntime,
) -> None:
    """已准入人工 Job 阻止 drain，并允许批准后的同 Job 越过物理边界。"""

    _pending, job_uuid = core_runtime.submit_manual(task_name="manual-drain")
    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )

    draining = core_runtime.scheduler.begin_drain()
    assert draining["phase"] == "draining"
    assert draining["active_device_job_ids"] == [job_uuid]

    service.decide_manual_confirmation(job_uuid, action="approve")
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [job_uuid]


def test_multiple_manual_confirmations_are_independent_until_one_rejects_task(
    core_runtime: CoreRuntime,
) -> None:
    """同一 Task 可并行等待多个确认；拒绝一个会取消其余等待。"""

    pairs = [
        _manual_node_job(
            core_runtime,
            task_name="parallel-manual",
            index=index,
            device_id=device,
        )
        for index, device in enumerate(("reactor-a", "reactor-b"))
    ]
    aggregate = core_runtime.submit_frozen(
        task_name="parallel-manual",
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": [pair[0] for pair in pairs],
            "handles": [],
            "edges": [],
        },
        jobs=[pair[1] for pair in pairs],
    )
    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    assert [job["manual_confirmation"]["status"] for job in aggregate["jobs"]] == [
        "pending",
        "pending",
    ]

    result = service.decide_manual_confirmation(
        pairs[0][1]["uuid"],
        action="reject",
    )
    assert result["task"]["status"] == "canceled"
    confirmations = {
        job["uuid"]: job["manual_confirmation"]["status"]
        for job in result["jobs"]
    }
    assert confirmations == {
        pairs[0][1]["uuid"]: "rejected",
        pairs[1][1]["uuid"]: "canceled",
    }
    assert core_runtime.dispatcher.dispatched == []


def test_opposite_manual_decisions_have_exactly_one_cas_winner(
    core_runtime: CoreRuntime,
) -> None:
    """批准/拒绝并发时只有一个成功，且设备最多下发一次。"""

    _pending, job_uuid = core_runtime.submit_manual(task_name="manual-race")
    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(service.decide_manual_confirmation, job_uuid, action=action)
            for action in ("approve", "reject")
        ]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except WorkflowConflict:
            outcomes.append("conflict")

    assert sum(item == "conflict" for item in outcomes) == 1
    assert len(core_runtime.dispatcher.dispatched) <= 1
    confirmation = WorkflowService(core_runtime.workflow_store).get_manual_confirmation(
        job_uuid
    )
    assert confirmation["status"] in {"approved", "rejected"}
    with pytest.raises(WorkflowConflict):
        service.decide_manual_confirmation(
            job_uuid,
            action="reject" if confirmation["status"] == "approved" else "approve",
        )
