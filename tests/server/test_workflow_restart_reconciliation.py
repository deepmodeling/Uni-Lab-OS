"""进程重启后执行态未知的 attempt 走人工裁决链，而不是把任务永久卡在 waiting_reconciliation。

- 恢复的 execution_unknown / intervention_required attempt 不重放设备动作，调度器开一条
  与执行面失败决策同形的裁决（retry / skip / operator_intervention / abort）；
- 裁决从同一个 ``/api/v1/error-decisions`` 暴露与提交；
- 全部裁决收敛后任务控制态从 waiting_reconciliation 恢复为 active。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.runtime.diagnostics import create_backend_app
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.dag.models import NodeState
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService


class _Executor:
    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.listener: Callable[..., None] | None = None
        self.error_policy: dict[str, Any] = {}

    def add_job_finished_listener(self, listener) -> None:
        self.listener = listener

    def remove_job_finished_listener(self, _listener) -> None:
        self.listener = None

    def dispatch(self, payload: dict) -> None:
        self.dispatched.append(payload)

    def cancel_task(self, _task_uuid: str) -> list[str]:
        return []

    def resolve_action_error_policy(self, _device_id: str, _action: str) -> dict[str, Any]:
        return dict(self.error_policy)


class _Runner:
    def __init__(self) -> None:
        self.terminal: list[tuple[str, NodeState]] = []

    def notify_terminal(self, run_uuid: str, state: NodeState) -> None:
        self.terminal.append((run_uuid, state))


@pytest.fixture()
def service() -> WorkflowService:
    instance = WorkflowService(":memory:")
    instance.set_task_submitter(lambda _uuid: None)
    try:
        yield instance
    finally:
        instance.close()


def _crashed_task(service: WorkflowService) -> tuple[dict, dict]:
    """一个单节点任务，attempt 1 在上一进程里已 running：重启后恢复即执行态未知。"""

    task = service.create_ad_hoc_device_action_task(
        device_id="pump-1",
        action_name="transfer",
        action_type="UniLabJsonCommand",
        param={"volume": 5},
        execution_policy={"always_free": True},
        description="restart reconciliation",
        meta_data={},
    )
    prepared = service.prepare_workflow_task_execution(task["uuid"])
    (run,) = prepared["runs"]
    service.mark_workflow_node_job_running(run["current_job_uuid"])
    return task, run


def _adopt(
    scheduler: BackendScheduler, service: WorkflowService, task_uuid: str
) -> tuple[dict, dict, _Runner]:
    """等价于新进程里 run_task 走到 _build_dag 之后：接管任务并起跑恢复的节点。"""

    prepared = service.prepare_workflow_task_execution(task_uuid)
    assert prepared["state"] == "waiting_reconciliation"
    dag, specs = scheduler._build_dag(prepared["task"], prepared["runs"])  # noqa: SLF001
    (run_uuid,) = dag.nodes
    scheduler._run_specs.update(specs)  # noqa: SLF001
    scheduler._run_to_task[run_uuid] = task_uuid  # noqa: SLF001
    runner = _Runner()
    scheduler._runners[task_uuid] = runner  # type: ignore[assignment]  # noqa: SLF001
    scheduler._start_node(prepared["task"], dag.nodes[run_uuid])  # noqa: SLF001
    return prepared["task"], prepared["runs"][0], runner


def test_recovered_unknown_attempt_opens_decision_instead_of_redispatch(service: WorkflowService) -> None:
    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    task, _run = _crashed_task(service)

    _task, run, _runner = _adopt(scheduler, service, task["uuid"])

    # 没有重放设备动作，也没有申请资源
    assert executor.dispatched == []
    (report,) = scheduler.list_reconciliation_decisions()
    assert report["job_id"] == run["current_job_uuid"]
    assert report["task_id"] == task["uuid"]
    assert report["node_run_uuid"] == run["uuid"]
    assert (report["device_id"], report["action_name"]) == ("pump-1", "transfer")
    assert report["exception_type"] == "ExecutionStateUnknown"
    assert report["recovered_status"] == "execution_unknown"
    assert [option["action"] for option in report["options"]] == [
        "retry",
        "skip",
        "operator_intervention",
        "abort",
    ]
    assert (report["retry_count"], report["max_retries"]) == (0, 3)
    assert scheduler.has_reconciliation_decision(report["decision_id"])
    # 裁决 id 对同一 attempt 稳定：再次重启后前端拿旧 id 仍能提交
    assert scheduler.list_reconciliation_decisions()[0]["decision_id"] == report["decision_id"]

    # attempt 保持"执行态未知"，只是挂上了待决策摘要；任务控制态提示需要处理
    job = service.get_workflow_node_job(run["current_job_uuid"])
    assert job["status"] == "execution_unknown"
    assert job["control_data"]["pending_decision"]["decision_id"] == report["decision_id"]
    assert service.get_workflow_task(task["uuid"])["control_status"] == "waiting_reconciliation"


def test_retry_decision_dispatches_a_new_attempt_and_restores_control_status(
    service: WorkflowService,
) -> None:
    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    task, _run = _crashed_task(service)
    _task, run, runner = _adopt(scheduler, service, task["uuid"])
    (report,) = scheduler.list_reconciliation_decisions()

    assert scheduler.resolve_reconciliation_decision(
        report["decision_id"],
        {"action": "retry", "reason": "设备核对后未执行", "job_id": report["job_id"], "device_id": "pump-1"},
    )

    # 旧 attempt 记 failed 并保留裁决；新 attempt 派发给执行器，DAG 节点不终结
    old = service.get_workflow_node_job(run["current_job_uuid"])
    assert old["status"] == "failed"
    assert old["error_resolution"]["selected_action"] == "retry"
    assert old["error_info"][0]["code"] == "execution_unknown"
    (payload,) = executor.dispatched
    assert payload["node_run_uuid"] == run["uuid"]
    assert payload["attempt_no"] == 2
    assert payload["job_id"] != run["current_job_uuid"]
    assert runner.terminal == []
    assert scheduler.list_reconciliation_decisions() == []
    assert service.get_workflow_task(task["uuid"])["control_status"] == "active"

    # 新 attempt 由执行器正常回报终态
    assert executor.listener is not None
    executor.listener(payload["job_id"], True, {"moved": 5}, "normal", {})
    view = service.get_workflow_node_run(run["uuid"])
    assert view["status"] == "succeeded"
    assert [(a["attempt_no"], a["status"]) for a in view["attempts"]] == [(1, "failed"), (2, "succeeded")]
    assert runner.terminal == [(run["uuid"], NodeState.SUCCESS)]


@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_state", "expected_return"),
    [
        ({"action": "skip"}, "skipped", NodeState.SUCCESS, None),
        (
            {"action": "operator_intervention", "result": {"moved": 5, "confirmed_by": "alice"}},
            "succeeded",
            NodeState.SUCCESS,
            {"moved": 5, "confirmed_by": "alice"},
        ),
        # 替换为成功可以不带 result：用户确认设备已完成即可
        ({"action": "operator_intervention"}, "succeeded", NodeState.SUCCESS, None),
        ({"action": "abort", "reason": "设备确实没做"}, "failed", NodeState.FAILED, None),
    ],
)
def test_non_retry_decisions_settle_the_same_attempt(
    service: WorkflowService,
    decision: dict,
    expected_status: str,
    expected_state: NodeState,
    expected_return: Any,
) -> None:
    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    task, _run = _crashed_task(service)
    _task, run, runner = _adopt(scheduler, service, task["uuid"])
    (report,) = scheduler.list_reconciliation_decisions()

    assert scheduler.resolve_reconciliation_decision(report["decision_id"], dict(decision))

    view = service.get_workflow_node_run(run["uuid"])
    assert view["status"] == expected_status
    assert view["attempt_count"] == 1
    assert view["return_info"].get("return_value") == expected_return
    job = service.get_workflow_node_job(run["current_job_uuid"])
    assert job["error_resolution"]["selected_action"] == decision["action"]
    assert job["error_resolution"]["reason"] == decision.get("reason", "")
    assert executor.dispatched == []
    assert runner.terminal == [(run["uuid"], expected_state)]
    assert service.get_workflow_task(task["uuid"])["control_status"] == "active"
    assert scheduler.list_reconciliation_decisions() == []


def test_option_object_carries_action_and_result(service: WorkflowService) -> None:
    """前端按 option 对象提交时，动作与 result 都从 option 里取。"""

    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    task, _run = _crashed_task(service)
    _task, run, _runner = _adopt(scheduler, service, task["uuid"])
    (report,) = scheduler.list_reconciliation_decisions()

    assert scheduler.resolve_reconciliation_decision(
        report["decision_id"],
        {"option": {"action": "operator_intervention", "result": {"ok": True}}},
    )
    assert service.get_workflow_node_run(run["uuid"])["return_info"]["return_value"] == {"ok": True}


def test_rejected_decisions_leave_the_attempt_pending(service: WorkflowService) -> None:
    executor = _Executor()
    executor.error_policy = {"max_retries": 0}
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    task, _run = _crashed_task(service)
    _task, run, runner = _adopt(scheduler, service, task["uuid"])
    (report,) = scheduler.list_reconciliation_decisions()
    decision_id = report["decision_id"]

    assert not scheduler.resolve_reconciliation_decision("not-a-decision", {"action": "abort"})
    assert not scheduler.resolve_reconciliation_decision(decision_id, {"action": "fallback"})
    assert not scheduler.resolve_reconciliation_decision(decision_id, {"action": "abort", "job_id": "other-job"})
    assert not scheduler.resolve_reconciliation_decision(decision_id, {"action": "abort", "device_id": "pump-9"})
    # 注册表 max_retries=0：重试上限与执行面放行失败决策时同一口径
    assert report["max_retries"] == 0
    assert not scheduler.resolve_reconciliation_decision(decision_id, {"action": "retry"})

    assert scheduler.has_reconciliation_decision(decision_id)
    assert service.get_workflow_node_job(run["current_job_uuid"])["status"] == "execution_unknown"
    assert runner.terminal == []
    assert service.get_workflow_task(task["uuid"])["control_status"] == "waiting_reconciliation"


def test_orphaned_intervention_required_attempt_is_reopened_for_decision(
    service: WorkflowService,
) -> None:
    """失败后等待决策时进程重启：执行面的 pending 已丢，调度器带着原错误重新开裁决。"""

    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    task, run = _crashed_task(service)
    service.mark_workflow_node_job_decision_pending(
        run["current_job_uuid"],
        {
            "decision_id": "executor-d-1",
            "exception_type": "CommunicationError",
            "error_message": "serial port closed",
            "options": [{"action": "retry"}, {"action": "abort"}],
        },
    )

    _task, run, _runner = _adopt(scheduler, service, task["uuid"])

    (report,) = scheduler.list_reconciliation_decisions()
    assert report["recovered_status"] == "intervention_required"
    assert report["exception_type"] == "CommunicationError"
    assert "serial port closed" in report["error_message"]
    assert report["decision_id"] != "executor-d-1"
    job = service.get_workflow_node_job(run["current_job_uuid"])
    assert job["status"] == "intervention_required"
    assert job["control_data"]["pending_decision"]["decision_id"] == report["decision_id"]

    assert scheduler.resolve_reconciliation_decision(report["decision_id"], {"action": "abort"})
    assert service.get_workflow_node_run(run["uuid"])["status"] == "failed"
    assert service.get_workflow_node_job(run["current_job_uuid"])["error_info"][0]["code"] == "action_failed"
    assert service.get_workflow_task(task["uuid"])["control_status"] == "active"


def _wait_until(predicate: Callable[[], Any], *, timeout: float = 5.0, description: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {description}")


def _crashed_two_node_workflow_task(service: WorkflowService) -> tuple[dict, list[dict]]:
    """整图任务（重启后可被恢复认领）：节点 1 的 attempt 在上一进程里已 running，节点 2 仍 pending。"""

    client = TestClient(create_workflow_app(service))
    workflow = client.post("/api/v1/workflows", json={"name": "重启恢复", "tags": []}).json()["data"]
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    material = str(uuid.uuid4())

    def node(node_uuid: str, action: str, depends_on: list[str], order: int) -> dict:
        return {
            "uuid": node_uuid,
            "name": f"wf-device/{action}",
            "type": "device_action",
            "material_uuid": material,
            "action_name": action,
            "action_type": "UniLabJsonCommand",
            "param": {"value": order},
            "pose": {"x": order * 300, "y": 120},
            "execution_policy": {"depends_on": depends_on, "always_free": True},
            "meta_data": {"target_device_id": "wf-device"},
        }

    saved = client.put(
        f"/api/v1/workflows/{workflow['uuid']}/graph",
        json={
            "revision": workflow["revision"],
            # 本夹具只有模拟执行器，不提供物料/动作注册表；不在此测试程序化 Site 解析。
            "site_binding_mode": "preserve",
            "nodes": [node(first, "prepare", [], 1), node(second, "measure", [first], 2)],
            "edges": [],
        },
    ).json()
    assert saved["code"] == 0, saved
    created = client.post(
        "/api/v1/workflow-tasks",
        json={"execution_kind": "workflow", "workflow_uuid": workflow["uuid"], "run_mode": "normal"},
    ).json()
    assert created["code"] == 0, created
    task = created["data"]
    prepared = service.prepare_workflow_task_execution(task["uuid"])
    assert prepared["state"] == "ready"
    runs = prepared["runs"]
    service.mark_workflow_node_job_running(runs[0]["current_job_uuid"])
    return task, runs


def test_restarted_scheduler_recovers_crashed_task_end_to_end() -> None:
    """新进程 start(recover=True)：任务被接管、开裁决；retry 后节点 1 由执行器完成，
    节点 2 照常派发，任务 succeeded。"""

    service = WorkflowService(":memory:")
    service.set_task_submitter(lambda _uuid: None)
    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    try:
        task, (first_run, second_run) = _crashed_two_node_workflow_task(service)
        assert service.get_workflow_task(task["uuid"])["status"] == "running"

        scheduler.start(recover=True)

        report = _wait_until(
            lambda: next(iter(scheduler.list_reconciliation_decisions()), None),
            description="重启恢复后开出裁决",
        )
        assert report["task_id"] == task["uuid"]
        assert report["node_run_uuid"] == first_run["uuid"]
        # 未知态节点没有重放，下游节点也没有越过它先跑
        assert executor.dispatched == []
        current = service.get_workflow_task(task["uuid"])
        assert current["status"] == "running"
        assert current["control_status"] == "waiting_reconciliation"

        assert scheduler.resolve_reconciliation_decision(report["decision_id"], {"action": "retry"})
        retry = _wait_until(lambda: executor.dispatched[0] if executor.dispatched else None, description="新 attempt 派发")
        assert (retry["node_run_uuid"], retry["attempt_no"]) == (first_run["uuid"], 2)
        assert service.get_workflow_task(task["uuid"])["control_status"] == "active"

        assert executor.listener is not None
        executor.listener(retry["job_id"], True, {"prepared": True}, "normal", {})
        downstream = _wait_until(
            lambda: executor.dispatched[1] if len(executor.dispatched) > 1 else None,
            description="下游节点派发",
        )
        assert (downstream["node_run_uuid"], downstream["attempt_no"]) == (second_run["uuid"], 1)
        executor.listener(downstream["job_id"], True, {"measured": 42}, "normal", {})

        final = _wait_until(
            lambda: (
                current
                if (current := service.get_workflow_task(task["uuid"]))["status"] == "succeeded"
                else None
            ),
            description="任务到达 succeeded",
        )
        assert final["control_status"] == "active"
        assert final["output"][first_run["workflow_node_uuid"]]["return_value"] == {"prepared": True}
        assert final["output"][second_run["workflow_node_uuid"]]["return_value"] == {"measured": 42}
        view = service.get_workflow_node_run(first_run["uuid"])
        assert [(a["attempt_no"], a["status"]) for a in view["attempts"]] == [(1, "failed"), (2, "succeeded")]
        assert scheduler.list_reconciliation_decisions() == []
    finally:
        scheduler.stop()
        service.close()


def test_error_decisions_api_merges_executor_and_reconciliation_decisions(
    service: WorkflowService,
) -> None:
    class _ExecutionBackend:
        def __init__(self) -> None:
            self.resolved: list[tuple[str, dict]] = []

        def list_error_decisions(self) -> list[dict]:
            return [{"decision_id": "exec-1", "job_id": "job-x", "device_id": "pump-2", "options": [{"action": "abort"}]}]

        def resolve_error_decision(self, decision_id: str, decision: dict) -> bool:
            self.resolved.append((decision_id, decision))
            return decision_id == "exec-1"

    executor = _Executor()
    scheduler = BackendScheduler(service, executor)  # type: ignore[arg-type]
    backend = _ExecutionBackend()
    task, _run = _crashed_task(service)
    _task, run, _runner = _adopt(scheduler, service, task["uuid"])
    client = TestClient(create_backend_app(lambda: scheduler, lambda: backend))

    listed = ok_data(client.get("/api/v1/error-decisions"))["decisions"]
    assert [item["decision_id"] for item in listed][0] == "exec-1"
    (reconciliation,) = [item for item in listed if item["job_id"] == run["current_job_uuid"]]
    assert reconciliation["exception_type"] == "ExecutionStateUnknown"

    # 调度器持有的裁决由调度器收敛；其余仍交给执行面
    # V1：放行未被接受（不在等待 / 被拒绝）即 HTTP 404，经信封换算为该路由口径的"不存在"
    rejected = client.post(f"/api/v1/error-decisions/{reconciliation['decision_id']}", json={"action": "fallback"})
    error_of(rejected, 4001)
    resolved = client.post(
        f"/api/v1/error-decisions/{reconciliation['decision_id']}",
        json={"action": "operator_intervention", "result": {"moved": 5}, "reason": "已人工核对"},
    )
    assert ok_data(resolved) == {"decision_id": reconciliation["decision_id"], "status": "delivered"}
    assert service.get_workflow_node_run(run["uuid"])["status"] == "succeeded"
    assert backend.resolved == []

    forwarded = client.post("/api/v1/error-decisions/exec-1", json={"action": "abort"})
    ok_data(forwarded)
    assert backend.resolved[0][0] == "exec-1"
    assert ok_data(client.get("/api/v1/error-decisions"))["decisions"] == backend.list_error_decisions()


def test_error_decisions_api_works_without_execution_backend(service: WorkflowService) -> None:
    """--role backend：没有执行面也要能列出并收敛调度器的裁决。"""

    scheduler = BackendScheduler(service, _Executor())  # type: ignore[arg-type]
    task, _run = _crashed_task(service)
    _task, run, _runner = _adopt(scheduler, service, task["uuid"])
    client = TestClient(create_backend_app(lambda: scheduler, lambda: None))

    (item,) = ok_data(client.get("/api/v1/error-decisions"))["decisions"]
    ok_data(client.post(f"/api/v1/error-decisions/{item['decision_id']}", json={"action": "skip"}))
    assert service.get_workflow_node_run(run["uuid"])["status"] == "skipped"
    assert ok_data(client.get("/api/v1/error-decisions"))["decisions"] == []
    # 既无执行面也无调度器：依赖暂不可用 5001（原先的 HTTP 503）
    error_of(TestClient(create_backend_app(lambda: None, lambda: None)).get("/api/v1/error-decisions"), 5001)
