"""HTTP 提交 → 持久化单点许可 → 原 DAG/attempt：步进不是另起一份单点任务。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
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

    def finish(self, index, *, retry=False, success=True):
        info = {"error_resolution": {"selected_action": "retry"}} if retry else {}
        self.callback(self.dispatched[index]["job_id"], success, {"index": index}, "normal", info)


def wait_for(predicate):
    deadline = time.monotonic() + 5
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


def create_task(client, *, mode="step", parallel=False, count=3):
    workflow = client.post("/api/v1/workflows", json={"name": "单点许可测试"}).json()["data"]
    nodes = []
    for index in range(count):
        nodes.append({
            "uuid": str(uuid4()), "name": f"动作{index}", "type": "device_action",
            "material_uuid": str(uuid4()), "action_name": "record", "action_type": "UniLabJsonCommand",
            "param": {"index": index}, "pose": {"x": index, "y": 0},
            "execution_policy": {"depends_on": [] if parallel or not nodes else [nodes[-1]["uuid"]]},
            "meta_data": {"target_device_id": f"device-{index}"},
        })
    saved = client.put(f"/api/v1/workflows/{workflow['uuid']}/graph",
                       # 该调度器单测不安装物料/注册表权威，不走程序化 Site 解析。
                       json={"revision": workflow["revision"], "nodes": nodes, "edges": [], "site_binding_mode": "preserve"})
    assert saved.json()["code"] == 0, saved.text
    response = client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"], "run_mode": mode})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def command(client, task, kind="step", *, revision=None, key=None):
    if revision is None:
        revision = client.get(f"/api/v1/workflow-tasks/{task['uuid']}").json()["data"]["control_revision"]
    return client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json={
        "type": kind, "expected_revision": revision, "idempotency_key": key or str(uuid4()),
    })


@pytest.mark.parametrize("parallel", [False, True])
def test_one_click_one_attempt_and_no_locks_for_waiting_nodes(stack, parallel):
    service, executor, scheduler, client = stack
    task = create_task(client, parallel=parallel)
    assert task["control_status"] == "paused"
    time.sleep(0.08)
    assert executor.dispatched == []
    for index in range(3):
        assert command(client, task).status_code == 200
        wait_for(lambda: len(executor.dispatched) == index + 1)
        # 同一动作尚未完成时，连点也不能积攒许可。
        assert command(client, task).json()["code"] == 3003
        time.sleep(0.03)
        assert len(executor.dispatched) == index + 1
        executor.finish(index)
        wait_for(lambda: service.get_workflow_task(task["uuid"])["control_status"] == "paused")
        time.sleep(0.03)
        assert len(executor.dispatched) == index + 1
        # 未获许可的分支没有申请动作锁。
        assert not scheduler._waiting_resource_jobs
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    assert {p["task_id"] for p in executor.dispatched} == {task["uuid"]}
    assert all(run["attempt_count"] == 1 for run in service.list_workflow_node_runs(task["uuid"]))
    assert command(client, task).json()["code"] == 3003


def test_two_tabs_and_replayed_command_do_not_grant_twice(stack):
    service, executor, _, client = stack
    task = create_task(client, parallel=True)
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda key: command(client, task, revision=0, key=key), ["a", "b"]))
    assert sorted(r.json()["code"] for r in responses) == [0, 3003]
    winning_key = "a" if responses[0].json()["code"] == 0 else "b"
    wait_for(lambda: len(executor.dispatched) == 1)
    executor.finish(0)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["control_status"] == "paused")
    assert command(client, task, revision=0, key=winning_key).status_code == 200
    assert command(client, task, revision=0, key="stale-page").json()["code"] == 3003
    assert command(client, task, "resume", revision=0, key=winning_key).json()["code"] == 3003
    time.sleep(0.05)
    assert len(executor.dispatched) == 1


@pytest.mark.parametrize("during_step", [False, True])
def test_resume_continues_original_dag_with_normal_parallelism(stack, during_step):
    service, executor, _, client = stack
    task = create_task(client, parallel=True)
    if during_step:
        command(client, task)
        wait_for(lambda: len(executor.dispatched) == 1)
    assert command(client, task, "resume").json()["data"]["run_mode"] == "normal"
    wait_for(lambda: len(executor.dispatched) == 3)
    for index in range(3):
        executor.finish(index)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
    assert service.get_workflow_task(task["uuid"])["control_status"] == "active"


def test_retry_is_a_new_attempt_requiring_another_step(stack):
    service, executor, _, client = stack
    task = create_task(client, count=1)
    command(client, task)
    wait_for(lambda: len(executor.dispatched) == 1)
    executor.finish(0, success=False, retry=True)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["control_status"] == "paused")
    time.sleep(0.05)
    assert len(executor.dispatched) == 1
    command(client, task)
    wait_for(lambda: len(executor.dispatched) == 2)
    assert executor.dispatched[1]["node_run_uuid"] == executor.dispatched[0]["node_run_uuid"]
    assert executor.dispatched[1]["attempt_no"] == 2
    executor.finish(1)
    wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")


def test_normal_mode_is_unchanged_and_invalid_controls_are_rejected(stack):
    _, executor, _, client = stack
    task = create_task(client, mode="normal", parallel=True)
    wait_for(lambda: len(executor.dispatched) == 3)
    assert command(client, task).json()["code"] == 3003
    for data in [{}, {"type": "step", "expected_revision": True, "idempotency_key": "x"},
                 {"type": "retry", "expected_revision": 0, "idempotency_key": "x"}]:
        response = client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands", json=data)
        assert response.json()["code"] == 1000
    for index in range(3):
        executor.finish(index)


@pytest.mark.parametrize("terminal", ["succeeded", "failed"])
def test_restart_after_last_attempt_settles_paused_task(terminal):
    service = WorkflowService(":memory:")
    try:
        with TestClient(create_workflow_app(service)) as client:
            task = create_task(client, count=1)
            command(client, task)
            run = service.list_workflow_node_runs(task["uuid"])[0]
            assert service.claim_task_step(task["uuid"], run["current_job_uuid"])
            service.mark_workflow_node_job_running(run["current_job_uuid"])
            service.record_workflow_node_job_terminal(run["current_job_uuid"], status=terminal)
            assert service.get_workflow_task(task["uuid"])["control_status"] == "paused"
            # 模拟 attempt 已提交而 runner 收尾尚未执行就重启。
            recovered = service.prepare_workflow_task_execution(task["uuid"])
            assert recovered["state"] == "terminal"
            assert service.get_workflow_task(task["uuid"])["status"] == terminal
    finally:
        service.close()


@pytest.mark.parametrize("inflight", [False, True])
def test_restart_preserves_step_progress_without_replaying_devices(tmp_path, inflight):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    with TestClient(create_workflow_app(service)) as client:
        task = create_task(client)
        command(client, task, key="before-restart")
        wait_for(lambda: len(executor.dispatched) == 1)
        if not inflight:
            executor.finish(0)
            wait_for(lambda: service.get_workflow_task(task["uuid"])["control_status"] == "paused")
        service.set_task_submitter(None)
        scheduler.stop()
    service.close()

    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    service.set_task_submitter(scheduler.submit)
    try:
        scheduler.start(recover=True)
        with TestClient(create_workflow_app(service)) as client:
            if inflight:
                wait_for(lambda: service.get_workflow_task(task["uuid"])["control_status"] == "waiting_reconciliation")
                assert command(client, task, "resume").json()["code"] == 3003
                assert service.list_workflow_node_runs(task["uuid"])[0]["status"] in {"execution_unknown", "intervention_required"}
            else:
                assert service.get_workflow_task(task["uuid"])["control_status"] == "paused"
                assert command(client, task, revision=0, key="before-restart").json()["code"] == 0
                time.sleep(0.05)
                assert executor.dispatched == []
                assert command(client, task).json()["code"] == 0
                wait_for(lambda: len(executor.dispatched) == 1)
                assert executor.dispatched[0]["action_args"] == {"index": 1}
            if inflight:
                time.sleep(0.05)
                assert executor.dispatched == []
    finally:
        service.set_task_submitter(None)
        scheduler.stop()
        service.close()
