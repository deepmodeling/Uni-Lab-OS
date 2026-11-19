"""人工确认闸门的 durable 语义、API 与调度器闭环回归。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.dag.models import NodeState
from unilabos.server.backend.scheduler.service import (
    BackendScheduler,
    BackendSchedulingError,
)
from unilabos.server.services.runtime.workflow.service import (
    WorkflowConflict,
    WorkflowService,
)


class _Executor:
    def __init__(self) -> None:
        self.dispatched: list[dict] = []

    def add_job_finished_listener(self, listener) -> None:
        self.listener = listener

    def dispatch(self, payload: dict) -> None:
        self.dispatched.append(payload)

    def cancel_task(self, _task_uuid: str) -> list[str]:
        return []


class _Runner:
    def __init__(self) -> None:
        self.terminal: list[tuple[str, NodeState]] = []

    def notify_terminal(self, run_uuid: str, state: NodeState) -> None:
        self.terminal.append((run_uuid, state))


def _manual_task(
    service: WorkflowService,
    param: dict,
) -> tuple[dict, dict]:
    service.set_task_submitter(lambda _task_uuid: None)
    task = service.create_ad_hoc_device_action_task(
        device_id="host_node",
        action_name="manual_confirm",
        action_type="UniLabJsonCommand",
        param=param,
        execution_policy={"always_free": True},
        description="manual confirmation test",
        meta_data={},
    )
    prepared = service.prepare_workflow_task_execution(task["uuid"])
    assert prepared["state"] == "ready"
    (run,) = prepared["runs"]
    return task, run


def test_missing_assignee_and_explicit_empty_list_are_distinct() -> None:
    service = WorkflowService(":memory:")
    try:
        _task, missing_run = _manual_task(service, {"timeout_seconds": 3600})
        with pytest.raises(WorkflowConflict):
            service.open_workflow_manual_confirmation(
                missing_run["current_job_uuid"],
                param=missing_run["param"],
            )

        _task, empty_run = _manual_task(
            service,
            {"assignee_user_ids": [], "timeout_seconds": 3600},
        )
        confirmation = service.open_workflow_manual_confirmation(
            empty_run["current_job_uuid"],
            param=empty_run["param"],
        )
        assert confirmation["status"] == "pending"
        assert confirmation["assignee_user_ids"] == []
        assert confirmation["param"]["assignee_user_ids"] == []

        approved = service.decide_workflow_manual_confirmation(
            confirmation["uuid"],
            action="approve",
            decision_idempotency_key="manual-empty-1",
        )
        assert approved["status"] == "approved"
        assert approved["confirmed_by"] == "operator"
        # 同一幂等键重放只读回既有决策，不产生第二次状态变化。
        replay = service.decide_workflow_manual_confirmation(
            confirmation["uuid"],
            action="approve",
            decision_idempotency_key="manual-empty-1",
        )
        assert replay["uuid"] == approved["uuid"]
        assert replay["decided_at"] == approved["decided_at"]
    finally:
        service.close()


def test_assignee_enforcement_and_expiration_are_durable() -> None:
    service = WorkflowService(":memory:")
    callbacks: list[dict] = []
    service.set_manual_confirmation_resolver(callbacks.append)
    try:
        _task, run = _manual_task(
            service,
            {"assignee_user_ids": ["alice"], "timeout_seconds": 3600},
        )
        confirmation = service.open_workflow_manual_confirmation(
            run["current_job_uuid"],
            param=run["param"],
        )
        with pytest.raises(WorkflowConflict) as not_assignee:
            service.decide_workflow_manual_confirmation(
                confirmation["uuid"],
                action="approve",
                confirmed_by="bob",
            )
        # 前端拿到的是具体原因（指派名单），而不是笼统的"资源冲突"
        assert not_assignee.value.code == "manual_confirmation_not_assignee"
        approved = service.decide_workflow_manual_confirmation(
            confirmation["uuid"],
            action="confirm",
            confirmed_by="alice",
            decision_idempotency_key="assigned-1",
        )
        assert approved["status"] == "approved"
        assert callbacks[-1]["status"] == "approved"

        _task, expiring_run = _manual_task(
            service,
            {"assignee_user_ids": [], "timeout_seconds": 1},
        )
        expiring = service.open_workflow_manual_confirmation(
            expiring_run["current_job_uuid"],
            param=expiring_run["param"],
            timeout_seconds=1,
        )
        future = (
            datetime.now(timezone.utc) + timedelta(seconds=5)
        ).isoformat().replace("+00:00", "Z")
        expired = service.expire_workflow_manual_confirmations(now=future)
        assert [row["uuid"] for row in expired] == [expiring["uuid"]]
        assert service.get_workflow_manual_confirmation(expiring["uuid"])["status"] == "timed_out"
        assert callbacks[-1]["status"] == "timed_out"
    finally:
        service.close()


def test_manual_confirmation_survives_service_reinstantiation(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    first = WorkflowService(str(database))
    try:
        task, run = _manual_task(
            first,
            {"assignee_user_ids": [], "timeout_seconds": 3600},
        )
        opened = first.open_workflow_manual_confirmation(
            run["current_job_uuid"],
            param=run["param"],
        )
        opened_uuid = opened["uuid"]
    finally:
        first.close()

    second = WorkflowService(str(database))
    try:
        second.set_task_submitter(lambda _task_uuid: None)
        recovered = second.prepare_workflow_task_execution(task["uuid"])
        assert recovered["state"] == "ready"
        (recovered_run,) = recovered["runs"]
        assert recovered_run["status"] == "pending"
        assert recovered_run["control_data"]["manual_confirmation"]["uuid"] == opened_uuid
        restored = second.get_workflow_manual_confirmation(opened_uuid)
        assert restored["status"] == "pending"
        assert restored["assignee_user_ids"] == []
        assert restored["workflow_node_job_uuid"] == run["current_job_uuid"]
    finally:
        second.close()


def test_manual_confirmation_api_records_decision() -> None:
    service = WorkflowService(":memory:")
    try:
        _task, run = _manual_task(
            service,
            {"assignee_user_ids": [], "timeout_seconds": 3600},
        )
        opened = service.open_workflow_manual_confirmation(
            run["current_job_uuid"],
            param=run["param"],
        )
        client = TestClient(create_workflow_app(service))
        # V1 路径按作业寻址；dev 的确认人 / 决策幂等键作为可选扩展字段保留
        response = client.post(
            f"/api/v1/workflow-node-jobs/{run['current_job_uuid']}/manual-confirmation",
            json={
                "action": "approve",
                "confirmed_by": "alice",
                "decision_idempotency_key": "api-manual-1",
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "approved"
        # V1 只接受 approve / reject
        invalid = client.post(
            f"/api/v1/workflow-node-jobs/{run['current_job_uuid']}/manual-confirmation", json={"action": "cancel"},
        )
        assert invalid.json()["code"] == 1000
        # 已有决策后换一个决策：状态冲突
        conflict = client.post(
            f"/api/v1/workflow-node-jobs/{run['current_job_uuid']}/manual-confirmation",
            json={"action": "reject", "confirmed_by": "alice", "decision_idempotency_key": "api-manual-2"},
        )
        assert conflict.json()["code"] == 3003
        assert client.get(
            f"/api/v1/workflow-manual-confirmations/{opened['uuid']}"
        ).json()["data"]["confirmed_by"] == "alice"
    finally:
        service.close()


def test_scheduler_holds_manual_node_until_decision_without_device_dispatch() -> None:
    service = WorkflowService(":memory:")
    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    try:
        task, run = _manual_task(
            service,
            {"assignee_user_ids": [], "timeout_seconds": 3600},
        )
        prepared = service.prepare_workflow_task_execution(task["uuid"])
        dag, specs = scheduler._build_dag(prepared["task"], prepared["runs"])  # noqa: SLF001
        run_uuid = run["uuid"]
        scheduler._run_specs.update(specs)  # noqa: SLF001
        scheduler._run_to_task[run_uuid] = task["uuid"]  # noqa: SLF001
        runner = _Runner()
        scheduler._runners[task["uuid"]] = runner  # noqa: SLF001

        scheduler._start_node(task, dag.nodes[run_uuid])  # noqa: SLF001
        opened = service.get_manual_confirmation_for_job(run["current_job_uuid"])
        assert opened is not None
        assert opened["status"] == "pending"
        assert executor.dispatched == []
        assert service.get_workflow_node_job(run["current_job_uuid"])["status"] == "pending"

        service.decide_workflow_manual_confirmation(
            opened["uuid"],
            action="approve",
            confirmed_by="operator",
            decision_idempotency_key="scheduler-manual-1",
        )
        assert service.get_workflow_node_job(run["current_job_uuid"])["status"] == "succeeded"
        assert service.get_workflow_node_run(run_uuid)["status"] == "succeeded"
        assert runner.terminal == [(run_uuid, NodeState.SUCCESS)]
        assert executor.dispatched == []
    finally:
        service.set_manual_confirmation_resolver(None)
        service.close()


def test_scheduler_rejects_missing_assignee_key_before_opening_gate() -> None:
    service = WorkflowService(":memory:")
    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    try:
        task, run = _manual_task(service, {"timeout_seconds": 3600})
        prepared = service.prepare_workflow_task_execution(task["uuid"])
        dag, specs = scheduler._build_dag(prepared["task"], prepared["runs"])  # noqa: SLF001
        run_uuid = run["uuid"]
        scheduler._run_specs.update(specs)  # noqa: SLF001
        scheduler._run_to_task[run_uuid] = task["uuid"]  # noqa: SLF001
        scheduler._runners[task["uuid"]] = _Runner()  # noqa: SLF001

        with pytest.raises(BackendSchedulingError):
            scheduler._start_node(task, dag.nodes[run_uuid])  # noqa: SLF001
        assert service.get_manual_confirmation_for_job(run["current_job_uuid"]) is None
        assert executor.dispatched == []
    finally:
        service.set_manual_confirmation_resolver(None)
        service.close()
