"""Workflow Debugger 的公开 HTTP 合同与静态禁用语义。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput

START_NODE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DISABLED_NODE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class RecordingTaskSchedulerBridge:
    """只替代真实调度器边界，保留公开服务和 SQLite 写模型。"""

    def __init__(self, store: WorkflowStore) -> None:
        self.store = store
        self.steps: list[tuple[str, str | None]] = []

    def submit(self, task: dict[str, Any]) -> dict[str, Any]:
        return {"task": self.store.get_task(task["uuid"]), "jobs": self.store.list_jobs(task["uuid"])}

    def step(
        self,
        task_uuid: str,
        *,
        target_node_uuid: str | None = None,
    ) -> dict[str, Any]:
        self.steps.append((task_uuid, target_node_uuid))
        return {"workflow_id": task_uuid, "state": "paused", "dispatched": [target_node_uuid]}


def _runtime(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    bridge = RecordingTaskSchedulerBridge(store)
    service = WorkflowService(store, task_scheduler_bridge=bridge)  # type: ignore[arg-type]
    client = TestClient(create_workflow_app(service))
    workflow = client.post(
        "/api/v1/workflows",
        json={"name": "debug fixture", "tags": [], "meta_data": {}},
    ).json()["data"]
    graph = client.put(
        f"/api/v1/workflows/{workflow['uuid']}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": START_NODE_UUID,
                    "name": "可运行节点",
                    "type": "compute",
                    "pose": {"x": 0, "y": 0},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                },
                {
                    "uuid": DISABLED_NODE_UUID,
                    "name": "禁用节点",
                    "type": "compute",
                    "pose": {"x": 200, "y": 0},
                    "param": {},
                    "execution_policy": {},
                    "disabled": True,
                    "minimized": False,
                    "meta_data": {},
                },
            ],
            "edges": [],
        },
    )
    assert graph.status_code == 200, graph.text
    return client, store, bridge, workflow["uuid"]


def test_retired_debug_task_create_returns_gone(tmp_path) -> None:
    client, store, _bridge, workflow_uuid = _runtime(tmp_path)

    response = client.post(
        "/api/v1/debug/workflow-tasks",
        json={
            "workflow_uuid": workflow_uuid,
            "start_node_uuids": [START_NODE_UUID],
            "breakpoint_node_uuids": [START_NODE_UUID],
            "input": {},
            "meta_data": {},
        },
    )

    assert response.status_code == 410, response.text
    assert response.json()["error"]["code"] == "debug_api_retired"
    store.close()


def test_retired_debug_detail_and_command_return_gone(tmp_path) -> None:
    client, store, _bridge, _workflow_uuid = _runtime(tmp_path)

    detail = client.get("/api/v1/debug/workflow-tasks/retired-task")
    command = client.post(
        "/api/v1/debug/workflow-tasks/retired-task/commands",
        json={"type": "step"},
    )

    assert detail.status_code == 410, detail.text
    assert command.status_code == 410, command.text
    assert detail.json()["error"]["code"] == "debug_api_retired"
    assert command.json()["error"]["code"] == "debug_api_retired"
    store.close()


def test_debug_scope_keeps_material_source_needed_by_start_node() -> None:
    source_uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    skipped_uuid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    downstream_uuid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    prepared = PreparedTaskInput(
        workflow_snapshot={"nodes": []},
        resolved_input={},
        execution_plan={
            "nodes": [
                {
                    "uuid": source_uuid,
                    "kind": "material_source",
                    "material_binding_targets": [
                        {
                            "workflow_node_uuid": START_NODE_UUID,
                            "param_key": "resource",
                        }
                    ],
                },
                {"uuid": skipped_uuid, "kind": "device_action"},
                {"uuid": START_NODE_UUID, "kind": "device_action"},
                {"uuid": downstream_uuid, "kind": "device_action"},
            ],
            "edges": [
                {
                    "source_node_uuid": source_uuid,
                    "target_node_uuid": START_NODE_UUID,
                },
                {
                    "source_node_uuid": skipped_uuid,
                    "target_node_uuid": START_NODE_UUID,
                },
                {
                    "source_node_uuid": START_NODE_UUID,
                    "target_node_uuid": downstream_uuid,
                },
            ],
            "handles": [],
        },
        jobs=[
            {"workflow_node_uuid": source_uuid},
            {"workflow_node_uuid": skipped_uuid},
            {"workflow_node_uuid": START_NODE_UUID},
            {"workflow_node_uuid": downstream_uuid},
        ],
    )

    scoped = WorkflowService._scope_debug_task_input(
        prepared,
        start_node_uuid=START_NODE_UUID,
        breakpoint_node_uuids=[],
    )

    assert [node["uuid"] for node in scoped.execution_plan["nodes"]] == [
        source_uuid,
        START_NODE_UUID,
        downstream_uuid,
    ]
    assert [job["workflow_node_uuid"] for job in scoped.jobs] == [
        source_uuid,
        START_NODE_UUID,
        downstream_uuid,
    ]
    assert scoped.execution_plan["edges"] == [
        {
            "source_node_uuid": source_uuid,
            "target_node_uuid": START_NODE_UUID,
        },
        {
            "source_node_uuid": START_NODE_UUID,
            "target_node_uuid": downstream_uuid,
        },
    ]


def test_debug_scope_rebinds_material_across_skipped_resource_passthrough() -> None:
    source_uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    skipped_uuid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    downstream_uuid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    prepared = PreparedTaskInput(
        workflow_snapshot={"nodes": []},
        resolved_input={},
        execution_plan={
            "nodes": [
                {
                    "uuid": source_uuid,
                    "kind": "material_source",
                    "material_binding_targets": [
                        {
                            "workflow_node_uuid": skipped_uuid,
                            "param_key": "material",
                        }
                    ],
                },
                {"uuid": skipped_uuid, "kind": "device_action"},
                {"uuid": START_NODE_UUID, "kind": "device_action"},
                {"uuid": downstream_uuid, "kind": "device_action"},
            ],
            "edges": [
                {
                    "source_node_uuid": source_uuid,
                    "target_node_uuid": skipped_uuid,
                    "source_type": "ResourceSlot",
                    "target_type": "ResourceSlot",
                    "target_data_key": "material",
                },
                {
                    "source_node_uuid": skipped_uuid,
                    "target_node_uuid": downstream_uuid,
                    "source_type": "ResourceSlot",
                    "target_type": "ResourceSlot",
                    "target_data_key": "resource",
                },
                {
                    "source_node_uuid": START_NODE_UUID,
                    "target_node_uuid": downstream_uuid,
                    "dependency_only": True,
                },
            ],
            "handles": [],
        },
        jobs=[
            {"workflow_node_uuid": source_uuid},
            {"workflow_node_uuid": skipped_uuid},
            {"workflow_node_uuid": START_NODE_UUID},
            {"workflow_node_uuid": downstream_uuid},
        ],
    )

    scoped = WorkflowService._scope_debug_task_input(
        prepared,
        start_node_uuid=START_NODE_UUID,
        breakpoint_node_uuids=[],
    )

    assert [node["uuid"] for node in scoped.execution_plan["nodes"]] == [
        source_uuid,
        START_NODE_UUID,
        downstream_uuid,
    ]
    source = scoped.execution_plan["nodes"][0]
    assert source["material_binding_targets"] == [
        {
            "workflow_node_uuid": downstream_uuid,
            "param_key": "resource",
        }
    ]
    assert scoped.execution_plan["edges"] == [
        {
            "source_node_uuid": START_NODE_UUID,
            "target_node_uuid": downstream_uuid,
            "dependency_only": True,
        }
    ]
