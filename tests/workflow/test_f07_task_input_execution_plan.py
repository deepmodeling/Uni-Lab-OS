"""F07 工作流任务（WorkflowTask）输入与冻结执行计划（ExecutionPlan）合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import TaskInputError, prepare_task_input

WORKFLOW_UUID = "61000000-0000-4000-8000-000000000001"
NODE_UUID = "62000000-0000-4000-8000-000000000001"
TEMPLATE_UUID = "63000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "64000000-0000-4000-8000-000000000001"
MATERIAL_UUID = "65000000-0000-4000-8000-000000000001"


def _input_contract() -> dict[str, Any]:
    """构造包含必填标量和可选默认值的工作流输入合同。

    参数：无。返回：版本 1 工作流输入合同（WorkflowInputContract）。异常：无。
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
                "type": "manual_confirm",
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
                "node_type": "manual_confirm",
                "type": "manual_confirm",
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
                    "type": "manual_confirm",
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
    assert prepared.execution_plan["nodes"][0]["param"] == {"count": 3}
    assert prepared.jobs[0]["param"] == {"count": 3}
    assert prepared.workflow_snapshot == graph
    assert graph == original_graph
    assert plan == original_plan
    assert jobs == original_jobs


def test_task_input_quantity_is_projected_to_material_source_lot_requirement() -> None:
    """来源节点的数量绑定应在冻结计划时生成 lot 需求。"""

    source_uuid = "66000000-0000-4000-8000-000000000001"
    source_template_uuid = "67000000-0000-4000-8000-000000000001"
    material_template_uuid = "68000000-0000-4000-8000-000000000001"
    mount_uuid = "69000000-0000-4000-8000-000000000001"
    graph = {
        "workflow": {"uuid": WORKFLOW_UUID, "revision": 1, "meta_data": {}},
        "nodes": [
            {
                "uuid": source_uuid,
                "workflow_node_template_uuid": source_template_uuid,
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "resource_template_uuid": material_template_uuid,
                    "mount": {"uuid": mount_uuid},
                    "material_uuid": None,
                    "site": None,
                    "slot_range": None,
                    "flow_role": "reagent",
                },
                "meta_data": {
                    "unilab": {
                        "quantity_parameter": "volume_ml",
                        "quantity_unit": "ml",
                    }
                },
                "execution_policy": {},
                "disabled": False,
            }
        ],
        "edges": [],
        "node_templates": [
            {
                "uuid": source_template_uuid,
                "node_type": "material_source",
                "type": "material_source",
            }
        ],
        "handle_templates": [],
    }

    plan, _jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="normal",
        target_node_uuid=None,
        input_value={"volume_ml": 100},
    )

    requirements = plan["nodes"][0]["material_requirements"]
    assert {item["quantity"] for item in requirements if "quantity" in item} == {100.0}
    assert {item["unit"] for item in requirements if "quantity" in item} == {"ml"}


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
                        "type": "manual_confirm",
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


def test_batch_task_repeats_one_workflow_with_independent_inputs(
    tmp_path: Path,
) -> None:
    """A logical BatchTask must persist and expose each child's own input."""

    client, store = _client(tmp_path / "workflow-batch-task.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        response = client.post(
            "/api/v1/workflow-batch-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "description": "two independently parameterized samples",
                "meta_data": {"campaign": "ethanol-nacl"},
                "instances": [
                    {
                        "instance_id": "sample-001",
                        "input": {"count": 100},
                        "meta_data": {"stack_site": "L1B1"},
                    },
                    {
                        "instance_id": "sample-002",
                        "input": {"count": 200},
                        "meta_data": {"stack_site": "L1B2"},
                    },
                ],
            },
        )

        assert response.status_code == 201
        batch = response.json()["data"]
        assert batch["workflow_uuid"] == workflow_uuid
        assert batch["status"] == "pending"
        assert batch["total_instances"] == 2
        assert [item["instance_id"] for item in batch["instances"]] == [
            "sample-001",
            "sample-002",
        ]
        assert [item["input"]["count"] for item in batch["instances"]] == [
            100,
            200,
        ]
        assert batch["instances"][0]["meta_data"] == {"stack_site": "L1B1"}

        fetched = client.get(
            f"/api/v1/workflow-batch-tasks/{batch['uuid']}"
        )
        assert fetched.status_code == 200
        assert fetched.json()["data"] == batch
        assert _row_counts(store) == (2, 2)

        child_uuids = [item["workflow_task_uuid"] for item in batch["instances"]]
        store._conn.execute(
            "UPDATE workflow_task SET status = 'succeeded' WHERE uuid = ?",
            (child_uuids[0],),
        )
        store._conn.execute(
            "UPDATE workflow_task SET status = 'failed' WHERE uuid = ?",
            (child_uuids[1],),
        )
        store._conn.commit()
        aggregate = client.get(
            f"/api/v1/workflow-batch-tasks/{batch['uuid']}"
        ).json()["data"]
        assert aggregate["status"] == "partial_success"
    finally:
        store.close()


def test_batch_task_validates_all_inputs_before_creating_any_child(
    tmp_path: Path,
) -> None:
    """One malformed instance must reject the batch without partial writes."""

    client, store = _client(tmp_path / "workflow-batch-task-invalid.db")
    try:
        workflow_uuid = _create_workflow(client, store)
        before = _row_counts(store)
        response = client.post(
            "/api/v1/workflow-batch-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "instances": [
                    {"instance_id": "sample-001", "input": {"count": 100}},
                    {"instance_id": "sample-002", "input": {"count": "bad"}},
                ],
            },
        )

        assert response.status_code == 200
        assert response.json()["code"] == 1000
        assert _row_counts(store) == before
    finally:
        store.close()


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
