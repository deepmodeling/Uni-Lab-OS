"""cancel：不再派发；已下发的动作收到取消请求，锁保持到设备真实返回。"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.registry.action_policy import SUCCESS_TYPE_CANCELLATION
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService


class Executor:
    def __init__(self):
        self.dispatched = []
        self.cancel_requests = []

    def add_job_finished_listener(self, callback):
        self.callback = callback

    def dispatch(self, payload):
        self.dispatched.append(payload)

    def cancel_job(self, job_uuid):
        self.cancel_requests.append(job_uuid)

    def cancel_task(self, task_uuid):
        return []

    def finish(self, index, *, success=True, cancelled=False):
        suc_type = SUCCESS_TYPE_CANCELLATION if cancelled else "normal"
        self.callback(self.dispatched[index]["job_id"], success and not cancelled, {"index": index}, suc_type, {})


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
    workflow = client.post("/api/v1/workflows", json={"name": "取消测试"}).json()["data"]
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


def command(client, task, kind):
    revision = client.get(f"/api/v1/workflow-tasks/{task['uuid']}").json()["data"]["control_revision"]
    return client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json={
        "type": kind, "expected_revision": revision, "idempotency_key": str(uuid4()),
    })


def accepted(response):
    """V1：命令受理 HTTP 201，返回命令投影。"""

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["code"] == 0 and body["data"]["type"] == "cancel", body
    return body["data"]


def status(service, task):
    return service.get_workflow_task(task["uuid"])["status"]


def test_cancel_keeps_the_device_locked_until_the_running_action_returns(stack):
    service, executor, scheduler, client = stack
    first = create_task(client, devices=["shared-device"])
    wait_for(lambda: len(executor.dispatched) == 1)
    second = create_task(client, devices=["shared-device"])
    wait_for(lambda: len(scheduler._waiting_resource_jobs) + len(scheduler._waiting_interval_jobs) == 1)

    cancel = accepted(command(client, first, "cancel"))
    assert cancel["status"] == "succeeded" and cancel["result"]["status"] == "canceling"
    assert status(service, first) == "canceling"
    first_job = executor.dispatched[0]["job_id"]
    wait_for(lambda: executor.cancel_requests == [first_job])
    assert service.get_workflow_node_job(first_job)["status"] == "cancel_requested"

    # 设备还没返回：锁不让出，第二个任务继续等
    time.sleep(0.1)
    assert len(executor.dispatched) == 1
    assert status(service, first) == "canceling"

    executor.finish(0, cancelled=True)
    wait_for(lambda: status(service, first) == "canceled")
    wait_for(lambda: len(executor.dispatched) == 2)
    assert executor.dispatched[1]["task_id"] == second["uuid"]
    executor.finish(1)
    wait_for(lambda: status(service, second) == "succeeded")


def test_cancel_finishes_at_once_when_nothing_is_running(stack):
    service, executor, scheduler, client = stack
    holder = create_task(client, devices=["shared-device"])
    wait_for(lambda: len(executor.dispatched) == 1)
    queued = create_task(client, devices=["shared-device", "other-device"])
    wait_for(lambda: len(scheduler._waiting_resource_jobs) + len(scheduler._waiting_interval_jobs) == 1)

    accepted(command(client, queued, "cancel"))
    wait_for(lambda: status(service, queued) == "canceled")
    assert not scheduler._waiting_resource_jobs and not scheduler._waiting_interval_jobs
    assert executor.cancel_requests == []
    assert {run["status"] for run in service.list_workflow_node_runs(queued["uuid"])} == {"canceled"}

    executor.finish(0)
    wait_for(lambda: status(service, holder) == "succeeded")
    time.sleep(0.05)
    assert len(executor.dispatched) == 1


def test_cancel_a_paused_step_task(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a", "device-b"], mode="step")
    accepted(command(client, task, "cancel"))
    wait_for(lambda: status(service, task) == "canceled")
    time.sleep(0.05)
    assert executor.dispatched == []


def test_failure_drains_running_siblings_before_releasing_their_device(stack):
    service, executor, scheduler, client = stack
    task = create_task(client, devices=["device-a", "device-b"], parallel=True)
    wait_for(lambda: len(executor.dispatched) == 2)
    by_device = {payload["device_id"]: index for index, payload in enumerate(executor.dispatched)}
    waiting = create_task(client, devices=["device-b"])
    wait_for(lambda: len(scheduler._waiting_resource_jobs) + len(scheduler._waiting_interval_jobs) == 1)

    executor.finish(by_device["device-a"], success=False)
    sibling = executor.dispatched[by_device["device-b"]]["job_id"]
    wait_for(lambda: sibling in executor.cancel_requests)
    # 兄弟动作仍在 device-b 上执行：任务未收尾，device-b 不让给下一个任务
    time.sleep(0.1)
    assert status(service, task) == "running"
    assert len(executor.dispatched) == 2

    executor.finish(by_device["device-b"], cancelled=True)
    wait_for(lambda: status(service, task) == "failed")
    wait_for(lambda: len(executor.dispatched) == 3)
    assert executor.dispatched[2]["task_id"] == waiting["uuid"]
    executor.finish(2)
    wait_for(lambda: status(service, waiting) == "succeeded")


def test_cancel_conflicts(stack):
    service, executor, _, client = stack
    task = create_task(client, devices=["device-a"])
    wait_for(lambda: len(executor.dispatched) == 1)
    accepted(command(client, task, "cancel"))
    # 取消中再取消：幂等受理，不重复请求设备取消
    again = accepted(command(client, task, "cancel"))
    assert again["status"] == "succeeded" and again["result"]["status"] == "canceling"
    time.sleep(0.05)
    assert len(executor.cancel_requests) == 1
    executor.finish(0, cancelled=True)
    wait_for(lambda: status(service, task) == "canceled")
    # 终态任务：业务拒绝（status=rejected），不是接口错误
    final = accepted(command(client, task, "cancel"))
    assert final["status"] == "rejected" and final["result"]["reason"] == "task_is_terminal"
