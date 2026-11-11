"""运行请求不能丢弃客户端以为已生效的字段。"""
import pytest
from fastapi.testclient import TestClient
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowService

@pytest.mark.parametrize("unsupported", [
    {"priority": "high"}, {"priority": "normal"},
    {"inventory_bindings": [{"material_uuid": "vial-1"}]},
    {"inventory_bindings": []}, {"expected_revison": 2},
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
