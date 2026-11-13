"""V1 任务生命周期契约测试的公共夹具：内存 runtime.db + 真实 BackendScheduler + 假执行器。"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.services.runtime.workflow.service import WorkflowService


class FakeExecutor:
    """记录派发并按测试指令回报完成的执行器。"""

    def __init__(self) -> None:
        self.dispatched: List[Dict[str, Any]] = []
        self.callback = None

    def add_job_finished_listener(self, callback) -> None:
        self.callback = callback

    def dispatch(self, payload) -> None:
        self.dispatched.append(payload)

    def cancel_task(self, task_uuid):
        return []

    def finish(self, index: int, *, success: bool = True) -> None:
        self.callback(self.dispatched[index]["job_id"], success, {"index": index}, "normal", {})


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class Stack:
    def __init__(self, *, with_scheduler: bool = True) -> None:
        self.service = WorkflowService(":memory:")
        self.executor = FakeExecutor()
        self.scheduler: Optional[BackendScheduler] = None
        if with_scheduler:
            self.scheduler = BackendScheduler(self.service, self.executor)
            self.service.set_task_submitter(self.scheduler.submit)
        self.app = create_workflow_app(self.service)
        self.client = TestClient(self.app)

    def close(self) -> None:
        self.client.close()
        self.service.set_task_submitter(None)
        if self.scheduler is not None:
            self.scheduler.stop()
        self.service.close()

    def create_workflow(self, devices, *, parallel: bool = False) -> Dict[str, Any]:
        workflow = self.client.post("/api/v1/workflows", json={"name": "生命周期测试"}).json()["data"]
        nodes: List[Dict[str, Any]] = []
        for index, device in enumerate(devices):
            nodes.append({
                "uuid": str(uuid4()), "name": f"动作{index}", "type": "device_action",
                "material_uuid": str(uuid4()), "action_name": "record", "action_type": "UniLabJsonCommand",
                "param": {"index": index}, "pose": {"x": index, "y": 0},
                "execution_policy": {"depends_on": [] if parallel or not nodes else [nodes[-1]["uuid"]]},
                "meta_data": {"target_device_id": device},
            })
        saved = self.client.put(
            f"/api/v1/workflows/{workflow['uuid']}/graph",
            json={"revision": workflow["revision"], "nodes": nodes, "edges": [], "site_binding_mode": "preserve"},
        )
        assert saved.json()["code"] == 0, saved.text
        workflow["nodes"] = nodes
        return workflow

    def create_task(self, devices, *, mode: str = "normal", parallel: bool = False, **extra) -> Dict[str, Any]:
        workflow = self.create_workflow(devices, parallel=parallel)
        response = self.client.post(
            "/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"], "run_mode": mode, **extra}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["code"] == 0, body
        task = body["data"]
        task["_workflow"] = workflow
        return task

    def command(self, task, kind: str, *, key: Optional[str] = None, **extra):
        return self.client.post(
            f"/api/v1/workflow-tasks/{task['uuid']}/commands",
            json={"type": kind, "idempotency_key": key or str(uuid4()), **extra},
        )

    def task(self, task) -> Dict[str, Any]:
        return self.service.get_workflow_task(task["uuid"])


__all__ = ["FakeExecutor", "Stack", "wait_for"]
