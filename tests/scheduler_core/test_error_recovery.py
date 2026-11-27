"""错误处置决定与持久 DAG/资源恢复的纵向合同。"""

import pytest

from tests.scheduler_core.conftest import stable_uuid
from tests.scheduler_core.test_control_flow import dependency, device_node, job
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)


def test_retry_inherits_certain_failed_scope_after_restart(core_runtime):
    """明确失败的连续占用交给新 attempt，旧结果保留且成功后不遗留租约。"""
    runtime = core_runtime
    nodes = [
        device_node(
            node_uuid=stable_uuid(f"retry-node:{i}"),
            parent_uuid=None,
            device_id="reactor-a",
            material_uuid=runtime.device_materials["reactor-a"],
            action="run",
        )
        for i in range(2)
    ]
    edges = [dependency(nodes[0]["uuid"], nodes[1]["uuid"], name="retry-edge")]
    plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(
                {
                    "nodes": nodes,
                    "edges": edges,
                    "resources": ["reactor-a"],
                }
            ),
            {
                "reactor-a": {
                    "canonical_key": f"/devices/{runtime.device_materials['reactor-a']}",
                    "kind": "device",
                }
            },
        )
    )
    for node in nodes:
        node["resource_plan_id"] = plan["plan_id"]
        node["resource_interval_ids"] = [
            item["interval_id"]
            for item in plan["intervals"]
            if node["uuid"] in item["node_uuids"]
        ]
        node["resource_acquire_set_id"] = next(
            (
                item["acquire_set_id"]
                for item in plan["acquire_sets"]
                if node["uuid"] == item["node_uuid"]
            ),
            "",
        )
    jobs = [
        job(
            job_uuid=stable_uuid(f"retry-job:{i}"),
            node_uuid=node["uuid"],
            index=i,
            kind="device_action",
        )
        for i, node in enumerate(nodes)
    ]
    aggregate = runtime.submit_frozen(
        task_name="retry-scope",
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "nodes": nodes,
            "edges": edges,
            "handles": [],
            "resource_plan": plan,
            "capabilities": plan["capabilities"],
        },
        jobs=jobs,
    )
    task_uuid = aggregate["task"]["uuid"]
    first_job = runtime.dispatcher.dispatched[0]["job_id"]
    runtime.scheduler.on_job_finished(first_job, False, {"error": "fixture failure"})
    control = runtime.bridge.error_control
    snapshot = control.snapshot()
    decision = snapshot["errors"][0]
    applied = runtime.bridge.decide_error(
        decision["decision_id"],
        {
            "action": "retry_current_node",
            "expected_decision_version": decision["decision_version"],
            "expected_station_version": snapshot["station_version"],
            "observed_error_epoch": snapshot["error_epoch"],
            "resume_requested": False,
        },
        idempotency_key="retry",
        operator_id="tester",
    )
    assert applied["status"] == "APPLIED"
    retry_job = applied["result"]["retry_job_uuid"]
    runtime.restart_scheduler()
    assert len(runtime.dispatcher.dispatched) == 1
    control = runtime.bridge.error_control
    snapshot = control.snapshot()
    control.resume(
        {
            "expected_station_version": snapshot["station_version"],
            "observed_error_epoch": snapshot["error_epoch"],
            "operator_confirmed": True,
            "confirm_snapshot_id": snapshot["snapshot_id"],
        },
        idempotency_key="resume",
        operator_id="tester",
    )
    runtime.bridge.reschedule()
    assert runtime.dispatcher.dispatched[-1]["job_id"] == retry_job
    old_claims = runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim WHERE job_uuid=?", (first_job,)
    )
    assert old_claims and all(row["state"] == "released" for row in old_claims)
    runtime.scheduler.on_job_finished(retry_job, True, {"retried": True})
    runtime.scheduler.on_job_finished(
        runtime.dispatcher.dispatched[-1]["job_id"], True, {}
    )
    assert runtime.workflow_store.get_task(task_uuid)["status"] == "succeeded"
    assert runtime.workflow_store.get_job(first_job)["status"] == "failed"
    assert runtime.workflow_store.get_job(first_job)["current_attempt"] is False
    assert (
        runtime.inventory_store.query_all(
            "SELECT claim_uuid FROM station_execution_claim WHERE task_uuid=? AND state!='released'",
            (task_uuid,),
        )
        == []
    )


@pytest.mark.parametrize("restart", [False, True])
def test_late_success_keeps_successor_blocked_until_retry_finishes(
    core_runtime, restart
):
    """UNKNOWN 之后的明确成功清掉错误决策，且这次结算不会派发后继。"""
    from unilabos.app.scheduler.dispatch import CommittedJobOutcome

    runtime = core_runtime
    runtime.submit(
        task_name="late-result", devices=["reactor-a", "reactor-b"], edges=[(0, 1)]
    )
    first = runtime.dispatcher.dispatched[0]["job_id"]
    runtime.scheduler.on_job_outcome(
        first, CommittedJobOutcome("failed", {}, [], ["unknown-command"])
    )
    runtime.scheduler.on_job_outcome(
        first, CommittedJobOutcome("succeeded", {"late": True}, [], [])
    )
    if restart:
        runtime.restart_scheduler()
    assert runtime.bridge.error_control.snapshot()["errors"] == []
    assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [first]


@pytest.mark.parametrize("mode", ["pause", "resume"])
def test_task_mode_changes_cannot_bypass_error_and_recovery_preserves_latest_mode(
    core_runtime, mode
):
    """错误等待期间的单步切换不能放行，决定应用后按最新执行模式恢复。"""
    runtime = core_runtime
    task = runtime.submit(
        task_name="mode-during-error",
        devices=["reactor-a", "reactor-b"],
        edges=[(0, 1)],
    )
    identity = task["task"]["uuid"]
    first = runtime.dispatcher.dispatched[0]["job_id"]
    runtime.scheduler.on_job_finished(first, False, {})
    runtime.bridge.pause(identity)
    if mode == "resume":
        runtime.bridge.resume(identity)
    assert (
        runtime.workflow_store.get_task(identity)["control_status"] == "error_waiting"
    )
    snapshot = runtime.bridge.error_control.snapshot()
    decision = snapshot["errors"][0]
    result = runtime.bridge.decide_error(
        decision["decision_id"],
        {
            "action": "retry_current_node",
            "expected_decision_version": decision["decision_version"],
            "expected_station_version": snapshot["station_version"],
            "observed_error_epoch": snapshot["error_epoch"],
            "resume_requested": True,
            "operator_confirmed": True,
            "confirm_snapshot_id": snapshot["snapshot_id"],
        },
        idempotency_key="retry-mode",
        operator_id="tester",
    )
    assert result["status"] == "APPLIED"
    assert runtime.workflow_store.get_task(identity)["control_status"] == (
        "paused" if mode == "pause" else "active"
    )
    if mode == "pause":
        assert len(runtime.dispatcher.dispatched) == 1
        runtime.bridge.resume(identity)
    assert (
        runtime.dispatcher.dispatched[-1]["job_id"]
        == result["result"]["retry_job_uuid"]
    )


def test_approved_manual_confirmation_survives_station_pause_and_restart(core_runtime):
    """审批型普通节点在工站暂停期间批准，重启后仍按同一身份等待恢复。"""
    from unilabos.workflow.service import WorkflowService

    runtime = core_runtime
    _, manual_job = runtime.submit_manual(
        task_name="manual-restored", device_id="reactor-a"
    )
    runtime.submit(task_name="other-failure", devices=["reactor-b"])
    failed = runtime.dispatcher.dispatched[-1]["job_id"]
    runtime.scheduler.on_job_finished(failed, False, {})
    WorkflowService(
        runtime.workflow_store, task_scheduler_bridge=runtime.bridge
    ).decide_manual_confirmation(manual_job, action="approve")
    assert runtime.workflow_store.get_job(manual_job)["status"] == "pending"
    runtime.restart_scheduler()
    control = runtime.bridge.error_control
    snapshot = control.snapshot()
    control.resume(
        {
            "expected_station_version": snapshot["station_version"],
            "observed_error_epoch": snapshot["error_epoch"],
            "operator_confirmed": True,
            "confirm_snapshot_id": snapshot["snapshot_id"],
        },
        idempotency_key="manual-resume",
        operator_id="tester",
    )
    runtime.bridge.reschedule()
    assert [item["job_id"] for item in runtime.dispatcher.dispatched] == [
        failed,
        manual_job,
    ]


@pytest.mark.parametrize("restart", [False, True])
def test_repeat_retry_keeps_committed_rounds_and_other_tasks_schedulable(core_runtime, restart):
    """控制节点重试保留已完成轮次；重启后仍能报告当前错误并调度其他任务。"""
    from tests.scheduler_core.test_control_flow import submit_repeat_task

    runtime = core_runtime
    aggregate = submit_repeat_task(runtime)
    for iteration in range(3):
        runtime.scheduler.on_job_finished(runtime.dispatcher.dispatched[-1]["job_id"], True, {"qualified": False})
        runtime.scheduler.on_job_finished(runtime.dispatcher.dispatched[-1]["job_id"], True, {"next_dose": iteration + 2})
    control = runtime.bridge.error_control
    snapshot = control.snapshot()
    decision = snapshot["errors"][0]
    result = runtime.bridge.decide_error(decision["decision_id"], {
        "action": "retry_current_node",
        "expected_station_version": snapshot["station_version"],
        "expected_decision_version": decision["decision_version"],
        "observed_error_epoch": snapshot["error_epoch"],
        "resume_requested": False,
    }, idempotency_key="retry-loop", operator_id="tester")
    retry_job = result["result"]["retry_job_uuid"]
    if restart:
        runtime.restart_scheduler()
    control = runtime.bridge.error_control
    snapshot = control.snapshot()
    control.resume({
        "expected_station_version": snapshot["station_version"],
        "observed_error_epoch": snapshot["error_epoch"],
        "operator_confirmed": True,
        "confirm_snapshot_id": snapshot["snapshot_id"],
    }, idempotency_key="resume-loop", operator_id="tester")
    runtime.bridge.reschedule()
    # 冻结的循环上限仍不满足，重试应重新显示该错误，而不是重放已成功的设备动作。
    snapshot = control.snapshot()
    assert snapshot["mode"] == "PAUSED"
    assert snapshot["errors"][0]["source_job_uuid"] == retry_job
    assert "最大迭代次数" in snapshot["errors"][0]["meta_data"]["message"]
    assert len(runtime.dispatcher.dispatched) == 6
    assert len([j for j in runtime.workflow_store.list_jobs(aggregate["task"]["uuid"]) if j["status"] == "succeeded"]) == 6
    control.resume({
        "expected_station_version": snapshot["station_version"],
        "observed_error_epoch": snapshot["error_epoch"],
        "operator_confirmed": True,
        "confirm_snapshot_id": snapshot["snapshot_id"],
    }, idempotency_key="resume-other-tasks", operator_id="tester")
    other = runtime.submit(task_name="unrelated-after-loop", devices=["robot-a"])
    assert runtime.dispatcher.dispatched[-1]["task_id"] == other["task"]["uuid"]
