"""编排画布的提交契约（无节点模板）：POST /workflows → PUT graph → POST /workflow-tasks。

画布节点不引用 workflow_node_template：设备动作节点靠 ``type=device_action`` +
``material_uuid`` + ``action_name`` 描述，执行顺序用 ``execution_policy.depends_on``
（与 @workflow 声明式步骤同一套约定），``edges`` 为空。画布显式使用
``site_binding_mode=preserve``，避免把默认标签当成程序化导入自动确认。本测试冻结这条 HTTP 路径。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowService


def _node(node_uuid: str, name: str, device_material: str, action: str, depends_on: list[str], x: int) -> dict:
    return {
        "uuid": node_uuid,
        "name": name,
        "type": "device_action",
        "material_uuid": device_material,
        "action_name": action,
        "action_type": "UniLabJsonCommand",
        "param": {"value": x},
        "pose": {"x": x * 300, "y": 120},
        "execution_policy": {"depends_on": depends_on},
        "meta_data": {"target_device_id": "wf-device", "editor_node_id": f"n{x}"},
    }


def test_editor_submit_path_without_node_templates() -> None:
    service = WorkflowService(":memory:")
    client = TestClient(create_workflow_app(service))
    try:
        created = client.post(
            "/api/v1/workflows",
            json={"name": "画布流程", "tags": ["openlab-editor"], "description": "来自编排画布", "meta_data": {"source": "openlab-editor"}},
        ).json()
        assert created["code"] == 0, created
        workflow = created["data"]

        device_material = str(uuid.uuid4())
        first, second, confirm = (str(uuid.uuid4()) for _ in range(3))
        saved = client.put(
            f"/api/v1/workflows/{workflow['uuid']}/graph",
            json={
                "revision": workflow["revision"],
                "site_binding_mode": "preserve",
                "nodes": [
                    _node(first, "wf-device/record", device_material, "record", [], 1),
                    _node(second, "wf-device/record", device_material, "record", [first], 2),
                    {
                        "uuid": confirm,
                        "name": "人工确认",
                        "type": "manual_confirm",
                        "param": {"label": "人工确认", "prompt": "检查样品"},
                        "pose": {"x": 900, "y": 120},
                        "execution_policy": {"depends_on": [second]},
                        "meta_data": {"editor_node_id": "n3"},
                    },
                ],
                "edges": [],
            },
        ).json()
        assert saved["code"] == 0, saved
        graph = saved["data"]
        assert graph["workflow"]["revision"] == workflow["revision"] + 1
        by_uuid = {node["uuid"]: node for node in graph["nodes"]}
        assert set(by_uuid) == {first, second, confirm}
        assert by_uuid[second]["execution_policy"] == {"depends_on": [first]}
        assert by_uuid[first]["material_uuid"] == device_material
        assert graph["node_templates"] == [] and graph["handle_templates"] == []

        # 再存一次要带新 revision；旧 revision 是并发冲突（3003）
        stale = client.put(
            f"/api/v1/workflows/{workflow['uuid']}/graph",
            json={"revision": workflow["revision"], "nodes": graph["nodes"], "edges": [], "site_binding_mode": "preserve"},
        ).json()
        assert stale["code"] == 3003

        task = client.post(
            "/api/v1/workflow-tasks",
            json={"execution_kind": "workflow", "workflow_uuid": workflow["uuid"], "run_mode": "normal", "description": "画布提交"},
        )
        assert task.status_code == 201, task.text
        body = task.json()
        assert body["code"] == 0, body
        assert body["data"]["status"] == "pending"
        plan = body["data"]["execution_plan"]
        # depends_on 参与拓扑排序：计划顺序 / topological_index 与调度器实际 DAG 一致
        assert [node["uuid"] for node in plan["nodes"]] == [first, second, confirm]
        assert [node["kind"] for node in plan["nodes"]] == ["device_action", "device_action", "manual_confirm"]
        assert [node["topological_index"] for node in plan["nodes"]] == [0, 1, 2]

        runs = client.get(f"/api/v1/workflow-tasks/{body['data']['uuid']}/node-runs").json()["data"]
        assert [run["workflow_node_uuid"] for run in runs] == [first, second, confirm]
        assert [run["topological_index"] for run in runs] == [0, 1, 2]
    finally:
        service.close()


def test_depends_on_cycle_is_rejected_before_saving_graph() -> None:
    service = WorkflowService(":memory:")
    client = TestClient(create_workflow_app(service))
    try:
        workflow = client.post("/api/v1/workflows", json={"name": "cycle", "tags": []}).json()["data"]
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        material = str(uuid.uuid4())
        saved = client.put(
            f"/api/v1/workflows/{workflow['uuid']}/graph",
            json={
                "revision": workflow["revision"],
                "site_binding_mode": "preserve",
                "nodes": [_node(a, "a", material, "record", [b], 1), _node(b, "b", material, "record", [a], 2)],
                "edges": [],
            },
        ).json()
        assert saved["code"] == 1000, saved
        assert "循环" in saved["error"]["msg"]
        graph = service.get_graph(workflow["uuid"])
        assert graph["nodes"] == []
        assert graph["workflow"]["revision"] == workflow["revision"]
    finally:
        service.close()


def test_edges_still_require_templates() -> None:
    """有连线就必须有模板：画布把顺序写进 depends_on，而不是伪造 Handle UUID。"""

    service = WorkflowService(":memory:")
    client = TestClient(create_workflow_app(service))
    try:
        workflow = client.post("/api/v1/workflows", json={"name": "bad", "tags": []}).json()["data"]
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        result = client.put(
            f"/api/v1/workflows/{workflow['uuid']}/graph",
            json={
                "revision": workflow["revision"],
                "site_binding_mode": "preserve",
                "nodes": [
                    _node(a, "a", str(uuid.uuid4()), "record", [], 1),
                    _node(b, "b", str(uuid.uuid4()), "record", [], 2),
                ],
                "edges": [
                    {
                        "uuid": str(uuid.uuid4()),
                        "source_node_uuid": a,
                        "target_node_uuid": b,
                        "source_handle_uuid": str(uuid.uuid4()),
                        "target_handle_uuid": str(uuid.uuid4()),
                    }
                ],
            },
        ).json()
        assert result["code"] != 0
    finally:
        service.close()
