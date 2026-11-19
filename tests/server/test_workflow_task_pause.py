"""pause：自动运行中的任务转单步并关上派发闸门；已下发的动作照常跑完。"""

from __future__ import annotations

import time
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.api.runtime.diagnostics import create_backend_app
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService


class Executor:
    def __init__(self):
        self.dispatched = []

    def add_job_finished_listener(self, callback):
        self.callback = callback

    def dispatch(self, payload):
        self.dispatched.append(payload)

    def cancel_task(self, task_uuid):
        return []

    def finish(self, index, *, success=True):
        self.callback(self.dispatched[index]["job_id"], success, {"index": index}, "normal", {})


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture
def stack():
    service = WorkflowService(":memory:")
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    with TestClient(create_workflow_app(service)) as client:
        yield service, executor, scheduler, client
    service.set_task_submitter(None)
    scheduler.stop()
    service.close()


def create_task(client, *, devices, mode="normal", parallel=False):
    workflow = client.post("/api/v1/workflows", json={"name": "暂停测试"}).json()["data"]
    nodes = []
    for index, device in enumerate(devices):
        nodes.append({
            "uuid": str(uuid4()), "name": f"动作{index}", "type": "device_action",
            "material_uuid": str(uuid4()), "action_name": "record", "action_type": "UniLabJsonCommand",
            "param": {"index": index}, "pose": {"x": index, "y": 0},
            "execution_policy": {"depends_on": [] if parallel or not nodes else [nodes[-1]["uuid"]]},
            "meta_data": {"target_device_id": device},
        })
    saved = client.put(f"/api/v1/workflows/{workflow['uuid']}/graph",
                       json={"revision": workflow["revision"], "nodes": nodes, "edges": [],
                             "site_binding_mode": "preserve"})
    assert saved.json()["code"] == 0, saved.text
    response = client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"], "run_mode": mode})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def command(client, task, kind, *, key=None, **extra):
    """V1 合同：命令不再要求 expected_revision；成功受理返回 HTTP 201 + 命令投影。"""

    return client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json={
        "type": kind, "idempotency_key": key or str(uuid4()), **extra,
    })


def accepted(response, status="succeeded"):
    command = ok_data(response, status=201)
    assert command["status"] == status, command
    return command


def rejected(response, reason):
    command = ok_data(response, status=201)
    assert command["status"] == "rejected" and command["result"]["reason"] == reason, command
    return command


def task_of(service, task):
    return service.get_workflow_task(task["uuid"])


def test_pause_lets_the_running_action_finish_then_stops_dispatching(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b", "device-c"])
    wait_for(lambda: len(executor.dispatched) == 1)

    paused = accepted(command(client, task, "pause"))
    assert paused["type"] == "pause" and paused["workflow_task_uuid"] == task["uuid"]
    # V1：pause 只关派发闸门，执行模式仍是普通
    current = task_of(service, task)
    assert (current["execution_mode"], current["dispatch_gate"], current["control_status"]) == (
        "normal", "paused", "paused",
    )

    # 已下发的动作照常完成；后继不再派发。
    executor.finish(0)
    time.sleep(0.1)
    assert len(executor.dispatched) == 1
    assert task_of(service, task)["status"] == "running"

    # 暂停的普通任务可以单步（先切到单步再放行一个节点），也可以恢复自动。
    accepted(command(client, task, "step"), status="pending")
    wait_for(lambda: len(executor.dispatched) == 2)
    assert task_of(service, task)["execution_mode"] == "step"
    executor.finish(1)
    wait_for(lambda: task_of(service, task)["control_status"] == "paused")
    accepted(command(client, task, "resume"))
    wait_for(lambda: len(executor.dispatched) == 3)
    executor.finish(2)
    wait_for(lambda: task_of(service, task)["status"] == "succeeded")


def test_pause_then_resume_reopens_the_gate_in_normal_mode(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b"])
    wait_for(lambda: len(executor.dispatched) == 1)
    accepted(command(client, task, "pause"))
    executor.finish(0)
    time.sleep(0.1)
    assert len(executor.dispatched) == 1
    accepted(command(client, task, "resume"))
    current = task_of(service, task)
    assert (current["execution_mode"], current["dispatch_gate"]) == ("normal", "active")
    wait_for(lambda: len(executor.dispatched) == 2)
    executor.finish(1)
    wait_for(lambda: task_of(service, task)["status"] == "succeeded")


def test_pause_withdraws_a_node_queued_for_a_lock(stack):
    service, executor, scheduler, client = stack
    first = create_task(client, devices=["shared-device"])
    wait_for(lambda: len(executor.dispatched) == 1)
    second = create_task(client, devices=["shared-device"])
    wait_for(lambda: len(scheduler._waiting_resource_jobs) + len(scheduler._waiting_interval_jobs) == 1)

    accepted(command(client, second, "pause"))
    # 排队等锁的 attempt 退出队列，不再占着位置
    wait_for(lambda: not scheduler._waiting_resource_jobs and not scheduler._waiting_interval_jobs)

    executor.finish(0)
    wait_for(lambda: task_of(service, first)["status"] == "succeeded")
    time.sleep(0.1)
    assert len(executor.dispatched) == 1, "暂停的任务拿到锁也不能派发"

    accepted(command(client, second, "step"), status="pending")
    wait_for(lambda: len(executor.dispatched) == 2)
    assert executor.dispatched[1]["task_id"] == second["uuid"]
    executor.finish(1)
    wait_for(lambda: task_of(service, second)["status"] == "succeeded")


def test_switch_to_step_lets_the_running_action_finish_then_waits_for_steps(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b"])
    wait_for(lambda: len(executor.dispatched) == 1)

    accepted(command(client, task, "switch_to_step"))
    current = task_of(service, task)
    assert (current["execution_mode"], current["dispatch_gate"], current["control_status"]) == (
        "step", "paused", "paused",
    )
    executor.finish(0)
    time.sleep(0.1)
    assert len(executor.dispatched) == 1
    # 单步模式下 pause / switch_to_step 不再适用：业务拒绝而不是接口错误
    rejected(command(client, task, "pause"), "task_is_not_in_normal_mode")
    rejected(command(client, task, "switch_to_step"), "task_is_not_in_normal_mode")
    accepted(command(client, task, "step"), status="pending")
    wait_for(lambda: len(executor.dispatched) == 2)
    executor.finish(1)
    wait_for(lambda: task_of(service, task)["status"] == "succeeded")


def test_step_targets_one_node_and_waits_while_a_step_executes(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b"], mode="step", parallel=True)
    nodes = task_of(service, task)["execution_plan"]["nodes"]
    target = nodes[1]["uuid"]
    accepted(command(client, task, "step", target_node_uuid=target), status="pending")
    wait_for(lambda: len(executor.dispatched) == 1)
    assert executor.dispatched[0]["node_id"] == target
    # 在途单步未结束时不能再放行
    rejected(command(client, task, "step"), "task_is_not_paused_step")
    executor.finish(0)
    wait_for(lambda: task_of(service, task)["control_status"] == "paused")
    rejected(command(client, task, "step", target_node_uuid=target), "target_node_not_ready")
    error_of(command(client, task, "pause", target_node_uuid=target), 1000)


def test_pause_conflicts_and_idempotent_replay(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b"])
    wait_for(lambda: len(executor.dispatched) == 1)

    first = accepted(command(client, task, "pause", key="pause-1"))
    revision = task_of(service, task)["control_revision"]
    replay = accepted(command(client, task, "pause", key="pause-1"))
    assert replay["uuid"] == first["uuid"]
    assert task_of(service, task)["control_revision"] == revision
    # 同键不同命令：V1 按参数错误拒绝，不登记新命令
    error_of(command(client, task, "resume", key="pause-1"), 1000)
    # 可选的 expected_revision 仍是跨页面 CAS：过期时 3003 且不登记
    error_of(command(client, task, "resume", expected_revision=revision - 1), 3003)
    assert task_of(service, task)["control_revision"] == revision
    # 已暂停再暂停是幂等的空操作（V1 语义）
    accepted(command(client, task, "pause"))

    executor.finish(0)
    accepted(command(client, task, "resume", expected_revision=revision + 1))
    wait_for(lambda: len(executor.dispatched) == 2)
    executor.finish(1)
    wait_for(lambda: task_of(service, task)["status"] == "succeeded")
    # 任务结束后不再接受暂停：业务拒绝
    rejected(command(client, task, "pause"), "task_is_terminal")
    error_of(command(client, task, "bogus"), 1000)


def test_scheduler_drain_finishes_current_action_and_resumes_waiting_work(stack):
    service, executor, scheduler, client = stack
    task = create_task(client, devices=["device-a", "device-b"])
    wait_for(lambda: len(executor.dispatched) == 1)
    with TestClient(create_backend_app(lambda: scheduler, lambda: executor)) as management:
        status = ok_data(management.post("/api/v1/scheduler/drain", json={}))
        assert status["phase"] == "draining"
        assert status["active_device_job_ids"] == [executor.dispatched[0]["job_id"]]
        executor.finish(0)
        wait_for(lambda: bool(scheduler._waiting_resource_jobs))
        assert ok_data(management.get("/api/v1/scheduler/drain"))["phase"] == "drained"
        assert len(executor.dispatched) == 1
        assert task_of(service, task)["status"] == "running"
        ok_data(management.post("/api/v1/scheduler/drain/resume", json={}))
        wait_for(lambda: len(executor.dispatched) == 2)
        executor.finish(1)
        wait_for(lambda: task_of(service, task)["status"] == "succeeded")


def test_drain_observes_job_before_runtime_accepts_dispatch(stack, monkeypatch):
    service, executor, scheduler, client = stack
    entered, release = threading.Event(), threading.Event()
    original = executor.dispatch

    def blocked_dispatch(payload):
        entered.set()
        assert release.wait(timeout=5)
        original(payload)

    monkeypatch.setattr(executor, "dispatch", blocked_dispatch)
    try:
        task = create_task(client, devices=["device-a"])
        assert entered.wait(timeout=5)
        assert executor.dispatched == []
        with TestClient(create_backend_app(lambda: scheduler, lambda: executor)) as management:
            status = ok_data(management.post("/api/v1/scheduler/drain", json={}))
            assert status["phase"] == "draining"
            assert status["active_device_job_count"] == 1
            release.set()
            wait_for(lambda: len(executor.dispatched) == 1)
            executor.finish(0)
            wait_for(lambda: task_of(service, task)["status"] == "succeeded")
            assert ok_data(management.get("/api/v1/scheduler/drain"))["phase"] == "drained"
    finally:
        release.set()
