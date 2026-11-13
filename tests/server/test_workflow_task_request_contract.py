"""运行请求不能丢弃客户端以为已生效的字段。"""
import pytest
from fastapi.testclient import TestClient
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowService

# V1 合同下 priority / inventory_bindings 是整图任务的合法字段；单点设备动作给出非默认值、
# 或绑定项带 V1 之外的字段时仍拒绝（不静默丢弃）。
@pytest.mark.parametrize("unsupported", [
    {"priority": "high"}, {"priority": "urgent"},
    {"inventory_bindings": [{"material_uuid": "vial-1"}]},
    {"inventory_bindings": [{"requirement_key": "k", "inventory_type": "reagent",
                             "inventory_uuid": "7d3f5c2e-9a41-4b8e-8f1d-2c6b9e0a4d17",
                             "reserved_quantity": 1, "quantity_unit": "mL"}]},
    {"expected_revison": 2},
])
def test_reject_unsupported_fields_before_creating_task(unsupported):
    service = WorkflowService(":memory:")
    try:
        with TestClient(create_workflow_app(service)) as client:
            response = client.post("/api/v1/workflow-tasks", json={
                "execution_kind": "ad_hoc_device_action", "device_id": "reactor", "action_name": "report", "param": {}, **unsupported,
            })
            assert response.json()["code"] == 1000, response.text
            assert next(iter(unsupported)) in response.json()["error"]["msg"]
            assert service.list_workflow_tasks()["total"] == 0
    finally:
        service.close()

@pytest.mark.parametrize("defaults", [{"priority": "normal"}, {"inventory_bindings": []}])
def test_action_task_accepts_v1_defaults(defaults):
    service = WorkflowService(":memory:")
    try:
        with TestClient(create_workflow_app(service)) as client:
            response = client.post("/api/v1/workflow-tasks", json={
                "execution_kind": "ad_hoc_device_action", "device_id": "reactor", "action_name": "report",
                "param": {}, "idempotency_key": "k", **defaults,
            })
            assert response.status_code == 201 and response.json()["code"] == 0, response.text
            assert response.json()["data"]["priority"] == "normal"
    finally:
        service.close()


def test_dev_action_task_body_still_creates_idempotently():
    service = WorkflowService(":memory:")
    try:
        with TestClient(create_workflow_app(service)) as client:
            body = {"execution_kind": "ad_hoc_device_action", "device_id": "reactor", "action_name": "report", "param": {}, "idempotency_key": "same-click"}
            first = client.post("/api/v1/workflow-tasks", json=body)
            second = client.post("/api/v1/workflow-tasks", json=body)
            assert first.status_code == second.status_code == 201
            assert first.json()["data"]["uuid"] == second.json()["data"]["uuid"]
            assert service.list_workflow_tasks()["total"] == 1
    finally:
        service.close()
