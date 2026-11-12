"""V1 任务生命周期合同：创建 DTO（priority / inventory_bindings）、列表与详情、命令、作业投影。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.api_envelope import error_of, ok_data
from tests.server.v1_lifecycle_support import Stack, wait_for


@pytest.fixture
def stack():
    value = Stack()
    yield value
    value.close()


def _binding(**overrides):
    return {
        "requirement_key": "buffer",
        "inventory_type": "reagent",
        "inventory_uuid": str(uuid4()),
        "reserved_quantity": 2.5,
        "quantity_unit": "mL",
        **overrides,
    }


def test_create_accepts_v1_priority_and_inventory_bindings(stack: Stack) -> None:
    binding = _binding()
    task = stack.create_task(["device-a"], priority="high", inventory_bindings=[binding],
                             description="样品 1", meta_data={"sample_id": "S1"})
    assert task["priority"] == "high"
    assert task["inventory_bindings"] == [binding]
    assert task["execution_mode"] == "normal" and task["dispatch_gate"] == "active"
    assert task["wait_reason"] == {} and task["description"] == "样品 1"
    detail = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}"))
    assert detail["priority"] == "high" and detail["inventory_bindings"] == [binding]
    # 默认值
    plain = stack.create_task(["device-b"])
    assert plain["priority"] == "normal" and plain["inventory_bindings"] == []


@pytest.mark.parametrize("body", [
    {"priority": "urgent"},
    {"inventory_bindings": [_binding(inventory_type="sample")]},
    {"inventory_bindings": [_binding(reserved_quantity=0)]},
    {"inventory_bindings": [_binding(inventory_uuid="not-a-uuid")]},
    {"inventory_bindings": [_binding(quantity_unit=" ")]},
    {"inventory_bindings": [_binding(), _binding()]},
])
def test_create_rejects_invalid_v1_fields(stack: Stack, body) -> None:
    workflow = stack.create_workflow(["device-a"])
    response = stack.client.post("/api/v1/workflow-tasks", json={"workflow_uuid": workflow["uuid"], **body})
    error_of(response, 1000)


def test_idempotent_create_fingerprints_v1_fields(stack: Stack) -> None:
    workflow = stack.create_workflow(["device-a"])
    body = {"workflow_uuid": workflow["uuid"], "idempotency_key": "submit-1", "priority": "high"}
    first = ok_data(stack.client.post("/api/v1/workflow-tasks", json=body), status=201)
    again = ok_data(stack.client.post("/api/v1/workflow-tasks", json=body), status=201)
    assert again["uuid"] == first["uuid"]
    error_of(stack.client.post("/api/v1/workflow-tasks", json={**body, "priority": "normal"}), 3003)


def test_list_filters_by_execution_kind_and_reports_has_more(stack: Stack) -> None:
    stack.create_task(["device-a"])
    stack.create_task(["device-b"])
    page = ok_data(stack.client.get("/api/v1/workflow-tasks", params={"page_size": 1}))
    assert page["total"] == 2 and page["has_more"] is True and len(page["items"]) == 1
    assert "output" not in page["items"][0]
    workflow_only = ok_data(stack.client.get("/api/v1/workflow-tasks", params={"execution_kind": "workflow"}))
    assert workflow_only["total"] == 2
    ad_hoc = ok_data(stack.client.get("/api/v1/workflow-tasks", params={"execution_kind": "ad_hoc_device_action"}))
    assert ad_hoc["total"] == 0 and ad_hoc["has_more"] is False
    error_of(stack.client.get("/api/v1/workflow-tasks", params={"execution_kind": "debug"}), 1000)


def test_jobs_carry_topological_index_and_executor_kind(stack: Stack) -> None:
    task = stack.create_task(["device-a", "device-b"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    jobs = ok_data(stack.client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs"))
    assert [job["topological_index"] for job in jobs] == [0, 1]
    assert {job["executor_kind"] for job in jobs} == {"device_action"}
    first = jobs[0]
    assert first["attempt"] == 1 and first["current_attempt"] is True
    assert first["start_state"] == "START_COMMITTED" and jobs[1]["start_state"] == "NOT_STARTED"
    assert first["execution_source"] == "workflow" and isinstance(first["execution_policy"], dict)
    single = ok_data(stack.client.get(f"/api/v1/workflow-node-jobs/{first['uuid']}"))
    assert single["topological_index"] == 0 and single["executor_kind"] == "device_action"
    error_of(stack.client.get(f"/api/v1/workflow-node-jobs/{uuid4()}"), 3002)


def test_command_response_is_the_v1_command_projection(stack: Stack) -> None:
    task = stack.create_task(["device-a", "device-b"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    response = stack.command(task, "pause", key="k-1", description="换样", meta_data={"source": "unilabos-frontend"})
    command = ok_data(response, status=201)
    assert set(command) >= {
        "uuid", "create_time", "update_time", "meta_data", "workflow_task_uuid", "type",
        "idempotency_key", "status", "result", "trace_context",
    }
    assert command["type"] == "pause" and command["idempotency_key"] == "k-1"
    assert command["description"] == "换样" and command["meta_data"] == {"source": "unilabos-frontend"}
    error_of(stack.client.post(f"/api/v1/workflow-tasks/{task['uuid']}/commands",
                               json={"type": "pause", "idempotency_key": "x", "unknown": 1}), 1000)
    error_of(stack.command({"uuid": str(uuid4())}, "pause"), 3002)


def test_unlock_resources_requires_confirmation_and_abnormal_terminal(stack: Stack) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    # 必须确认现场安全并填写说明
    error_of(stack.command(task, "unlock_resources"), 1000)
    error_of(stack.command(task, "unlock_resources", description="现场已清理"), 1000)
    body = {"description": "现场已清理", "meta_data": {"confirmed_physical_safe": True}}
    running = ok_data(stack.command(task, "unlock_resources", **body), status=201)
    assert running["status"] == "rejected" and running["result"]["reason"] == "task_is_not_abnormal_terminal"
    stack.executor.finish(0, success=False)
    wait_for(lambda: stack.task(task)["status"] == "failed")
    unlocked = ok_data(stack.command(task, "unlock_resources", **body), status=201)
    assert unlocked["status"] == "succeeded", unlocked
    assert set(unlocked["result"]["released"]) == {"claims", "intervals", "device_tenancies"}
    with stack.service.write_lock:
        active = stack.service.connection.execute(
            "SELECT COUNT(*) FROM workflow_execution_claim WHERE task_uuid=? AND state!='released'",
            (task["uuid"],),
        ).fetchone()[0]
    assert active == 0
