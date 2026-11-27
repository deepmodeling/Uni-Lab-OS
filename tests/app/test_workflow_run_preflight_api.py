"""工作流候选运行预检（RunPreflight）的公共只读合同。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.run_preflight import build_run_preflight_report
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


def _client(tmp_path):
    """创建隔离的工作流预检应用。"""

    store = WorkflowStore(tmp_path / "workflow_preflight.db")
    return TestClient(create_workflow_app(WorkflowService(store))), store


def test_empty_workflow_preflight_is_read_only_and_runnable_now(tmp_path) -> None:
    """空图预检须返回可展示报告、禁止缓存且不能创建工作流任务。"""

    client, store = _client(tmp_path)
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "空流程", "tags": [], "meta_data": {}},
    ).json()["data"]

    response = client.get(
        f"/api/v1/workflows/{workflow['uuid']}/run-preflight"
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    report = response.json()["data"]
    assert report["workflow_uuid"] == workflow["uuid"]
    assert report["workflow_revision"] == 1
    assert report["run_mode"] == "normal"
    assert report["status"] == "runnable_now"
    assert report["can_run"] is True
    assert report["summary"]["execution_node_count"] == 0
    assert report["summary"]["blocking_check_count"] == 0
    assert report["summary"]["deferred_check_count"] == 1
    assert len(report["checks"]) == 2
    resource_check = next(
        check for check in report["checks"] if check["type"] == "resource_lock"
    )
    assert resource_check["status"] == "deferred"
    assert resource_check["code"] == "resource_admission_at_dispatch"

    tasks = client.get(
        "/api/v1/workflow-tasks", params={"workflow_uuid": workflow["uuid"]}
    ).json()["data"]
    assert tasks["items"] == []
    store.close()


def test_pure_manual_gate_without_device_action_is_invalid(tmp_path: Path) -> None:
    """人工确认必须包装真实设备动作，不再兼容纯人工闸门。"""

    client, store = _client(tmp_path)
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "人工复核", "tags": [], "meta_data": {}},
    ).json()["data"]
    node_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    client.put(
        f"/api/v1/workflows/{workflow['uuid']}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": node_uuid,
                    "name": "确认样品",
                    "type": "manual_confirm",
                    "pose": {},
                    "param": {},
                    "manual_confirmation": {"timeout_seconds": 7200},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    )
    saved_graph = client.get(
        f"/api/v1/workflows/{workflow['uuid']}/graph"
    ).json()["data"]
    assert saved_graph["nodes"][0]["manual_confirmation"] == {
        "timeout_seconds": 7200
    }

    report = client.get(
        f"/api/v1/workflows/{workflow['uuid']}/run-preflight"
    ).json()["data"]
    assert report["status"] == "invalid"
    assert report["can_run"] is False
    invalid = next(
        check for check in report["checks"] if check["type"] == "execution_plan"
    )
    assert invalid["code"] == "execution_plan_invalid"
    assert "真实设备动作" in invalid["message"]
    store.close()


def test_manual_confirmation_device_continuation_uses_device_preflight(
    monkeypatch,
) -> None:
    """包装真实设备动作的人工确认节点不能跳过当前设备 Gate 4。"""

    node_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setattr(
        ExecutionPlanBuilder,
        "build",
        lambda self, graph, run_mode, target_node_uuid: (
            {
                "nodes": [
                    {
                        "uuid": node_uuid,
                        "kind": "manual_confirm",
                        "manual_confirmation": {"timeout_seconds": 3600},
                        "device_id": "reactor-a",
                        "material_uuid": "device-material-a",
                        "action_name": "heat",
                    }
                ]
            },
            [],
        ),
    )
    observed: list[dict[str, object]] = []

    report = build_run_preflight_report(
        graph={
            "workflow": {"uuid": "workflow-a", "revision": 1},
            "nodes": [{"uuid": node_uuid, "name": "确认后加热"}],
        },
        run_mode="normal",
        target_node_uuid=None,
        material_resolver=None,
        device_preflight=lambda planned: observed.append(dict(planned))
        or {
            "local_device_id": "reactor-a",
            "material_uuid": "device-material-a",
        },
    )

    assert observed[0]["manual_confirmation"] == {"timeout_seconds": 3600}
    device_check = next(
        item for item in report["checks"] if item["type"] == "device_selection"
    )
    assert device_check["status"] == "passed"


def test_single_node_preflight_requires_explicit_target(tmp_path) -> None:
    """单节点预检缺少目标身份时须返回稳定输入错误。"""

    client, store = _client(tmp_path)
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "单节点流程", "tags": [], "meta_data": {}},
    ).json()["data"]
    response = client.get(
        f"/api/v1/workflows/{workflow['uuid']}/run-preflight",
        params={"run_mode": "single_node"},
    )
    assert response.status_code == 200
    assert response.json()["code"] == 1000
    store.close()


def test_preflight_reports_temporary_inventory_unavailability(tmp_path) -> None:
    """当前库存无法准入时必须返回 temporarily_unavailable 而不是 invalid。"""

    client, store = _client(tmp_path)
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "等待库存", "tags": [], "meta_data": {}},
    ).json()["data"]

    response = client.post(
        f"/api/v1/workflows/{workflow['uuid']}/run-preflight",
        json={
            "input": {},
            "inventory_bindings": [
                {
                    "requirement_key": "sample",
                    "inventory_type": "reagent",
                    "inventory_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "reserved_quantity": 1,
                    "quantity_unit": "mL",
                }
            ],
        },
    )

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["status"] == "temporarily_unavailable"
    assert report["can_run"] is False
    gap = next(
        check for check in report["checks"] if check["type"] == "quantity_inventory"
    )
    assert gap["status"] == "blocked"
    store.close()


def test_step_task_preflight_failure_creates_no_task(tmp_path) -> None:
    """Step 创建必须先通过 Preflight；失败时任务槽与 Task/Job 均零写入。"""

    client, store = _client(tmp_path)
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "Step 等待库存", "tags": [], "meta_data": {}},
    ).json()["data"]

    response = client.post(
        "/api/v1/workflow-tasks",
        json={
            "workflow_uuid": workflow["uuid"],
            "run_mode": "step",
            "input": {},
            "inventory_bindings": [
                {
                    "requirement_key": "sample",
                    "inventory_type": "reagent",
                    "inventory_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "reserved_quantity": 1,
                    "quantity_unit": "mL",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["code"] == 3003
    assert response.json()["error"]["code"] == "preflight_failed"
    tasks = client.get(
        "/api/v1/workflow-tasks", params={"workflow_uuid": workflow["uuid"]}
    ).json()["data"]
    assert tasks["items"] == []
    store.close()


def test_step_task_rejects_middle_start_target_without_writes(tmp_path) -> None:
    """Step 必须从入口开始；调用方不能借 target 绕过前向数据依赖。"""

    client, store = _client(tmp_path)
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "Step 禁止中间开始", "tags": [], "meta_data": {}},
    ).json()["data"]

    response = client.post(
        "/api/v1/workflow-tasks",
        json={
            "workflow_uuid": workflow["uuid"],
            "run_mode": "step",
            "target_node_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "input": {},
        },
    )

    assert response.status_code == 200
    assert response.json()["code"] == 1000
    tasks = client.get(
        "/api/v1/workflow-tasks", params={"workflow_uuid": workflow["uuid"]}
    ).json()["data"]
    assert tasks["items"] == []
    store.close()


def test_invalid_initial_source_cannot_run_empty_baseline(tmp_path) -> None:
    """A failed first compilation cannot silently execute an empty applied graph."""
    client, store = _client(tmp_path)
    workflow = client.post(
        "/api/v1/workflows", json={"name": "Uncompiled recipe"}
    ).json()["data"]
    # The standalone test service has no editable package; seed the same persisted
    # compiler diagnostic consumed by both public execution paths.
    with store._lock:
        store._conn.execute(
            "INSERT INTO workflow_authoring (workflow_uuid, diagnostics, update_time) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (workflow["uuid"], '[{"severity":"error","message":"Missing child operation"}]'),
        )
        store._conn.commit()
    report = client.get(f"/api/v1/workflows/{workflow['uuid']}/run-preflight").json()["data"]
    assert report["status"] == "invalid"
    assert report["can_run"] is False
    assert report["checks"][0]["code"] == "invalid_initial_source"
    for mode in ("normal", "step"):
        response = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": workflow["uuid"], "run_mode": mode, "input": {}},
        ).json()
        assert response["code"] != 0
    assert client.get(
        "/api/v1/workflow-tasks", params={"workflow_uuid": workflow["uuid"]}
    ).json()["data"]["items"] == []
    store.close()


def test_valid_unapplied_candidate_cannot_run_empty_initial_graph(tmp_path) -> None:
    """初次草稿编译成功仍只是预览，不能把空白应用图当作成功实验。"""
    import json

    client, store = _client(tmp_path)
    workflow = client.post("/api/v1/workflows", json={"name": "草稿预览"}).json()["data"]
    with store._lock:
        store._conn.execute(
            "INSERT INTO workflow_authoring (workflow_uuid, candidate, diagnostics, update_time) "
            "VALUES (?, ?, '[]', CURRENT_TIMESTAMP)",
            (workflow["uuid"], json.dumps({"graph": {"nodes": [{"uuid": "preview-node"}]}})),
        )
        store._conn.commit()
    report = client.get(f"/api/v1/workflows/{workflow['uuid']}/run-preflight").json()["data"]
    assert report["can_run"] is False
    assert "尚未应用" in report["checks"][0]["message"]
    for mode in ("normal", "step"):
        result = client.post("/api/v1/workflow-tasks", json={
            "workflow_uuid": workflow["uuid"], "run_mode": mode, "input": {},
        }).json()
        assert result["code"] != 0
    assert client.get("/api/v1/workflow-tasks").json()["data"]["items"] == []
    store.close()
