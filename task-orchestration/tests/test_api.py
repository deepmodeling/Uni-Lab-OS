"""HTTP contract tests for task orchestration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier
import time

from fastapi.testclient import TestClient, TestClient as RawTestClient
from pydantic import ValidationError
import pytest

from task_orchestration.conditions import OpcConditionProvider
from task_orchestration.main import create_app
from task_orchestration.models import (
    OpcSnapshotState,
    PlcRegistration,
    TaskScheduleEntry,
    Template,
    Trigger,
    Workspace,
)
from task_orchestration.service import WorkspaceService, WorkspaceServiceError
from task_orchestration.store import VersionConflictError, WorkspaceStore


def test_readme_opc_trigger_uses_runtime_plc_device_id():
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert '"provider_id"' not in readme
    assert '"plc_device_id":"szlab_poly_plc"' in readme


def test_health_reports_service_status(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_local_api_mutations_ignore_legacy_bearer_token_configuration(tmp_path, monkeypatch):
    """本地模式不应再按 UI、gateway 或 admin token 拒绝请求。"""
    monkeypatch.setenv("TASK_ORCHESTRATION_UI_TOKEN", "ui-secret")
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    monkeypatch.setenv("TASK_ORCHESTRATION_ADMIN_TOKEN", "admin-secret")
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = RawTestClient(create_app(tmp_path))

    workspace = client.put("/workspaces", json={
        "expected_version": 0,
        "workspace": {"workflow_path": "demo.json", "templates": [], "task_instances": []},
    })
    assert workspace.status_code == 200

    registration = client.post("/opc/registrations", json={
        "workflow_path": "demo.json",
        "expected_version": 1,
        "registration": {
            "plc_device_id": "line",
            "runtime_url": "opc.tcp://test:4840",
            "variables": ["ready"],
        },
    })
    assert registration.status_code == 200

    template = client.post("/templates", json={
        "workflow_path": "demo.json",
        "expected_version": 2,
        "template": _template("task"),
    })
    assert template.status_code == 200

    snapshot = client.post("/opc/snapshots", json={
        "workflow_path": "demo.json",
        "expected_version": 3,
        "plc_device_id": "line",
        "sequence": 1,
        "values": {"ready": True},
    })
    assert snapshot.status_code == 200


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


def test_cors_allows_workflow_ui_content_type_requests(tmp_path):
    client = TestClient(create_app(tmp_path))

    response = client.options(
        "/workspaces",
        headers={
            "Origin": "http://127.0.0.1:8014",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8014"
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


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
            "dynamic_resource_leases": [],
            "pause_reason": None,
            "schedule_entries": [],
            "opc_snapshots": [],
            "plc_registrations": [],
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

    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    conflict = client.put("/workspaces", json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "workspace version conflict"


def test_workspace_write_remains_versioned_without_admin_token(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    payload = {
        "expected_version": 0,
        "workspace": {"workflow_path": "demo.json", "templates": [], "task_instances": []},
    }
    saved = client.put(
        "/workspaces",
        json=payload,
    )
    conflict = client.put(
        "/workspaces",
        json=payload,
    )

    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "workspace version conflict"


def test_opc_snapshot_accepts_requests_without_gateway_token(tmp_path):
    client = _client_with_workflow(tmp_path)
    body = {
        "workflow_path": "demo.json",
        "expected_version": 0,
        "plc_device_id": "line",
        "sequence": 1,
        "values": {"ready": True},
    }
    assert client.post("/opc/snapshots", json=body).status_code == 200


def test_gateway_snapshot_is_available_without_configured_token(tmp_path):
    client = _client_with_workflow(tmp_path)
    response = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "plc_device_id": "line",
            "sequence": 1,
            "values": {"ready": True},
        },
    )
    assert response.status_code == 200


def test_opc_snapshot_rejects_oversized_declared_body(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    response = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "plc_device_id": "line",
            "sequence": 1,
            "values": {"ready": True},
        },
        headers={"Content-Length": "999999"},
    )
    assert response.status_code == 422


def test_opc_snapshot_persists_raw_values_for_task_observability(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    trigger = {
        "kind": "opc",
        "config": {"plc_device_id": "line", "variable": "ready", "value": True},
    }
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("task", input_triggers=[trigger]),
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
            "plc_device_id": "line",
            "sequence": 2,
            "values": {"ready": True},
        },
        headers=headers,
    )
    assert pushed.status_code == 200
    assert pushed.json()["workspace"]["opc_snapshots"][0]["values"] == {"ready": True}

    workspace = client.get("/workspaces", params={"workflow_path": "demo.json"}).json()["workspace"]
    assert workspace["opc_snapshots"][0]["plc_device_id"] == "line"
    assert workspace["opc_snapshots"][0]["values"] == {"ready": True}
    assert "ready" not in workspace["events"][-1]["payload"]
    restarted = TestClient(create_app(tmp_path))
    planned = restarted.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert planned.json()["schedule"]["startable_instance_ids"]
    assert planned.json()["workspace"]["opc_snapshots"][0]["values"] == {"ready": True}
    advanced = restarted.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 4},
    )
    assert advanced.status_code == 200
    assert advanced.json()["workspace"]["opc_snapshots"][0]["values"] == {"ready": True}


def test_opc_replay_does_not_write_event_or_increment_version(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    headers = {"Authorization": "Bearer gateway-secret"}
    request = {
        "workflow_path": "demo.json",
        "expected_version": 0,
        "plc_device_id": "line",
        "sequence": 2,
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


def test_store_load_migrates_legacy_sidecar_but_put_remains_strict(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    legacy_workspace = {
        "workflow_path": "demo.json",
        "templates": [{
            "id": "legacy-template",
            "name": "Legacy",
            "node_ids": ["node-1", "node-2"],
            "trigger": {"kind": "legacy", "value": True},
        }],
        "task_instances": [{
            "id": "legacy-completed",
            "template_id": "legacy-template",
            "status": "completed",
            "started_at": 10,
            "finished_at": 20,
        }],
    }
    sidecar = tmp_path / "demo.json.task-workspace.json"
    original = json.dumps({"version": 4, "workspace": legacy_workspace})
    sidecar.write_text(original, encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    loaded = client.get("/workspaces", params={"workflow_path": "demo.json"})
    assert loaded.status_code == 200
    template = loaded.json()["workspace"]["templates"][0]
    instance = loaded.json()["workspace"]["task_instances"][0]
    assert "trigger" not in template
    assert instance["execution_state"]["cursor"] == 2
    assert instance["execution_state"]["active_execution_id"] is None
    assert sidecar.read_text(encoding="utf-8") == original

    rejected_trigger = client.put(
        "/workspaces",
        json={"expected_version": 4, "workspace": legacy_workspace},
    )
    assert rejected_trigger.status_code == 422
    explicit_empty_state = {
        **legacy_workspace,
        "templates": [{
            key: value
            for key, value in legacy_workspace["templates"][0].items()
            if key != "trigger"
        }],
        "task_instances": [{
            **legacy_workspace["task_instances"][0],
            "execution_state": {},
        }],
    }
    rejected_state = client.put(
        "/workspaces",
        json={"expected_version": 4, "workspace": explicit_empty_state},
    )
    assert rejected_state.status_code == 422


def test_reset_workspace_removes_only_current_workflow_sidecar(tmp_path):
    first_workflow = tmp_path / "first.json"
    second_workflow = tmp_path / "second.json"
    first_workflow.write_text('{"name": "first"}', encoding="utf-8")
    second_workflow.write_text('{"name": "second"}', encoding="utf-8")
    first_sidecar = tmp_path / "first.json.task-workspace.json"
    second_sidecar = tmp_path / "second.json.task-workspace.json"
    first_sidecar.write_text("{not json", encoding="utf-8")
    second_sidecar.write_text(json.dumps({
        "version": 1,
        "workspace": Workspace(workflow_path="second.json").model_dump(mode="json"),
    }), encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    reset = client.post("/workspaces/reset", json={"workflow_path": "first.json"})

    assert reset.status_code == 200
    assert reset.json()["version"] == 0
    assert reset.json()["workspace"]["workflow_path"] == "first.json"
    assert not first_sidecar.exists()
    assert second_sidecar.exists()
    assert first_workflow.read_text(encoding="utf-8") == '{"name": "first"}'
    assert second_workflow.read_text(encoding="utf-8") == '{"name": "second"}'


def test_reset_workspace_clears_in_memory_opc_sequence(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    registration = {
        "workflow_path": "demo.json",
        "registration": {
            "plc_device_id": "line",
            "runtime_url": "opc.tcp://test:4840",
            "variables": ["ready"],
        },
    }
    assert client.post("/opc/registrations", json=registration).status_code == 200
    first = client.post("/opc/snapshots", json={
        "workflow_path": "demo.json",
        "expected_version": 1,
        "plc_device_id": "line",
        "sequence": 10,
        "values": {"ready": True},
    })
    assert first.json()["accepted"] is True

    assert client.post(
        "/workspaces/reset", json={"workflow_path": "demo.json"}
    ).status_code == 200
    assert client.post("/opc/registrations", json=registration).status_code == 200
    restarted = client.post("/opc/snapshots", json={
        "workflow_path": "demo.json",
        "expected_version": 1,
        "plc_device_id": "line",
        "sequence": 1,
        "values": {"ready": False},
    })

    assert restarted.status_code == 200
    assert restarted.json()["accepted"] is True
    assert restarted.json()["workspace"]["opc_snapshots"][0]["sequence"] == 1


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


@pytest.mark.parametrize("invalid_state", ["completed_early", "active_wrong_node"])
def test_put_workspace_rejects_invalid_execution_cursor_state(
    tmp_path, invalid_state
):
    client = _client_with_running_action_tasks(tmp_path)
    workspace = client.get(
        "/workspaces", params={"workflow_path": "demo.json"}
    ).json()["workspace"]
    instance = next(
        item for item in workspace["task_instances"] if item["id"] == "pipeline-1"
    )
    if invalid_state == "completed_early":
        instance.update({"status": "completed", "finished_at": 2})
    else:
        instance["execution_state"] = {
            "cursor": 0,
            "records": [{
                "node_id": "s07-action",
                "attempt": 1,
                "execution_id": "wrong-node",
                "status": "running",
                "started_at": 2,
            }],
            "active_node_id": "s07-action",
            "active_execution_id": "wrong-node",
        }

    response = client.put(
        "/workspaces",
        json={"expected_version": 1, "workspace": workspace},
    )

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
    (tmp_path / "demo.json.task-workspace.json").write_text(
        json.dumps({
            "version": 0,
            "workspace": Workspace(
                workflow_path="demo.json",
                opc_snapshots=[
                    OpcSnapshotState(
                        plc_device_id="line",
                        sequence=1,
                        values={"ready": True},
                        updated_at_by_variable={"ready": time.monotonic()},
                    )
                ],
                plc_registrations=[
                    PlcRegistration(
                        plc_device_id=device_id,
                        runtime_url="opc.tcp://test:4840",
                        variables=variables,
                    )
                    for device_id, variables in {
                        "line": ["ready"],
                        "line-1": ["ready"],
                        "default": ["S09 空闲"],
                        "plc-a": ["ready", "done"],
                    }.items()
                ],
            ).model_dump(mode="json"),
        }),
        encoding="utf-8",
    )
    return TestClient(create_app(tmp_path))


def _template(
    template_id: str,
    *,
    resources: list[str] | None = None,
    input_triggers: list[dict] | None = None,
    output_triggers: list[dict] | None = None,
):
    default_trigger = {
        "kind": "opc",
        "config": {"plc_device_id": "line", "variable": "ready", "value": True},
    }
    return {
        "id": template_id,
        "name": template_id,
        "node_ids": [f"{template_id}-node"],
        "resources": resources or [],
        "input_triggers": [default_trigger] if input_triggers is None else input_triggers,
        "output_triggers": [default_trigger] if output_triggers is None else output_triggers,
    }


def test_instance_parameter_overrides_are_saved_only_on_the_target_instance(tmp_path):
    client = _client_with_workflow(tmp_path)
    workspace = {
        "workflow_path": "demo.json",
        "templates": [{
            **_template("prepare"),
            "node_ids": ["s06-action", "s07-action"],
        }],
        "task_instances": [
            {
                "id": "sample-a-prepare",
                "template_id": "prepare",
                "sample_id": "Sample A",
                "status": "waiting",
            },
            {
                "id": "sample-b-prepare",
                "template_id": "prepare",
                "sample_id": "Sample B",
                "status": "waiting",
            },
        ],
    }
    assert client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": workspace},
    ).status_code == 200

    response = client.patch(
        "/instances/sample-a-prepare/parameters",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "node_parameters": {
                "s06-action": {"volume_ml": 12.5},
                "s07-action": {"temperature_c": 80},
            },
        },
    )

    assert response.status_code == 200
    instances = response.json()["workspace"]["task_instances"]
    assert instances[0]["payload"]["node_parameters"] == {
        "s06-action": {"volume_ml": 12.5},
        "s07-action": {"temperature_c": 80},
    }
    assert instances[1]["payload"] == {}


def test_generate_instances_applies_remembered_parameters_to_each_new_instance(tmp_path):
    client = _client_with_workflow(tmp_path)
    workspace = {
        "workflow_path": "demo.json",
        "templates": [{
            **_template("prepare"),
            "node_ids": ["s06-action", "s07-action"],
        }],
        "task_instances": [],
    }
    assert client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": workspace},
    ).status_code == 200

    response = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["prepare"],
            "sample_ids": ["Sample A", "Sample B"],
            "template_node_parameters": {
                "prepare": {
                    "s06-action": {"volume_ml": 12.5},
                    "s07-action": {"temperature_c": 80},
                },
            },
        },
    )

    assert response.status_code == 200
    instances = response.json()["workspace"]["task_instances"]
    assert len(instances) == 2
    assert all(instance["payload"]["node_parameters"] == {
        "s06-action": {"volume_ml": 12.5},
        "s07-action": {"temperature_c": 80},
    } for instance in instances)


def test_generate_instances_applies_parameters_by_sample(tmp_path):
    client = _client_with_workflow(tmp_path)
    workspace = {
        "workflow_path": "demo.json",
        "templates": [{
            **_template("prepare"),
            "node_ids": ["s06-action"],
        }],
        "task_instances": [],
    }
    assert client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": workspace},
    ).status_code == 200

    response = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["prepare"],
            "sample_ids": ["Sample A", "Sample B", "Sample D"],
            "sample_template_node_parameters": {
                "Sample A": {
                    "prepare": {"s06-action": {"coarse_position": 1}},
                },
                "Sample B": {
                    "prepare": {"s06-action": {"coarse_position": 2}},
                },
                "Sample D": {
                    "prepare": {"s06-action": {"coarse_position": 4}},
                },
            },
        },
    )

    assert response.status_code == 200
    instances = {
        instance["sample_id"]: instance
        for instance in response.json()["workspace"]["task_instances"]
    }
    assert instances["Sample A"]["payload"]["node_parameters"] == {
        "s06-action": {"coarse_position": 1},
    }
    assert instances["Sample B"]["payload"]["node_parameters"] == {
        "s06-action": {"coarse_position": 2},
    }
    assert instances["Sample D"]["payload"]["node_parameters"] == {
        "s06-action": {"coarse_position": 4},
    }


def test_generate_instances_rejects_remembered_parameters_for_unknown_nodes(tmp_path):
    client = _client_with_workflow(tmp_path)
    workspace = {
        "workflow_path": "demo.json",
        "templates": [_template("prepare")],
        "task_instances": [],
    }
    assert client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": workspace},
    ).status_code == 200

    response = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["prepare"],
            "sample_ids": ["Sample A"],
            "template_node_parameters": {
                "prepare": {"removed-node": {"volume_ml": 12.5}},
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unknown_action_node"


def test_instance_parameter_overrides_reject_running_instances(tmp_path):
    client = _client_with_workflow(tmp_path)
    workspace = {
        "workflow_path": "demo.json",
        "templates": [_template("prepare")],
        "task_instances": [{
            "id": "sample-a-prepare",
            "template_id": "prepare",
            "sample_id": "Sample A",
            "status": "running",
            "started_at": 1,
            "execution_state": {
                "active_node_id": "prepare-node",
                "active_execution_id": "execution-1",
                "records": [{
                    "node_id": "prepare-node",
                    "execution_id": "execution-1",
                    "attempt": 1,
                    "status": "running",
                    "started_at": 1,
                }],
            },
        }],
    }
    assert client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": workspace},
    ).status_code == 200

    response = client.patch(
        "/instances/sample-a-prepare/parameters",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "node_parameters": {"prepare-node": {"volume_ml": 12.5}},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "instance_parameters_locked"


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


def test_template_api_persists_task_and_node_dependencies(tmp_path):
    client = _client_with_workflow(tmp_path)
    predecessor = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("pour"),
        },
    )
    assert predecessor.status_code == 200

    dependent_template = _template("close")
    dependent_template["dependencies"] = [{"template_id": "pour"}]
    created = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template": dependent_template,
        },
    )
    assert created.status_code == 200
    close_template = next(
        template
        for template in created.json()["workspace"]["templates"]
        if template["id"] == "close"
    )
    assert close_template["dependencies"] == [
        {"template_id": "pour", "node_id": None}
    ]

    updated = client.patch(
        "/templates/close",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "dependencies": [
                {"template_id": "pour", "node_id": "pour-node"}
            ],
        },
    )
    assert updated.status_code == 200
    close_template = next(
        template
        for template in updated.json()["workspace"]["templates"]
        if template["id"] == "close"
    )
    assert close_template["dependencies"] == [
        {"template_id": "pour", "node_id": "pour-node"}
    ]

    rejected = client.patch(
        "/templates/close",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "dependencies": [{"template_id": "close"}],
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "invalid_task_dependencies"


def test_template_dependency_hot_update_applies_during_active_run(tmp_path):
    client = _client_with_workflow(tmp_path)
    workspace = {
        "workflow_path": "demo.json",
        "templates": [
            _template("transfer", input_triggers=[], output_triggers=[]),
            _template("density", input_triggers=[], output_triggers=[]),
            _template("vial", input_triggers=[], output_triggers=[]),
        ],
        "task_instances": [
            {
                "id": "sample-a-transfer",
                "template_id": "transfer",
                "status": "completed",
                "sample_id": "Sample A",
                "order": 0,
                "started_at": 1,
                "finished_at": 2,
                "execution_state": {"cursor": 1},
            },
            {
                "id": "sample-a-density",
                "template_id": "density",
                "status": "running",
                "sample_id": "Sample A",
                "order": 1,
                "started_at": 3,
            },
            {
                "id": "sample-a-vial",
                "template_id": "vial",
                "status": "pending",
                "sample_id": "Sample A",
                "order": 2,
            },
        ],
        "scheduler_paused": False,
    }
    saved = client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": workspace},
    )
    assert saved.status_code == 200

    updated = client.patch(
        "/templates/vial",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "dependencies": [{"template_id": "transfer"}],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["workspace"]["scheduler_paused"] is False

    advanced = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 2},
    )
    assert advanced.status_code == 200
    instances = {
        item["id"]: item
        for item in advanced.json()["workspace"]["task_instances"]
    }
    assert instances["sample-a-density"]["status"] == "running"
    assert instances["sample-a-vial"]["status"] == "running"


def test_delete_templates_is_atomic_and_cascades_related_state(tmp_path):
    client = _client_with_workflow(tmp_path)
    for version, template_id in enumerate(("prepare", "measure")):
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
            "template_ids": ["prepare", "measure"],
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "template_ids": ["prepare", "measure"],
            "sample_ids": ["sample-a"],
        },
    )
    assert generated.status_code == 200
    instance_ids = [
        item["id"] for item in generated.json()["workspace"]["task_instances"]
    ]

    rejected = client.post(
        "/templates:delete",
        json={
            "workflow_path": "demo.json",
            "expected_version": 4,
            "template_ids": ["prepare", "missing"],
        },
    )
    assert rejected.status_code == 404
    unchanged = client.get(
        "/workspaces", params={"workflow_path": "demo.json"}
    ).json()
    assert [item["id"] for item in unchanged["workspace"]["templates"]] == [
        "prepare",
        "measure",
    ]

    deleted = client.post(
        "/templates:delete",
        json={
            "workflow_path": "demo.json",
            "expected_version": 4,
            "template_ids": ["prepare", "measure", "prepare"],
        },
    )
    assert deleted.status_code == 200
    workspace = deleted.json()["workspace"]
    assert workspace["templates"] == []
    assert workspace["task_instances"] == []
    assert workspace["scheduled_template_ids"] == []
    event = workspace["events"][-1]
    assert event["kind"] == "templates_deleted"
    assert event["payload"] == {
        "deleted_template_ids": ["prepare", "measure"],
        "deleted_instance_ids": instance_ids,
    }


def test_clear_instances_removes_queue_but_keeps_templates(tmp_path):
    client = _client_with_workflow(tmp_path)
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("prepare"),
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["prepare"],
            "sample_ids": ["sample-a"],
        },
    )
    assert generated.status_code == 200
    instance_id = generated.json()["workspace"]["task_instances"][0]["id"]
    assert client.post(
        "/schedule:plan",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "paused": True,
        },
    ).status_code == 200
    cleared = client.post(
        "/instances:clear",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert cleared.status_code == 200
    body = cleared.json()["workspace"]
    assert body["task_instances"] == []
    assert body["templates"][0]["id"] == "prepare"
    assert body["events"][-1]["kind"] == "instances_cleared"
    assert body["events"][-1]["payload"] == {"deleted_instance_ids": [instance_id]}


def test_clear_instances_drops_instance_scoped_events(tmp_path):
    client = _client_with_workflow(tmp_path)
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("prepare"),
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["prepare"],
            "sample_ids": ["sample-a"],
        },
    )
    assert generated.status_code == 200
    instance_id = generated.json()["workspace"]["task_instances"][0]["id"]
    patched = client.put(
        "/workspaces",
        json={
            "expected_version": 2,
            "workspace": {
                **generated.json()["workspace"],
                "events": [
                    *generated.json()["workspace"]["events"],
                    {
                        "kind": "completed",
                        "timestamp": 1,
                        "instance_id": instance_id,
                        "template_id": "prepare",
                        "payload": {},
                    },
                ],
            },
        },
    )
    assert patched.status_code == 200
    assert client.post(
        "/schedule:plan",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "paused": True,
        },
    ).status_code == 200
    cleared = client.post(
        "/instances:clear",
        json={"workflow_path": "demo.json", "expected_version": 4},
    )
    assert cleared.status_code == 200
    events = cleared.json()["workspace"]["events"]
    assert events[-1]["kind"] == "instances_cleared"
    assert all(item.get("instance_id") is None for item in events)


def test_clear_instances_allows_stuck_running_after_pause(tmp_path):
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
    assert generated.status_code == 200
    assert client.post(
        "/schedule:plan",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "paused": False,
        },
    ).status_code == 200
    advanced = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 4},
    )
    assert advanced.status_code == 200
    running_ids = [
        item["id"]
        for item in advanced.json()["workspace"]["task_instances"]
        if item["status"] == "running"
    ]
    assert running_ids
    assert client.post(
        "/schedule:plan",
        json={
            "workflow_path": "demo.json",
            "expected_version": 5,
            "paused": True,
        },
    ).status_code == 200
    cleared = client.post(
        "/instances:clear",
        json={"workflow_path": "demo.json", "expected_version": 6},
    )
    assert cleared.status_code == 200
    assert cleared.json()["workspace"]["task_instances"] == []
    assert set(cleared.json()["workspace"]["events"][-1]["payload"]["deleted_instance_ids"]) >= set(running_ids)


def test_clear_instances_rejects_active_scheduler(tmp_path):
    client = _client_with_workflow(tmp_path)
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("prepare"),
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["prepare"],
            "sample_ids": ["sample-a"],
        },
    )
    assert generated.status_code == 200
    assert client.post(
        "/schedule:plan",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "paused": False,
        },
    ).status_code == 200
    rejected = client.post(
        "/instances:clear",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "scheduler_active"


def test_reset_instances_progress_keeps_queue_order_and_parameters(tmp_path):
    client = _client_with_workflow(tmp_path)
    workspace = {
        "workflow_path": "demo.json",
        "templates": [_template("prepare"), _template("measure")],
        "task_instances": [
            {
                "id": "sample-a-prepare",
                "template_id": "prepare",
                "status": "completed",
                "sample_id": "Sample A",
                "order": 0,
                "payload": {
                    "node_parameters": {
                        "prepare-node": {"target_weight": 2.5}
                    }
                },
                "not_before": 1_000,
                "started_at": 1_100,
                "finished_at": 1_200,
                "execution_state": {
                    "cursor": 1,
                    "records": [
                        {
                            "node_id": "prepare-node",
                            "attempt": 1,
                            "execution_id": "old-completed-execution",
                            "status": "succeeded",
                            "started_at": 1_100,
                            "finished_at": 1_200,
                            "result": {"success": True},
                        }
                    ],
                },
            },
            {
                "id": "sample-a-measure",
                "template_id": "measure",
                "status": "failed",
                "sample_id": "Sample A",
                "order": 1,
                "payload": {
                    "node_parameters": {
                        "measure-node": {"repeat_count": 3}
                    }
                },
                "not_before": 3_000,
                "started_at": 1_300,
                "finished_at": 1_400,
                "execution_state": {
                    "cursor": 0,
                    "records": [
                        {
                            "node_id": "measure-node",
                            "attempt": 1,
                            "execution_id": "old-failed-execution",
                            "status": "failed",
                            "started_at": 1_300,
                            "finished_at": 1_400,
                            "error": {"code": "test_failure"},
                        }
                    ],
                },
            },
        ],
        "events": [
            {
                "kind": "completed",
                "timestamp": 1_200,
                "instance_id": "sample-a-prepare",
                "template_id": "prepare",
                "payload": {},
            }
        ],
        "scheduled_template_ids": ["prepare", "measure"],
        "scheduler_paused": True,
        "plc_registrations": [],
    }
    saved = client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": workspace},
    )
    assert saved.status_code == 200

    reset = client.post(
        "/instances:reset-progress",
        json={"workflow_path": "demo.json", "expected_version": 1},
    )

    assert reset.status_code == 200
    body = reset.json()["workspace"]
    instances = sorted(body["task_instances"], key=lambda item: item["order"])
    assert len(instances) == 2
    assert {item["id"] for item in instances}.isdisjoint(
        {"sample-a-prepare", "sample-a-measure"}
    )
    assert [item["template_id"] for item in instances] == ["prepare", "measure"]
    assert [item["sample_id"] for item in instances] == ["Sample A", "Sample A"]
    assert [item["payload"] for item in instances] == [
        workspace["task_instances"][0]["payload"],
        workspace["task_instances"][1]["payload"],
    ]
    assert instances[1]["not_before"] - instances[0]["not_before"] == 2_000
    assert all(item["status"] == "waiting" for item in instances)
    assert all(item["started_at"] is None for item in instances)
    assert all(item["finished_at"] is None for item in instances)
    assert all(
        item["execution_state"]
        == {
            "cursor": 0,
            "records": [],
            "active_execution_id": None,
            "active_node_id": None,
        }
        for item in instances
    )
    assert body["scheduled_template_ids"] == ["prepare", "measure"]
    assert body["scheduler_paused"] is True
    assert body["schedule_entries"] == []
    assert body["events"][-1]["kind"] == "instances_progress_reset"
    assert body["events"][-1]["payload"] == {"reset_instance_count": 2}
    assert all(item.get("instance_id") is None for item in body["events"])


def test_reset_instances_progress_requires_paused_idle_workspace(tmp_path):
    client = _client_with_workflow(tmp_path)
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("prepare"),
        },
    ).status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["prepare"],
            "sample_ids": ["sample-a"],
        },
    )
    assert generated.status_code == 200

    active_scheduler = client.post(
        "/instances:reset-progress",
        json={"workflow_path": "demo.json", "expected_version": 2},
    )
    assert active_scheduler.status_code == 409
    assert active_scheduler.json()["detail"]["code"] == "scheduler_active"

    paused = client.post(
        "/schedule:plan",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "paused": True,
        },
    )
    assert paused.status_code == 200
    paused_workspace = paused.json()["workspace"]
    instance = paused_workspace["task_instances"][0]
    instance["status"] = "running"
    instance["started_at"] = 2_000
    instance["execution_state"] = {
        "cursor": 0,
        "records": [
            {
                "node_id": "prepare-node",
                "attempt": 1,
                "execution_id": "active-execution",
                "status": "running",
                "started_at": 2_000,
            }
        ],
        "active_execution_id": "active-execution",
        "active_node_id": "prepare-node",
    }
    with_active_action = client.put(
        "/workspaces",
        json={"expected_version": 3, "workspace": paused_workspace},
    )
    assert with_active_action.status_code == 200

    rejected = client.post(
        "/instances:reset-progress",
        json={"workflow_path": "demo.json", "expected_version": 4},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "actions_in_flight"


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


def test_opc_plan_and_advance_defer_resource_locks_to_action_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    opc_trigger = {
        "kind": "opc",
        "config": {"plc_device_id": "line-1", "variable": "ready", "value": True},
    }
    for version, template_id in enumerate(("robot-a", "robot-b")):
        assert client.post(
            "/templates",
            json={
                "workflow_path": "demo.json",
                "expected_version": version,
                "template": {
                    **_template(
                        template_id, resources=["robot"], input_triggers=[opc_trigger]
                    ),
                    "node_ids": [],
                },
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
            "plc_device_id": "line-1",
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
    assert planned.json()["schedule"]["startable_instance_ids"] == [first_id, second_id]
    assert second_id not in planned.json()["schedule"]["waiting_reasons"]

    advanced = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 6},
    )
    assert advanced.status_code == 200
    assert {item["id"]: item["status"] for item in advanced.json()["workspace"]["task_instances"]} == {
        first_id: "running",
        second_id: "running",
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
        second_id: "completed",
    }


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
                        "plc_device_id": "default",
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
            "plc_device_id": "default",
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


@pytest.mark.parametrize("kind,config", [
    ("resource", {"resource": "robot"}),
    ("workstation", {"workstation": "s09"}),
    ("internal", {"key": "prepared", "value": True}),
])
def test_legacy_trigger_kinds_are_rejected(tmp_path, kind, config):
    client = _client_with_workflow(tmp_path)
    response = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": {
                **_template("legacy", resources=["robot"]),
                "input_triggers": [{"kind": kind, "config": config}],
            },
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("resource", ["resource:robot", "workstation:s09"])
def test_template_resources_ignore_legacy_policy_namespaces(tmp_path, resource):
    client = _client_with_workflow(tmp_path)

    response = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("reserved", resources=[resource]),
        },
    )

    assert response.status_code == 200
    assert response.json()["workspace"]["templates"][0]["resources"] == []


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
    assert "Input should be 'opc'" in response.text


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


def test_plc_output_trigger_is_informational_and_does_not_block_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    input_condition = {
        "kind": "opc",
        "config": {"plc_device_id": "plc-a", "variable": "ready", "value": True},
    }
    output_condition = {
        "kind": "opc",
        "config": {"plc_device_id": "plc-a", "variable": "done", "value": True},
    }
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": {
                **_template(
                    "plc-task",
                    input_triggers=[input_condition],
                    output_triggers=[output_condition],
                ),
                "node_ids": [],
            },
        },
    ).status_code == 200
    instance_id = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["plc-task"],
            "sample_ids": ["sample-a"],
        },
    ).json()["workspace"]["task_instances"][0]["id"]
    headers = {"Authorization": "Bearer gateway-secret"}
    assert client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "plc_device_id": "plc-a",
            "sequence": 1,
            "values": {"ready": True, "done": False},
        },
        headers=headers,
    ).status_code == 200
    started = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )
    assert started.json()["workspace"]["task_instances"][0]["status"] == "running"
    completed = client.post(
        "/schedule:advance",
        json={
            "workflow_path": "demo.json",
            "expected_version": 4,
            "completed_instance_ids": [instance_id],
        },
    )
    assert completed.json()["workspace"]["task_instances"][0]["status"] == "completed"
    assert completed.json()["schedule"]["waiting_reasons"] == {}
    assert completed.json()["workspace"]["templates"][0]["output_triggers"] == [
        output_condition
    ]


def test_opc_version_conflict_does_not_leave_snapshot_for_future_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    trigger = {"kind": "opc", "config": {"plc_device_id": "line-1", "variable": "ready", "value": True}}
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("opc-task", input_triggers=[trigger]),
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
            "plc_device_id": "line-1",
            "sequence": 2,
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
    trigger = {"kind": "opc", "config": {"plc_device_id": "line", "variable": "ready", "value": True}}
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("opc-task", input_triggers=[trigger]),
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
            "plc_device_id": "line",
            "sequence": 2,
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
    service.register_plc_variables(
        "demo.json",
        0,
        PlcRegistration(
            plc_device_id="line",
            runtime_url="opc.tcp://test:4840",
            variables=["ready"],
        ),
    )
    monkeypatch.setattr(store, "_write_json_atomically", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        service.push_opc_snapshot("demo.json", 1, "line", 1, {"ready": True})

    assert not conditions.evaluate(
        "demo.json",
        Trigger(kind="opc", config={"plc_device_id": "line", "variable": "ready", "value": True}),
    ).satisfied


def test_plan_persists_task_entries_for_samples_and_actual_times(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_ADMIN_TOKEN", "admin-secret")
    client = _client_with_workflow(tmp_path)
    templates = [
        _template("robot", resources=["robot"]),
        _template("station", resources=["station"]),
        _template("sample-first"),
        _template("sample-second"),
        _template("running"),
        {**_template("done"), "node_ids": []},
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
    assert entries["robot-b"]["start_at"] == entries["robot-a"]["start_at"]
    assert entries["station-a"]["start_at"] == entries["robot-a"]["start_at"]
    assert entries["sample-2"]["start_at"] == entries["sample-1"]["end_at"]
    assert entries["running-1"]["start_at"] == 500
    assert entries["done-1"]["start_at"] == 100
    assert entries["done-1"]["end_at"] == 250
    assert entries["done-1"]["state"] == "done"
    assert all(entry["resources"] == [] for entry in entries.values())
    assert planned.json()["workspace"]["schedule_entries"] == list(entries.values())


def test_advance_records_injected_start_and_finish_times_in_schedule_entries(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    now = [100]
    service = WorkspaceService(WorkspaceStore(tmp_path), clock=lambda: now[0])
    service.register_plc_variables(
        "demo.json",
        0,
        PlcRegistration(
            plc_device_id="line",
            runtime_url="opc.tcp://test:4840",
            variables=["ready"],
        ),
    )
    trigger = Trigger(
        kind="opc",
        config={"plc_device_id": "line", "variable": "ready", "value": True},
    )
    service.create_template(
        "demo.json",
        1,
        Template(
            id="task",
            name="task",
            node_ids=[],
            input_triggers=[trigger],
            output_triggers=[trigger],
        ),
    )
    service.push_opc_snapshot("demo.json", 2, "line", 1, {"ready": True})
    generated = service.generate_instances("demo.json", 3, ["task"], ["sample"])
    instance_id = generated.workspace.task_instances[0].id

    started, _ = service.advance("demo.json", 4)
    started_instance = started.workspace.task_instances[0]
    assert started_instance.status == "running"
    assert started_instance.started_at == 100
    assert started.workspace.schedule_entries[0].start_at == 100

    now[0] = 250
    completed, _ = service.advance("demo.json", 5, [instance_id])
    completed_instance = completed.workspace.task_instances[0]
    assert completed_instance.status == "completed"
    assert completed_instance.finished_at == 250
    assert completed.workspace.schedule_entries[0].end_at == 250


def test_sample_start_interval_delays_each_sample_and_rechecks_trigger(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    now_ms = [1_000]
    condition_now = [1.0]
    conditions = OpcConditionProvider(
        snapshot_ttl_seconds=30.0,
        clock=lambda: condition_now[0],
    )
    store = WorkspaceStore(tmp_path)
    trigger = Trigger(
        kind="opc",
        config={
            "plc_device_id": "line",
            "variable": "ready",
            "value": True,
        },
    )
    store.put(
        Workspace(
            workflow_path="demo.json",
            templates=[
                Template(
                    id="task",
                    name="task",
                    node_ids=[],
                    input_triggers=[trigger],
                )
            ],
            plc_registrations=[
                PlcRegistration(
                    plc_device_id="line",
                    runtime_url="opc.tcp://test:4840",
                    variables=["ready"],
                )
            ],
        ),
        expected_version=0,
    )
    service = WorkspaceService(
        store,
        conditions=conditions,
        clock=lambda: now_ms[0],
    )
    conditions.update("demo.json", "line", 1, {"ready": True})

    generated = service.generate_instances(
        "demo.json",
        1,
        ["task"],
        ["Sample A", "Sample B"],
        sample_start_interval_seconds=1.0,
    )
    instances = {
        item.sample_id: item for item in generated.workspace.task_instances
    }
    entries = {
        item.sample_id: item for item in generated.workspace.schedule_entries
    }

    assert instances["Sample A"].not_before == 1_000
    assert instances["Sample B"].not_before == 2_000
    assert entries["Sample A"].start_at == 1_000
    assert entries["Sample B"].start_at == 2_000

    started_a, _ = service.advance("demo.json", 2)
    states = {
        item.sample_id: item.status for item in started_a.workspace.task_instances
    }
    assert states == {"Sample A": "running", "Sample B": "waiting"}

    now_ms[0] = 2_000
    condition_now[0] = 2.0
    conditions.update("demo.json", "line", 2, {"ready": False})
    blocked_b, schedule = service.advance("demo.json", 3)
    states = {
        item.sample_id: item.status for item in blocked_b.workspace.task_instances
    }
    assert states == {"Sample A": "completed", "Sample B": "waiting"}
    sample_b = instances["Sample B"]
    assert schedule.waiting_reasons[sample_b.id].code == "opc_value_mismatch"

    conditions.update("demo.json", "line", 3, {"ready": True})
    started_b, _ = service.advance("demo.json", 4)
    states = {
        item.sample_id: item.status for item in started_b.workspace.task_instances
    }
    assert states == {"Sample A": "completed", "Sample B": "running"}


def test_sample_start_interval_is_shared_by_templates_of_the_same_sample(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    store.put(
        Workspace(
            workflow_path="demo.json",
            templates=[
                Template(id="first", name="first", node_ids=["first-node"]),
                Template(id="second", name="second", node_ids=["second-node"]),
            ],
        ),
        expected_version=0,
    )
    service = WorkspaceService(store, clock=lambda: 10_000)

    generated = service.generate_instances(
        "demo.json",
        1,
        ["first", "second"],
        ["Sample A", "Sample B"],
        sample_start_interval_seconds=1.0,
    )
    by_sample = {
        sample_id: sorted(
            (
                item
                for item in generated.workspace.task_instances
                if item.sample_id == sample_id
            ),
            key=lambda item: item.order,
        )
        for sample_id in ("Sample A", "Sample B")
    }

    assert [item.order for item in by_sample["Sample A"]] == [0, 1]
    assert [item.not_before for item in by_sample["Sample A"]] == [10_000, 10_000]
    assert [item.order for item in by_sample["Sample B"]] == [0, 1]
    assert [item.not_before for item in by_sample["Sample B"]] == [11_000, 11_000]


def test_running_gantt_entry_extends_through_current_time(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    store.put(
        Workspace(
            workflow_path="demo.json",
            templates=[
                Template(
                    id="running-template",
                    name="running-template",
                    node_ids=["node"],
                )
            ],
            task_instances=[{
                "id": "running-instance",
                "template_id": "running-template",
                "status": "running",
                "sample_id": "sample",
                "order": 0,
                "started_at": 1_000,
            }],
        ),
        expected_version=0,
    )

    response, _ = WorkspaceService(
        store,
        clock=lambda: 100_000,
    ).plan("demo.json", 1)
    entry = response.workspace.schedule_entries[0]

    assert entry.start_at == 1_000
    assert entry.end_at == 100_000


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


def test_plc_registration_gates_template_conditions_and_runtime_url_change(tmp_path, monkeypatch):
    """模板只能引用当前 PLC runtime 注册的变量，切换 URL 必须废弃旧快照。"""
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    headers = {"Authorization": "Bearer gateway-secret"}
    trigger = {
        "kind": "opc",
        "config": {"plc_device_id": "unregistered-plc", "variable": "ready", "value": True},
    }
    template = _template(
        "registered-task",
        input_triggers=[trigger],
        output_triggers=[trigger],
    )

    unregistered = client.post(
        "/templates",
        json={"workflow_path": "demo.json", "expected_version": 0, "template": template},
    )
    assert unregistered.status_code == 409
    assert unregistered.json()["detail"]["code"] == "unregistered_plc_variable"

    empty_conditions = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("empty", input_triggers=[], output_triggers=[]),
        },
    )
    assert empty_conditions.status_code == 200

    registered = client.post(
        "/opc/registrations",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "registration": {
                "plc_device_id": "unregistered-plc",
                "runtime_url": "opc.tcp://first:4840",
                "variables": ["ready"],
            },
        },
        headers=headers,
    )
    assert registered.status_code == 200
    created = client.post(
        "/templates",
        json={"workflow_path": "demo.json", "expected_version": 2, "template": template},
    )
    assert created.status_code == 200
    snapshot = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "plc_device_id": "unregistered-plc",
            "sequence": 1,
            "values": {"ready": True},
        },
        headers=headers,
    )
    assert snapshot.status_code == 200

    changed = client.post(
        "/opc/registrations",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "registration": {
                "plc_device_id": "unregistered-plc",
                "runtime_url": "opc.tcp://second:4840",
                "variables": ["ready"],
            },
        },
        headers=headers,
    )
    assert changed.status_code == 200
    workspace = changed.json()["workspace"]
    assert workspace["scheduler_paused"] is True
    assert not [
        item for item in workspace["opc_snapshots"]
        if item["plc_device_id"] == "unregistered-plc"
    ]
    assert next(
        item for item in workspace["plc_registrations"]
        if item["plc_device_id"] == "unregistered-plc"
    ) == {
        "plc_device_id": "unregistered-plc",
        "runtime_url": "opc.tcp://second:4840",
        "variables": ["ready"],
        "aliases": {},
    }


def test_template_update_rejects_empty_or_unregistered_conditions(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    headers = {"Authorization": "Bearer gateway-secret"}
    assert client.post(
        "/opc/registrations",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "registration": {
                "plc_device_id": "unregistered-plc",
                "runtime_url": "opc.tcp://first:4840",
                "variables": ["ready"],
            },
        },
        headers=headers,
    ).status_code == 200
    trigger = {
        "kind": "opc",
        "config": {"plc_device_id": "plc-a", "variable": "ready", "value": True},
    }
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template": _template("task", input_triggers=[trigger], output_triggers=[trigger]),
        },
    ).status_code == 200

    empty = client.patch(
        "/templates/task",
        json={"workflow_path": "demo.json", "expected_version": 2, "input_triggers": []},
    )
    assert empty.status_code == 200
    missing = client.patch(
        "/templates/task",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "output_triggers": [{
                "kind": "opc",
                "config": {"plc_device_id": "plc-a", "variable": "missing", "value": True},
            }],
        },
    )
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "unregistered_plc_variable"


def test_plc_registration_resolves_english_aliases_to_canonical_csv_names(tmp_path, monkeypatch):
    """中文 CSV 节点名是 Task 持久化和快照判定的唯一键。"""
    monkeypatch.setenv("TASK_ORCHESTRATION_GATEWAY_TOKEN", "gateway-secret")
    client = _client_with_workflow(tmp_path)
    headers = {"Authorization": "Bearer gateway-secret"}
    canonical_name = "S08取放料产品"
    english_alias = "S08_PickPlace_Product"

    registration = client.post(
        "/opc/registrations",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "registration": {
                "plc_device_id": "szlab_poly_plc",
                "runtime_url": "opc.tcp://plc:4840",
                "variables": [canonical_name, "Ready"],
                "aliases": {english_alias: canonical_name},
            },
        },
        headers=headers,
    )
    assert registration.status_code == 200

    alias_trigger = {
        "kind": "opc",
        "config": {
            "plc_device_id": "szlab_poly_plc",
            "variable": english_alias,
            "value": 7,
        },
    }
    direct_trigger = {
        "kind": "opc",
        "config": {
            "plc_device_id": "szlab_poly_plc",
            "variable": "Ready",
            "value": True,
        },
    }
    created = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template": _template(
                "alias-task",
                input_triggers=[alias_trigger],
                output_triggers=[direct_trigger],
            ),
        },
    )
    assert created.status_code == 200
    stored_template = created.json()["workspace"]["templates"][0]
    assert stored_template["input_triggers"][0]["config"]["variable"] == canonical_name
    assert stored_template["output_triggers"][0]["config"]["variable"] == "Ready"

    snapshot = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "plc_device_id": "szlab_poly_plc",
            "sequence": 1,
            "values": {english_alias: 7, "Ready": True},
        },
        headers=headers,
    )
    assert snapshot.status_code == 200
    values = next(
        item["values"]
        for item in snapshot.json()["workspace"]["opc_snapshots"]
        if item["plc_device_id"] == "szlab_poly_plc"
    )
    assert values == {canonical_name: 7, "Ready": True}


def test_plc_registration_uses_latest_workspace_when_frontend_version_is_stale(tmp_path):
    """PLC runtime 注册不能被前端轮询或模板写入推进的版本阻塞。"""
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(tmp_path))
    assert client.put(
        "/workspaces",
        json={
            "expected_version": 0,
            "workspace": {"workflow_path": "demo.json", "templates": [], "task_instances": []},
        },
    ).status_code == 200

    registered = client.post(
        "/opc/registrations",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "registration": {
                "plc_device_id": "line",
                "runtime_url": "opc.tcp://test:4840",
                "variables": ["ready"],
            },
        },
    )

    assert registered.status_code == 200
    assert registered.json()["version"] == 2
    assert registered.json()["workspace"]["plc_registrations"] == [{
        "plc_device_id": "line",
        "runtime_url": "opc.tcp://test:4840",
        "variables": ["ready"],
        "aliases": {},
    }]


def test_concurrent_plc_registrations_ignore_stale_frontend_versions(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    service = WorkspaceService(WorkspaceStore(tmp_path))

    def register(device_id: str):
        return service.register_plc_variables(
            "demo.json",
            0,
            PlcRegistration(
                plc_device_id=device_id,
                runtime_url=f"opc.tcp://{device_id}:4840",
                variables=["ready"],
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(register, ("line-a", "line-b")))

    workspace = service.store.get("demo.json")
    assert workspace.version == 2
    assert {item.plc_device_id for item in workspace.workspace.plc_registrations} == {
        "line-a",
        "line-b",
    }


def _action_request(
    expected_version: int,
    instance_id: str,
    node_id: str,
    execution_id: str,
    **extra,
) -> dict:
    return {
        "workflow_path": "demo.json",
        "expected_version": expected_version,
        "instance_id": instance_id,
        "node_id": node_id,
        "execution_id": execution_id,
        **extra,
    }


def _client_with_running_action_tasks(tmp_path) -> TestClient:
    client = _client_with_workflow(tmp_path)
    trigger_ready = {
        "kind": "opc",
        "config": {"plc_device_id": "plc-a", "variable": "ready", "value": True},
    }
    trigger_done = {
        "kind": "opc",
        "config": {"plc_device_id": "plc-a", "variable": "done", "value": True},
    }
    workspace = {
        "workflow_path": "demo.json",
        "templates": [
            {
                **_template(
                    "pipeline",
                    resources=["robot", "s07", "s06"],
                    input_triggers=[trigger_ready],
                    output_triggers=[trigger_done],
                ),
                "node_ids": ["robot-action", "s07-action", "s06-action"],
            },
            {
                **_template(
                    "other",
                    resources=["s07", "s06"],
                    input_triggers=[trigger_ready],
                    output_triggers=[trigger_done],
                ),
                "node_ids": ["other-action"],
            },
        ],
        "task_instances": [
            {
                "id": "pipeline-1",
                "template_id": "pipeline",
                "status": "running",
                "sample_id": "sample-a",
                "order": 0,
                "started_at": 1,
            },
            {
                "id": "other-1",
                "template_id": "other",
                "status": "running",
                "sample_id": "sample-b",
                "order": 0,
                "started_at": 1,
            },
        ],
        "opc_snapshots": [
            {
                "plc_device_id": "plc-a",
                "sequence": 1,
                "values": {"ready": True, "done": False},
                "updated_at_by_variable": {"ready": 1.0, "done": 1.0},
            }
        ],
        "plc_registrations": [
            {
                "plc_device_id": "plc-a",
                "runtime_url": "opc.tcp://test:4840",
                "variables": ["ready", "done"],
            }
        ],
    }
    saved = client.put(
        "/workspaces",
        json={"expected_version": 0, "workspace": workspace},
    )
    assert saved.status_code == 200
    return client


@pytest.mark.parametrize("replay_kind", ["claim", "succeed", "fail"])
def test_action_replay_persists_legacy_lease_cleanup(tmp_path, replay_kind):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    terminal_status = {
        "claim": "running",
        "succeed": "succeeded",
        "fail": "failed",
    }[replay_kind]
    record = {
        "node_id": "node-1",
        "attempt": 1,
        "execution_id": "legacy-execution",
        "status": terminal_status,
        "started_at": 1,
        "resources": ["robot"],
    }
    if terminal_status in {"succeeded", "failed"}:
        record["finished_at"] = 2
    if terminal_status == "succeeded":
        record["result"] = {"ok": True}
    if terminal_status == "failed":
        record["error"] = {"code": "failed"}
    instance = {
        "id": "instance",
        "template_id": "template",
        "status": "failed" if replay_kind == "fail" else "running",
        "sample_id": "sample",
        "order": 0,
        "started_at": 1,
        "finished_at": 2 if replay_kind == "fail" else None,
        "execution_state": {
            "cursor": 1 if replay_kind == "succeed" else 0,
            "records": [record],
            "active_node_id": "node-1" if replay_kind == "claim" else None,
            "active_execution_id": (
                "legacy-execution" if replay_kind == "claim" else None
            ),
        },
    }
    workspace_payload = {
        "workflow_path": "demo.json",
        "templates": [{
            "id": "template",
            "name": "template",
            "node_ids": ["node-1", "node-2"],
        }],
        "task_instances": [instance],
        "dynamic_resource_leases": [{
            "resource": "robot",
            "instance_id": "instance",
            "node_id": "node-1",
            "execution_id": "legacy-execution",
            "acquired_at": 1,
        }],
    }
    if replay_kind == "fail":
        workspace_payload.update({
            "scheduler_paused": True,
            "pause_reason": {
                "code": "action_failed",
                "message": "failed",
                "instance_id": "instance",
                "node_id": "node-1",
                "execution_id": "legacy-execution",
                "timestamp": 2,
                "detail": {"error": {"code": "failed"}},
            },
        })
    store = WorkspaceStore(tmp_path)
    store.put(Workspace.model_validate(workspace_payload), expected_version=0)
    service = WorkspaceService(store)

    if replay_kind == "claim":
        response = service.claim_action(
            "demo.json",
            1,
            "instance",
            "node-1",
            "legacy-execution",
            ["ignored"],
        )
    elif replay_kind == "succeed":
        response = service.succeed_action(
            "demo.json",
            1,
            "instance",
            "node-1",
            "legacy-execution",
            result={"ok": True},
            release_resources=["ignored"],
        )
    else:
        response = service.fail_action(
            "demo.json",
            1,
            "instance",
            "node-1",
            "legacy-execution",
            error={"code": "failed"},
        )

    assert response.version == 2
    assert response.workspace.dynamic_resource_leases == []
    persisted = store.get("demo.json")
    assert persisted.version == 2
    assert persisted.workspace.dynamic_resource_leases == []


def test_action_claim_succeed_ignores_resources_replays_and_output_gate(tmp_path):
    client = _client_with_running_action_tasks(tmp_path)

    claimed = client.post(
        "/actions:claim",
        json=_action_request(
            1,
            "pipeline-1",
            "robot-action",
            "exec-robot",
            resources=["robot"],
        ),
    )
    assert claimed.status_code == 200
    assert claimed.json()["workspace"]["dynamic_resource_leases"] == []
    claimed_record = next(
        item
        for item in claimed.json()["workspace"]["task_instances"]
        if item["id"] == "pipeline-1"
    )["execution_state"]["records"][0]
    assert claimed_record["resources"] == []

    replay = client.post(
        "/actions:claim",
        json=_action_request(
            1,
            "pipeline-1",
            "robot-action",
            "exec-robot",
            resources=["robot"],
        ),
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == 2
    normalized_replay = client.post(
        "/actions:claim",
        json=_action_request(
            1,
            "pipeline-1",
            "robot-action",
            "exec-robot",
            resources=["s07"],
        ),
    )
    assert normalized_replay.status_code == 200
    assert normalized_replay.json()["version"] == 2

    robot_done = client.post(
        "/actions:succeed",
        json=_action_request(
            2,
            "pipeline-1",
            "robot-action",
            "exec-robot",
            result={"value": True},
            release_resources=["robot"],
        ),
    )
    assert robot_done.status_code == 200
    body = robot_done.json()["workspace"]
    pipeline = next(item for item in body["task_instances"] if item["id"] == "pipeline-1")
    assert pipeline["status"] == "running"
    assert pipeline["execution_state"]["cursor"] == 1
    assert pipeline["execution_state"]["records"][0]["release_resources"] == []
    assert body["dynamic_resource_leases"] == []
    success_replay = client.post(
        "/actions:succeed",
        json=_action_request(
            2,
            "pipeline-1",
            "robot-action",
            "exec-robot",
            result={"value": True},
            release_resources=["robot"],
        ),
    )
    assert success_replay.status_code == 200
    assert success_replay.json()["version"] == 3
    success_conflict = client.post(
        "/actions:succeed",
        json=_action_request(
            2,
            "pipeline-1",
            "robot-action",
            "exec-robot",
            result={"value": 1},
            release_resources=["robot"],
        ),
    )
    assert success_conflict.status_code == 409
    assert success_conflict.json()["detail"]["code"] == "action_replay_conflict"

    station_claim = client.post(
        "/actions:claim",
        json=_action_request(
            3,
            "pipeline-1",
            "s07-action",
            "exec-s07",
            resources=["s07", "s06"],
        ),
    )
    assert station_claim.status_code == 200
    station_done = client.post(
        "/actions:succeed",
        json=_action_request(
            4,
            "pipeline-1",
            "s07-action",
            "exec-s07",
            result=None,
            release_resources=[],
        ),
    )
    assert station_done.status_code == 200

    transferred = client.post(
        "/actions:claim",
        json=_action_request(
            5,
            "pipeline-1",
            "s06-action",
            "exec-s06",
            resources=["s07", "s06"],
        ),
    )
    assert transferred.status_code == 200
    assert transferred.json()["workspace"]["dynamic_resource_leases"] == []
    parallel = client.post(
        "/actions:claim",
        json=_action_request(
            6,
            "other-1",
            "other-action",
            "exec-other",
            resources=["s07", "s06"],
        ),
    )
    assert parallel.status_code == 200
    assert parallel.json()["workspace"]["dynamic_resource_leases"] == []
    stale = client.post(
        "/actions:claim",
        json=_action_request(
            5,
            "other-1",
            "other-action",
            "exec-stale",
            resources=[],
        ),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "action_already_active"

    final_action = client.post(
        "/actions:succeed",
        json=_action_request(
            7,
            "pipeline-1",
            "s06-action",
            "exec-s06",
            result={"ok": True},
            release_resources=[],
        ),
    )
    assert final_action.status_code == 200
    pipeline = next(
        item
        for item in final_action.json()["workspace"]["task_instances"]
        if item["id"] == "pipeline-1"
    )
    assert pipeline["status"] == "completed"
    assert pipeline["execution_state"]["cursor"] == 3
    assert final_action.json()["workspace"]["dynamic_resource_leases"] == []


def test_failed_action_pauses_workspace_without_leases_and_never_retries(tmp_path):
    client = _client_with_running_action_tasks(tmp_path)
    claimed = client.post(
        "/actions:claim",
        json=_action_request(
            1,
            "pipeline-1",
            "robot-action",
            "exec-fail",
            resources=["robot"],
        ),
    )
    assert claimed.status_code == 200

    failed = client.post(
        "/actions:fail",
        json=_action_request(
            2,
            "pipeline-1",
            "robot-action",
            "exec-fail",
            error={"code": "robot_timeout", "retryable": False},
        ),
    )
    assert failed.status_code == 200
    workspace = failed.json()["workspace"]
    instance = next(item for item in workspace["task_instances"] if item["id"] == "pipeline-1")
    assert instance["status"] == "failed"
    assert instance["execution_state"]["active_execution_id"] is None
    assert [record["status"] for record in instance["execution_state"]["records"]] == ["failed"]
    assert workspace["scheduler_paused"] is True
    assert workspace["pause_reason"]["code"] == "action_failed"
    assert workspace["pause_reason"]["detail"] == {
        "error": {"code": "robot_timeout", "retryable": False}
    }
    assert workspace["dynamic_resource_leases"] == []

    replay = client.post(
        "/actions:fail",
        json=_action_request(
            2,
            "pipeline-1",
            "robot-action",
            "exec-fail",
            error={"code": "robot_timeout", "retryable": False},
        ),
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == 3
    conflict = client.post(
        "/actions:fail",
        json=_action_request(
            2,
            "pipeline-1",
            "robot-action",
            "exec-fail",
            error={"code": "robot_timeout", "retryable": 0},
        ),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "action_replay_conflict"
    unpause = client.post(
        "/schedule:plan",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "paused": False,
        },
    )
    assert unpause.status_code == 409
    assert unpause.json()["detail"]["code"] == "workspace_recovery_required"
    rejected = client.post(
        "/actions:claim",
        json=_action_request(
            3,
            "other-1",
            "other-action",
            "exec-after-fail",
            resources=[],
        ),
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "workspace_paused"
    assert len(
        next(
            item
            for item in replay.json()["workspace"]["task_instances"]
            if item["id"] == "pipeline-1"
        )["execution_state"]["records"]
    ) == 1


def test_explicit_completion_requires_exhausted_cursor_and_no_active_action(tmp_path):
    client = _client_with_running_action_tasks(tmp_path)
    snapshot = client.post(
        "/opc/snapshots",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "plc_device_id": "plc-a",
            "sequence": 2,
            "values": {"done": True},
        },
    )
    assert snapshot.status_code == 200

    premature = client.post(
        "/schedule:advance",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "completed_instance_ids": ["pipeline-1"],
        },
    )
    assert premature.status_code == 409
    assert premature.json()["detail"]["code"] == "execution_not_finished"

    claimed = client.post(
        "/actions:claim",
        json=_action_request(
            2,
            "pipeline-1",
            "robot-action",
            "exec-active",
            resources=[],
        ),
    )
    assert claimed.status_code == 200
    active = client.post(
        "/schedule:advance",
        json={
            "workflow_path": "demo.json",
            "expected_version": 3,
            "completed_instance_ids": ["pipeline-1"],
        },
    )
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "execution_not_finished"

    workspace = claimed.json()["workspace"]
    pipeline = next(
        item for item in workspace["task_instances"] if item["id"] == "pipeline-1"
    )
    pipeline["execution_state"] = {
        "cursor": 3,
        "records": [
            {
                "node_id": node_id,
                "attempt": 1,
                "execution_id": f"completed-{index}",
                "status": "succeeded",
                "started_at": index * 2 + 1,
                "finished_at": index * 2 + 2,
            }
            for index, node_id in enumerate(
                ["robot-action", "s07-action", "s06-action"]
            )
        ],
        "active_execution_id": None,
        "active_node_id": None,
    }
    saved = client.put(
        "/workspaces",
        json={"expected_version": 3, "workspace": workspace},
    )
    assert saved.status_code == 200
    completed = client.post(
        "/schedule:advance",
        json={
            "workflow_path": "demo.json",
            "expected_version": 4,
            "completed_instance_ids": ["pipeline-1"],
        },
    )
    assert completed.status_code == 200
    assert next(
        item
        for item in completed.json()["workspace"]["task_instances"]
        if item["id"] == "pipeline-1"
    )["status"] == "completed"


def test_concurrent_action_claims_across_stores_are_version_serialized(
    tmp_path,
):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    first_store = WorkspaceStore(tmp_path)
    second_store = WorkspaceStore(tmp_path)
    first_store.put(
        Workspace(
            workflow_path="demo.json",
            templates=[
                Template(
                    id="shared",
                    name="shared",
                    node_ids=["node"],
                    resources=["robot"],
                )
            ],
            task_instances=[
                {
                    "id": "instance-a",
                    "template_id": "shared",
                    "status": "running",
                    "sample_id": "sample-a",
                    "order": 0,
                    "started_at": 1,
                },
                {
                    "id": "instance-b",
                    "template_id": "shared",
                    "status": "running",
                    "sample_id": "sample-b",
                    "order": 0,
                    "started_at": 1,
                },
            ],
        ),
        expected_version=0,
    )
    services = [WorkspaceService(first_store), WorkspaceService(second_store)]
    barrier = Barrier(2)

    def claim(index: int) -> str:
        barrier.wait()
        try:
            services[index].claim_action(
                "demo.json",
                1,
                f"instance-{'a' if index == 0 else 'b'}",
                "node",
                f"exec-{index}",
                ["robot"],
            )
            return "success"
        except WorkspaceServiceError as exc:
            return exc.code
        except VersionConflictError:
            return "version_conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, (0, 1)))

    assert outcomes.count("success") == 1
    assert next(item for item in outcomes if item != "success") == "version_conflict"
    final = WorkspaceStore(tmp_path).get("demo.json").workspace
    assert final.dynamic_resource_leases == []
    records = [
        record
        for instance in final.task_instances
        for record in instance.execution_state.records
    ]
    assert len(records) == 1
    assert records[0].status == "running"


def test_advance_starts_three_samples_before_action_resource_claims(tmp_path):
    client = _client_with_workflow(tmp_path)
    created = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template("shared", resources=["robot", "s07", "s06"]),
        },
    )
    assert created.status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["shared"],
            "sample_ids": ["sample-a", "sample-b", "sample-c"],
        },
    )
    started = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 2},
    )

    assert generated.status_code == 200
    assert started.status_code == 200
    assert [item["status"] for item in started.json()["workspace"]["task_instances"]] == [
        "running",
        "running",
        "running",
    ]


def test_empty_trigger_template_starts_and_completes_after_its_nodes(tmp_path):
    client = _client_with_workflow(tmp_path)
    created = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template(
                "ungated",
                input_triggers=[],
                output_triggers=[],
            ),
        },
    )
    assert created.status_code == 200
    generated = client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["ungated"],
            "sample_ids": ["sample"],
        },
    )
    instance_id = generated.json()["workspace"]["task_instances"][0]["id"]
    started = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 2},
    )
    assert started.json()["workspace"]["task_instances"][0]["status"] == "running"

    claimed = client.post(
        "/actions:claim",
        json=_action_request(
            3,
            instance_id,
            "ungated-node",
            "exec-ungated",
            resources=["legacy-robot"],
        ),
    )
    assert claimed.status_code == 200
    completed = client.post(
        "/actions:succeed",
        json=_action_request(
            4,
            instance_id,
            "ungated-node",
            "exec-ungated",
            release_resources=["unknown-resource"],
        ),
    )

    assert completed.status_code == 200
    instance = completed.json()["workspace"]["task_instances"][0]
    assert instance["status"] == "completed"
    assert instance["execution_state"]["records"][0]["resources"] == []
    assert instance["execution_state"]["records"][0]["release_resources"] == []
    assert completed.json()["workspace"]["dynamic_resource_leases"] == []


def test_last_action_completes_without_waiting_for_false_output_trigger(tmp_path):
    client = _client_with_running_action_tasks(tmp_path)
    workspace = client.get(
        "/workspaces", params={"workflow_path": "demo.json"}
    ).json()["workspace"]
    pipeline = next(
        item for item in workspace["task_instances"] if item["id"] == "pipeline-1"
    )
    pipeline["execution_state"] = {
        "cursor": 2,
        "records": [
            {
                "node_id": node_id,
                "attempt": 1,
                "execution_id": f"done-{index}",
                "status": "succeeded",
                "started_at": index * 2 + 1,
                "finished_at": index * 2 + 2,
            }
            for index, node_id in enumerate(["robot-action", "s07-action"])
        ],
    }
    saved = client.put(
        "/workspaces",
        json={"expected_version": 1, "workspace": workspace},
    )
    assert saved.status_code == 200
    claimed = client.post(
        "/actions:claim",
        json=_action_request(
            2,
            "pipeline-1",
            "s06-action",
            "last-execution",
            resources=[],
        ),
    )
    completed = client.post(
        "/actions:succeed",
        json=_action_request(
            3,
            "pipeline-1",
            "s06-action",
            "last-execution",
            release_resources=[],
        ),
    )

    assert claimed.status_code == 200
    assert completed.status_code == 200
    assert next(
        item
        for item in completed.json()["workspace"]["task_instances"]
        if item["id"] == "pipeline-1"
    )["status"] == "completed"


def test_poll_completes_exhausted_task_without_output_trigger_gate(tmp_path):
    client = _client_with_workflow(tmp_path)
    false_output = {
        "kind": "opc",
        "config": {"plc_device_id": "plc-a", "variable": "done", "value": True},
    }
    assert client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": {
                **_template(
                    "poll-complete",
                    input_triggers=[],
                    output_triggers=[false_output],
                ),
                "node_ids": [],
            },
        },
    ).status_code == 200
    assert client.post(
        "/instances:generate",
        json={
            "workflow_path": "demo.json",
            "expected_version": 1,
            "template_ids": ["poll-complete"],
            "sample_ids": ["sample"],
        },
    ).status_code == 200
    started = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 2},
    )
    polled = client.post(
        "/schedule:advance",
        json={"workflow_path": "demo.json", "expected_version": 3},
    )

    assert started.json()["workspace"]["task_instances"][0]["status"] == "running"
    assert polled.json()["workspace"]["task_instances"][0]["status"] == "completed"
    assert polled.json()["schedule"]["waiting_reasons"] == {}


def test_claim_ignores_shared_resources_and_normalizes_replay_semantics(tmp_path):
    client = _client_with_running_action_tasks(tmp_path)

    first = client.post(
        "/actions:claim",
        json=_action_request(
            1,
            "pipeline-1",
            "robot-action",
            "exec-first",
            resources=["shared"],
        ),
    )
    second = client.post(
        "/actions:claim",
        json=_action_request(
            2,
            "other-1",
            "other-action",
            "exec-second",
            resources=["shared"],
        ),
    )
    replay_with_different_legacy_payload = client.post(
        "/actions:claim",
        json=_action_request(
            1,
            "pipeline-1",
            "robot-action",
            "exec-first",
            resources=["different"],
        ),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert replay_with_different_legacy_payload.status_code == 200
    assert replay_with_different_legacy_payload.json()["version"] == 3
    workspace = second.json()["workspace"]
    assert workspace["dynamic_resource_leases"] == []
    assert all(
        record["resources"] == []
        for instance in workspace["task_instances"]
        for record in instance["execution_state"]["records"]
    )


def test_plan_clears_legacy_leases_but_get_does_not_rewrite_sidecar(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    sidecar = tmp_path / "demo.json.task-workspace.json"
    legacy_workspace = Workspace.model_validate({
        "workflow_path": "demo.json",
        "templates": [{
            "id": "legacy",
            "name": "legacy",
            "node_ids": ["node"],
            "resources": ["robot"],
        }],
        "task_instances": [{
            "id": "instance",
            "template_id": "legacy",
            "status": "running",
            "sample_id": "sample",
            "order": 0,
            "started_at": 1,
            "execution_state": {
                "records": [{
                    "node_id": "node",
                    "attempt": 1,
                    "execution_id": "legacy-exec",
                    "status": "running",
                    "started_at": 1,
                    "resources": ["robot"],
                }],
                "active_node_id": "node",
                "active_execution_id": "legacy-exec",
            },
        }],
        "dynamic_resource_leases": [{
            "resource": "robot",
            "instance_id": "instance",
            "node_id": "node",
            "execution_id": "legacy-exec",
            "acquired_at": 1,
        }],
    })
    sidecar.write_text(
        json.dumps({
            "version": 1,
            "workspace": legacy_workspace.model_dump(mode="json"),
        }),
        encoding="utf-8",
    )
    original = sidecar.read_text(encoding="utf-8")
    client = TestClient(create_app(tmp_path))

    loaded = client.get("/workspaces", params={"workflow_path": "demo.json"})
    assert loaded.status_code == 200
    assert loaded.json()["workspace"]["dynamic_resource_leases"]
    assert sidecar.read_text(encoding="utf-8") == original

    rebuilt = client.post(
        "/schedule:plan",
        json={"workflow_path": "demo.json", "expected_version": 1},
    )
    assert rebuilt.status_code == 200
    assert rebuilt.json()["workspace"]["dynamic_resource_leases"] == []


def test_put_workspace_clears_legacy_dynamic_resource_leases(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    legacy_workspace = Workspace.model_validate({
        "workflow_path": "demo.json",
        "templates": [{
            "id": "legacy",
            "name": "legacy",
            "node_ids": ["node"],
            "resources": ["robot"],
        }],
        "task_instances": [{
            "id": "instance",
            "template_id": "legacy",
            "status": "running",
            "sample_id": "sample",
            "order": 0,
            "started_at": 1,
            "execution_state": {
                "records": [{
                    "node_id": "node",
                    "attempt": 1,
                    "execution_id": "legacy-exec",
                    "status": "running",
                    "started_at": 1,
                    "resources": ["robot"],
                }],
                "active_node_id": "node",
                "active_execution_id": "legacy-exec",
            },
        }],
        "dynamic_resource_leases": [{
            "resource": "robot",
            "instance_id": "instance",
            "node_id": "node",
            "execution_id": "legacy-exec",
            "acquired_at": 1,
        }],
    })
    client = TestClient(create_app(tmp_path))

    saved = client.put(
        "/workspaces",
        json={
            "expected_version": 0,
            "workspace": legacy_workspace.model_dump(mode="json"),
        },
    )

    assert saved.status_code == 200
    assert saved.json()["workspace"]["dynamic_resource_leases"] == []
    assert client.get(
        "/workspaces", params={"workflow_path": "demo.json"}
    ).json()["workspace"]["dynamic_resource_leases"] == []


def test_template_create_and_update_clear_legacy_resources(tmp_path):
    client = _client_with_workflow(tmp_path)
    created = client.post(
        "/templates",
        json={
            "workflow_path": "demo.json",
            "expected_version": 0,
            "template": _template(
                "created",
                resources=["resource:legacy-robot", "", "resource:legacy-robot"],
            ),
        },
    )
    assert created.status_code == 200
    assert created.json()["workspace"]["templates"][0]["resources"] == []

    legacy_workspace = created.json()["workspace"]
    legacy_workspace["templates"][0]["resources"] = ["reintroduced"]
    persisted = client.put(
        "/workspaces",
        json={"expected_version": 1, "workspace": legacy_workspace},
    )
    assert persisted.status_code == 200
    updated = client.patch(
        "/templates/created",
        json={
            "workflow_path": "demo.json",
            "expected_version": 2,
            "name": "Updated",
        },
    )

    assert updated.status_code == 200
    assert updated.json()["workspace"]["templates"][0]["resources"] == []


def test_gantt_ignores_legacy_resources_across_samples(tmp_path):
    (tmp_path / "demo.json").write_text("{}", encoding="utf-8")
    store = WorkspaceStore(tmp_path)
    store.put(
        Workspace(
            workflow_path="demo.json",
            templates=[
                Template(
                    id="shared",
                    name="shared",
                    node_ids=["node"],
                    resources=["legacy-robot"],
                )
            ],
            task_instances=[
                {
                    "id": "sample-a",
                    "template_id": "shared",
                    "status": "pending",
                    "sample_id": "a",
                    "order": 0,
                },
                {
                    "id": "sample-b",
                    "template_id": "shared",
                    "status": "pending",
                    "sample_id": "b",
                    "order": 0,
                },
            ],
        ),
        expected_version=0,
    )
    response, schedule = WorkspaceService(
        store,
        clock=lambda: 1_000,
    ).plan("demo.json", 1)
    entries = {entry.instance_id: entry for entry in schedule.entries}

    assert entries["sample-a"].resources == []
    assert entries["sample-b"].resources == []
    assert entries["sample-a"].start_at == entries["sample-b"].start_at == 1_000
    assert response.workspace.schedule_entries == schedule.entries
