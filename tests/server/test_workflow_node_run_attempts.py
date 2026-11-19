"""节点运行（node run）与 attempt（job）两级模型的存储契约。

- 每个节点一个稳定的节点运行；attempt 是物理执行，attempt 1 随节点运行一起创建；
- 节点运行的 status/return_info/error_info 是当前 attempt 的投影；
- ``retry`` 决策在同一事务里：失败 attempt 记 failed 并保留，追加 attempt N+1 成为当前，
  节点运行回到 pending 而不是 failed；
- 恢复判定只看节点运行，历史 failed attempt 不会把任务判成终态。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowService


@pytest.fixture()
def service() -> WorkflowService:
    instance = WorkflowService(":memory:")
    try:
        yield instance
    finally:
        instance.close()


def _single_node_task(service: WorkflowService) -> tuple[dict, dict]:
    service.set_task_submitter(lambda _uuid: None)
    task = service.create_ad_hoc_device_action_task(
        device_id="fault_injector",
        action_name="run_flaky",
        action_type="UniLabJsonCommand",
        param={"step_name": "flaky", "failures_before_success": 1},
        execution_policy={"always_free": True},
        description="node run contract",
        meta_data={},
    )
    prepared = service.prepare_workflow_task_execution(task["uuid"])
    assert prepared["state"] == "ready"
    (run,) = prepared["runs"]
    return task, run


def test_task_creation_yields_one_run_with_first_attempt(service: WorkflowService) -> None:
    task, run = _single_node_task(service)
    assert run["status"] == "pending"
    assert run["attempt_count"] == 1
    assert run["executor_kind"] == "device_action"
    assert run["param"] == {"step_name": "flaky", "failures_before_success": 1}

    detailed = service.get_workflow_node_run(run["uuid"])
    (attempt,) = detailed["attempts"]
    assert attempt["uuid"] == run["current_job_uuid"]
    assert attempt["workflow_node_run_uuid"] == run["uuid"]
    assert attempt["attempt_no"] == 1
    assert attempt["trigger"] == "initial"
    assert "retry_of_job_uuid" not in attempt
    assert attempt["param"] == run["param"]

    # attempt 平铺视图与节点运行视图指向同一 attempt
    assert [job["uuid"] for job in service.list_workflow_node_jobs(task["uuid"])] == [attempt["uuid"]]


def test_run_projects_current_attempt_status_and_result(service: WorkflowService) -> None:
    _task, run = _single_node_task(service)
    job_uuid = run["current_job_uuid"]

    service.mark_workflow_node_job_running(job_uuid)
    running = service.get_workflow_node_run(run["uuid"])
    assert running["status"] == "running"
    assert running["started_at"]

    outcome = service.record_workflow_node_job_terminal(
        job_uuid,
        status="succeeded",
        return_info={"suc": True, "return_value": {"calls": 1}},
    )
    assert outcome["next_job"] is None
    assert outcome["run"]["status"] == "succeeded"
    assert outcome["run"]["return_info"] == {"suc": True, "return_value": {"calls": 1}}
    assert outcome["run"]["finished_at"]
    assert outcome["job"]["status"] == "succeeded"


def test_retry_decision_appends_attempt_in_one_transaction(service: WorkflowService) -> None:
    task, run = _single_node_task(service)
    first_uuid = run["current_job_uuid"]
    service.mark_workflow_node_job_running(first_uuid)

    outcome = service.record_workflow_node_job_terminal(
        first_uuid,
        status="failed",
        return_info={"suc": False, "error": "transient-failure"},
        error_info=[{"code": "action_failed"}],
        error_resolution={"decision_id": "d-1", "selected_action": "retry", "reason": "瞬时故障"},
    )

    failed, projected, next_job = outcome["job"], outcome["run"], outcome["next_job"]
    # 失败 attempt 如实保留
    assert failed["status"] == "failed"
    assert failed["attempt_no"] == 1
    assert failed["error_resolution"]["selected_action"] == "retry"
    assert failed["error_info"] == [{"code": "action_failed"}]
    # 新 attempt 成为当前，节点运行回到 pending，任务不中断
    assert next_job is not None
    assert next_job["attempt_no"] == 2
    assert next_job["trigger"] == "retry_decision"
    assert next_job["retry_of_job_uuid"] == first_uuid
    assert next_job["workflow_node_run_uuid"] == run["uuid"]
    assert next_job["param"] == run["param"]
    assert projected["status"] == "pending"
    assert projected["current_job_uuid"] == next_job["uuid"]
    assert projected["attempt_count"] == 2
    assert projected["return_info"] == {}
    assert projected["error_info"] == []
    assert "finished_at" not in projected

    # 新 attempt 成功后：节点运行=当前结果，attempts=完整历史
    service.mark_workflow_node_job_running(next_job["uuid"])
    done = service.record_workflow_node_job_terminal(
        next_job["uuid"],
        status="succeeded",
        return_info={"suc": True, "return_value": {"calls": 2}},
    )
    assert done["next_job"] is None
    view = service.get_workflow_node_run(run["uuid"])
    assert view["status"] == "succeeded"
    assert view["return_info"]["return_value"] == {"calls": 2}
    assert view["started_at"] == service.get_workflow_node_job(first_uuid)["started_at"]
    assert [(a["attempt_no"], a["status"]) for a in view["attempts"]] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    # 平铺视图按 attempt 序号
    assert [j["attempt_no"] for j in service.list_workflow_node_jobs(task["uuid"])] == [1, 2]
    # 幂等：已终态 attempt 再记一次不产生新 attempt
    again = service.record_workflow_node_job_terminal(first_uuid, status="failed")
    assert again["next_job"] is None and again["run"]["attempt_count"] == 2


def test_pending_decision_puts_attempt_and_run_into_intervention_required(
    service: WorkflowService,
) -> None:
    _task, run = _single_node_task(service)
    job_uuid = run["current_job_uuid"]
    service.mark_workflow_node_job_running(job_uuid)

    pending = service.mark_workflow_node_job_decision_pending(
        job_uuid,
        {
            "decision_id": "d-1",
            "exception_type": "RuntimeError",
            "error_message": "boom",
            "options": [{"action": "retry"}, {"action": "abort"}],
            "retry_count": 0,
            "max_retries": 3,
            "expires_at": 123.0,
        },
    )
    assert pending["status"] == "intervention_required"
    assert pending["control_data"]["pending_decision"] == {
        "decision_id": "d-1",
        "exception_type": "RuntimeError",
        "error_message": "boom",
        "options": ["retry", "abort"],
        "retry_count": 0,
        "max_retries": 3,
        "expires_at": 123.0,
    }
    projected = service.get_workflow_node_run(run["uuid"])
    assert projected["status"] == "intervention_required"

    # 决策放行后正常收敛（这里选 retry）：attempt failed，新 attempt pending
    outcome = service.record_workflow_node_job_terminal(
        job_uuid,
        status="failed",
        error_info=[{"code": "action_failed"}],
        error_resolution={"decision_id": "d-1", "selected_action": "retry"},
    )
    assert outcome["job"]["status"] == "failed"
    assert outcome["job"]["control_data"]["pending_decision"]["decision_id"] == "d-1"
    assert outcome["run"]["status"] == "pending"
    assert outcome["next_job"]["attempt_no"] == 2


def test_abort_decision_projects_failed_without_new_attempt(service: WorkflowService) -> None:
    _task, run = _single_node_task(service)
    job_uuid = run["current_job_uuid"]
    service.mark_workflow_node_job_running(job_uuid)
    outcome = service.record_workflow_node_job_terminal(
        job_uuid,
        status="failed",
        error_info=[{"code": "action_failed"}],
        error_resolution={"selected_action": "abort"},
    )
    assert outcome["next_job"] is None
    assert outcome["run"]["status"] == "failed"
    assert outcome["run"]["attempt_count"] == 1
    assert outcome["run"]["error_info"] == [{"code": "action_failed"}]


def test_recovery_judges_task_by_node_run_not_by_failed_history(service: WorkflowService) -> None:
    task, run = _single_node_task(service)
    first_uuid = run["current_job_uuid"]
    service.mark_workflow_node_job_running(first_uuid)
    service.record_workflow_node_job_terminal(
        first_uuid,
        status="failed",
        error_info=[{"code": "action_failed"}],
        error_resolution={"selected_action": "retry"},
    )

    # 重启后重新认领：节点运行是 pending（attempt 2 待派发），任务继续而不是 failed
    prepared = service.prepare_workflow_task_execution(task["uuid"])
    assert prepared["state"] == "ready"
    assert [r["status"] for r in prepared["runs"]] == ["pending"]
    assert service.get_workflow_task(task["uuid"])["status"] == "running"


def test_recovery_marks_in_flight_attempt_and_run_execution_unknown(
    service: WorkflowService,
) -> None:
    task, run = _single_node_task(service)
    service.mark_workflow_node_job_running(run["current_job_uuid"])

    prepared = service.prepare_workflow_task_execution(task["uuid"])
    # waiting_reconciliation：任务可被调度器接管，但在飞 attempt 要由人裁决而不是重放
    assert prepared["state"] == "waiting_reconciliation"
    assert prepared["task"]["control_status"] == "waiting_reconciliation"
    assert prepared["task"]["attention_reason"]
    assert prepared["task"]["reconciliation_resume_control_status"] == "active"
    (recovered,) = prepared["runs"]
    assert recovered["status"] == "execution_unknown"
    attempt = service.get_workflow_node_job(run["current_job_uuid"])
    assert attempt["status"] == "execution_unknown"
    assert attempt["uncertainty_reason"]

    # 再次重启（仍未裁决）不会把任务当成不可恢复：状态原样、reason 不被覆盖
    again = service.prepare_workflow_task_execution(task["uuid"])
    assert again["state"] == "waiting_reconciliation"
    assert again["runs"][0]["status"] == "execution_unknown"
    assert again["task"]["reconciliation_resume_control_status"] == "active"


def test_reconciliation_decision_keeps_execution_unknown_until_settled(
    service: WorkflowService,
) -> None:
    """开裁决时 attempt 保持 execution_unknown（不是已知失败）；retry 收敛后任务控制态恢复。"""

    task, run = _single_node_task(service)
    job_uuid = run["current_job_uuid"]
    service.mark_workflow_node_job_running(job_uuid)
    service.prepare_workflow_task_execution(task["uuid"])

    # 尚有待裁决 attempt：settle 是 no-op
    still = service.settle_workflow_task_reconciliation(task["uuid"])
    assert still["control_status"] == "waiting_reconciliation"

    pending = service.mark_workflow_node_job_decision_pending(
        job_uuid,
        {"decision_id": "r-1", "exception_type": "ExecutionStateUnknown", "options": [{"action": "retry"}]},
    )
    assert pending["status"] == "execution_unknown"
    assert pending["control_data"]["pending_decision"]["decision_id"] == "r-1"
    assert service.get_workflow_node_run(run["uuid"])["status"] == "execution_unknown"

    outcome = service.record_workflow_node_job_terminal(
        job_uuid,
        status="failed",
        error_info=[{"code": "execution_unknown"}],
        error_resolution={"decision_id": "r-1", "selected_action": "retry"},
    )
    assert outcome["next_job"]["attempt_no"] == 2
    assert outcome["run"]["status"] == "pending"

    settled = service.settle_workflow_task_reconciliation(task["uuid"])
    assert settled["control_status"] == "active"
    assert "attention_reason" not in settled
    assert "reconciliation_resume_control_status" not in settled
    # 恢复后的任务对调度器就是普通 ready 任务，新 attempt 正常派发
    assert service.prepare_workflow_task_execution(task["uuid"])["state"] == "ready"


def test_reconciliation_skip_and_replace_settle_without_new_attempt(
    service: WorkflowService,
) -> None:
    for status, return_info in (
        ("skipped", {"suc": True, "suc_type": "skip", "return_value": None}),
        ("succeeded", {"suc": True, "suc_type": "operator_intervention", "return_value": {"ok": 1}}),
    ):
        task, run = _single_node_task(service)
        job_uuid = run["current_job_uuid"]
        service.mark_workflow_node_job_running(job_uuid)
        service.prepare_workflow_task_execution(task["uuid"])

        outcome = service.record_workflow_node_job_terminal(
            job_uuid,
            status=status,
            return_info=return_info,
            error_resolution={"decision_id": "r", "selected_action": return_info["suc_type"]},
        )
        assert outcome["next_job"] is None
        assert outcome["run"]["status"] == status
        assert outcome["run"]["attempt_count"] == 1
        assert service.settle_workflow_task_reconciliation(task["uuid"])["control_status"] == "active"


def test_node_run_api_returns_current_result_with_attempt_history(service: WorkflowService) -> None:
    task, run = _single_node_task(service)
    first_uuid = run["current_job_uuid"]
    service.mark_workflow_node_job_running(first_uuid)
    outcome = service.record_workflow_node_job_terminal(
        first_uuid,
        status="failed",
        error_info=[{"code": "action_failed"}],
        error_resolution={"selected_action": "retry"},
    )
    second_uuid = outcome["next_job"]["uuid"]
    service.mark_workflow_node_job_running(second_uuid)
    service.record_workflow_node_job_terminal(
        second_uuid, status="succeeded", return_info={"suc": True, "return_value": {"calls": 2}}
    )

    client = TestClient(create_workflow_app(service))
    listed = client.get(f"/api/v1/workflow-tasks/{task['uuid']}/node-runs").json()["data"]
    (node_run,) = listed
    assert node_run["uuid"] == run["uuid"]
    assert node_run["status"] == "succeeded"
    assert node_run["return_info"]["return_value"] == {"calls": 2}
    assert node_run["attempt_count"] == 2
    assert [a["attempt_no"] for a in node_run["attempts"]] == [1, 2]
    assert node_run["attempts"][0]["error_resolution"]["selected_action"] == "retry"
    assert node_run["attempts"][1]["retry_of_job_uuid"] == first_uuid

    single = client.get(f"/api/v1/workflow-node-runs/{run['uuid']}").json()["data"]
    assert single["current_job_uuid"] == second_uuid
    # attempt 平铺视图与 attempt 详情保持 job 粒度
    jobs = client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs").json()["data"]
    assert [j["uuid"] for j in jobs] == [first_uuid, second_uuid]
    detail = client.get(f"/api/v1/workflow-node-jobs/{second_uuid}").json()["data"]
    assert detail["workflow_node_run_uuid"] == run["uuid"] and detail["trigger"] == "retry_decision"
    assert client.get("/api/v1/workflow-node-runs/not-a-uuid").json()["code"] != 0


def test_close_node_run_terminates_current_attempt(service: WorkflowService) -> None:
    _task, run = _single_node_task(service)
    closed = service.close_workflow_node_run(run["uuid"], status="canceled")
    assert closed["status"] == "canceled"
    assert service.get_workflow_node_job(run["current_job_uuid"])["status"] == "canceled"
    # 已终态的节点运行再关闭是幂等的
    assert service.close_workflow_node_run(run["uuid"], status="failed")["status"] == "canceled"
