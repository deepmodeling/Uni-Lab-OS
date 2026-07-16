"""HTTP contract tests for task orchestration."""

from __future__ import annotations

import json
import sys

from fastapi.testclient import TestClient, TestClient as RawTestClient
from pydantic import ValidationError
import pytest

from task_orchestration.conditions import OpcConditionProvider
from task_orchestration.main import create_app
from task_orchestration.models import TaskScheduleEntry, Template, Trigger, Workspace
from task_orchestration.service import WorkspaceService
from task_orchestration.store import WorkspaceStore


@pytest.fixture(autouse=True)
def ui_token(monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_UI_TOKEN", "ui-secret")

    def authenticated_client(*args, **kwargs):
        client = RawTestClient(*args, **kwargs)
        client.headers["Authorization"] = "Bearer ui-secret"
        return client
    monkeypatch.setattr(sys.modules[__name__], "TestClient", authenticated_client)


def test_health_reports_service_status(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ui_mutations_require_configured_bearer_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TASK_ORCHESTRATION_UI_TOKEN")
    client = RawTestClient(create_app(tmp_path))
    body = {
        "workflow_path": "demo.json",
        "expected_version": 0,
        "template": _template("task"),
    }
    assert client.post("/templates", json=body).status_code == 403

    monkeypatch.setenv("TASK_ORCHESTRATION_UI_TOKEN", "ui-secret")
    client = RawTestClient(create_app(tmp_path))
    assert client.post("/templates", json=body).status_code == 403
    assert client.post(
        "/templates",
        json=body,
        headers={"Authorization": "Bearer ui-secret"},
    ).status_code == 200


def test_cors_allows_vite_task_workspace_requests(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.options(
        "/workspaces",
        headers={
            "Origin": "http://127.0.0.1:5174",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5174"


def test_cors_allows_workflow_ui_authorized_requests(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.options(
        "/workspaces",
        headers={
            "Origin": "http://127.0.0.1:8014",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8014"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_get_workspace_returns_default_versioned_response(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get("/workspaces", params={"workflow_path": "demo.json"})

    assert response.status_code == 200
    assert response.json() == {
        "version": 0,
        "workspace": {
            "workflow_path": "demo.json",
            "templates": [],
            "task_instances": [],
            "events": [],
            "scheduled_template_ids": [],
            "scheduler_paused": False,
            "schedule_entries": [],
            "opc_snapshots": [],
        },
    }


def test_versioned_api_prefix_exposes_workspace_routes(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get("/api/v1/workspaces", params={"workflow_path": "demo.json"})

    assert response.status_code == 200
    assert response.json()["workspace"]["workflow_path"] == "demo.json"


def test_put_workspace_requires_matching_version(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    payload = {
        "expected_version": 0,
        "workspace": {
            "workflow_path": "demo.json",
            "templates": [],
            "task_instances": [],
        },
    }

    saved = client.put("/workspaces", json=payload)

    assert saved.status_code == 403


def test_admin_workspace_write_requires_configured_bearer_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_ADMIN_TOKEN", "admin-secret")
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    payload = {
        "expected_version": 0,
        "workspace": {"workflow_path": "demo.json", "templates": [], "task_instances": []},
    }
    assert client.put("/workspaces", json=payload).status_code == 403
    saved = client.put(
        "/workspaces",
        json=payload,
        headers={"Authorization": "Bearer admin-secret"},
    )
    conflict = client.put(
        "/workspaces",
        json=payload,
        headers={"Authorization": "Bearer admin-secret"},
    )

    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "workspace version conflict"


def test_gateway_token_protects_opc_snapshot_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    body = {
        "workflow_path": "demo.json",
        "expected_version": 0,
        "provider_id": "line",
        "sequence": 1,
        "values": {"ready": True},
    }
    assert client.post("/opc/snapshots", json=body).status_code == 403
    pushed = client.post(
        "/opc/snapshots",
        json=body,
        headers={"Authorization": "Bearer gateway-secret"},
    ).status_code == 200


def test_gateway_snapshot_requires_configured_token_by_default(tmp_path):
    client = _client_with_workflow(tmp_path)
    pushed = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "provider_id": "line",
            "sequence": 1,
            "values": {"ready": True},
        },
    ).status_code == 403


def test_opc_snapshot_rejects_oversized_declared_body(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    response = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "provider_id": "line",
            "sequence": 1,
            "values": {"ready": True},
        },
        headers={"Content-Length": "999999"},
    )
    assert response.status_code == 422


def test_opc_snapshot_persists_without_exposing_values_in_audit_event(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    trigger = {
        "kind": "opc",
        "config": {"provider_id": "line", "variable": "ready", "value": True},
    }
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("task", trigger=trigger),
        },
    ).status_code == 200
    assert client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["task"],
            "sample_ids": ["sample"],
        },
    ).status_code == 200
    headers = {"Authorization": "Bearer gateway-secret"}
    pushed = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "provider_id": "line",
            "sequence": 1,
            "values": {"ready": True},
        },
        headers=headers,
    )
    assert pushed.status_code == 200
    assert "values" not in pushed.json()["workspace"]["opc_snapshots"][0]

    workspace = client.get("/workspaces", params={"workflow_path": "demo.json"}).json()["workspace"]
    assert workspace["opc_snapshots"][0]["variable_count"] == 1
    assert "values" not in workspace["opc_snapshots"][0]
    assert "ready" not in workspace["events"][-1]["payload"]
    restarted = TestClient(create_app(tmp_path))
    planned = restarted.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert planned.json()["schedule"]["startable_instance_ids"]
    assert "values" not in planned.json()["workspace"]["opc_snapshots"][0]
    advanced = restarted.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 4},
    )
    assert advanced.status_code == 200
    assert "values" not in advanced.json()["workspace"]["opc_snapshots"][0]


def test_opc_replay_does_not_write_event_or_increment_version(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    headers = {"Authorization": "Bearer gateway-secret"}
    request = {
        "workflow_path": "demo.json",
        "expected_version": 0,
        "provider_id": "line",
        "sequence": 1,
        "values": {"ready": True},
    }
    first = client.post("/opc/snapshots", json=request, headers=headers)
    assert first.status_code == 200
    replay = client.post(
        "/opc/snapshots",
        json={**request, "expected_version": 1},
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.json()["accepted"] is False
    assert replay.json()["version"] == 1
    assert len(replay.json()["workspace"]["events"]) == 1


@pytest.mark.parametrize(
    "contents",
    [
        "{not json",
        '{"version": "invalid", "workspace": {"workflow_path": "demo.json"}}',
    ],
)
def test_corrupt_sidecar_returns_stable_validation_error(tmp_path, contents):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    (tmp_path / "demo.json.task-workspace.json").write_text(contents, encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    response = client.get("/workspaces", params={"workflow_path": "demo.json"})

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid task workspace sidecar"}


def test_put_workspace_rejects_unknown_fields(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    payload = {
        "expected_version": 0,
        "workspace": {
            "workflow_path": "demo.json",
            "templates": [],
            "task_instances": [],
            "unexpected": True,
        },
    }

    response = client.put("/workspaces", json=payload)

    assert response.status_code == 422


def test_put_rejects_invalid_instance_lifecycle_and_plan_returns_422_for_sidecar(tmp_path):
    client = _client_with_workflow(tmp_path)
    invalid_workspace = {
        "workflow_path": "demo.json",
        "templates": [],
        "task_instances": [
            {
                "id": "invalid",
                "template_id": "template",
                "status": "completed",
                "finished_at": 2,
            }
        ],
    }
    assert client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": invalid_workspace},
    ).status_code == 422

    (tmp_path / "demo.json.task-workspace.json").write_text(
        json.dumps({"version": 1, "workspace": invalid_workspace}),
        encoding="utf-8",
    )
    planned = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 1},
    )
    assert planned.status_code == 422
    assert planned.json() == {"detail": "invalid task workspace sidecar"}


def _client_with_workflow(tmp_path) -> TestClient:
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    return TestClient(create_app(tmp_path))


def _template(template_id: str, *, resources: list[str] | None = None, trigger=None):
    return {
        "id": template_id,
        "name": template_id,
        "node_ids": [f"{template_id}-node"],
        "resources": resources or [],
        "trigger": trigger,
    }


def test_template_lifecycle_cascades_instances_and_pending_generation(tmp_path):
    client = _client_with_workflow(tmp_path)
    created = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("prepare"),
        },
    )
    assert created.status_code == 200
    assert created.json()["workspace"]["templates"][0]["name"] == "prepare"

    renamed = client.patch(
        "/templates/prepare",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "name": "Prepare sample",
            "trigger": {"kind": "manual", "config": {}},
        },
    )
    assert renamed.status_code == 200
    assert renamed.json()["workspace"]["templates"][0]["name"] == "Prepare sample"

    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "template_ids": ["prepare"],
            "sample_ids": ["sample-a"],
        },
    )
    assert generated.status_code == 200

    deleted = client.delete(
        "/templates/prepare",
        params={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert deleted.status_code == 200
    assert deleted.json()["workspace"]["templates"] == []
    assert deleted.json()["workspace"]["task_instances"] == []
    event = deleted.json()["workspace"]["events"][-1]
    assert event["kind"] == "template_deleted"
    assert event["template_id"] is None
    assert event["payload"] == {
        "deleted_template_id": "prepare",
        "deleted_instance_ids": [generated.json()["workspace"]["task_instances"][0]["id"]]
    }
    assert event["id"] and event["timestamp"] >= 0


def test_scheduled_templates_endpoint_persists_requested_order_without_admin_token(tmp_path):
    client = _client_with_workflow(tmp_path)
    for version, template_id in enumerate(("first", "second")):
        assert client.post(
            "/templates",
            json={
                "workflow_path": "demo.json",
                "expected_version": version,
                "template": _template(template_id),
            },
        ).status_code == 200

    saved = client.put(
        "/workspaces/scheduled-templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "template_ids": ["second", "first"],
        },
    )

    assert saved.status_code == 200
    assert saved.json()["version"] == 3
    assert saved.json()["workspace"]["scheduled_template_ids"] == ["second", "first"]
    assert saved.json()["workspace"]["events"][-1]["kind"] == "scheduled_templates_updated"
    assert saved.json()["workspace"]["events"][-1]["payload"] == {
        "template_ids": ["second", "first"]
    }
    missing = client.put(
        "/workspaces/scheduled-templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "template_ids": ["missing"],
        },
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "template_not_found"


def test_generation_and_move_preserve_per_sample_sequence(tmp_path):
    client = _client_with_workflow(tmp_path)
    for version, template_id in enumerate(("first", "second")):
        assert client.post(
            "/templates",
            json={
                "workflow_path": "demo.json",
                "expected_version": version,
                "template": _template(template_id),
            },
        ).status_code == 200

    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "template_ids": ["first", "second"],
            "sample_ids": ["sample-a", "sample-b"],
        },
    )
    instances = generated.json()["workspace"]["task_instances"]
    assert [(item["sample_id"], item["template_id"], item["order"]) for item in instances] == [
        ("sample-a", "first", 0),
        ("sample-a", "second", 1),
        ("sample-b", "first", 0),
        ("sample-b", "second", 1),
    ]

    moved = client.post(
        f"/instances/{instances[1]['id']}:move",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "order": 0,
        },
    )
    assert moved.status_code == 200
    sample_a = [
        (item["template_id"], item["order"])
        for item in moved.json()["workspace"]["task_instances"]
        if item["sample_id"] == "sample-a"
    ]
    assert sample_a == [("first", 1), ("second", 0)]

    started = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 4},
    )
    assert started.status_code == 200

    forbidden = client.post(
        f"/instances/{instances[1]['id']}:move",
        json={"workflow_path": "demo.json", "expected_version": 5, "order": 1},
    )
    assert forbidden.status_code == 409
    assert forbidden.json()["detail"]["code"] == "instance_not_reorderable"


def test_opc_plan_and_advance_apply_resource_locks_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    opc_trigger = {
        "kind": "opc",
        "config": {"provider_id": "line-1", "variable": "ready", "value": True},
    }
    for version, template_id in enumerate(("robot-a", "robot-b")):
        assert client.post(
            "/templates",
            json={
                "workflow_path": "demo.json",
                "expected_version": version,
                "template": _template(template_id, resources=["robot"], trigger=opc_trigger),
            },
        ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "template_ids": ["robot-a"],
            "sample_ids": ["sample-a", "sample-b"],
        },
    )
    assert generated.status_code == 200
    first_id, second_id = [
        item["id"] for item in generated.json()["workspace"]["task_instances"]
    ]

    waiting = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert waiting.status_code == 200
    assert waiting.json()["schedule"]["waiting_reasons"][first_id]["code"] == "opc_variable_missing"

    pushed = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 4,
            "provider_id": "line-1",
            "sequence": 1,
            "values": {"ready": True},
        },
        headers={"Authorization": "Bearer gateway-secret"},
    )
    assert pushed.status_code == 200

    planned = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 5},
    )
    assert planned.status_code == 200
    assert planned.json()["schedule"]["startable_instance_ids"] == [first_id]
    assert planned.json()["schedule"]["waiting_reasons"][second_id]["code"] == "resource_unavailable"

    advanced = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 6},
    )
    assert advanced.status_code == 200
    assert {item["id"]: item["status"] for item in advanced.json()["workspace"]["task_instances"]} == {
        first_id: "running",
        second_id: "pending",
    }
    scheduled_event = next(
        event for event in advanced.json()["workspace"]["events"]
        if event["kind"] == "scheduled" and event["instance_id"] == first_id
    )
    assert scheduled_event["payload"]["satisfied_triggers"] == [opc_trigger]

    completed = client.post(
        "/schedule:advance",
        json={
            "workflow_path": "demo.json",
            "expected_version": 7,
            "completed_instance_ids": [first_id],
        },
    )
    assert completed.status_code == 200
    completed_body = completed.json()
    assert {item["id"]: item["status"] for item in completed_body["workspace"]["task_instances"]} == {
        first_id: "completed",
        second_id: "running",
    }
    assert any(event["kind"] == "output" for event in completed_body["workspace"]["events"])


def test_paused_scheduler_does_not_dispatch(tmp_path):
    client = _client_with_workflow(tmp_path)
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("manual", resources=["robot"]),
        },
    ).status_code == 200
    assert client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["manual"],
            "sample_ids": ["sample-a"],
        },
    ).status_code == 200

    paused = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 2, "paused": True},
    )
    assert paused.status_code == 200
    dispatched = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert dispatched.status_code == 200
    assert dispatched.json()["workspace"]["task_instances"][0]["status"] == "pending"


def test_default_provider_snapshot_starts_frontend_mapped_opc_trigger(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": {
                **_template("csv-opc"),
                "input_triggers": [{
                    "kind": "opc",
                    "config": {
                        "provider_id": "default",
                        "variable": "S09 空闲",
                        "value": True,
                    },
                }],
            },
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["csv-opc"],
            "sample_ids": ["sample-a"],
        },
    )
    instance_id = generated.json()["workspace"]["task_instances"][0]["id"]
    assert client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "provider_id": "default",
            "sequence": 1,
            "values": {"S09 空闲": True},
        },
        headers={"Authorization": "Bearer gateway-secret"},
    ).status_code == 200

    started = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )

    assert started.status_code == 200
    assert started.json()["workspace"]["task_instances"][0]["id"] == instance_id
    assert started.json()["workspace"]["task_instances"][0]["status"] == "running"


def test_system_resource_trigger_starts_first_task_and_serializes_shared_resource(tmp_path):
    client = _client_with_workflow(tmp_path)
    system_resource_trigger = {
        "kind": "resource",
        "config": {"resource": "robot"},
    }
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": {
                **_template("shared", resources=["robot"]),
                "input_triggers": [system_resource_trigger],
                "output_triggers": [{
                    "kind": "resource",
                    "config": {"resource": "robot", "event": "released"},
                }],
            },
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["shared"],
            "sample_ids": ["sample-a", "sample-b"],
        },
    )
    first_id, second_id = [
        item["id"] for item in generated.json()["workspace"]["task_instances"]
    ]

    started = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 2},
    )
    assert {item["id"]: item["status"] for item in started.json()["workspace"]["task_instances"]} == {
        first_id: "running",
        second_id: "pending",
    }
    completed = client.post(
        "/schedule:advance",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "completed_instance_ids": [first_id],
        },
    )
    assert {item["id"]: item["status"] for item in completed.json()["workspace"]["task_instances"]} == {
        first_id: "completed",
        second_id: "running",
    }
    assert any(
        event["kind"] == "output"
        and event["payload"]["trigger"]["config"] == {"resource": "robot", "event": "released"}
        for event in completed.json()["workspace"]["events"]
    )


def test_workstation_constraint_is_an_independent_mutex_when_template_resources_empty(tmp_path):
    client = _client_with_workflow(tmp_path)
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": {
                **_template("station-only", resources=[]),
                "input_triggers": [{
                    "kind": "workstation",
                    "config": {"workstation": "s09"},
                }],
            },
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["station-only"],
            "sample_ids": ["sample-a", "sample-b"],
        },
    )
    first_id, second_id = [
        item["id"] for item in generated.json()["workspace"]["task_instances"]
    ]

    started = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 2},
    )
    assert {item["id"]: item["status"] for item in started.json()["workspace"]["task_instances"]} == {
        first_id: "running",
        second_id: "pending",
    }
    assert started.json()["schedule"]["waiting_reasons"][second_id]["code"] == "workstation_unavailable"
    entry = next(
        item for item in started.json()["workspace"]["schedule_entries"]
        if item["instance_id"] == first_id
    )
    assert entry["resources"] == ["s09"]
    completed = client.post(
        "/schedule:advance",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "completed_instance_ids": [first_id],
        },
    )
    assert {item["id"]: item["status"] for item in completed.json()["workspace"]["task_instances"]} == {
        first_id: "completed",
        second_id: "running",
    }


@pytest.mark.parametrize("resource", ["resource:robot", "workstation:s09"])
def test_template_resources_reject_policy_reserved_namespaces(tmp_path, resource):
    client = _client_with_workflow(tmp_path)

    response = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("reserved", resources=[resource]),
        },
    )

    assert response.status_code == 422
    assert "reserved policy resource prefix" in response.text


def test_legacy_opc_condition_trigger_is_rejected_with_validation_error(tmp_path):
    client = _client_with_workflow(tmp_path)

    response = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": {
                **_template("legacy"),
                "input_triggers": [{
                    "kind": "opc_condition",
                    "config": {
                        "variable_name": "S09 空闲",
                        "data_type": "BOOL",
                        "value": True,
                    },
                }],
            },
        },
    )

    assert response.status_code == 422
    assert "legacy opc_condition" in response.text


def test_plan_and_advance_do_not_overwrite_scheduled_template_configuration(tmp_path):
    client = _client_with_workflow(tmp_path)
    for version, template_id in enumerate(("first", "second")):
        assert client.post(
            "/templates",
            json={
                "workflow_path": "demo.json",
                "expected_version": version,
                "template": _template(template_id),
            },
        ).status_code == 200
    assert client.put(
        "/workspaces/scheduled-templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "template_ids": ["second"],
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "template_ids": ["first"],
            "sample_ids": ["sample-a"],
        },
    )
    planned = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": generated.json()["version"]},
    )
    advanced = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": planned.json()["version"]},
    )

    assert planned.json()["workspace"]["scheduled_template_ids"] == ["second"]
    assert advanced.json()["workspace"]["scheduled_template_ids"] == ["second"]
    assert advanced.json()["workspace"]["schedule_entries"][0]["resources"] == []


def test_output_trigger_event_unblocks_internal_input_without_opc_write(tmp_path):
    client = _client_with_workflow(tmp_path)
    producer = _template("producer")
    producer["output_triggers"] = [
        {"kind": "internal", "config": {"key": "prepared", "value": True}}
    ]
    consumer = _template("consumer")
    consumer["input_triggers"] = [
        {"kind": "internal", "config": {"key": "prepared", "value": True}}
    ]
    for version, template in enumerate((producer, consumer)):
        assert client.post(
            "/templates",
            json={
                "workflow_path": "demo.json",
                "expected_version": version,
                "template": template,
            },
        ).status_code == 200
    producer_instance = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "template_ids": ["producer"],
            "sample_ids": ["sample-a"],
        },
    ).json()["workspace"]["task_instances"][0]["id"]
    consumer_instance = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "template_ids": ["consumer"],
            "sample_ids": ["sample-b"],
        },
    ).json()["workspace"]["task_instances"][1]["id"]

    assert client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 4},
    ).status_code == 200
    before_completion = client.get(
        "/workspaces", params={"workflow_path": "demo.json"}
    ).json()["workspace"]
    assert {item["id"]: item["status"] for item in before_completion["task_instances"]} == {
        producer_instance: "running",
        consumer_instance: "waiting",
    }
    assert client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 5, "paused": True},
    ).status_code == 200
    completed = client.post(
        "/schedule:advance",
        json={
            "workflow_path": "demo.json",
            "expected_version": 6,
            "completed_instance_ids": [producer_instance],
        },
    )
    assert completed.status_code == 200
    workspace = completed.json()["workspace"]
    assert {item["id"]: item["status"] for item in workspace["task_instances"]}[consumer_instance] == "pending"
    output = next(event for event in workspace["events"] if event["kind"] == "output")
    assert output["payload"]["trigger"] == {
        "kind": "internal",
        "config": {"key": "prepared", "value": True},
    }


def test_opc_version_conflict_does_not_leave_snapshot_for_future_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    trigger = {"kind": "opc", "config": {"provider_id": "line", "variable": "ready", "value": True}}
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("opc-task", trigger=trigger),
        },
    ).status_code == 200
    assert client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["opc-task"],
            "sample_ids": ["sample-a"],
        },
    ).status_code == 200

    conflict = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "provider_id": "line",
            "sequence": 1,
            "values": {"ready": True},
        },
        headers={"Authorization": "Bearer gateway-secret"},
    )
    assert conflict.status_code == 409
    planned = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 2},
    )
    assert planned.json()["schedule"]["waiting_reasons"]


def test_opc_snapshot_uses_same_key_for_absolute_and_relative_workflow_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    trigger = {"kind": "opc", "config": {"provider_id": "line", "variable": "ready", "value": True}}
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("opc-task", trigger=trigger),
        },
    ).status_code == 200
    assert client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["opc-task"],
            "sample_ids": ["sample-a"],
        },
    ).status_code == 200
    assert client.post(
        "/opc/snapshots",
        json={
            "workflow_path": str(tmp_path / "demo.json"),
            "expected_version": 2,
            "provider_id": "line",
            "sequence": 1,
            "values": {"ready": True},
        },
        headers={"Authorization": "Bearer gateway-secret"},
    ).status_code == 200

    planned = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert planned.json()["schedule"]["startable_instance_ids"]


def test_opc_write_failure_rolls_back_provider_snapshot(tmp_path, monkeypatch):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    conditions = OpcConditionProvider()
    service = WorkspaceService(store, conditions=conditions)
    monkeypatch.setattr(store, "_write_json_atomically", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        service.push_opc_snapshot("demo.json", 0, "line", 1, {"ready": True})

    assert not conditions.evaluate(
        "demo.json",
        Trigger(kind="opc", config={"provider_id": "line", "variable": "ready", "value": True}),
    ).satisfied


def test_plan_persists_task_entries_for_resources_samples_and_actual_times(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_ADMIN_TOKEN", "admin-secret")
    client = _client_with_workflow(tmp_path)
    templates = [
        _template("robot", resources=["robot"]),
        _template("station", resources=["station"]),
        _template("sample-first"),
        _template("sample-second"),
        _template("running"),
        _template("done"),
    ]
    payload = {
        "expected_version": 0,
        "workspace": {
            "workflow_path": "demo.json",
            "templates": templates,
            "task_instances": [
                {"id": "robot-a", "template_id": "robot", "status": "pending", "sample_id": "a", "order": 0},
                {"id": "robot-b", "template_id": "robot", "status": "pending", "sample_id": "b", "order": 0},
                {"id": "station-a", "template_id": "station", "status": "pending", "sample_id": "c", "order": 0},
                {"id": "sample-1", "template_id": "sample-first", "status": "pending", "sample_id": "same", "order": 0},
                {"id": "sample-2", "template_id": "sample-second", "status": "pending", "sample_id": "same", "order": 1},
                {"id": "running-1", "template_id": "running", "status": "running",
                    "sample_id": "running", "order": 0, "started_at": 500},
                {"id": "done-1", "template_id": "done", "status": "completed",
                    "sample_id": "done", "order": 0, "started_at": 100, "finished_at": 250},
            ],
        },
    }
    assert client.put(
        "/workspaces",
        json=payload,
        headers={"Authorization": "Bearer admin-secret"},
    ).status_code == 200

    planned = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 1},
    )
    assert planned.status_code == 200
    entries = {
        entry["instance_id"]: entry for entry in planned.json()["schedule"]["entries"]
    }
    assert entries["robot-a"]["start_at"] > 1_000_000_000_000
    assert entries["robot-a"]["end_at"] - entries["robot-a"]["start_at"] == 15000
    assert entries["robot-b"]["start_at"] == entries["robot-a"]["end_at"]
    assert entries["station-a"]["start_at"] == entries["robot-a"]["start_at"]
    assert entries["sample-2"]["start_at"] == entries["sample-1"]["end_at"]
    assert entries["running-1"]["start_at"] == 500
    assert entries["done-1"]["start_at"] == 100
    assert entries["done-1"]["end_at"] == 250
    assert entries["done-1"]["state"] == "done"
    assert planned.json()["workspace"]["schedule_entries"] == list(entries.values())


def test_advance_records_injected_start_and_finish_times_in_schedule_entries(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    now = [100]
    service = WorkspaceService(WorkspaceStore(tmp_path), clock=lambda: now[0])
    service.create_template(
        "demo.json",
        0,
        Template(id="task", name="task", node_ids=["node"]),
    )
    generated = service.generate_instances("demo.json", 1, ["task"], ["sample"])
    instance_id = generated.workspace.task_instances[0].id

    started, _ = service.advance("demo.json", 2)
    started_instance = started.workspace.task_instances[0]
    assert started_instance.status == "running"
    assert started_instance.started_at == 100
    assert started.workspace.schedule_entries[0].start_at == 100

    now[0] = 250
    completed, _ = service.advance("demo.json", 3, [instance_id])
    completed_instance = completed.workspace.task_instances[0]
    assert completed_instance.status == "completed"
    assert completed_instance.finished_at == 250
    assert completed.workspace.schedule_entries[0].end_at == 250


def test_delete_template_cascades_its_schedule_entries(tmp_path):
    workflow = tmp_path / "demo.json"
    workflow.write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    store.put(
        Workspace(
            workflow_path="demo.json",
            templates=[Template(id="template", name="template")],
            task_instances=[{"id": "instance", "template_id": "template", "status": "pending"}],
            schedule_entries=[
                TaskScheduleEntry(
                    instance_id="instance",
                    template_id="template",
                    sample_id="sample",
                    start_at=0,
                    end_at=1,
                    resources=[],
                    state="planned",
                )
            ],
        ),
        expected_version=0,
    )

    deleted = WorkspaceService(store).delete_template("demo.json", 1, "template")

    assert deleted.workspace.schedule_entries == []


def test_real_running_resource_interval_precedes_pending_gantt_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_ADMIN_TOKEN", "admin-secret")
    client = _client_with_workflow(tmp_path)
    payload = {
        "expected_version": 0,
        "workspace": {
            "workflow_path": "demo.json",
            "templates": [_template("shared", resources=["robot"])],
            "task_instances": [
                {"id": "pending", "template_id": "shared", "status": "pending", "sample_id": "a", "order": 0},
                {"id": "running", "template_id": "shared", "status": "running",
                    "sample_id": "z", "order": 0, "started_at": 100},
            ],
        },
    }
    assert client.put(
        "/workspaces",
        json=payload,
        headers={"Authorization": "Bearer admin-secret"},
    ).status_code == 200

    planned = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 1},
    )
    entries = {entry["instance_id"]: entry for entry in planned.json()["schedule"]["entries"]}
    assert entries["running"]["start_at"] == 100
    assert entries["pending"]["start_at"] >= entries["running"]["end_at"]


def test_schedule_entry_rejects_end_before_start():
    with pytest.raises(ValidationError):
        TaskScheduleEntry(
            instance_id="task",
            template_id="template",
            sample_id="sample",
            start_at=2,
            end_at=1,
            resources=[],
            state="planned",
        )
