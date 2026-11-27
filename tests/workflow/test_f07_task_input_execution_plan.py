"""F07 工作流任务（WorkflowTask）输入与冻结执行计划（ExecutionPlan）合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.config.config import EdgeControlConfig
from unilabos.persistence.inspection import table_exists
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.resource_lock_plan import deserialize_resource_plan
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import (
    PreparedTaskInput,
    TaskInputError,
    prepare_task_input,
)
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

WORKFLOW_UUID = "61000000-0000-4000-8000-000000000001"
NODE_UUID = "62000000-0000-4000-8000-000000000001"
TEMPLATE_UUID = "63000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "64000000-0000-4000-8000-000000000001"
MATERIAL_UUID = "65000000-0000-4000-8000-000000000001"


def _input_contract() -> dict[str, Any]:
    """构造包含必填标量和可选默认值的工作流输入合同。

    参数：无。返回：工作流输入合同（schema version 1）（WorkflowInputContract）。异常：无。
    """

    return {
        "version": 1,
        "parameters": [
            {
                "name": "count",
                "schema": {"type": "integer", "minimum": 1},
                "required": True,
            },
            {
                "name": "label",
                "schema": {"type": "string"},
                "required": False,
                "default": "automatic",
            },
        ],
    }


def _binding_graph() -> dict[str, Any]:
    """构造把 ``count`` 绑定到活动节点目标连接点（Handle）的应用图。

    参数：无。返回：可直接构造计划并校验输入绑定的冻结工作流图。异常：无。
    """

    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "revision": 2,
            "name": "task input",
            "tags": [],
            "meta_data": {
                "unilab": {
                    "input_contract": _input_contract(),
                    "output_contract": {"version": 1, "outputs": []},
                    "output_bindings": {},
                }
            },
        },
        "nodes": [
            {
                "uuid": NODE_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "name": "approval",
                "type": "compute",
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {
                    "unilab": {
                        "input_bindings": {TARGET_HANDLE_UUID: {"parameter": "count"}}
                    }
                },
            }
        ],
        "edges": [],
        "node_templates": [
            {
                "uuid": TEMPLATE_UUID,
                "node_type": "compute",
                "type": "compute",
            }
        ],
        "handle_templates": [
            {
                "uuid": TARGET_HANDLE_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "handle_key": "count",
                "io_type": "target",
                "display_name": "Count",
                "description": "",
                "type": "integer",
                "required": True,
                "data_source": "executor",
                "data_key": "count",
                "meta_data": {"unilab": {"value_schema": {"type": "integer"}}},
            }
        ],
    }


def _client(database_path: Path) -> tuple[TestClient, WorkflowStore]:
    """创建隔离的工作流 HTTP 客户端和可检查写入数的存储。

    参数：``database_path`` 是本测试独占的 SQLite 路径。返回：客户端与存储。
    异常：数据库初始化失败时保留底层异常。
    """

    store = WorkflowStore(database_path)
    return TestClient(create_workflow_app(WorkflowService(store))), store


def _create_workflow(client: TestClient, store: WorkflowStore) -> str:
    """通过公共 HTTP 接口创建带输入合同的单节点工作流。

    参数：``client`` 是工作流应用客户端，``store`` 用于安装本应由发布编译器
    产生的保留输入合同夹具。返回：新工作流 UUID。异常：断言暴露创建、夹具
    安装或保存失败。
    """

    created = client.post(
        "/api/v1/workflows",
        json={
            "name": "task input",
            "tags": [],
            "meta_data": {
                "unilab": {
                    "input_contract": _input_contract(),
                    "output_contract": {"version": 1, "outputs": []},
                    "output_bindings": {},
                }
            },
        },
    )
    assert created.status_code == 201
    workflow_uuid = created.json()["data"]["uuid"]
    # ``unilab`` 是发布编译器所有的保留元数据；本测试不复刻整条创作应用流程，
    # 只在数据库夹具中安装该已验证事实，再通过公共任务 HTTP 接口验收 F07。
    store._conn.execute(
        "UPDATE workflow SET meta_data = ? WHERE uuid = ?",
        (
            encode_json(
                {
                    "unilab": {
                        "input_contract": _input_contract(),
                        "output_contract": {"version": 1, "outputs": []},
                        "output_bindings": {},
                    }
                },
                sort_keys=True,
            ).decode("utf-8"),
            workflow_uuid,
        ),
    )
    store._conn.commit()
    saved = client.put(
        f"/api/v1/workflows/{workflow_uuid}/graph",
        json={
            "revision": 1,
            "nodes": [
                {
                    "uuid": NODE_UUID,
                    "name": "approval",
                    "type": "compute",
                    "pose": {},
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
    assert saved.status_code == 200
    assert saved.json()["code"] == 0
    return workflow_uuid


def _row_counts(store: WorkflowStore) -> tuple[int, int]:
    """读取工作流任务与工作流节点作业（WorkflowNodeJob）物理行数。

    参数：``store`` 是当前测试存储。返回：任务行数和作业行数。异常：保留
    SQLite 查询异常。
    """

    task_count = store._conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0]
    job_count = store._conn.execute(
        "SELECT COUNT(*) FROM workflow_node_job"
    ).fetchone()[0]
    return int(task_count), int(job_count)


def test_scalar_input_and_default_are_frozen_into_plan_and_jobs() -> None:
    """标量和默认值须在写入前规范化并绑定到计划与首次作业。

    参数：无。返回：无。异常：合同回归由断言暴露。
    """

    graph = _binding_graph()
    plan, jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    original_graph = deepcopy(graph)
    original_plan = deepcopy(plan)
    original_jobs = deepcopy(jobs)

    prepared = prepare_task_input(
        graph=graph,
        raw_input={"count": 3},
        execution_plan=plan,
        jobs=jobs,
    )

    assert prepared.resolved_input == {"count": 3, "label": "automatic"}
    action_node = next(
        node
        for node in prepared.execution_plan["nodes"]
        if node["kind"] == "compute"
    )
    action_job = next(
        job for job in prepared.jobs if job["executor_kind"] == "compute"
    )
    assert action_node["param"] == {"count": 3}
    assert action_job["param"] == {"count": 3}
    assert prepared.workflow_snapshot == graph
    assert graph == original_graph
    assert plan == original_plan
    assert jobs == original_jobs


def test_dynamic_inventory_quantity_is_frozen_from_resolved_task_input() -> None:
    """数量需求应使用本次 Task 输入换算，且不修改应用图默认值。"""

    graph = _binding_graph()
    graph["inventory_requirements"] = [
        {
            "uuid": "66000000-0000-4000-8000-000000000001",
            "consume_node_uuid": NODE_UUID,
            "requirement_key": "dynamic-liquid",
            "target_type": "current_substance",
            "required_quantity": 0.5,
            "quantity_unit": "mL",
            "allow_split": False,
            "meta_data": {
                "unilab": {
                    "quantity_binding": {
                        "kind": "workflow_input",
                        "parameter": "count",
                    },
                    "quantity_scale": 0.5,
                }
            },
        }
    ]
    plan, jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="normal",
        target_node_uuid=None,
    )

    prepared = prepare_task_input(
        graph=graph,
        raw_input={"count": 4},
        execution_plan=plan,
        jobs=jobs,
    )

    assert prepared.workflow_snapshot["inventory_requirements"][0][
        "required_quantity"
    ] == 2.0
    assert graph["inventory_requirements"][0]["required_quantity"] == 0.5

    graph["workflow"]["meta_data"]["unilab"]["input_contract"]["parameters"][
        0
    ]["schema"]["minimum"] = 0
    zero = prepare_task_input(
        graph=graph,
        raw_input={"count": 0},
        execution_plan=plan,
        jobs=jobs,
    )
    assert zero.workflow_snapshot["inventory_requirements"] == []


def test_workflow_task_priority_enum_is_accepted_and_persisted(
    tmp_path: Path,
) -> None:
    """工作流任务接口接受 normal/high，并把默认值和显式值分别写入 SQLite。

    参数：``tmp_path`` 提供隔离运行库。返回：无。异常：公共 HTTP 接口或任务
    持久化合同不满足时由断言报告；本测试不触碰调度器排序实现。
    """

    client, store = _client(tmp_path / "task-priority.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        high = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "input": {"count": 1},
                "priority": "high",
            },
        )
        normal = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "input": {"count": 1},
            },
        )
        invalid = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "input": {"count": 1},
                "priority": "urgent",
            },
        )

        assert high.status_code == 201
        assert normal.status_code == 201
        assert invalid.status_code == 200
        assert invalid.json()["code"] == 1000
        assert high.json()["data"]["priority"] == "high"
        assert normal.json()["data"]["priority"] == "normal"
        high_uuid = high.json()["data"]["uuid"]
        normal_uuid = normal.json()["data"]["uuid"]
        assert client.get(f"/api/v1/workflow-tasks/{high_uuid}").json()["data"][
            "priority"
        ] == "high"
        assert client.get(f"/api/v1/workflow-tasks/{normal_uuid}").json()["data"][
            "priority"
        ] == "normal"
        assert client.get(
            f"/api/v1/workflow-tasks?workflow_uuid={workflow_uuid}"
        ).json()["data"]["total"] == 2
    finally:
        store.close()


def test_station_invocation_is_idempotent_and_allows_same_workflow_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 Backend Task 用不同调用键可重复调用工作流，同键只创建一次。

    参数：``tmp_path`` 提供隔离运行库，``monkeypatch`` 安装工站协议密钥。返回无；
    断言任务/作业身份、优先级、入口参数和事务发件箱均由工站生成并持久化。
    """

    client, store = _client(tmp_path / "station-invocation.db")
    try:
        _create_workflow(client, store)
        monkeypatch.setattr(EdgeControlConfig, "api_key", "station-secret")
        backend_task_uuid = "71000000-0000-4000-8000-000000000001"
        headers = {"Authorization": "Bearer station-secret"}
        first_body = {
            "backend_task_uuid": backend_task_uuid,
            "invocation_key": "global-node-a",
            "workflow_name": "task input",
            "input": {"count": 2},
            "priority": 8,
        }

        first = client.post(
            "/api/v1/station/workflow-invocations",
            json=first_body,
            headers=headers,
        )
        replay = client.post(
            "/api/v1/station/workflow-invocations",
            json=first_body,
            headers=headers,
        )
        second = client.post(
            "/api/v1/station/workflow-invocations",
            json={**first_body, "invocation_key": "global-node-b"},
            headers=headers,
        )

        assert first.status_code == 201
        assert replay.status_code == 201
        assert second.status_code == 201
        first_task = first.json()["data"]
        assert replay.json()["data"]["uuid"] == first_task["uuid"]
        assert second.json()["data"]["uuid"] != first_task["uuid"]
        assert first_task["backend_task_uuid"] == backend_task_uuid
        assert first_task["invocation_key"] == "global-node-a"
        assert first_task["priority"] == 8.0
        assert first_task["input"] == {"count": 2, "label": "automatic"}
        assert _row_counts(store) == (2, 4)
        # 工站事件发件箱已经停止写入；新库不应为了兼容旧查询创建空表。
        with store.read() as connection:
            assert not table_exists(connection, "workflow_station_event_outbox")
    finally:
        store.close()


def test_station_invocation_rejects_changed_replay_and_internal_node_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一调用键不能改入口参数，协议也不接受 DAG 中间节点参数。"""

    client, store = _client(tmp_path / "station-conflict.db")
    try:
        _create_workflow(client, store)
        monkeypatch.setattr(EdgeControlConfig, "api_key", "station-secret")
        headers = {"Authorization": "Bearer station-secret"}
        body = {
            "backend_task_uuid": "71000000-0000-4000-8000-000000000002",
            "invocation_key": "global-node-a",
            "workflow_name": "task input",
            "input": {"count": 2},
        }
        assert client.post(
            "/api/v1/station/workflow-invocations",
            json=body,
            headers=headers,
        ).status_code == 201

        conflict = client.post(
            "/api/v1/station/workflow-invocations",
            json={**body, "input": {"count": 3}},
            headers=headers,
        )
        forbidden = client.post(
            "/api/v1/station/workflow-invocations",
            json={**body, "node_params": {NODE_UUID: {"count": 3}}},
            headers=headers,
        )

        assert conflict.status_code == 200
        assert conflict.json()["code"] == 3003
        assert forbidden.status_code == 422
        assert _row_counts(store) == (1, 2)
    finally:
        store.close()


def test_station_invocation_replay_keeps_first_revision_after_definition_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作流修订变化后，同一工站调用仍返回首次冻结的 Task。

    参数：``tmp_path`` 隔离运行库，``monkeypatch`` 安装工站协议密钥。返回无；
    若网络重放改绑新修订、创建第二组 Job 或错误冲突，则由断言失败。
    """

    client, store = _client(tmp_path / "station-revision-replay.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        monkeypatch.setattr(EdgeControlConfig, "api_key", "station-secret")
        headers = {"Authorization": "Bearer station-secret"}
        body = {
            "backend_task_uuid": "71000000-0000-4000-8000-000000000003",
            "invocation_key": "global-node-a",
            "workflow_name": "task input",
            "input": {"count": 2},
        }
        first = client.post(
            "/api/v1/station/workflow-invocations",
            json=body,
            headers=headers,
        )
        assert first.status_code == 201
        first_task = first.json()["data"]

        evolved = client.put(
            f"/api/v1/workflows/{workflow_uuid}/graph",
            json={
                "revision": 2,
                "nodes": [
                    {
                        "uuid": NODE_UUID,
                        "name": "approval revision 2",
                        "type": "compute",
                        "pose": {},
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
        assert evolved.status_code == 200
        assert evolved.json()["code"] == 0

        replay = client.post(
            "/api/v1/station/workflow-invocations",
            json=body,
            headers=headers,
        )

        assert replay.status_code == 201
        assert replay.json()["data"]["uuid"] == first_task["uuid"]
        assert replay.json()["data"]["workflow_snapshot"] == (
            first_task["workflow_snapshot"]
        )
        assert _row_counts(store) == (1, 2)
    finally:
        store.close()


def test_station_invocation_pins_published_revision_and_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规范工站调用必须按发布指纹冻结修订，并保存全局身份与截止时间。

    参数：``tmp_path`` 隔离运行库，``monkeypatch`` 安装工站协议密钥。返回无；
    若调用退回按名称选择当前定义、接受错误指纹或丢失 deadline，则由断言失败。
    """

    client, store = _client(tmp_path / "station-published-revision.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        current = client.get(f"/api/v1/workflows/{workflow_uuid}/graph").json()[
            "data"
        ]
        published = client.post(
            f"/api/v1/workflows/{workflow_uuid}/publications",
            json={"revision": current["workflow"]["revision"]},
        )
        assert published.status_code == 201
        contract = published.json()["data"]

        evolved = client.put(
            f"/api/v1/workflows/{workflow_uuid}/graph",
            json={
                "revision": current["workflow"]["revision"],
                "nodes": [
                    {
                        "uuid": NODE_UUID,
                        "name": "approval after publication",
                        "type": "compute",
                        "pose": {},
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
        assert evolved.status_code == 200

        monkeypatch.setattr(EdgeControlConfig, "api_key", "station-secret")
        headers = {"Authorization": "Bearer station-secret"}
        global_task_uuid = "71000000-0000-4000-8000-000000000004"
        body = {
            "task_uuid": global_task_uuid,
            "invocation_key": "global-node-published",
            "workflow_id": workflow_uuid,
            "revision_fingerprint": contract["revision_fingerprint"],
            "normalized_input": {"count": 4},
            "priority": 5,
            "deadline": "2030-01-02T03:04:05Z",
        }
        accepted = client.post(
            "/api/v1/station/workflow-invocations",
            json=body,
            headers=headers,
        )

        assert accepted.status_code == 201
        task = accepted.json()["data"]
        assert task["backend_task_uuid"] == global_task_uuid
        assert task["global_task_uuid"] == global_task_uuid
        assert task["revision_fingerprint"] == contract["source_hash"]
        assert task["deadline"] == "2030-01-02T03:04:05Z"
        assert task["workflow_snapshot"]["workflow"]["revision"] == contract[
            "workflow_revision"
        ]
        assert task["workflow_snapshot"]["nodes"][0]["name"] == "approval"

        changed_replay = client.post(
            "/api/v1/station/workflow-invocations",
            json={**body, "deadline": "2030-01-02T03:04:06Z"},
            headers=headers,
        )
        assert changed_replay.status_code == 200
        assert changed_replay.json()["code"] == 3003

        rejected = client.post(
            "/api/v1/station/workflow-invocations",
            json={
                **body,
                "invocation_key": "global-node-wrong-revision",
                "revision_fingerprint": "sha256:" + "f" * 64,
            },
            headers=headers,
        )
        assert rejected.status_code == 200
        assert rejected.json()["code"] == 3003
        assert store._conn.execute(
            "SELECT COUNT(*) FROM workflow_task"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_workflow_boundary_jobs_publish_input_as_result(tmp_path: Path) -> None:
    """纯数据工作流必须创建正式输入/输出 Job 并持久化结果记录。

    参数：``tmp_path`` 隔离运行库。返回无；断言只经过 WorkflowService 的 HTTP
    边界观察任务、作业与结果，内部表结构不作为行为合同。
    """

    client, store = _client(tmp_path / "workflow-boundary-jobs.db")
    try:
        created = client.post(
            "/api/v1/workflows",
            json={"name": "identity workflow", "tags": [], "meta_data": {}},
        )
        assert created.status_code == 201
        workflow_uuid = created.json()["data"]["uuid"]
        boundary_contract = {
            "unilab": {
                "input_contract": {
                    "version": 1,
                    "parameters": [
                        {
                            "name": "count",
                            "schema": {"type": "integer"},
                            "required": True,
                        }
                    ],
                },
                "output_contract": {
                    "version": 1,
                    "outputs": [
                        {"name": "echo", "schema": {"type": "integer"}}
                    ],
                },
                "output_bindings": {
                    "echo": {"kind": "workflow_input", "parameter": "count"}
                },
            }
        }
        store._conn.execute(
            "UPDATE workflow SET meta_data = ? WHERE uuid = ?",
            (encode_json(boundary_contract, sort_keys=True).decode(), workflow_uuid),
        )
        store._conn.commit()

        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": "normal",
                "input": {"count": 7},
                "meta_data": {},
            },
        )

        assert response.status_code == 201
        task = response.json()["data"]
        jobs = client.get(
            f"/api/v1/workflow-tasks/{task['uuid']}/jobs"
        ).json()["data"]
        assert [job["executor_kind"] for job in jobs] == [
            "workflow_input",
            "workflow_output",
        ]
        assert [job["status"] for job in jobs] == ["succeeded", "succeeded"]
        assert jobs[0]["return_info"] == {"count": 7}
        assert jobs[1]["return_info"] == {"echo": 7}
        assert task["output"] == {"echo": 7}
        assert task["status"] == "succeeded"
    finally:
        store.close()


def test_input_only_workflow_finishes_after_input_job_succeeds(tmp_path: Path) -> None:
    """仅含输入边界的纯数据工作流应随输入 Job 原子成功而完成。"""

    client, store = _client(tmp_path / "workflow-input-only.db")
    try:
        created = client.post(
            "/api/v1/workflows",
            json={"name": "input only", "tags": [], "meta_data": {}},
        ).json()["data"]
        contract = {
            "unilab": {
                "input_contract": {
                    "version": 1,
                    "parameters": [
                        {
                            "name": "count",
                            "schema": {"type": "integer"},
                            "required": True,
                        }
                    ],
                },
                "output_contract": {"version": 1, "outputs": []},
                "output_bindings": {},
            }
        }
        store._conn.execute(
            "UPDATE workflow SET meta_data = ? WHERE uuid = ?",
            (encode_json(contract, sort_keys=True).decode(), created["uuid"]),
        )
        store._conn.commit()

        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": created["uuid"],
                "run_mode": "normal",
                "input": {"count": 5},
                "meta_data": {},
            },
        )

        task = response.json()["data"]
        jobs = client.get(
            f"/api/v1/workflow-tasks/{task['uuid']}/jobs"
        ).json()["data"]
        assert [(job["executor_kind"], job["status"]) for job in jobs] == [
            ("workflow_input", "succeeded")
        ]
        assert task["status"] == "succeeded"
        assert task["output"] == {}
    finally:
        store.close()


def test_workflow_output_success_does_not_finish_unrelated_branch(
    tmp_path: Path,
) -> None:
    """输出 Job 只等待显式数据边，成功后不得替代整个任务终态判断。"""

    client, store = _client(tmp_path / "workflow-output-independent.db")
    try:
        created = client.post(
            "/api/v1/workflows",
            json={"name": "independent branch", "tags": [], "meta_data": {}},
        ).json()["data"]
        contract = {
            "unilab": {
                "input_contract": {
                    "version": 1,
                    "parameters": [
                        {
                            "name": "count",
                            "schema": {"type": "integer"},
                            "required": True,
                        }
                    ],
                },
                "output_contract": {
                    "version": 1,
                    "outputs": [
                        {"name": "echo", "schema": {"type": "integer"}}
                    ],
                },
                "output_bindings": {
                    "echo": {"kind": "workflow_input", "parameter": "count"}
                },
            }
        }
        store._conn.execute(
            "UPDATE workflow SET meta_data = ? WHERE uuid = ?",
            (encode_json(contract, sort_keys=True).decode(), created["uuid"]),
        )
        store._conn.commit()
        saved = client.put(
            f"/api/v1/workflows/{created['uuid']}/graph",
            json={
                "revision": 1,
                "nodes": [
                    {
                        "uuid": NODE_UUID,
                        "name": "unrelated approval",
                        "type": "compute",
                        "pose": {},
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
        assert saved.status_code == 200

        task = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": created["uuid"],
                "run_mode": "normal",
                "input": {"count": 9},
                "meta_data": {},
            },
        ).json()["data"]
        jobs = client.get(
            f"/api/v1/workflow-tasks/{task['uuid']}/jobs"
        ).json()["data"]
        by_kind = {job["executor_kind"]: job for job in jobs}

        assert by_kind["workflow_output"]["status"] == "succeeded"
        assert by_kind["workflow_output"]["return_info"] == {"echo": 9}
        assert by_kind["compute"]["status"] == "pending"
        assert task["status"] == "pending"
        assert task["output"] == {"echo": 9}
    finally:
        store.close()


def test_node_output_binding_creates_result_after_source_job_succeeds(
    tmp_path: Path,
) -> None:
    """来源 Job 成功后，输出 Job 必须按冻结 Handle 映射生成任务结果。"""

    store = WorkflowStore(tmp_path / "workflow-node-output.db")
    try:
        workflow = store.create_workflow(
            workflow_uuid=str(uuid4()),
            name="node output",
            tags=[],
            description=None,
            meta_data={},
        )
        source_handle_uuid = "64000000-0000-4000-8000-000000000002"
        graph = {
            "workflow": {
                **workflow,
                "meta_data": {
                    "unilab": {
                        "input_contract": {"version": 1, "parameters": []},
                        "output_contract": {
                            "version": 1,
                            "outputs": [
                                {
                                    "name": "measurement",
                                    "schema": {"type": "integer"},
                                }
                            ],
                        },
                        "output_bindings": {
                            "measurement": {
                                "kind": "node_output",
                                "workflow_node_uuid": NODE_UUID,
                                "source_handle_uuid": source_handle_uuid,
                            }
                        },
                    }
                },
            },
            "nodes": [
                {
                    "uuid": NODE_UUID,
                    "workflow_node_template_uuid": TEMPLATE_UUID,
                    "name": "measure",
                    "type": "compute",
                    "pose": {},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
            "node_templates": [
                {
                    "uuid": TEMPLATE_UUID,
                    "node_type": "compute",
                    "type": "compute",
                }
            ],
            "handle_templates": [
                {
                    "uuid": source_handle_uuid,
                    "workflow_node_template_uuid": TEMPLATE_UUID,
                    "handle_key": "measurement",
                    "io_type": "source",
                    "display_name": "Measurement",
                    "description": "",
                    "type": "integer",
                    "required": False,
                    "data_source": "executor",
                    "data_key": "value",
                    "meta_data": {
                        "unilab": {"value_schema": {"type": "integer"}}
                    },
                }
            ],
        }
        plan, jobs = ExecutionPlanBuilder().build(
            graph,
            run_mode="normal",
            target_node_uuid=None,
        )
        prepared = prepare_task_input(
            graph=graph,
            raw_input={},
            execution_plan=plan,
            jobs=jobs,
        )
        task_uuid = str(uuid4())
        task = store.create_task_with_jobs(
            workflow_uuid=workflow["uuid"],
            task_uuid=task_uuid,
            run_mode="normal",
            target_node_uuid=None,
            description=None,
            meta_data={},
            plan_builder=lambda _graph: PreparedTaskInput(
                workflow_snapshot=prepared.workflow_snapshot,
                resolved_input=prepared.resolved_input,
                execution_plan=prepared.execution_plan,
                jobs=prepared.jobs,
            ),
            applied_graph=graph,
        )
        source_job = next(
            job
            for job in store.list_jobs(task_uuid)
            if job["executor_kind"] == "compute"
        )
        store._conn.execute(
            "UPDATE workflow_task SET status = 'running' WHERE uuid = ?",
            (task_uuid,),
        )
        store._conn.execute(
            "UPDATE workflow_node_job SET status = 'running' WHERE uuid = ?",
            (source_job["uuid"],),
        )
        store._conn.commit()

        aggregate = TaskRuntimeProjection(store).project_job_finished(
            job_uuid=source_job["uuid"],
            scheduler_state="success",
            return_info={"return_value": {"value": 12}},
        )
        by_kind = {job["executor_kind"]: job for job in aggregate["jobs"]}

        assert task["status"] == "pending"
        assert by_kind["workflow_output"]["status"] == "succeeded"
        assert by_kind["workflow_output"]["return_info"] == {"measurement": 12}
        assert aggregate["task"]["output"] == {"measurement": 12}
        assert aggregate["task"]["status"] == "succeeded"
    finally:
        store.close()


@pytest.mark.parametrize(
    "raw_input",
    [
        {},
        {"count": "three"},
        {"count": 3, "unknown": True},
    ],
)
def test_required_malformed_and_extra_inputs_write_nothing(
    tmp_path: Path,
    raw_input: dict[str, Any],
) -> None:
    """必填缺失、类型错误和多余字段必须在同一事务中零写入。

    参数：``tmp_path`` 隔离数据库，``raw_input`` 是不合法输入样例。返回：无。
    异常：公共接口或原子性回归由断言暴露。
    """

    client, store = _client(tmp_path / "task-input-invalid.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        before = _row_counts(store)
        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": "normal",
                "input": raw_input,
                "meta_data": {},
            },
        )
        assert response.status_code == 200
        assert response.json()["code"] == 1000
        assert _row_counts(store) == before
    finally:
        store.close()


def test_http_task_input_and_snapshot_remain_frozen_after_workflow_evolves(
    tmp_path: Path,
) -> None:
    """公共创建响应须回显输入，后续图修订不得改变既有任务快照。

    参数：``tmp_path`` 隔离数据库。返回：无。异常：HTTP、默认值或快照冻结
    回归由断言暴露。
    """

    client, store = _client(tmp_path / "task-input-frozen.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": "normal",
                "input": {"count": 7},
                "meta_data": {},
            },
        )
        assert response.status_code == 201
        assert response.json()["code"] == 0
        created_task = response.json()["data"]
        assert created_task["input"] == {"count": 7, "label": "automatic"}
        frozen_snapshot = deepcopy(created_task["workflow_snapshot"])
        frozen_plan = deepcopy(created_task["execution_plan"])

        evolved = client.put(
            f"/api/v1/workflows/{workflow_uuid}/graph",
            json={
                "revision": 2,
                "nodes": [
                    {
                        "uuid": NODE_UUID,
                        "name": "renamed approval",
                        "type": "compute",
                        "pose": {},
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
        assert evolved.status_code == 200
        assert evolved.json()["code"] == 0

        fetched = client.get(f"/api/v1/workflow-tasks/{created_task['uuid']}").json()[
            "data"
        ]
        assert fetched["input"] == {"count": 7, "label": "automatic"}
        assert fetched["workflow_snapshot"] == frozen_snapshot
        assert fetched["execution_plan"] == frozen_plan
    finally:
        store.close()


def test_http_task_presentation_batches_compact_plan_and_jobs(
    tmp_path: Path,
) -> None:
    """控制台任务投影应一次返回紧凑冻结拓扑和 Job 状态。

    参数：``tmp_path`` 隔离数据库。返回无。异常：列表重新携带完整工作流快照、
    丢失矩阵字段或退化成逐 Task Job 请求时由断言暴露。
    """

    client, store = _client(tmp_path / "task-presentation.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        created = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": "normal",
                "input": {"count": 7},
                "meta_data": {},
            },
        ).json()["data"]

        response = client.get(
            "/api/v1/workflow-task-presentations",
            params={
                "status": "pending",
                "page": 1,
                "page_size": 20,
            },
        )

        assert response.status_code == 200
        page = response.json()["data"]
        assert page["total"] == 1
        task = page["items"][0]
        assert task["uuid"] == created["uuid"]
        assert set(task["workflow_snapshot"]) == {"workflow"}
        assert task["workflow_snapshot"]["workflow"]["name"] == "task input"
        assert NODE_UUID in {node["uuid"] for node in task["execution_plan"]["nodes"]}
        assert task["execution_plan"]["edges"] == []
        assert NODE_UUID in {job["workflow_node_uuid"] for job in task["jobs"]}
        assert all("execution_policy" not in job for job in task["jobs"])
        assert all("param" not in job for job in task["jobs"])
        assert all("feedback_data" not in job for job in task["jobs"])
        assert all("return_info" not in job for job in task["jobs"])
        assert set(task["input"]) == {"sample", "sample_id"}
        assert "nodes" in created["workflow_snapshot"]
    finally:
        store.close()


def test_http_task_presentation_keeps_resource_slot_material_identities(
    tmp_path: Path,
) -> None:
    """紧凑展示投影须保留冻结输入合同解析出的物料稳定身份。"""

    client, store = _client(tmp_path / "task-presentation-materials.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        created = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": "normal",
                "input": {"count": 7},
                "meta_data": {},
            },
        ).json()["data"]
        frozen_snapshot = deepcopy(created["workflow_snapshot"])
        frozen_snapshot["workflow"]["meta_data"]["unilab"]["input_contract"][
            "parameters"
        ].append(
            {
                "name": "vessel",
                "schema": {"$slot": "ResourceSlot"},
                "required": True,
            }
        )
        store._conn.execute(
            "UPDATE workflow_task SET workflow_snapshot = ?, input = ? WHERE uuid = ?",
            (
                encode_json(frozen_snapshot, sort_keys=True).decode("utf-8"),
                encode_json(
                    {
                        "count": 7,
                        "label": "automatic",
                        "vessel": {"uuid": MATERIAL_UUID},
                    },
                    sort_keys=True,
                ).decode("utf-8"),
                created["uuid"],
            ),
        )
        store._conn.commit()

        response = client.get(
            "/api/v1/workflow-task-presentations",
            params={"view": "matrix", "terminal_limit": 20},
        )

        assert response.status_code == 200
        task = response.json()["data"]["items"][0]
        assert task["material_uuids"] == [MATERIAL_UUID]
        assert set(task["input"]) == {"sample", "sample_id"}
        assert "input_contract" not in task["workflow_snapshot"]["workflow"]
    finally:
        store.close()


def test_http_task_presentation_matrix_returns_active_attention_and_recent_terminal_tasks(
    tmp_path: Path,
) -> None:
    """矩阵视图应以一次请求返回全部活动项、关注项和有限近期终态项。"""

    client, store = _client(tmp_path / "task-presentation-matrix.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        tasks: list[dict[str, Any]] = []
        for index in range(6):
            response = client.post(
                "/api/v1/workflow-tasks",
                json={
                    "workflow_uuid": workflow_uuid,
                    "run_mode": "normal",
                    "input": {"count": index + 1},
                    "meta_data": {"sample_id": f"sample-{index}"},
                },
            )
            assert response.status_code == 201, response.text
            tasks.append(response.json()["data"])
            # 当前 Task，全部创建完成后再安装测试所需的并行状态集合。
            store._conn.execute(
                "UPDATE workflow_task SET status = 'succeeded' WHERE uuid = ?",
                (tasks[-1]["uuid"],),
            )
            store._conn.commit()

        fixtures = (
            (tasks[0]["uuid"], "pending", "none", "2026-01-01T00:00:00Z", None),
            (tasks[1]["uuid"], "running", "none", "2026-01-01T00:00:01Z", None),
            # 该任务创建最早、完成最晚，必须按终态发生时间而不是创建时间入选。
            (tasks[2]["uuid"], "succeeded", "none", "2025-01-01T00:00:00Z", "2026-01-02T00:00:06Z"),
            (tasks[3]["uuid"], "failed", "none", "2026-01-01T00:00:03Z", "2026-01-01T00:00:03Z"),
            (tasks[4]["uuid"], "timeout", "none", "2026-01-01T00:00:04Z", "2026-01-01T00:00:04Z"),
            (
                tasks[5]["uuid"],
                "canceled",
                "requires_attention",
                "2026-01-01T00:00:05Z",
                "2026-01-01T00:00:05Z",
            ),
        )
        store._conn.executemany(
            """
            UPDATE workflow_task
            SET status = ?, cleanup_status = ?, create_time = ?, update_time = ?,
                finished_at = ?
            WHERE uuid = ?
            """,
            [
                (
                    status,
                    cleanup_status,
                    created_at,
                    finished_at or created_at,
                    finished_at,
                    task_uuid,
                )
                for task_uuid, status, cleanup_status, created_at, finished_at in fixtures
            ],
        )
        store._conn.commit()

        response = client.get(
            "/api/v1/workflow-task-presentations",
            params={"view": "matrix", "terminal_limit": 2},
        )

        assert response.status_code == 200
        page = response.json()["data"]
        returned = {item["uuid"] for item in page["items"]}
        assert returned == {
            tasks[0]["uuid"],
            tasks[1]["uuid"],
            tasks[4]["uuid"],
            tasks[2]["uuid"],
            tasks[5]["uuid"],
        }
        assert page["total"] == 5
        assert all("jobs" in item for item in page["items"])
    finally:
        store.close()


def test_ephemeral_definition_creates_durable_restart_safe_task(
    tmp_path: Path,
) -> None:
    """进程内工作流定义应能创建不依赖定义表的持久 Task 快照。

    参数：``tmp_path`` 隔离运行事实文件库。返回：无；定义目录关闭后，重新打开
    文件库仍能读取 Task、输入和冻结图，且文件库从未写入 workflow 定义行。
    """

    database_path = tmp_path / "ephemeral-definition-runtime.db"
    runtime_store = WorkflowStore(
        database_path,
        persist_workflow_definitions=False,
    )
    definition_store = WorkflowStore(":memory:")
    service = WorkflowService(
        runtime_store,
        definition_store=definition_store,
    )
    client = TestClient(create_workflow_app(service))
    try:
        workflow_uuid = _create_workflow(client, definition_store)
        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": "normal",
                "input": {"count": 4},
                "meta_data": {},
            },
        )
        assert response.status_code == 201
        task = response.json()["data"]
        assert runtime_store.count_rows("workflow") == 0
        assert runtime_store.count_rows("workflow_node") == 0
        assert task["workflow_uuid"] == workflow_uuid
        assert task["workflow_snapshot"]["workflow"]["uuid"] == workflow_uuid
        task_uuid = task["uuid"]
    finally:
        service.close()

    reopened = WorkflowStore(
        database_path,
        persist_workflow_definitions=False,
    )
    try:
        recovered = reopened.get_task(task_uuid)
        assert recovered["input"] == {"count": 4, "label": "automatic"}
        assert recovered["workflow_snapshot"]["workflow"]["uuid"] == workflow_uuid
        assert reopened.count_rows("workflow") == 0
        from unilabos.persistence.database import SQLiteDatabase

        if isinstance(reopened._conn, SQLiteDatabase):
            assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        else:
            # PostgreSQL 外键写入时检查，另确认没有未验证的外键约束。
            assert reopened._conn.execute(
                "SELECT conname FROM pg_constraint WHERE contype = 'f' AND NOT convalidated"
            ).fetchall() == []
    finally:
        reopened.close()


def test_resource_slot_task_input_is_resolved_by_material_authority() -> None:
    """ResourceSlot 任务输入须由物料权威解析并校验模板允许集合。

    参数：无。返回：无。异常：解析或模板约束回归由断言暴露。
    """

    graph = _binding_graph()
    graph["workflow"]["meta_data"]["unilab"]["input_contract"] = {
        "version": 1,
        "parameters": [
            {
                "name": "sample",
                "schema": {
                    "$slot": "ResourceSlot",
                    "allowed_resource_template_uuids": [TEMPLATE_UUID],
                },
                "required": True,
            }
        ],
    }
    graph["workflow"]["meta_data"]["unilab"]["output_contract"] = {
        "version": 1,
        "outputs": [
            {
                "name": "sample",
                "schema": {
                    "$slot": "ResourceSlot",
                    "allowed_resource_template_uuids": [TEMPLATE_UUID],
                },
                "implicit": True,
            }
        ],
    }
    graph["workflow"]["meta_data"]["unilab"]["output_bindings"] = {
        "sample": {"kind": "workflow_input", "parameter": "sample"}
    }
    graph["nodes"][0]["meta_data"] = {}
    graph["handle_templates"][0]["required"] = False
    plan, jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="normal",
        target_node_uuid=None,
    )

    prepared = prepare_task_input(
        graph=graph,
        raw_input={"sample": {"uuid": MATERIAL_UUID}},
        execution_plan=plan,
        jobs=jobs,
        resource_resolver=lambda material_uuid: {
            "uuid": material_uuid,
            "resource_template_uuid": TEMPLATE_UUID,
        },
    )
    assert prepared.resolved_input == {"sample": {"uuid": MATERIAL_UUID}}

    with pytest.raises(TaskInputError):
        prepare_task_input(
            graph=graph,
            raw_input={"sample": {"uuid": MATERIAL_UUID}},
            execution_plan=plan,
            jobs=jobs,
            resource_resolver=lambda material_uuid: {
                "uuid": material_uuid,
                "resource_template_uuid": WORKFLOW_UUID,
            },
        )


def test_dynamic_material_inputs_are_added_to_resource_plan_after_binding() -> None:
    """动态 ResourceSlot 参数绑定后必须刷新动作物料资源计划。"""

    action_node_uuid = "66000000-0000-4000-8000-000000000001"
    action_template_uuid = "67000000-0000-4000-8000-000000000001"
    source_handle_uuid = "68000000-0000-4000-8000-000000000001"
    target_handle_uuid = "68000000-0000-4000-8000-000000000002"
    source_uuid = "69000000-0000-4000-8000-000000000001"
    target_uuid = "69000000-0000-4000-8000-000000000002"
    device_uuid = "66000000-0000-4000-8000-000000000003"
    graph = {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "revision": 1,
            "name": "dynamic material action",
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "source",
                                "schema": {"$slot": "ResourceSlot"},
                                "required": True,
                            },
                            {
                                "name": "target",
                                "schema": {"$slot": "ResourceSlot"},
                                "required": True,
                            },
                        ],
                    },
                    "output_contract": {
                        "version": 1,
                        "outputs": [
                            {
                                "name": "source",
                                "schema": {"$slot": "ResourceSlot"},
                                "implicit": True,
                            },
                            {
                                "name": "target",
                                "schema": {"$slot": "ResourceSlot"},
                                "implicit": True,
                            },
                        ],
                    },
                    "output_bindings": {
                        "source": {"kind": "workflow_input", "parameter": "source"},
                        "target": {"kind": "workflow_input", "parameter": "target"},
                    },
                }
            },
        },
        "nodes": [
            {
                "uuid": action_node_uuid,
                "workflow_node_template_uuid": action_template_uuid,
                "name": "dynamic material action",
                "pose": {},
                "material_uuid": device_uuid,
                "type": "ILab",
                "action_name": "transfer",
                "action_type": "UniLabJsonCommandAsync",
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "meta_data": {
                    "unilab": {
                        "executor_binding": {"mode": "fixed", "device_id": device_uuid},
                        "input_bindings": {
                            source_handle_uuid: {"parameter": "source"},
                            target_handle_uuid: {"parameter": "target"},
                        },
                    }
                },
            }
        ],
        "edges": [],
        "node_templates": [
            {
                "uuid": action_template_uuid,
                "node_type": "ILab",
                "type": "UniLabJsonCommandAsync",
                "schema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "object", "x-unilabos-material-lock": True},
                        "target": {"type": "object", "x-unilabos-material-lock": True},
                    },
                },
                "meta_data": {
                    "unilab": {
                        "action_contract_schema": {
                            "type": "object",
                            "properties": {
                                "goal": {
                                    "type": "object",
                                    "properties": {
                                        "source": {
                                            "type": "object",
                                            "x-unilabos-material-lock": True,
                                        },
                                        "target": {
                                            "type": "object",
                                            "x-unilabos-material-lock": True,
                                        },
                                    },
                                }
                            },
                                "x-unilabos-action-contract": {
                                    "version": 2,
                                },
                        }
                    }
                },
            }
        ],
        "handle_templates": [
            {
                "uuid": source_handle_uuid,
                "workflow_node_template_uuid": action_template_uuid,
                "handle_key": "source",
                "data_key": "source",
                "data_source": "goal",
                "io_type": "target",
                "type": "ResourceSlot",
                "required": True,
                "meta_data": {"unilab": {"value_schema": {"$slot": "ResourceSlot"}}},
            },
            {
                "uuid": target_handle_uuid,
                "workflow_node_template_uuid": action_template_uuid,
                "handle_key": "target",
                "data_key": "target",
                "data_source": "goal",
                "io_type": "target",
                "type": "ResourceSlot",
                "required": True,
                "meta_data": {"unilab": {"value_schema": {"$slot": "ResourceSlot"}}},
            },
        ],
    }
    plan, jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="normal",
        target_node_uuid=None,
    )

    prepared = prepare_task_input(
        graph=graph,
        raw_input={
            "source": {"uuid": source_uuid},
            "target": {"uuid": target_uuid},
        },
        execution_plan=plan,
        jobs=jobs,
        resource_resolver=lambda material_uuid: {
            "uuid": material_uuid,
            "resource_template_uuid": TEMPLATE_UUID,
        },
    )

    resource_plan = deserialize_resource_plan(
        prepared.execution_plan["resource_plan"]
    )
    resource_keys = {resource.canonical_key for resource in resource_plan.resources}
    assert f"material/{source_uuid}/exclusive" in resource_keys
    assert f"material/{target_uuid}/exclusive" in resource_keys
