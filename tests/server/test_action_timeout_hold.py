"""硬超时后锁不放（ADR 0008，差异核查 SC-N5）。

硬超时是工作流的自动继续规则：attempt 照常按超时结算，任务按决策链 / 超时策略继续；
但 Python 杀不掉同步驱动线程，所以该 attempt 的动作 / 物料锁、执行 Claim、资源区间和设备托管
都继续由它持有。只有驱动真实返回（迟到结果，不改已结算的 attempt）或操作员强制释放才放开。
其间需要这些锁的后续作业（同任务后续节点、超时节点的重试、其他任务）等待而不是失败，并在
``/api/v1/scheduler/resources`` 里以超时的 attempt 为阻塞者；持有状态跨调度权威重启保留。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend import execution as execution_module
from unilabos.server.backend.execution import JobExecutionBackend
from unilabos.server.backend.execution_queue import QueueItem
from unilabos.server.backend.scheduler.dag.runner import TaskDagRunner
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService

DEVICE = "device-1"


def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class Adapter:
    """最小执行适配器：``slow`` 声明 0.1 s 硬超时，结果由测试手动回报（模拟杀不掉的同步驱动）。"""

    def __init__(self) -> None:
        self._action_value_mappings = {DEVICE: {"slow": {"timeout": 0.1}, "quick": {}}}
        self.sent: List[QueueItem] = []
        self.cancelled: List[str] = []

    def send_goal(self, item: QueueItem, **_kwargs: Any) -> None:
        self.sent.append(item)

    def cancel_goal(self, job_id: str) -> None:
        self.cancelled.append(job_id)

    def item(self, job_id: str) -> QueueItem:
        return next(item for item in self.sent if item.job_id == job_id)


class Stack:
    """本机拓扑：真实执行面 + 真实调度权威 + 文件 runtime.db。"""

    def __init__(self, path) -> None:
        self.path = path
        self.service = WorkflowService(path)
        self.adapter = Adapter()
        self.backend = JobExecutionBackend(host_node_getter=lambda: self.adapter)
        self.backend.start()
        self.scheduler = BackendScheduler(self.service, self.backend)
        self.backend.result_bridges.append(self.scheduler)
        self.service.set_task_submitter(self.scheduler.submit)
        self.client = TestClient(create_workflow_app(self.service))

    def close(self) -> None:
        self.client.close()
        self.service.set_task_submitter(None)
        self.scheduler.stop()
        self.backend.stop()
        self.service.close()

    def create_task(self, actions: List[str]) -> Dict[str, Any]:
        workflow = ok_data(self.client.post("/api/v1/workflows", json={"name": "超时持锁"}))
        nodes: List[Dict[str, Any]] = []
        for index, action in enumerate(actions):
            nodes.append({
                "uuid": str(uuid4()), "name": f"{action}{index}", "type": "device_action",
                "material_uuid": str(uuid4()), "action_name": action, "action_type": "UniLabJsonCommand",
                "param": {}, "pose": {"x": index, "y": 0},
                "execution_policy": {"depends_on": [nodes[-1]["uuid"]] if nodes else []},
                "meta_data": {"target_device_id": DEVICE},
            })
        ok_data(self.client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={
            "revision": workflow["revision"], "nodes": nodes, "edges": [], "site_binding_mode": "preserve",
        }))
        task = ok_data(self.client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"]}),
                       status=201)
        task["_nodes"] = nodes
        return task

    def task(self, task) -> Dict[str, Any]:
        return self.service.get_workflow_task(task["uuid"])

    def job(self, job_uuid: str) -> Dict[str, Any]:
        return self.service.get_workflow_node_job(job_uuid)

    def jobs(self, task) -> List[Dict[str, Any]]:
        return self.service.list_workflow_node_jobs(task["uuid"])

    def timed_out_decision(self) -> Dict[str, Any]:
        wait_for(lambda: any(item["exception_type"] == "TimeoutException"
                             for item in self.backend.list_error_decisions()))
        return next(item for item in self.backend.list_error_decisions()
                    if item["exception_type"] == "TimeoutException")

    def decide(self, decision: Dict[str, Any], action: str, **extra: Any) -> None:
        assert self.backend.resolve_error_decision(
            decision["decision_id"], {"action": action, "scheduler_updated": True, **extra}
        )

    def claim_state(self, job_uuid: str) -> str:
        with self.service.write_lock:
            row = self.service.connection.execute(
                "SELECT state FROM workflow_execution_claim WHERE job_uuid=?", (job_uuid,)
            ).fetchone()
        return row["state"]

    def uses(self, job_uuid: str) -> int:
        with self.service.write_lock:
            return self.service.connection.execute(
                "SELECT COUNT(*) FROM workflow_resource_use WHERE job_uuid=?", (job_uuid,)
            ).fetchone()[0]

    def waiting(self, job_uuid: str):
        snapshot = self.scheduler.resource_snapshot().model_dump(mode="json", exclude_none=True)
        return [item for item in snapshot["requests"] if item["owner_uuid"] == job_uuid
                and item["status"] == "waiting"]

    def held_request(self, job_uuid: str):
        snapshot = self.scheduler.resource_snapshot().model_dump(mode="json", exclude_none=True)
        return [item for item in snapshot["requests"] if item["owner_uuid"] == job_uuid
                and item["status"] == "held"]

    def return_late(self, job_uuid: str, status: str = "success") -> None:
        """驱动线程终于返回（真实结果）。"""

        self.backend.publish_job_status(
            {}, self.adapter.item(job_uuid), status, {"suc": status == "success", "return_value": {"late": True}}
        )


@pytest.fixture(autouse=True)
def fast_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_module, "HARD_TIMEOUT_GRACE_SECONDS", 0.0)


@pytest.fixture
def stack(tmp_path):
    value = Stack(tmp_path / "runtime.db")
    yield value
    value.close()


def test_hard_timeout_continues_workflow_and_later_node_waits_for_the_held_device(stack: Stack) -> None:
    task = stack.create_task(["slow", "quick"])
    decision = stack.timed_out_decision()
    timed_out = decision["job_id"]
    assert stack.adapter.cancelled == [timed_out]
    # 决策链：人工替换结果后工作流继续，attempt 结算为成功（结果来自人工）
    stack.decide(decision, "operator_intervention", result={"manual": True})
    wait_for(lambda: stack.job(timed_out)["status"] == "succeeded")
    held = stack.job(timed_out)
    assert held["control_data"]["timeout_hold"]["state"] == "held"
    assert held["control_data"]["timeout_hold"]["kind"] == "timeout"

    # 锁没有释放：执行 Claim、动作使用权、调度器内存锁都仍归超时的 attempt
    assert stack.claim_state(timed_out) == "timed_out"
    assert stack.uses(timed_out) == 1
    assert stack.held_request(timed_out)
    locks = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks"))
    (lease,) = locks["locks"]
    assert lease["state"] == "timed_out" and lease["workflow_node_job_uuid"] == timed_out
    assert lease["can_release"] is True

    # 同任务的下一个节点需要同一台设备：等待而不是失败，阻塞者是超时的 attempt
    next_job = next(job["uuid"] for job in stack.jobs(task) if job["uuid"] != timed_out)
    wait_for(lambda: stack.waiting(next_job))
    (waiting,) = stack.waiting(next_job)
    assert waiting["blockers"] == [timed_out]
    time.sleep(0.1)
    assert [item.job_id for item in stack.adapter.sent] == [timed_out]
    assert stack.task(task)["status"] == "running"

    # 驱动真实返回：记录迟到结果，不改已结算的 attempt；锁随之释放，等待的节点派发
    stack.return_late(timed_out, "failed")
    wait_for(lambda: len(stack.adapter.sent) == 2)
    settled = stack.job(timed_out)
    assert settled["status"] == "succeeded"
    assert settled["return_info"]["return_value"] == {"manual": True}
    assert settled["control_data"]["late_outcome"]["status"] == "failed"
    assert settled["control_data"]["timeout_hold"]["state"] == "released"
    assert settled["control_data"]["timeout_hold"]["release_reason"] == "late_outcome"
    assert stack.claim_state(timed_out) == "released"
    assert stack.uses(timed_out) == 0
    assert stack.adapter.sent[1].job_id == next_job
    stack.backend.publish_job_status({}, stack.adapter.sent[1], "success", {"suc": True, "return_value": {}})
    wait_for(lambda: stack.task(task)["status"] == "succeeded")


def test_retry_of_timed_out_node_waits_until_the_driver_returns(stack: Stack) -> None:
    task = stack.create_task(["slow"])
    decision = stack.timed_out_decision()
    timed_out = decision["job_id"]
    stack.decide(decision, "retry")
    wait_for(lambda: len(stack.jobs(task)) == 2)
    retry = next(job["uuid"] for job in stack.jobs(task) if job["uuid"] != timed_out)
    first = stack.job(timed_out)
    assert first["status"] == "failed" and first["error_info"][0]["code"] == "action_timeout"
    assert stack.claim_state(timed_out) == "timed_out"
    # 重试的新 attempt 等待原 attempt 的设备，而不是派发或失败
    wait_for(lambda: stack.waiting(retry))
    assert stack.waiting(retry)[0]["blockers"] == [timed_out]
    time.sleep(0.1)
    assert [item.job_id for item in stack.adapter.sent] == [timed_out]
    assert stack.task(task)["status"] == "running"

    stack.return_late(timed_out, "success")
    wait_for(lambda: len(stack.adapter.sent) == 2)
    assert stack.adapter.sent[1].job_id == retry
    # 迟到的成功不会把已结算的超时 attempt 改回成功
    assert stack.job(timed_out)["status"] == "failed"
    assert stack.job(timed_out)["control_data"]["late_outcome"]["status"] == "success"
    stack.backend.publish_job_status({}, stack.adapter.sent[1], "success", {"suc": True, "return_value": {}})
    wait_for(lambda: stack.task(task)["status"] == "succeeded")


def test_force_release_frees_a_held_attempt_while_its_retry_waits(stack: Stack) -> None:
    task = stack.create_task(["slow"])
    decision = stack.timed_out_decision()
    timed_out = decision["job_id"]
    stack.decide(decision, "retry")
    wait_for(lambda: len(stack.jobs(task)) == 2)
    retry = next(job["uuid"] for job in stack.jobs(task) if job["uuid"] != timed_out)
    wait_for(lambda: stack.waiting(retry))

    locks = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks"))
    (lease,) = locks["locks"]
    # 任务仍在运行（重试在等这把锁），超时持有的锁照样可以人工释放
    assert locks["task_status"] == "running" and lease["state"] == "timed_out" and lease["can_release"] is True
    url = f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks/{lease['uuid']}/force-release"
    body = {"expected_claim_uuid": lease["claim_uuid"], "expected_fencing_token": lease["fencing_token"],
            "reason": "已到现场确认设备停止", "physical_settlement_confirmed": True}
    # 前置条件仍然生效：未确认现场安全、Claim / Fence CAS 不一致都拒绝
    error_of(stack.client.post(url, json={**body, "physical_settlement_confirmed": False}), 3003)
    error_of(stack.client.post(url, json={**body, "expected_fencing_token": lease["fencing_token"] + 1}), 3003)
    error_of(stack.client.post(url, json={**body, "expected_claim_uuid": str(uuid4())}), 3003)
    assert stack.claim_state(timed_out) == "timed_out"

    released = ok_data(stack.client.post(url, json=body))
    assert released["status"] == "released" and released["workflow_node_job_uuid"] == timed_out
    wait_for(lambda: len(stack.adapter.sent) == 2)
    assert stack.adapter.sent[1].job_id == retry
    hold = stack.job(timed_out)["control_data"]["timeout_hold"]
    assert hold["state"] == "force_released" and hold["release_reason"] == "已到现场确认设备停止"
    assert stack.uses(timed_out) == 0

    # 释放之后驱动才返回：只记录迟到结果，不再碰任何锁（重试的 attempt 仍持有设备）
    stack.return_late(timed_out, "success")
    wait_for(lambda: "late_outcome" in stack.job(timed_out)["control_data"])
    assert stack.claim_state(retry) == "running"
    assert stack.held_request(retry)
    stack.backend.publish_job_status({}, stack.adapter.sent[1], "success", {"suc": True, "return_value": {}})
    wait_for(lambda: stack.task(task)["status"] == "succeeded")


def test_abort_fails_task_but_keeps_device_until_force_release(stack: Stack) -> None:
    task = stack.create_task(["slow"])
    decision = stack.timed_out_decision()
    timed_out = decision["job_id"]
    stack.decide(decision, "abort")
    wait_for(lambda: stack.task(task)["status"] == "failed")
    assert stack.claim_state(timed_out) == "timed_out"

    other = stack.create_task(["quick"])
    other_job = stack.jobs(other)[0]["uuid"]
    wait_for(lambda: stack.waiting(other_job))
    assert stack.waiting(other_job)[0]["blockers"] == [timed_out]
    # 任务已终态但锁未放，删除资格同样被挡住
    eligibility = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/delete-eligibility"))
    assert eligibility["can_delete"] is False

    (lease,) = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks"))["locks"]
    url = f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks/{lease['uuid']}/force-release"
    ok_data(stack.client.post(url, json={
        "expected_claim_uuid": lease["claim_uuid"], "expected_fencing_token": lease["fencing_token"],
        "reason": "现场核实设备已停止", "physical_settlement_confirmed": True,
    }))
    wait_for(lambda: len(stack.adapter.sent) == 2)
    assert stack.adapter.sent[1].job_id == other_job


def test_cancel_during_timeout_decision_settles_now_and_keeps_locks(stack: Stack) -> None:
    task = stack.create_task(["slow"])
    decision = stack.timed_out_decision()
    timed_out = decision["job_id"]
    response = stack.client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json={
        "type": "cancel", "idempotency_key": str(uuid4()),
    })
    assert response.status_code == 201, response.text
    # 驱动还没返回，任务也不卡在 canceling：attempt 按取消结算，锁继续持有
    wait_for(lambda: stack.task(task)["status"] == "canceled")
    assert stack.job(timed_out)["status"] == "canceled"
    assert stack.claim_state(timed_out) == "timed_out"
    assert stack.backend.list_error_decisions() == []
    stack.return_late(timed_out, "canceled")
    wait_for(lambda: stack.claim_state(timed_out) == "released")


def test_late_result_before_decision_settles_without_hold(stack: Stack) -> None:
    """驱动在决策放行前就返回：没有需要持有的锁，迟到结果随终态记录。"""

    task = stack.create_task(["slow"])
    decision = stack.timed_out_decision()
    timed_out = decision["job_id"]
    stack.return_late(timed_out, "success")
    stack.decide(decision, "abort")
    wait_for(lambda: stack.task(task)["status"] == "failed")
    job = stack.job(timed_out)
    assert job["status"] == "failed"
    assert job["control_data"]["late_outcome"]["status"] == "success"
    assert "timeout_hold" not in job["control_data"]
    assert stack.claim_state(timed_out) == "released"
    assert stack.uses(timed_out) == 0


# ── 调度权威重启 ────────────────────────────────────────────────


class Executor:
    """调度权威侧的假执行器（远端 Host 在另一个进程里，回报按测试指令到达）。"""

    def __init__(self) -> None:
        self.dispatched: List[Dict[str, Any]] = []
        self.callback = None

    def add_job_finished_listener(self, callback) -> None:
        self.callback = callback

    def dispatch(self, payload) -> None:
        self.dispatched.append(payload)

    def cancel_task(self, task_uuid):
        return []

    def time_out(self, index: int) -> None:
        """Host 的硬超时闸门放行了 abort 决策，驱动仍在跑。"""

        self.callback(self.dispatched[index]["job_id"], False, None, "normal", {
            "error_info": {"exception_type": "TimeoutException", "error_message": "timeout", "timeout_seconds": 1},
            "error_resolution": {"selected_action": "abort", "scheduler_updated": True},
            "timeout_hold": {"kind": "timeout", "timeout_seconds": 1, "timed_out_at": time.time(),
                             "reason": "driver_still_running"},
        })

    def finish(self, index: int) -> None:
        self.callback(self.dispatched[index]["job_id"], True, {}, "normal", {})


def open_authority(path):
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    client = TestClient(create_workflow_app(service))
    return SimpleNamespace(service=service, executor=executor, scheduler=scheduler, client=client)


def crash(authority) -> None:
    authority.client.close()
    authority.service.set_task_submitter(None)
    with mock.patch.object(TaskDagRunner, "cancel", lambda self: None):
        authority.scheduler.stop()
    authority.service.close()


def create_simple_task(client, device: str) -> Dict[str, Any]:
    workflow = ok_data(client.post("/api/v1/workflows", json={"name": "重启持锁"}))
    node = {
        "uuid": str(uuid4()), "name": "动作", "type": "device_action", "material_uuid": str(uuid4()),
        "action_name": "record", "action_type": "UniLabJsonCommand", "param": {}, "pose": {"x": 0, "y": 0},
        "execution_policy": {"depends_on": []}, "meta_data": {"target_device_id": device},
    }
    ok_data(client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={
        "revision": workflow["revision"], "nodes": [node], "edges": [], "site_binding_mode": "preserve",
    }))
    return ok_data(client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"]}), status=201)


def test_timeout_hold_survives_authority_restart_until_the_late_result(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    first = open_authority(path)
    task = create_simple_task(first.client, "shared-device")
    wait_for(lambda: len(first.executor.dispatched) == 1)
    timed_out = first.executor.dispatched[0]["job_id"]
    first.executor.time_out(0)
    wait_for(lambda: first.service.get_workflow_task(task["uuid"])["status"] == "failed")
    crash(first)

    second = open_authority(path)
    try:
        second.scheduler.start(recover=True)
        # 重启后照旧持有：持久 Claim 仍是 timed_out，内存锁按原申请恢复
        snapshot = second.scheduler.resource_snapshot()
        assert [(item.owner_uuid, item.status) for item in snapshot.requests] == [(timed_out, "held")]
        other = create_simple_task(second.client, "shared-device")
        wait_for(lambda: len(second.scheduler._waiting_interval_jobs) == 1)
        waiting = [item for item in second.scheduler.resource_snapshot().requests
                   if item.task_uuid == other["uuid"] and item.status == "waiting"]
        assert len(waiting) == 1 and waiting[0].blockers == [timed_out]
        time.sleep(0.1)
        assert second.executor.dispatched == []

        # 驱动真实返回（Host 一直在跑，事件在新权威上到达）：锁释放，等待的任务派发
        assert second.scheduler.publish_job_late_outcome({"job_id": timed_out, "status": "success",
                                                          "return_info": {"return_value": {}}})
        wait_for(lambda: len(second.executor.dispatched) == 1)
        assert second.executor.dispatched[0]["task_id"] == other["uuid"]
        job = second.service.get_workflow_node_job(timed_out)
        assert job["status"] == "failed" and job["control_data"]["late_outcome"]["status"] == "success"
        second.executor.finish(0)
        wait_for(lambda: second.service.get_workflow_task(other["uuid"])["status"] == "succeeded")
    finally:
        crash(second)


def test_timeout_hold_survives_authority_restart_until_force_release(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    first = open_authority(path)
    task = create_simple_task(first.client, "shared-device")
    wait_for(lambda: len(first.executor.dispatched) == 1)
    timed_out = first.executor.dispatched[0]["job_id"]
    first.executor.time_out(0)
    wait_for(lambda: first.service.get_workflow_task(task["uuid"])["status"] == "failed")
    crash(first)

    second = open_authority(path)
    try:
        second.scheduler.start(recover=True)
        other = create_simple_task(second.client, "shared-device")
        wait_for(lambda: len(second.scheduler._waiting_interval_jobs) == 1)
        (lease,) = ok_data(second.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks"))["locks"]
        assert lease["state"] == "timed_out" and lease["can_release"] is True
        url = f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks/{lease['uuid']}/force-release"
        ok_data(second.client.post(url, json={
            "expected_claim_uuid": lease["claim_uuid"], "expected_fencing_token": lease["fencing_token"],
            "reason": "重启后核实设备已停止", "physical_settlement_confirmed": True,
        }))
        wait_for(lambda: len(second.executor.dispatched) == 1)
        assert second.executor.dispatched[0]["task_id"] == other["uuid"]
        assert [item.owner_uuid for item in second.scheduler.resource_snapshot().requests
                if item.status == "held"] == [second.executor.dispatched[0]["job_id"]]
    finally:
        crash(second)


# ── 默认两进程拓扑：Host 执行面 → runtime.v1 事件 → 调度权威 ─────────────


class _HistoryPayloads:
    """权威经控制面读 Host 的 history 载荷（这里直接读同一个 HistoryService）。"""

    def __init__(self, history) -> None:
        self.history = history

    def fetch_json(self, payload_uuid: str) -> Any:
        import json

        payload = self.history.get_payload(payload_uuid)
        return json.loads(payload.inline_payload.decode(payload.encoding))


def test_split_topology_reports_the_hold_and_the_late_outcome_over_runtime_v1(tmp_path) -> None:
    from tests.server.test_workflow_business_coordinator import _DataPlane, _document, _execute_payload
    from unilabos.server.backend.coordinator import WorkflowBusinessCoordinator
    from unilabos.server.backend.edge_control import EdgeControlService
    from unilabos.server.services.history import HistoryService
    from unilabos.server.services.runtime import RuntimeService

    runtime = RuntimeService(tmp_path / "edge-runtime.db")
    history = HistoryService(tmp_path / "edge-history.db")
    adapter = Adapter()
    adapter._action_value_mappings = {"pump-1": {"transfer": {}}}
    backend = JobExecutionBackend(host_node_getter=lambda: adapter)
    data_plane = _DataPlane()
    coordinator = WorkflowBusinessCoordinator(
        runtime, history, backend, endpoint_uuid="hostlink:edge-1", transport="hostlink",
        host_uuid="edge-1", instance_name="host", data_plane=data_plane,
    )
    backend.result_bridges.append(coordinator)
    backend.start()
    authority = EdgeControlService(payload_client=_HistoryPayloads(history))
    finished: List[tuple] = []
    late: List[Dict[str, Any]] = []
    authority.add_job_finished_listener(lambda *args: finished.append(args))
    authority.result_bridges.append(SimpleNamespace(publish_job_late_outcome=lambda report: late.append(report)))

    def deliver() -> None:
        for notice in coordinator.claim_edge_changes():
            authority.handle_message("edge_change", notice.model_dump(mode="json"))

    try:
        # 调度权威下发时已解析硬超时 0.1 s
        notice, document = _document("execute-1", 1, "execute_job", "payload-execute-1",
                                     {**_execute_payload(), "timeout_seconds": 0.1}, job_uuid="job-1")
        data_plane.documents["execute-1"] = document
        coordinator.handle_backend_notice(notice)
        wait_for(lambda: runtime.get_execution_job("job-1").status == "terminal_waiting")
        assert adapter.cancelled == ["job-1"]
        (decision,) = backend.list_error_decisions()
        assert decision["exception_type"] == "TimeoutException"

        release, release_document = _document("release-1", 2, "release_failed", "payload-release-1", {
            "decision_uuid": decision["decision_id"], "confirmed_scheduler_revision": 8,
            "adapter_command_uuid": "adapter-release-1", "selected_action": "abort", "reason": "超时策略",
        }, job_uuid="job-1")
        data_plane.documents["release-1"] = release_document
        coordinator.handle_backend_notice(release)
        wait_for(lambda: runtime.get_execution_job("job-1").status == "failed")
        deliver()
        (terminal,) = [args for args in finished if args[0] == "job-1"]
        assert terminal[1] is False
        hold = terminal[4]["timeout_hold"]
        assert hold["kind"] == "timeout" and hold["reason"] == "driver_still_running"
        assert late == []

        # 驱动终于返回：Host 不改已终态的 execution_job，只发 execution.late_outcome
        backend.publish_job_status({}, adapter.item("job-1"), "success", {"suc": True, "return_value": {"v": 1}})
        assert backend.wait_idle()
        assert runtime.get_execution_job("job-1").status == "failed"
        deliver()
        (report,) = late
        assert report["job_id"] == "job-1" and report["status"] == "success"
        assert report["return_info"]["return_value"] == {"v": 1}
        assert len(finished) == 1
    finally:
        backend.stop()
        runtime.close()
        history.close()
