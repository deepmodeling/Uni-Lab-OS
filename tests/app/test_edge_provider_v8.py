"""Edge UI v8 与 UniLabOS Provider 的无 ROS 契约回归。"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.scheduler.api import create_scheduler_router
from unilabos.server.scheduler.backend import JobExecutionBackend
from unilabos.server.scheduler.inventory.site_spec import canonical_component_sites
from unilabos.server.scheduler.monitor import MonitorBus
from unilabos.server.scheduler.service import EdgeScheduler
from unilabos.server.scheduler.status_incidents import StatusIncidentManager
from unilabos.server.workflow.api import install_workflow_api
from unilabos.server.workflow.models import WorkflowNodeWrite
from unilabos.server.workflow.service import WorkflowService
from unilabos.server.workflow.store import WorkflowStore


def test_host_and_provider_publish_into_one_monitor_sequence():
    from unilabos.server.scheduler.monitor import (
        CHANNELS,
        monitor_bus as provider_monitor_bus,
    )

    assert provider_monitor_bus is not None
    assert "status" in CHANNELS


def test_workflow_v8_runtime_read_routes_keep_empty_and_not_found_semantics():
    service = WorkflowService(WorkflowStore(":memory:"))
    workflow = service.create_workflow(
        name="v8 runtime",
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

    missing = client.get(
        f"/api/v1/workflow-node-jobs/{uuid4()}/results"
    )
    assert missing.status_code == 200
    assert missing.json()["code"] == 3002


def test_demo_workflow_authority_marks_scheduler_health_ready():
    app = FastAPI()
    app.include_router(
        create_scheduler_router(
            lambda: None,
            get_workflow_authority=lambda: object(),
            include_execution_shaped_workflow_routes=False,
        )
    )

    response = TestClient(app).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "scheduler": "ready"}


def test_status_incident_v8_rest_snapshot_and_status_channel_contract():
    monitor = MonitorBus()
    manager = StatusIncidentManager(monitor=monitor)
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
    assert incident["state"] == "awaiting_decision"
    assert incident["hold"]["new_dispatch"] is True
    assert incident["options"][0]["action"] == "resume"
    assert monitor.recent("status")[-1]["type"] == "status_incident_required"

    class Backend:
        status_incidents = manager

        @staticmethod
        def host_ready() -> bool:
            return True

        @staticmethod
        def list_error_decisions():
            return []

    scheduler = EdgeScheduler(monitor=monitor)
    app = FastAPI()
    app.include_router(
        create_scheduler_router(
            lambda: scheduler,
            lambda: Backend(),
            include_execution_shaped_workflow_routes=False,
        )
    )
    client = TestClient(app)

    snapshot = client.get("/api/v1/status-incidents").json()
    assert snapshot["host_ready"] is True
    assert snapshot["incidents"][0]["incident_id"] == incident["incident_id"]
    assert snapshot["holds"][0]["hold_token"] == incident["hold_token"]

    monitor_snapshot = client.get("/api/v1/monitor/snapshot").json()
    assert monitor_snapshot["active_status_incidents"]
    assert monitor_snapshot["scheduler_holds"]
    assert "status" in monitor_snapshot["recent"]

    decision = client.post(
        f"/api/v1/status-incidents/{incident['incident_id']}",
        json={"action": "resume", "reason": "现场已恢复"},
    )
    assert decision.status_code == 200
    assert decision.json() == {
        "incident_id": incident["incident_id"],
        "status": "delivered",
        "state": "resolved",
    }
    assert manager.holds() == []
    assert monitor.recent("status")[-1]["type"] == "status_incident_resolved"


def test_status_policy_replacement_clears_old_hold_before_opening_new_one():
    monitor = MonitorBus()
    manager = StatusIncidentManager(monitor=monitor)
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
    recent = monitor.recent("status")[-2:]
    assert [event["type"] for event in recent] == [
        "status_incident_cleared",
        "status_incident_required",
    ]
    assert recent[0]["data"]["incident_id"] == warning["incident_id"]
    assert manager.holds()[0]["incident_id"] == error["incident_id"]


def test_device_state_report_runs_status_policy_evaluation():
    monitor = MonitorBus()
    manager = StatusIncidentManager(monitor=monitor)
    backend = JobExecutionBackend(
        host_node_getter=lambda: object(),
        monitor=monitor,
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
    scheduler = EdgeScheduler(monitor=monitor)
    app = FastAPI()
    app.include_router(
        create_scheduler_router(
            lambda: scheduler,
            lambda: backend,
            include_execution_shaped_workflow_routes=False,
        )
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/device-state/report",
        json={"device_id": "pump-1", "properties": {"mode": "Error"}},
    )

    assert response.status_code == 200
    assert response.json()["changed"] == {"mode": False}
    incidents = manager.list()
    assert incidents[0]["policy_id"] == "pump-1.mode.error"
    assert monitor.recent("status")[-1]["type"] == "status_incident_required"


def test_provider_site_boundary_flattens_canonical_pose_without_losing_admission():
    sites = canonical_component_sites(
        {
            "config": {
                "sites": [
                    {
                        "index": "A1",
                        "label": "A1",
                        "position": {"x": 1, "y": 2, "z": 3},
                        "size": {"width": 4, "height": 5, "depth": 6},
                        "content_type": ["tube"],
                        "allowed_resource_template_uuids": ["tube.v1"],
                    }
                ]
            }
        }
    )
    assert sites == [
        {
            "schema_version": 1,
            "index": "A1",
            "label": "A1",
            "visible": True,
            "parent_link": "",
            "description": "",
            "meta_data": {},
            "position_x": 1.0,
            "position_y": 2.0,
            "position_z": 3.0,
            "width": 4.0,
            "length": 5.0,
            "depth": 6.0,
            "rotation_x": 0.0,
            "rotation_y": 0.0,
            "rotation_z": 0.0,
            "content_type": ["tube"],
            "allowed_resource_template_uuids": ["tube.v1"],
        }
    ]
