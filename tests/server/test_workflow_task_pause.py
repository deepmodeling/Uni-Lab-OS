"""pause：自动运行中的任务转单步并关上派发闸门；已下发的动作照常跑完。"""

from __future__ import annotations

import time
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_envelope import ok_data
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


def command(client, task, kind, *, key=None):
    revision = client.get(f"/api/v1/workflow-tasks/{task['uuid']}").json()["data"]["control_revision"]
    return client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json={
        "type": kind, "expected_revision": revision, "idempotency_key": key or str(uuid4()),
    })


def task_of(service, task):
    return service.get_workflow_task(task["uuid"])


def test_pause_lets_the_running_action_finish_then_stops_dispatching(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b", "device-c"])
    wait_for(lambda: len(executor.dispatched) == 1)

    paused = command(client, task, "pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["data"]["run_mode"] == "step"
    assert paused.json()["data"]["control_status"] == "paused"

    # 已下发的动作照常完成；后继不再派发。
    executor.finish(0)
    time.sleep(0.1)
    assert len(executor.dispatched) == 1
    assert task_of(service, task)["status"] == "running"

    # 暂停后可以逐个放行，也可以恢复自动。
    assert command(client, task, "step").status_code == 200
    wait_for(lambda: len(executor.dispatched) == 2)
    executor.finish(1)
    wait_for(lambda: task_of(service, task)["control_status"] == "paused")
    assert command(client, task, "resume").status_code == 200
    wait_for(lambda: len(executor.dispatched) == 3)
    executor.finish(2)
    wait_for(lambda: task_of(service, task)["status"] == "succeeded")


def test_pause_withdraws_a_node_queued_for_a_lock(stack):
    service, executor, scheduler, client = stack
    first = create_task(client, devices=["shared-device"])
    wait_for(lambda: len(executor.dispatched) == 1)
    second = create_task(client, devices=["shared-device"])
    wait_for(lambda: len(scheduler._waiting_resource_jobs) + len(scheduler._waiting_interval_jobs) == 1)

    assert command(client, second, "pause").status_code == 200
    # 排队等锁的 attempt 退出队列，不再占着位置
    wait_for(lambda: not scheduler._waiting_resource_jobs and not scheduler._waiting_interval_jobs)

    executor.finish(0)
    wait_for(lambda: task_of(service, first)["status"] == "succeeded")
    time.sleep(0.1)
    assert len(executor.dispatched) == 1, "暂停的任务拿到锁也不能派发"

    assert command(client, second, "step").status_code == 200
    wait_for(lambda: len(executor.dispatched) == 2)
    assert executor.dispatched[1]["task_id"] == second["uuid"]
    executor.finish(1)
    wait_for(lambda: task_of(service, second)["status"] == "succeeded")


def test_pause_during_an_executing_step_takes_effect_when_it_finishes(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b"], mode="step")
    assert command(client, task, "step").status_code == 200
    wait_for(lambda: len(executor.dispatched) == 1)

    paused = command(client, task, "pause")
    assert paused.status_code == 200, paused.text
    # 已认领且已下发的那一步照常跑完，期间控制态仍为 active
    assert paused.json()["data"]["control_status"] == "active"
    executor.finish(0)
    wait_for(lambda: task_of(service, task)["control_status"] == "paused")
    time.sleep(0.1)
    assert len(executor.dispatched) == 1


def test_pause_conflicts_and_idempotent_replay(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b"])
    wait_for(lambda: len(executor.dispatched) == 1)

    assert command(client, task, "pause", key="pause-1").status_code == 200
    revision = task_of(service, task)["control_revision"]
    replay = client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json={
        "type": "pause", "expected_revision": revision - 1, "idempotency_key": "pause-1",
    })
    assert replay.status_code == 200, replay.text
    assert task_of(service, task)["control_revision"] == revision
    # 已暂停再暂停是冲突
    assert command(client, task, "pause").json()["code"] == 3003

    executor.finish(0)
    assert command(client, task, "resume").status_code == 200
    wait_for(lambda: len(executor.dispatched) == 2)
    executor.finish(1)
    wait_for(lambda: task_of(service, task)["status"] == "succeeded")
    # 任务结束后不再接受暂停
    assert command(client, task, "pause").json()["code"] == 3003


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
