"""验证 Runtime 能力与工作流运行模式的独立边界。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.app.runtime_capabilities import (
    RuntimeCapabilities,
    reset_runtime_capabilities,
    set_runtime_capabilities,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import StoreConflict, WorkflowStore


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowService, WorkflowStore]:
    store = WorkflowStore(tmp_path / "runtime-capabilities.db")
    service = WorkflowService(store)
    return TestClient(create_workflow_app(service)), service, store


def _create_and_publish(
    client: TestClient,
    *,
    name: str,
    workflow_type: str = "normal",
) -> str:
    created = client.post(
        "/api/v1/workflows",
        json={
            "name": name,
            "workflow_type": workflow_type,
            "tags": [],
            "meta_data": {},
        },
    )
    assert created.status_code == 201
    workflow_uuid = created.json()["data"]["uuid"]
    graph = client.put(
        f"/api/v1/workflows/{workflow_uuid}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": str(uuid4()),
                    "name": f"节点-{name}",
                    "type": "compute",
                    "pose": {"x": 0, "y": 0},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    assert graph.status_code == 200
    revision = graph.json()["data"]["workflow"]["revision"]
    published = client.post(
        f"/api/v1/workflows/{workflow_uuid}/publications",
        json={"revision": revision},
    )
    assert published.status_code == 201
    return workflow_uuid


def _seed_active_task(
    store: WorkflowStore,
    workflow_uuid: str,
    *,
    run_mode: str,
    status: str = "running",
) -> str:
    task_uuid = str(uuid4())
    execution_mode = "step" if run_mode == "step" else "normal"
    control_status = "paused" if run_mode == "step" else "active"
    dispatch_gate = "paused" if run_mode == "step" else "active"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, meta_data, workflow_uuid,
                status, workflow_snapshot, execution_plan, run_mode,
                execution_mode, control_status, dispatch_gate, cleanup_status,
                trace_context, input, output, error_info
            ) VALUES (?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z',
                      '{}', ?, ?, '{}',
                      '{"version":1,"nodes":[],"edges":[],"handles":[]}',
                      ?, ?, ?, ?, 'none', '{}', '{}', '{}', '[]')
            """,
            (
                task_uuid,
                workflow_uuid,
                status,
                run_mode,
                execution_mode,
                control_status,
                dispatch_gate,
            ),
        )
    return task_uuid


@pytest.fixture(autouse=True)
def _reset_capabilities():
    reset_runtime_capabilities()
    yield
    reset_runtime_capabilities()


def test_runtime_capabilities_are_independent_of_task_run_mode() -> None:
    capabilities = set_runtime_capabilities({"can_debug": False})
    assert capabilities.can_debug is False
    assert capabilities.can_edit_definitions is True
    assert capabilities.can_view_drafts is True


def test_readiness_exposes_runtime_capabilities_instead_of_a_global_mode() -> None:
    from unilabos.app.web.server import api_readiness

    set_runtime_capabilities(
        {
            "can_edit_definitions": False,
            "can_view_drafts": True,
            "can_run_unpublished": False,
            "can_debug": False,
            "can_execute_device_actions": True,
        }
    )

    payload = json.loads(api_readiness().body)

    assert payload["capabilities"] == {
        "can_edit_definitions": False,
        "can_view_drafts": True,
        "can_run_unpublished": False,
        "can_debug": False,
        "can_execute_device_actions": True,
    }
    assert "startupMode" not in payload


def test_runtime_capabilities_limit_definition_visibility_and_writes(tmp_path: Path) -> None:
    client, service, store = _client(tmp_path)
    try:
        published_normal = _create_and_publish(client, name="已发布普通")
        draft_normal = client.post(
            "/api/v1/workflows",
            json={"name": "普通草稿", "workflow_type": "normal"},
        ).json()["data"]["uuid"]
        published_operation = _create_and_publish(
            client,
            name="已发布实验操作",
            workflow_type="experiment_operation",
        )
        set_runtime_capabilities(
            RuntimeCapabilities(
                can_edit_definitions=False,
                can_view_drafts=False,
                can_run_unpublished=False,
                can_debug=False,
                can_execute_device_actions=False,
            )
        )
        listed = client.get("/api/v1/workflows").json()["data"]["items"]
        assert [item["uuid"] for item in listed] == [published_normal]
        assert client.get(f"/api/v1/workflows/{draft_normal}").json()["code"] == 3002
        assert client.get(f"/api/v1/workflows/{published_operation}").json()["code"] == 3002
        blocked_write = client.post("/api/v1/workflows", json={"name": "不可写"})
        assert blocked_write.json()["code"] == 1001
        assert "不允许修改" in blocked_write.json()["error"]["msg"]
    finally:
        service.close()
        store.close()


def test_execution_capability_can_restrict_unpublished_runs_without_hiding_drafts(
    tmp_path: Path,
) -> None:
    client, service, store = _client(tmp_path)
    try:
        draft_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "仍可查看的草稿", "workflow_type": "normal"},
        ).json()["data"]["uuid"]
        set_runtime_capabilities(
            {
                "can_view_drafts": True,
                "can_run_unpublished": False,
            }
        )

        assert client.get(f"/api/v1/workflows/{draft_uuid}").json()["code"] == 0
        preflight = client.get(
            f"/api/v1/workflows/{draft_uuid}/run-preflight?run_mode=normal"
        )
        assert preflight.json()["code"] == 3002
        task = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": draft_uuid, "run_mode": "normal", "input": {}},
        )
        assert task.json()["code"] == 3002
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize("status", ["pending", "running", "canceling"])
def test_step_preflight_reports_active_task_conflict(
    tmp_path: Path,
    status: str,
) -> None:
    client, service, store = _client(tmp_path)
    try:
        workflow_uuid = _create_and_publish(client, name="单步准入")
        occupied_uuid = _seed_active_task(
            store,
            workflow_uuid,
            run_mode="normal",
            status=status,
        )
        report = client.get(
            f"/api/v1/workflows/{workflow_uuid}/run-preflight?run_mode=step"
        ).json()["data"]
        admission = next(item for item in report["checks"] if item["type"] == "execution_admission")
        assert report["can_run"] is False
        assert admission["code"] == "debug_task_conflict"
        assert admission["details"]["task_uuid"] == occupied_uuid
        rejected = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": workflow_uuid, "run_mode": "step", "input": {}},
        )
        assert rejected.json()["error"]["code"] == "debug_task_conflict"
    finally:
        service.close()
        store.close()


def test_step_preflight_waits_for_terminal_task_cleanup(tmp_path: Path) -> None:
    client, service, store = _client(tmp_path)
    try:
        workflow_uuid = _create_and_publish(client, name="等待清理")
        occupied_uuid = _seed_active_task(
            store,
            workflow_uuid,
            run_mode="normal",
            status="failed",
        )
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_task SET cleanup_status='required' WHERE uuid=?",
                (occupied_uuid,),
            )

        report = client.get(
            f"/api/v1/workflows/{workflow_uuid}/run-preflight?run_mode=step"
        ).json()["data"]
        admission = next(item for item in report["checks"] if item["type"] == "execution_admission")
        assert report["can_run"] is False
        assert admission["code"] == "debug_task_conflict"
        assert admission["details"]["task_uuid"] == occupied_uuid
    finally:
        service.close()
        store.close()


def test_normal_task_can_start_while_another_normal_task_is_active(tmp_path: Path) -> None:
    client, service, store = _client(tmp_path)
    try:
        first = _create_and_publish(client, name="普通任务一")
        second = _create_and_publish(client, name="普通任务二")
        _seed_active_task(store, first, run_mode="normal")
        created = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": second, "run_mode": "normal", "input": {}},
        )
        assert created.status_code == 201
        assert created.json()["code"] == 0
    finally:
        service.close()
        store.close()


def test_normal_task_is_rejected_while_step_task_is_active(tmp_path: Path) -> None:
    client, service, store = _client(tmp_path)
    try:
        first = _create_and_publish(client, name="单步任务")
        second = _create_and_publish(client, name="普通任务")
        occupied_uuid = _seed_active_task(store, first, run_mode="step")
        rejected = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": second, "run_mode": "normal", "input": {}},
        )
        assert rejected.json()["error"]["code"] == "debug_task_conflict"
        assert occupied_uuid in rejected.json()["error"]["msg"]
    finally:
        service.close()
        store.close()


def test_switching_an_active_task_to_step_respects_the_same_admission_gate(
    tmp_path: Path,
) -> None:
    client, service, store = _client(tmp_path)
    try:
        first = _create_and_publish(client, name="已有普通任务")
        second = _create_and_publish(client, name="准备单步切换")
        first_task = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": first, "run_mode": "normal", "input": {}},
        ).json()["data"]["uuid"]
        second_task = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": second, "run_mode": "normal", "input": {}},
        ).json()["data"]["uuid"]

        with pytest.raises(StoreConflict, match="debug_task_conflict"):
            store.set_task_execution_mode(
                second_task,
                execution_mode="switching_to_step",
                control_status="paused",
            )

        assert store.get_task(first_task)["execution_mode"] == "normal"
        assert store.get_task(second_task)["execution_mode"] == "normal"
    finally:
        service.close()
        store.close()


def test_concurrent_step_switches_allow_only_one_task(tmp_path: Path) -> None:
    client, service, store = _client(tmp_path)
    try:
        first = _create_and_publish(client, name="并发单步一")
        second = _create_and_publish(client, name="并发单步二")
        barrier = Barrier(2)

        def submit(workflow_uuid: str) -> str:
            barrier.wait()
            try:
                service.create_workflow_task(
                    workflow_uuid=workflow_uuid,
                    run_mode="step",
                    target_node_uuid=None,
                    input_value={},
                    description=None,
                    meta_data={},
                )
            except WorkflowConflict as error:
                return error.code
            return "ok"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, [first, second]))

        assert results.count("ok") == 1
        assert results.count("debug_task_conflict") == 1
    finally:
        service.close()
        store.close()


def test_step_execution_requires_debug_capability(tmp_path: Path) -> None:
    client, service, store = _client(tmp_path)
    try:
        workflow_uuid = _create_and_publish(client, name="禁止调试")
        set_runtime_capabilities({"can_debug": False})
        preflight = client.get(
            f"/api/v1/workflows/{workflow_uuid}/run-preflight?run_mode=step"
        )
        assert preflight.json()["error"]["code"] == "debug_not_allowed"
        task = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": workflow_uuid, "run_mode": "step", "input": {}},
        )
        assert task.json()["error"]["code"] == "debug_not_allowed"
    finally:
        service.close()
        store.close()


def test_step_task_resume_requires_debug_capability(tmp_path: Path) -> None:
    client, service, store = _client(tmp_path)
    try:
        workflow_uuid = _create_and_publish(client, name="禁止恢复单步")
        task_uuid = _seed_active_task(store, workflow_uuid, run_mode="step")
        set_runtime_capabilities({"can_debug": False})

        resumed = client.post(
            f"/api/v1/workflow-tasks/{task_uuid}/commands",
            json={"type": "resume", "idempotency_key": "resume-step-blocked"},
        )

        assert resumed.status_code == 201
        assert resumed.json()["data"]["status"] == "rejected"
        assert resumed.json()["data"]["result"]["reason"] == "debug_not_allowed"
        assert store.get_task(task_uuid)["execution_mode"] == "step"
    finally:
        service.close()
        store.close()
