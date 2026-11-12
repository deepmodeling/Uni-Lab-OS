"""调度权威重启：遗留在飞 attempt 在裁决前继续占着设备，后来的任务排队。"""

from __future__ import annotations

import time
from unittest import mock
from uuid import uuid4

from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.dag.runner import TaskDagRunner
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


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def create_task(client, device: str) -> dict:
    workflow = client.post("/api/v1/workflows", json={"name": "重启锁"}).json()["data"]
    node = {
        "uuid": str(uuid4()), "name": "动作", "type": "device_action",
        "material_uuid": str(uuid4()), "action_name": "record", "action_type": "UniLabJsonCommand",
        "param": {"index": 0}, "pose": {"x": 0, "y": 0}, "execution_policy": {"depends_on": []},
        "meta_data": {"target_device_id": device},
    }
    saved = client.put(f"/api/v1/workflows/{workflow['uuid']}/graph", json={
        "revision": workflow["revision"], "nodes": [node], "edges": [], "site_binding_mode": "preserve",
    })
    assert saved.json()["code"] == 0, saved.text
    response = client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"]})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_recovered_attempt_keeps_the_device_until_it_is_reconciled(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    with TestClient(create_workflow_app(service)) as client:
        first = create_task(client, "shared-device")
        wait_for(lambda: len(executor.dispatched) == 1)
    # 模拟进程在动作执行中崩溃：不回报结果，也不走优雅停止里的取消
    # （优雅停止会取消 runner，事件循环若先跑完收尾就把任务记成 canceled，那就不是崩溃了）
    service.set_task_submitter(None)
    with mock.patch.object(TaskDagRunner, "cancel", lambda self: None):
        scheduler.stop()
    service.close()

    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    try:
        scheduler.start(recover=True)
        wait_for(lambda: len(scheduler.list_reconciliation_decisions()) == 1)
        decision = scheduler.list_reconciliation_decisions()[0]
        assert decision["task_id"] == first["uuid"]

        with TestClient(create_workflow_app(service)) as client:
            second = create_task(client, "shared-device")
            # 遗留 attempt 仍占着 shared-device：第二个任务排队，不派发。
            # 跨任务的设备独占由持久资源区间准入先行裁决，排队发生在准入等待集合，
            # 尚未进入内存锁队列。
            wait_for(lambda: len(scheduler._waiting_interval_jobs) == 1)
            time.sleep(0.1)
            assert executor.dispatched == []
            held = [item for item in scheduler.resource_snapshot().intervals
                    if item["lock_key"] == "/devices/shared-device" and item["state"] == "held"]
            assert [(item["task_uuid"], item["acquired_job_uuid"]) for item in held] == [
                (first["uuid"], decision["job_id"])
            ]

            assert scheduler.resolve_reconciliation_decision(decision["decision_id"], {"action": "abort"})
            wait_for(lambda: len(executor.dispatched) == 1)
            assert executor.dispatched[0]["task_id"] == second["uuid"]
            executor.finish(0)
            wait_for(lambda: service.get_workflow_task(second["uuid"])["status"] == "succeeded")
        assert service.get_workflow_task(first["uuid"])["status"] == "failed"
    finally:
        service.set_task_submitter(None)
        scheduler.stop()
        service.close()
