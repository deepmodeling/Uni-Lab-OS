"""Edge UI and unified Backend Provider contracts without ROS."""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import ok_data
from unilabos.server.api.runtime import create_backend_router
from unilabos.server.backend.execution import JobExecutionBackend
from unilabos.server.backend.incidents import StatusIncidentManager
from unilabos.server.api.runtime.workflow import install_workflow_api
from unilabos.protocol.runtime.workflow import WorkflowNodeWrite
from unilabos.server.services.runtime.workflow.service import WorkflowService


def test_workflow_runtime_read_routes_keep_empty_and_not_found_semantics():
    service = WorkflowService(":memory:")
    workflow = service.create_workflow(
        name="runtime read routes",
        tags=[],
        description=None,
        meta_data={},
    )
    node = WorkflowNodeWrite(
        uuid=str(uuid4()),
        name="人工确认",
        type="manual_confirm",
    )
    service.save_graph(
        workflow["uuid"],
        revision=workflow["revision"],
        nodes=[node],
        edges=[],
    )
    task = service.create_workflow_task(
        workflow_uuid=workflow["uuid"],
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    job = service.list_workflow_node_jobs(task["uuid"])[0]

    app = FastAPI()
    install_workflow_api(app, service)
    client = TestClient(app)

    task_paths = (
        f"/api/v1/workflow-tasks/{task['uuid']}/manual-confirmations",
        f"/api/v1/workflow-tasks/{task['uuid']}/interventions",
    )
    job_paths = (
        f"/api/v1/workflow-node-jobs/{job['uuid']}/results",
        f"/api/v1/workflow-node-jobs/{job['uuid']}/feedback-history",
    )
    for path in (*task_paths, *job_paths):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"code": 0, "data": []}
        invalid_window = client.get(path, params={"limit": 0})
        assert invalid_window.status_code == 200
        assert invalid_window.json()["code"] == 1000

    missing = client.get(f"/api/v1/workflow-node-jobs/{uuid4()}/results")
    assert missing.status_code == 200
    assert missing.json()["code"] == 3002


def test_embedded_backend_scheduler_marks_health_local():
    app = FastAPI()
    app.include_router(
        create_backend_router(
            lambda: object(),
            lambda: object(),
        )
    )

    response = TestClient(app).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "scheduler": "local",
        "execution": "ready",
    }


def test_status_incident_rest_contract_uses_backend_execution_service():
    manager = StatusIncidentManager()
    incident = manager.observe(
        "heater-1",
        "operation_mode",
        "Error",
        {
            "normal_values": ["Idle", "Running"],
            "incidents": {
                "Error": {
                    "code": "heater.operation.error",
                    "message": "加热器进入错误状态",
                    "hold": True,
                }
            },
        },
        now=100.0,
    )
    assert incident is not None

    class Backend:
        status_incidents = manager

        @staticmethod
        def host_ready() -> bool:
            return True

        @staticmethod
        def list_error_decisions():
            return []

    app = FastAPI()
    app.include_router(create_backend_router(lambda: None, lambda: Backend()))
    client = TestClient(app)

    snapshot = ok_data(client.get("/api/v1/status-incidents"))
    assert snapshot["host_ready"] is True
    assert snapshot["incidents"][0]["incident_id"] == incident["incident_id"]
    assert snapshot["holds"][0]["hold_token"] == incident["hold_token"]

    decision = client.post(
        f"/api/v1/status-incidents/{incident['incident_id']}",
        json={"action": "resume", "reason": "现场已恢复"},
    )
    assert ok_data(decision) == {
        "incident_id": incident["incident_id"],
        "status": "delivered",
        "state": "resolved",
    }
    assert manager.holds() == []


def test_status_policy_replacement_clears_old_hold_before_opening_new_one():
    manager = StatusIncidentManager()
    events: list[str] = []
    manager.add_listener(lambda event: events.append(event["type"]))
    policy = {
        "incidents": {
            "Warning": {"code": "device.warning", "hold": True},
            "Error": {"code": "device.error", "hold": True},
        }
    }

    warning = manager.observe("device-1", "mode", "Warning", policy, now=1.0)
    error = manager.observe("device-1", "mode", "Error", policy, now=2.0)

    assert warning is not None and error is not None
    assert error["policy_id"] == "device.error"
    assert events[-2:] == [
        "status_incident_cleared",
        "status_incident_required",
    ]
    assert manager.holds()[0]["incident_id"] == error["incident_id"]


def test_device_state_projection_runs_status_policy_evaluation():
    manager = StatusIncidentManager()
    backend = JobExecutionBackend(
        host_node_getter=lambda: object(),
        status_incidents=manager,
        status_policy_resolver=lambda device_id, prop: {
            "normal_values": ["Idle"],
            "incidents": {
                "Error": {
                    "code": f"{device_id}.{prop}.error",
                    "hold": True,
                }
            },
        },
    )

    changed = backend.report_device_properties("pump-1", {"mode": "Error"})

    assert changed == {"mode": False}
    incidents = manager.list()
    assert incidents[0]["policy_id"] == "pump-1.mode.error"
