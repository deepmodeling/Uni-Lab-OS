"""V1 任务观测合同：展示投影、删除资格与软删除、单步候选、执行锁、运行日志、等待图。"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from tests.api_envelope import error_of, ok_data
from tests.server.v1_lifecycle_support import Stack, wait_for


@pytest.fixture
def stack():
    value = Stack()
    yield value
    value.close()


def test_presentations_embed_compact_jobs_and_matrix_window(stack: Stack) -> None:
    running = stack.create_task(["device-a", "device-b"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    page = ok_data(stack.client.get("/api/v1/workflow-task-presentations"))
    assert set(page) >= {"items", "total", "page", "page_size", "has_more"}
    item = next(task for task in page["items"] if task["uuid"] == running["uuid"])
    assert set(item["workflow_snapshot"]) == {"workflow"}
    assert item["workflow_snapshot"]["workflow"]["uuid"] == running["_workflow"]["uuid"]
    assert item["workflow_snapshot"]["workflow"]["name"] == "生命周期测试"
    nodes = item["execution_plan"]["nodes"]
    assert [node["name"] for node in nodes] == ["动作0", "动作1"]
    assert nodes[0]["device_id"] == "device-a" and nodes[0]["action_name"] == "record"
    assert len(item["jobs"]) == 2 and {"status", "topological_index", "executor_kind"} <= set(item["jobs"][0])
    assert "param" not in item["jobs"][0]

    matrix = ok_data(stack.client.get("/api/v1/workflow-task-presentations",
                                      params={"view": "matrix", "terminal_limit": 0}))
    assert [task["uuid"] for task in matrix["items"]] == [running["uuid"]]
    error_of(stack.client.get("/api/v1/workflow-task-presentations",
                              params={"view": "matrix", "status": "running"}), 1000)
    error_of(stack.client.get("/api/v1/workflow-task-presentations", params={"view": "board"}), 1000)
    error_of(stack.client.get("/api/v1/workflow-task-presentations",
                              params={"view": "matrix", "terminal_limit": 101}), 1000)


def test_delete_eligibility_and_soft_delete(stack: Stack) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    eligibility = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/delete-eligibility"))
    assert eligibility["can_delete"] is False and eligibility["already_deleted"] is False
    assert any("终态" in blocker for blocker in eligibility["delete_blockers"])
    error_of(stack.client.delete(f"/api/v1/workflow-tasks/{task['uuid']}"), 3003)

    stack.executor.finish(0)
    wait_for(lambda: stack.task(task)["status"] == "succeeded")
    eligibility = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/delete-eligibility"))
    assert eligibility == {
        "workflow_task_uuid": task["uuid"], "can_delete": True, "already_deleted": False,
        "delete_blockers": [], "status": "succeeded", "cleanup_status": eligibility["cleanup_status"],
        "deleted_at": None,
    }
    deleted = ok_data(stack.client.delete(f"/api/v1/workflow-tasks/{task['uuid']}"))
    assert deleted["status"] == "deleted" and deleted["workflow_task_uuid"] == task["uuid"]
    again = ok_data(stack.client.delete(f"/api/v1/workflow-tasks/{task['uuid']}"))
    assert again["status"] == "already_deleted" and again["deleted_at"] == deleted["deleted_at"]
    error_of(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}"), 3002)
    assert ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/delete-eligibility"))["already_deleted"]
    # 软删除只隐藏任务，作业与运行历史保留
    with stack.service.write_lock:
        jobs = stack.service.connection.execute(
            "SELECT COUNT(*) FROM workflow_node_job WHERE workflow_task_uuid=? AND deleted_at IS NULL", (task["uuid"],),
        ).fetchone()[0]
    assert jobs == 1
    error_of(stack.client.delete(f"/api/v1/workflow-tasks/{uuid4()}"), 3002)


def test_step_state_lists_ready_candidates_only_when_paused(stack: Stack) -> None:
    task = stack.create_task(["device-a", "device-b", "device-c"], mode="step", parallel=True)
    state = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/step-state"))
    assert state["execution_mode"] == "step" and state["control_status"] == "paused"
    assert state["can_step"] is True and state["requires_selection"] is True
    assert len(state["candidates"]) == 3
    assert set(state["candidates"][0]) == {"node_uuid", "name", "kind", "device_id", "action_name"}
    target = state["candidates"][2]["node_uuid"]
    ok_data(stack.command(task, "step", target_node_uuid=target), status=201)
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    busy = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/step-state"))
    assert busy["in_flight_job_count"] == 1 and busy["can_step"] is False and busy["candidates"] == []
    stack.executor.finish(0)
    wait_for(lambda: stack.task(task)["control_status"] == "paused")
    after = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/step-state"))
    assert target not in {item["node_uuid"] for item in after["candidates"]} and len(after["candidates"]) == 2


def test_execution_locks_list_and_force_release_with_cas(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    locks = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks"))
    assert locks["task_status"] == "running" and len(locks["locks"]) == 1
    lease = locks["locks"][0]
    assert lease["can_release"] is False and "失败、取消或超时" in lease["release_block_reason"]
    assert {"uuid", "lock_key", "claim_uuid", "fencing_token", "state", "workflow_node_job_uuid"} <= set(lease)
    body = {"expected_claim_uuid": lease["claim_uuid"], "expected_fencing_token": lease["fencing_token"],
            "reason": "现场已核实", "physical_settlement_confirmed": True}
    url = f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks/{lease['uuid']}/force-release"
    error_of(stack.client.post(url, json=body), 3003)
    error_of(stack.client.post(url, json={**body, "physical_settlement_confirmed": "yes"}), 1000)
    error_of(stack.client.post(url, json={**body, "expected_fencing_token": 0}), 1000)

    # 失败作业遗留的 Claim（模拟终态时未能释放；Claim 现与终态同事务经 release_terminal 释放）才可以人工释放
    from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims

    monkeypatch.setattr(ExecutionClaims, "release_terminal", classmethod(lambda cls, conn, job_uuid: False))
    stack.executor.finish(0, success=False)
    wait_for(lambda: stack.task(task)["status"] == "failed")
    locks = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks"))
    assert len(locks["locks"]) == 1
    lease = locks["locks"][0]
    assert lease["can_release"] is True and lease["release_block_reason"] is None
    error_of(stack.client.post(url, json={**body, "expected_fencing_token": lease["fencing_token"] + 1}), 3003)
    error_of(stack.client.post(url, json={**body, "physical_settlement_confirmed": False}), 3003)
    released = ok_data(stack.client.post(url, json=body))
    assert released["status"] == "released" and released["claim_uuid"] == lease["claim_uuid"]
    assert ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/execution-locks"))["locks"] == []
    assert ok_data(stack.client.post(url, json=body))["status"] == "already_released"


def test_task_events_page_with_after_sequence_cursor(stack: Stack) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    stack.executor.finish(0)
    wait_for(lambda: stack.task(task)["status"] == "succeeded")
    first = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/events", params={"limit": "2"}))
    assert len(first["items"]) == 2 and first["has_more"] is True
    assert first["next_cursor"] == first["items"][-1]["sequence"]
    rest = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/events",
                                    params={"after_sequence": str(first["next_cursor"])}))
    sequences = [item["sequence"] for item in first["items"] + rest["items"]]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    kinds = {item["kind"] for item in first["items"] + rest["items"]}
    assert "job_transition" in kinds
    terminal = [item for item in rest["items"] if item["kind"] == "job_transition" and item.get("to_status") == "succeeded"]
    assert terminal and "return_info" in terminal[-1]
    for params in ({"after_sequence": "-1"}, {"limit": "0"}, {"limit": "501"}, {"after_sequence": "x"}):
        error_of(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/events", params=params), 1000)


def test_wait_graph_reports_resource_waiters(stack: Stack) -> None:
    graph = ok_data(stack.client.get("/api/v1/workflow-runtime/wait-graph"))
    assert graph == {"nodes": [], "edges": [], "cycles": [], "deadlock_detected": False}
    holder = stack.create_task(["shared-device"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    waiting = stack.create_task(["shared-device"])
    wait_for(lambda: len(stack.scheduler._waiting_resource_jobs) + len(stack.scheduler._waiting_interval_jobs) == 1)
    time.sleep(0.05)
    graph = ok_data(stack.client.get("/api/v1/workflow-runtime/wait-graph"))
    assert set(graph) == {"nodes", "edges", "cycles", "deadlock_detected"}
    assert graph["deadlock_detected"] is False
    for node in graph["nodes"]:
        assert node["task_uuid"] == waiting["uuid"] and node["deadlock_cycle"] is False
    stack.executor.finish(0)
    wait_for(lambda: len(stack.executor.dispatched) == 2)
    stack.executor.finish(1)
    wait_for(lambda: stack.task(waiting)["status"] == "succeeded")
    assert stack.task(holder)["status"] == "succeeded"


def test_material_events_filter_the_materials_ledger_by_task_jobs(stack: Stack, monkeypatch) -> None:
    from types import SimpleNamespace

    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    job_uuid = stack.executor.dispatched[0]["job_id"]

    def change(sequence, job):
        return SimpleNamespace(sequence=sequence, event_uuid=f"e{sequence}", operation="move_material",
                               aggregate_type="material", aggregate_uuid="vial-1", delta={"to": "site-2"},
                               occurred_at_ms=1_700_000_000_000, actor_type="device", job_uuid=job)

    ledger = [change(1, job_uuid), change(2, "other-job"), change(3, None)]

    class Materials:
        def changes(self, *, after_sequence=0, limit=100):
            return [item for item in ledger if item.sequence > after_sequence][:limit]

    monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: Materials())
    events = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/material-events"))
    assert [event["sequence"] for event in events] == [1]
    event = events[0]
    assert event["kind"] == "move_material" and event["material_uuid"] == "vial-1"
    assert event["workflow_node_job_uuid"] == job_uuid and event["node_name"] == "动作0"
    assert event["occurred_at"].endswith("Z") and event["step_path"] == []
    monkeypatch.setattr("unilabos.server.backend.composition.get_materials_service", lambda: None)
    assert ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/material-events")) == []
    error_of(stack.client.get(f"/api/v1/workflow-tasks/{uuid4()}/material-events"), 3002)
