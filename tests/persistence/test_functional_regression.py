"""业务链路双库回归。

同一组用例在 SQLite 与 PostgreSQL 上各执行一次。覆盖创建工作流、实验操作、
物料、试剂、任务执行和调度，不只检查旧表是否还在。

用例：

- A1 普通工作流可创建、读取、改名，类型保持 normal，列表可按类型筛选。
- A2 空名称、未知类型不落库。
- A3 公共接口保存图并创建单节点任务，快照含全部节点，计划和作业只含目标。
- B1 实验操作可归类、按旧标签映射、按类别筛选；普通工作流不能挂类别；
  不存在的类别按未找到拒绝；被引用的类别不能删，解除引用后可以删。
- C1 物料位姿（含原点）写在物料行上，显式 null 不清除已有位置。
- C2 未知模板不创建物料。
- D1 内联试剂与容器一次提交，试剂视图和容器内容一致，试剂不是物理表。
- D2 非法内联试剂回滚，不留容器。
- D3 试剂身份可按 CAS 查询，数量修订冲突只提交一次，历史和台账留下。
- D4 删除后的试剂对数量协调器不可见。
- E1 已持久化的设备任务由调度桥派发，任务运行、作业已派发、身份稳定。
- E2 任务声明物料但调度器没有库存权威时不派发。
- E3 生产运行库没有工作流定义行时，任务仍可派发。
- E4 预留后的成功消费只扣一次，重放不重复扣减。
- E5 可用数量不足时预检失败且不写预留。
- E6 活动预留阻止把余量改到已承诺数量以下，释放后允许。
- F1 整条链路之后，已删除的库存表和工站发件箱不会被重建。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.workflow.quantity_inventory_test_support import (
    JOB_UUID as QUANTITY_JOB_UUID,
)
from tests.workflow.quantity_inventory_test_support import (
    TASK_UUID as QUANTITY_TASK_UUID,
)
from tests.workflow.quantity_inventory_test_support import _inventory, _workflow
from tests.workflow.test_authoring_engine import _engine
from tests.workflow.test_quantity_inventory_runtime import _create_task
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import (
    RESOURCE_DATA_CONFLICT,
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.inventory.workflow_quantity import (
    WorkflowQuantityInventoryAuthority,
    WorkflowQuantityReservationError,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.domain_source_target import DomainWorkflowSourceTarget
from unilabos.workflow.quantity_inventory import WorkflowQuantityInventory
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_discovery import discover_editable_sources
from unilabos.workflow.store import StoreConflict, WorkflowStore

_TASK_UUID = "21000000-0000-4000-8000-000000000091"
_NODE_UUID = "31000000-0000-4000-8000-000000000091"
_JOB_UUID = "41000000-0000-4000-8000-000000000091"
_NODE_A = "81000000-0000-4000-8000-0000000000a1"
_NODE_B = "81000000-0000-4000-8000-0000000000a2"
_CREATED_AT = "2026-10-09T00:00:00Z"
_DROPPED_INVENTORY_TABLES = (
    "material_inventory",
    "relative_position",
    "material_content_version",
    "sample",
    "current_substance",
    "reagent",
    "lab_zone",
    "lab_placement",
)


def _workflow_app(tmp_path: Path) -> tuple[TestClient, WorkflowService, WorkflowStore]:
    """装配把工作流定义写入当前数据库的应用。"""

    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    return TestClient(create_workflow_app(service)), service, store


def _domain_workflow_app(
    tmp_path: Path,
) -> tuple[TestClient, WorkflowService, WorkflowStore]:
    """装配带领域包的实验操作应用，定义仍写入当前数据库。"""

    selected_root = tmp_path / "domain"
    package_root = selected_root / "demo_domain"
    package_root.mkdir(parents=True)
    (selected_root / "package.yaml").write_text(
        "package:\n  name: demo_domain\nworkflows: []\n",
        encoding="utf-8",
    )
    plan = discover_editable_sources((selected_root,))
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(
        store,
        compiler=_engine(),
        source_target=DomainWorkflowSourceTarget.from_discovery_plan(plan),
    )
    service.replace_discovered_source_authorizations(plan)
    return TestClient(create_workflow_app(service)), service, store


def _inventory_app(tmp_path: Path) -> tuple[TestClient, InventoryStore]:
    """装配把物料和试剂写入当前数据库的应用。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    return TestClient(app), store


def _template(client: TestClient, template_id: str = "local.regression_bottle") -> str:
    """登记容器模板并返回 UUID。"""

    response = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": template_id,
                    "display_name": "回归试剂瓶",
                    "registry_type": "material",
                    "category": ["container"],
                    "metadata": {"capacity": {"max_volume_ul": 1000000}},
                    "model": {},
                    "class": {},
                    "handles": [],
                    "config_info": [],
                    "scene": [],
                    "device_params": {},
                }
            ]
        },
    )
    assert response.status_code == 200
    return response.json()["data"]["templates"][0]["uuid"]


def _reagent_info(client: TestClient) -> str:
    """登记乙醇身份并返回 UUID。"""

    response = client.post(
        "/api/v1/reagent-infos",
        json={
            "cas": "64-17-5",
            "name": "乙醇",
            "name_en": "Ethanol",
            "aliases": ["酒精"],
            "molecular_formula": "C2H6O",
            "density_g_per_ml": 0.789,
            "physical_state": "liquid",
            "meta_data": {"storage": "阴凉通风"},
        },
    )
    assert response.status_code == 201
    return response.json()["data"]["uuid"]


def _seed_device_task(
    store: WorkflowStore,
    workflow_uuid: str,
    *,
    with_material: bool,
) -> dict:
    """把一台设备动作写成待派发任务。工作流定义须已经存在，或运行库已去掉定义外键。"""

    execution_plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": _NODE_UUID,
                "kind": "device_action",
                "device_id": "reactor-a",
                "action_name": "distribute",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        }
                    },
                    "additionalProperties": False,
                },
                "material_requirements": (
                    [{"instance_uuid": "51000000-0000-4000-8000-000000000091"}]
                    if with_material
                    else []
                ),
            }
        ],
        "handles": [],
        "edges": [],
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, execution_mode, target_node_uuid,
                control_status, cleanup_status, trace_context, input, output,
                error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', ?,
                      'normal', 'normal', NULL, 'active', 'none', '{}', '{}',
                      '{}', '[]')
            """,
            (
                _TASK_UUID,
                _CREATED_AT,
                _CREATED_AT,
                workflow_uuid,
                json.dumps(execution_plan),
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status, attempt,
                param, feedback_data, return_info, control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (_JOB_UUID, _CREATED_AT, _CREATED_AT, _TASK_UUID, _NODE_UUID),
        )
    return store.get_task(_TASK_UUID)


def _submit(store: WorkflowStore, task: dict, scheduler: EdgeScheduler) -> dict:
    """把已持久化任务交给生产调度桥。"""

    bridge = importlib.import_module(
        "unilabos.workflow.task_scheduler_bridge"
    ).TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        return bridge.submit(task)
    finally:
        bridge.close()


def test_a1_normal_workflow_round_trip(database_mode, tmp_path: Path) -> None:
    """A1：普通工作流可创建、读取、改名，并只出现在 normal 列表里。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        created = client.post(
            "/api/v1/workflows",
            json={
                "name": "普通流程",
                "description": "普通工作流说明",
                "tags": ["regression"],
                "meta_data": {},
            },
        )
        assert created.status_code == 201
        body = created.json()["data"]
        assert body["workflow_type"] == "normal"
        assert body["description"] == "普通工作流说明"
        assert store.count_rows("workflow") == 1

        detail = client.get(f"/api/v1/workflows/{body['uuid']}").json()["data"]
        assert detail["name"] == "普通流程"
        renamed = client.put(
            f"/api/v1/workflows/{body['uuid']}",
            json={
                "name": "普通流程已改名",
                "description": "普通工作流说明",
                "tags": ["regression"],
                "meta_data": {},
            },
        )
        assert renamed.json()["data"]["name"] == "普通流程已改名"
        assert renamed.json()["data"]["workflow_type"] == "normal"
        listed = client.get(
            "/api/v1/workflows", params={"workflow_type": "normal"}
        ).json()["data"]["items"]
        assert [item["uuid"] for item in listed] == [body["uuid"]]
        assert database_mode in {"sqlite", "postgresql"}
    finally:
        service.close()
        store.close()


def test_a2_blank_name_and_unknown_type_leave_no_row(database_mode, tmp_path: Path) -> None:
    """A2：空名称和未知类型被拒绝，工作流表保持为空。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        blank = client.post(
            "/api/v1/workflows",
            json={"name": "   ", "tags": [], "meta_data": {}},
        )
        unknown = client.post(
            "/api/v1/workflows",
            json={
                "name": "旧业务名称",
                "workflow_type": "subworkflow",
                "tags": [],
                "meta_data": {},
            },
        )
        assert blank.json()["code"] == 1000
        assert unknown.json()["code"] != 0
        assert store.count_rows("workflow") == 0
    finally:
        service.close()
        store.close()


def test_a3_graph_and_single_node_task_freeze_only_the_target(
    database_mode, tmp_path: Path
) -> None:
    """A3：保存图后创建单节点任务，快照保留全图，计划和作业只保留目标。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        created = client.post(
            "/api/v1/workflows",
            json={"name": "单节点执行", "tags": [], "meta_data": {}},
        )
        assert created.status_code == 201
        workflow_uuid = created.json()["data"]["uuid"]
        saved = client.put(
            f"/api/v1/workflows/{workflow_uuid}/graph",
            json={
                "revision": 1,
                "nodes": [
                    {
                        "uuid": _NODE_A,
                        "name": "准备",
                        "type": "compute",
                        "pose": {},
                        "param": {},
                        "execution_policy": {},
                        "disabled": False,
                        "minimized": False,
                        "meta_data": {},
                    },
                    {
                        "uuid": _NODE_B,
                        "name": "计算",
                        "type": "compute",
                        "pose": {},
                        "param": {},
                        "execution_policy": {},
                        "disabled": False,
                        "minimized": False,
                        "meta_data": {},
                    },
                ],
                "edges": [],
            },
        )
        assert saved.json()["code"] == 0
        task = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow_uuid,
                "run_mode": "single_node",
                "target_node_uuid": _NODE_B,
                "input": {},
            },
        )
        assert task.status_code == 201, task.text
        task_body = task.json()["data"]
        assert {node["uuid"] for node in task_body["workflow_snapshot"]["nodes"]} == {
            _NODE_A,
            _NODE_B,
        }
        assert [node["uuid"] for node in task_body["execution_plan"]["nodes"]] == [
            _NODE_B
        ]
        jobs = client.get(f"/api/v1/workflow-tasks/{task_body['uuid']}/jobs").json()[
            "data"
        ]
        assert [job["workflow_node_uuid"] for job in jobs] == [_NODE_B]
        assert store.count_rows("workflow_task") == 1
        assert store.count_rows("workflow_node_job") == 1
    finally:
        service.close()
        store.close()


def test_b1_experiment_operation_category_lifecycle(
    database_mode, tmp_path: Path
) -> None:
    """B1：实验操作归类、旧标签、筛选和类别引用保护。"""

    client, service, store = _domain_workflow_app(tmp_path)
    try:
        categories = client.get("/api/v1/experiment-operation-categories").json()[
            "data"
        ]["items"]
        assert {item["name"] for item in categories} >= {
            "设备操作",
            "集成操作",
            "人工操作",
        }
        device_category = next(item for item in categories if item["name"] == "设备操作")
        created_category = client.post(
            "/api/v1/experiment-operation-categories",
            json={"name": "回归操作", "sort_order": 40},
        )
        assert created_category.status_code == 201
        category = created_category.json()["data"]
        renamed = client.put(
            f"/api/v1/experiment-operation-categories/{category['uuid']}",
            json={"name": "回归实验", "sort_order": 12},
        )
        assert renamed.json()["data"]["name"] == "回归实验"

        rejected = client.post(
            "/api/v1/workflows",
            json={
                "name": "错误分类",
                "workflow_type": "normal",
                "operation_category_uuid": category["uuid"],
                "tags": [],
                "meta_data": {},
            },
        )
        missing = client.post(
            "/api/v1/workflows",
            json={
                "name": "不存在的类别",
                "workflow_type": "experiment_operation",
                "operation_category_uuid": "91000000-0000-4000-8000-000000000091",
                "tags": [],
                "meta_data": {},
            },
        )
        assert rejected.json()["code"] == 1000
        assert missing.json()["code"] == 3002

        explicit = client.post(
            "/api/v1/workflows",
            json={
                "name": "显式设备操作",
                "workflow_type": "experiment_operation",
                "operation_category_uuid": device_category["uuid"],
                "tags": [],
                "meta_data": {},
            },
        )
        legacy = client.post(
            "/api/v1/workflows",
            json={
                "name": "旧标签设备操作",
                "workflow_type": "experiment_operation",
                "tags": ["device_operation"],
                "meta_data": {},
            },
        )
        assert explicit.status_code == 201
        assert explicit.json()["data"]["operation_category_uuid"] == device_category["uuid"]
        assert legacy.status_code == 201
        assert legacy.json()["data"]["operation_category_uuid"] == device_category["uuid"]
        filtered = client.get(
            "/api/v1/workflows",
            params={
                "workflow_type": "experiment_operation",
                "operation_category_uuid": device_category["uuid"],
            },
        ).json()["data"]["items"]
        assert {item["uuid"] for item in filtered} == {
            explicit.json()["data"]["uuid"],
            legacy.json()["data"]["uuid"],
        }

        referenced = client.post(
            "/api/v1/workflows",
            json={
                "name": "引用自定义类别",
                "workflow_type": "experiment_operation",
                "operation_category_uuid": category["uuid"],
                "tags": [],
                "meta_data": {},
            },
        ).json()["data"]
        in_use = client.delete(
            f"/api/v1/experiment-operation-categories/{category['uuid']}"
        )
        assert in_use.json()["code"] == 3003
        assert (
            client.get(f"/api/v1/workflows/{referenced['uuid']}").json()["data"][
                "operation_category_uuid"
            ]
            == category["uuid"]
        )
        cleared = client.put(
            f"/api/v1/workflows/{referenced['uuid']}",
            json={
                "name": referenced["name"],
                "workflow_type": "experiment_operation",
                "operation_category_uuid": None,
                "tags": [],
                "meta_data": {},
            },
        )
        assert cleared.json()["data"]["operation_category_uuid"] is None
        assert (
            client.delete(
                f"/api/v1/experiment-operation-categories/{category['uuid']}"
            ).json()["code"]
            == 0
        )
        assert store.count_rows("workflow") == 3
    finally:
        service.close()
        store.close()


def test_c1_material_pose_round_trip_keeps_origin_and_explicit_null(
    database_mode, tmp_path: Path
) -> None:
    """C1：非零位姿和原点都写在物料行上，显式 null 不删除已有位置。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client)
        positioned = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "POSITIONED",
                "name": "有位置",
                "relative_position": {
                    "position_x": 1.5,
                    "position_y": 2,
                    "position_z": 3,
                    "depth": 4,
                    "length": 5,
                    "width": 6,
                    "scale_x": 1,
                    "scale_y": 1,
                    "scale_z": 1,
                },
            },
        )
        origin = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "ORIGIN",
                "name": "原点",
                "relative_position": {
                    "position_x": 0,
                    "position_y": 0,
                    "position_z": 0,
                    "depth": 0,
                    "length": 0,
                    "width": 0,
                    "scale_x": 1,
                    "scale_y": 1,
                    "scale_z": 1,
                },
            },
        )
        assert positioned.status_code == 201
        assert origin.status_code == 201
        positioned_body = positioned.json()["data"]
        origin_body = origin.json()["data"]
        assert positioned_body["relative_position"]["position_x"] == 1.5
        assert origin_body["relative_position"]["position_x"] == 0
        kept = client.put(
            f"/api/v1/materials/{positioned_body['uuid']}",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "POSITIONED",
                "name": "有位置",
                "relative_position": None,
            },
        )
        assert kept.status_code == 200
        assert kept.json()["data"]["relative_position"]["position_x"] == 1.5
        assert store.query_one(
            "SELECT position_x, pose_uuid FROM material WHERE uuid=?",
            (positioned_body["uuid"],),
        )["position_x"] == 1.5
        origin_row = store.query_one(
            "SELECT position_x, pose_uuid FROM material WHERE uuid=?",
            (origin_body["uuid"],),
        )
        assert origin_row["position_x"] == 0
        assert origin_row["pose_uuid"]
        assert "relative_position" not in store.db.table_names()
    finally:
        store.close()


def test_c2_unknown_template_does_not_create_material(
    database_mode, tmp_path: Path
) -> None:
    """C2：模板不存在时物料创建失败，物料表没有半成品。"""

    client, store = _inventory_app(tmp_path)
    try:
        rejected = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": "91000000-0000-4000-8000-000000000092",
                "barcode": "MISSING-TEMPLATE",
                "name": "不应保留",
            },
        )
        assert rejected.json()["code"] != 0
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM material WHERE barcode=?",
            ("MISSING-TEMPLATE",),
        ) == {"count": 0}
    finally:
        store.close()


def test_d1_inline_reagent_is_atomic_and_readable_as_content(
    database_mode, tmp_path: Path
) -> None:
    """D1：容器和试剂一次创建，视图、内容行和数量协调器读到同一份数量。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client, "local.inline_bottle")
        info_uuid = _reagent_info(client)
        created = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "name": "乙醇瓶",
                "barcode": "INLINE-ETHANOL",
                "reagent": {
                    "reagent_info_uuid": info_uuid,
                    "quantity": 500,
                    "quantity_unit": "mL",
                    "concentration_value": 95,
                    "concentration_unit": "%",
                    "source": "frontend:workbench",
                    "meta_data": {"batch": "regression"},
                },
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()["data"]
        assert body["reagent"]["quantity"] == 500
        assert body["reagent"]["reagent_info_uuid"] == info_uuid
        reread = client.get(f"/api/v1/reagents/{body['reagent']['uuid']}").json()["data"]
        assert reread["quantity"] == 500
        assert reread["material_uuid"] == body["uuid"]
        content = WorkflowQuantityInventoryAuthority(
            store, edge_id="edge-regression", lab_id="lab-regression"
        ).content("reagent", body["reagent"]["uuid"])
        assert content["material_uuid"] == body["uuid"]
        assert float(content["quantity"]) == 500
        assert store.query_one(
            """
            SELECT kind, quantity FROM container_content
            WHERE material_uuid=? AND deleted_at IS NULL
            """,
            (body["uuid"],),
        )["kind"] == "reagent"
        tables = store.db.table_names()
        assert "container_content" in tables
        assert "reagent" not in tables
    finally:
        store.close()


def test_d2_invalid_inline_reagent_rolls_back_material(
    database_mode, tmp_path: Path
) -> None:
    """D2：内联试剂缺少已登记身份时，容器和内容都不保留。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client, "local.rollback_bottle")
        rejected = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "name": "不应保留的容器",
                "barcode": "INLINE-ROLLBACK",
                "reagent": {"cas": "67-56-1", "quantity": 10, "quantity_unit": "mL"},
            },
        )
        assert rejected.json()["code"] == 4001
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM material WHERE barcode=?",
            ("INLINE-ROLLBACK",),
        ) == {"count": 0}
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM container_content"
        ) == {"count": 0}
    finally:
        store.close()


def test_d3_reagent_revision_history_and_identity_lookup(
    database_mode, tmp_path: Path
) -> None:
    """D3：试剂身份可查询，错误修订不提交，正确修订留下历史和台账。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client, "local.revision_bottle")
        material = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "name": "修订瓶",
                "barcode": "REVISION-ETHANOL",
            },
        )
        assert material.status_code == 201
        material_uuid = material.json()["data"]["uuid"]
        info_uuid = _reagent_info(client)
        created = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": material_uuid,
                "cas": "64-17-5",
                "quantity": 500,
                "quantity_unit": "mL",
                "meta_data": {},
            },
        )
        assert created.status_code == 201
        reagent_uuid = created.json()["data"]["uuid"]
        assert created.json()["data"]["revision"] == 1
        conflict = client.put(
            f"/api/v1/reagents/{reagent_uuid}",
            json={
                "quantity": 450,
                "quantity_unit": "mL",
                "expected_revision": 9,
                "meta_data": {},
            },
        )
        updated = client.put(
            f"/api/v1/reagents/{reagent_uuid}",
            json={
                "quantity": 450,
                "quantity_unit": "mL",
                "expected_revision": 1,
                "meta_data": {},
            },
        )
        history = client.get(
            f"/api/v1/materials/{material_uuid}/reagent-history?page=1&page_size=100"
        ).json()["data"]
        registered = client.get("/api/v1/compounds/64-17-5").json()["data"]
        unavailable = client.get("/api/v1/compounds/67-56-1").json()["data"]

        assert conflict.json()["code"] == 4002
        assert updated.json()["data"]["revision"] == 2
        assert updated.json()["data"]["quantity"] == 450
        assert [entry["event_type"] for entry in history["items"]] == ["adjust", "add"]
        assert registered["status"] == "registered"
        assert unavailable["status"] == "unavailable"
        assert store.query_one(
            """
            SELECT quantity FROM container_content
            WHERE uuid=? AND kind='reagent' AND deleted_at IS NULL
            """,
            (reagent_uuid,),
        )["quantity"] == 450 or float(
            store.query_one(
                """
                SELECT quantity FROM container_content
                WHERE uuid=? AND kind='reagent' AND deleted_at IS NULL
                """,
                (reagent_uuid,),
            )["quantity"]
        ) == 450
        ledger = store.query_all(
            "SELECT op_type FROM inventory_ledger WHERE subject_type='reagent' "
            "ORDER BY ledger_id"
        )
        assert [row["op_type"] for row in ledger] == ["reagent.add", "reagent.adjust"]
        protected = client.delete(f"/api/v1/reagent-infos/{info_uuid}")
        assert protected.json()["code"] == 4000
    finally:
        store.close()


def test_d4_deleted_reagent_is_invisible_to_quantity_authority(
    database_mode, tmp_path: Path
) -> None:
    """D4：删除试剂后，数量协调器按视图读不到它。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client, "local.delete_bottle")
        material_uuid = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "name": "待删除瓶",
                "barcode": "DELETE-ETHANOL",
            },
        ).json()["data"]["uuid"]
        _reagent_info(client)
        reagent_uuid = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": material_uuid,
                "cas": "64-17-5",
                "quantity": 10,
                "quantity_unit": "mL",
                "meta_data": {},
            },
        ).json()["data"]["uuid"]
        assert client.delete(f"/api/v1/reagents/{reagent_uuid}").json()["code"] == 0
        authority = WorkflowQuantityInventoryAuthority(
            store, edge_id="edge-regression", lab_id="lab-regression"
        )
        with pytest.raises(WorkflowQuantityReservationError):
            authority.content("reagent", reagent_uuid)
        assert store.query_one(
            """
            SELECT COUNT(*) AS count FROM container_content
            WHERE uuid=? AND deleted_at IS NULL
            """,
            (reagent_uuid,),
        ) == {"count": 0}
    finally:
        store.close()


def test_e1_scheduler_dispatches_persisted_device_task(
    database_mode, tmp_path: Path
) -> None:
    """E1：公共接口创建的工作流上，调度桥派发设备任务并保持任务和作业身份。"""

    client, service, store = _workflow_app(tmp_path)
    observed: list[tuple[str, str]] = []

    class _ObservingDispatcher(RecordingDispatcher):
        """在物理派发边界记录标准任务和作业状态。"""

        def dispatch(self, payload) -> None:
            observed.append(
                (
                    store.get_task(_TASK_UUID)["status"],
                    store.get_job(_JOB_UUID)["status"],
                )
            )
            super().dispatch(payload)

    dispatcher = _ObservingDispatcher()
    try:
        workflow_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "待调度流程", "tags": [], "meta_data": {}},
        ).json()["data"]["uuid"]
        task = _seed_device_task(store, workflow_uuid, with_material=False)
        aggregate = _submit(store, task, EdgeScheduler(dispatcher=dispatcher))
        assert observed == [("running", "dispatched")]
        assert aggregate["task"]["status"] == "running"
        assert dispatcher.dispatched[0]["task_id"] == _TASK_UUID
        assert dispatcher.dispatched[0]["job_id"] == _JOB_UUID
        assert dispatcher.dispatched[0]["attempt"] == 1
        assert store.get_task(_TASK_UUID)["workflow_uuid"] == workflow_uuid
        assert store.get_job(_JOB_UUID)["status"] in {"dispatched", "running"}
        assert "workflow_station_event_outbox" not in store.db.table_names()
    finally:
        service.close()
        store.close()


def test_e2_material_requirement_without_inventory_does_not_dispatch(
    database_mode, tmp_path: Path
) -> None:
    """E2：任务带物料需求但调度器没有库存权威时，不派发且任务保持待处理。"""

    client, service, store = _workflow_app(tmp_path)
    dispatcher = RecordingDispatcher()
    try:
        workflow_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "缺库存流程", "tags": [], "meta_data": {}},
        ).json()["data"]["uuid"]
        task = _seed_device_task(store, workflow_uuid, with_material=True)
        with pytest.raises(RuntimeError, match="库存"):
            _submit(store, task, EdgeScheduler(dispatcher=dispatcher))
        assert dispatcher.dispatched == []
        assert store.get_task(_TASK_UUID)["status"] == "pending"
        assert store.get_job(_JOB_UUID)["status"] == "pending"
    finally:
        service.close()
        store.close()


def test_e3_runtime_store_dispatches_without_definition_row(
    database_mode, tmp_path: Path
) -> None:
    """E3：生产运行库不保存工作流定义时，任务仍能引用定义身份并完成派发。"""

    store = WorkflowStore(
        tmp_path / "workflow-runtime.db",
        persist_workflow_definitions=False,
    )
    observed: list[tuple[str, str]] = []

    class _ObservingDispatcher(RecordingDispatcher):
        """在物理派发边界记录标准任务和作业状态。"""

        def dispatch(self, payload) -> None:
            observed.append(
                (
                    store.get_task(_TASK_UUID)["status"],
                    store.get_job(_JOB_UUID)["status"],
                )
            )
            super().dispatch(payload)

    dispatcher = _ObservingDispatcher()
    try:
        assert store.count_rows("workflow") == 0
        workflow_uuid = "11000000-0000-4000-8000-000000000091"
        task = _seed_device_task(store, workflow_uuid, with_material=False)
        aggregate = _submit(store, task, EdgeScheduler(dispatcher=dispatcher))
        assert observed == [("running", "dispatched")]
        assert aggregate["task"]["status"] == "running"
        assert store.count_rows("workflow") == 0
        assert store.get_task(_TASK_UUID)["workflow_uuid"] == workflow_uuid
        assert store.get_job(_JOB_UUID)["status"] in {"dispatched", "running"}
        assert dispatcher.dispatched[0]["job_id"] == _JOB_UUID
    finally:
        store.close()


def test_e4_successful_consumption_deducts_reagent_once(
    database_mode, tmp_path: Path
) -> None:
    """E4：任务预留试剂后，成功消费只扣一次，重放结果相同。"""

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store, InventoryService(inventory_store)
    )
    try:
        _create_task(
            workflow_store,
            coordinator,
            graph=graph,
            prepared=prepared,
            reagent_uuid=reagent_uuid,
        )
        consumption = [
            {
                "inventory_type": "reagent",
                "inventory_uuid": reagent_uuid,
                "actual_quantity": 1500,
                "quantity_unit": "uL",
            }
        ]
        first = coordinator.consume_successful_job(
            task_uuid=QUANTITY_TASK_UUID,
            job_uuid=QUANTITY_JOB_UUID,
            consumptions=consumption,
        )
        replay = coordinator.consume_successful_job(
            task_uuid=QUANTITY_TASK_UUID,
            job_uuid=QUANTITY_JOB_UUID,
            consumptions=consumption,
        )
        assert first == replay
        assert first[0]["status"] == "consumed"
        assert float(first[0]["balance_before"]) == 10
        assert float(first[0]["balance_after"]) == 8.5
        remaining = inventory_store.query_one(
            "SELECT quantity FROM reagent WHERE uuid=?",
            (reagent_uuid,),
        )
        assert float(remaining["quantity"]) == 8.5
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_ledger "
            "WHERE workflow_node_job_uuid=? "
            "AND subject_type IN ('reagent','current_substance')",
            (QUANTITY_JOB_UUID,),
        ) == {"count": 1}
    finally:
        workflow_store.close()
        inventory_store.close()


def test_e5_insufficient_quantity_preflight_writes_nothing(
    database_mode, tmp_path: Path
) -> None:
    """E5：余量足够时预检不写库；余量不足时预检失败，预留和任务仍为空。"""

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store, InventoryService(inventory_store)
    )
    bindings = [
        {
            "requirement_key": "ethanol",
            "inventory_type": "reagent",
            "inventory_uuid": reagent_uuid,
            "reserved_quantity": 2,
            "quantity_unit": "mL",
        }
    ]
    try:
        allocations = coordinator.preflight_task_allocations(
            graph=graph, prepared=prepared, bindings=bindings
        )
        assert allocations[0]["reserved_quantity"] == 2
        assert workflow_store.count_rows("workflow_task") == 0
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_reservation"
        ) == {"count": 0}
        current = BackendReagentService(inventory_store).get_reagent(reagent_uuid)
        BackendReagentService(inventory_store).update_reagent(
            reagent_uuid,
            {**current, "quantity": 1, "expected_revision": current["revision"]},
        )
        with pytest.raises(StoreConflict, match="可用数量不足"):
            coordinator.preflight_task_allocations(
                graph=graph, prepared=prepared, bindings=bindings
            )
        assert float(
            inventory_store.query_one(
                "SELECT quantity FROM reagent WHERE uuid=?",
                (reagent_uuid,),
            )["quantity"]
        ) == 1
        assert inventory_store.query_one(
            "SELECT COUNT(*) AS count FROM inventory_reservation"
        ) == {"count": 0}
        assert workflow_store.count_rows("workflow_inventory_allocation") == 0
    finally:
        workflow_store.close()
        inventory_store.close()


def test_e6_active_reservation_protects_reagent_quantity(
    database_mode, tmp_path: Path
) -> None:
    """E6：活动预留阻止把余量降到承诺量以下，释放后允许减少。"""

    inventory_store, material_uuid, info_uuid, reagent_uuid = _inventory(tmp_path)
    workflow_store, graph, prepared = _workflow(
        tmp_path / "workflow.db",
        reagent_info_uuid=info_uuid,
        material_uuid=material_uuid,
    )
    coordinator = WorkflowQuantityInventory(
        workflow_store, InventoryService(inventory_store)
    )
    reagent_service = BackendReagentService(inventory_store)
    try:
        _create_task(
            workflow_store,
            coordinator,
            graph=graph,
            prepared=prepared,
            reagent_uuid=reagent_uuid,
        )
        current = reagent_service.get_reagent(reagent_uuid)
        with pytest.raises(BackendContractError) as raised:
            reagent_service.update_reagent(
                reagent_uuid,
                {**current, "quantity": 1, "expected_revision": current["revision"]},
            )
        assert raised.value.code == RESOURCE_DATA_CONFLICT
        increased = reagent_service.update_reagent(
            reagent_uuid,
            {**current, "quantity": 11, "expected_revision": current["revision"]},
        )
        assert float(increased["quantity"]) == 11
        coordinator.release_task(QUANTITY_TASK_UUID, reason="regression_terminal")
        released = reagent_service.update_reagent(
            reagent_uuid,
            {
                **increased,
                "quantity": 1,
                "expected_revision": increased["revision"],
            },
        )
        assert float(released["quantity"]) == 1
        assert inventory_store.query_one(
            "SELECT status FROM inventory_reservation WHERE workflow_id=? AND node_id=?",
            (QUANTITY_TASK_UUID, QUANTITY_JOB_UUID),
        ) == {"status": "released"}
    finally:
        workflow_store.close()
        inventory_store.close()


def test_f1_business_writes_do_not_recreate_dropped_tables(
    database_mode, tmp_path: Path
) -> None:
    """F1：创建工作流、物料和试剂后，已合并或已删除的表不会回来。"""

    workflow_client, service, workflow_store = _workflow_app(tmp_path)
    inventory_client, inventory_store = _inventory_app(tmp_path)
    try:
        assert workflow_client.post(
            "/api/v1/workflows",
            json={"name": "不变量流程", "tags": [], "meta_data": {}},
        ).status_code == 201
        template_uuid = _template(inventory_client, "local.invariant_bottle")
        info_uuid = _reagent_info(inventory_client)
        created = inventory_client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "name": "不变量瓶",
                "barcode": "INVARIANT",
                "reagent": {
                    "reagent_info_uuid": info_uuid,
                    "quantity": 5,
                    "quantity_unit": "mL",
                    "meta_data": {},
                },
            },
        )
        assert created.status_code == 201
        inventory_tables = inventory_store.db.table_names()
        workflow_tables = workflow_store.db.table_names()
        for name in _DROPPED_INVENTORY_TABLES:
            assert name not in inventory_tables
        assert "container_content" in inventory_tables
        assert "workflow_station_event_outbox" not in workflow_tables
        assert "workflow" in workflow_tables
        assert workflow_store.count_rows("workflow") == 1
    finally:
        service.close()
        workflow_store.close()
        inventory_store.close()
