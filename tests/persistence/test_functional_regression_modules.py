"""后端模块双库回归。

在已有主路径之外，按模块覆盖工作流编辑、实验操作类别、物料、试剂、样品、
现配物、调度依赖、入口库位、设备状态、边缘执行结果和实验室档案。每个用例
都在 SQLite 与 PostgreSQL 上各执行一次。
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.persistence.test_functional_regression import (
    _domain_workflow_app,
    _inventory_app,
    _reagent_info,
    _template,
    _workflow_app,
)
from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.ingress import (
    IngressReservationError,
    StationIngressAuthority,
)
from unilabos.app.scheduler.inventory.layout import create_lab_router
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.station_resource import (
    SqliteStationResourceInventory,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.models import (
    WorkflowInventoryRequirementWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

_FIRST_NODE = "32000000-0000-4000-8000-0000000000b1"
_SECOND_NODE = "32000000-0000-4000-8000-0000000000b2"
_FIRST_JOB = "42000000-0000-4000-8000-0000000000b1"
_SECOND_JOB = "42000000-0000-4000-8000-0000000000b2"
_CHAIN_TASK = "22000000-0000-4000-8000-0000000000b1"
_CREATED_AT = "2026-10-09T00:00:00Z"


def _compute_node(name: str, **extra: object) -> dict:
    """返回不依赖设备模板的计算节点。"""

    body = {
        "name": name,
        "type": "compute",
        "pose": {"x": 10, "y": 20},
        "param": {"expression": "1 + 1"},
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "meta_data": {},
    }
    body.update(extra)
    return body


def _close_workflow(service: WorkflowService, store: WorkflowStore) -> None:
    service.close()
    store.close()


def _container(client: TestClient, barcode: str, name: str) -> str:
    """创建一个空容器并返回物料 UUID。"""

    template_uuid = _template(client, f"local.container.{barcode}")
    response = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "name": name,
            "barcode": barcode,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["uuid"]


def _lab_app(tmp_path: Path) -> tuple[TestClient, InventoryStore, InventoryService]:
    """装配实验室档案、仓储和入口共用的库存应用。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    service = InventoryService(store)
    app = FastAPI()
    app.include_router(create_lab_router(service))
    return TestClient(app), store, service


def test_w4_node_edit_duplicate_and_delete_advance_one_revision(
    database_mode, tmp_path: Path
) -> None:
    """W4：节点增删改和复制共享整图修订，删除后图为空。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        workflow_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "节点编辑", "tags": [], "meta_data": {}},
        ).json()["data"]["uuid"]
        created = client.post(
            f"/api/v1/workflows/{workflow_uuid}/nodes",
            json=_compute_node("计算 A"),
        )
        assert created.status_code == 201
        node = created.json()["data"]
        page = client.get(
            f"/api/v1/workflows/{workflow_uuid}/nodes",
            params={"page": 1, "page_size": 1},
        ).json()["data"]
        assert page["total"] == 1
        assert page["items"][0]["uuid"] == node["uuid"]
        patched = client.patch(
            f"/api/v1/workflow-nodes/{node['uuid']}",
            json={"name": "计算 A（已修改）", "description": "用于校验"},
        ).json()["data"]
        assert patched["name"] == "计算 A（已修改）"
        duplicate = client.post(
            f"/api/v1/workflow-nodes/{node['uuid']}/duplicate",
            json={},
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["data"]["uuid"] != node["uuid"]
        assert duplicate.json()["data"]["name"] == "计算 A（已修改） copy"
        remaining = client.post(
            f"/api/v1/workflows/{workflow_uuid}/batch-delete",
            json={"node_uuids": [duplicate.json()["data"]["uuid"]], "edge_uuids": []},
        ).json()["data"]
        assert [item["uuid"] for item in remaining["nodes"]] == [node["uuid"]]
        assert client.delete(f"/api/v1/workflow-nodes/{node['uuid']}").json()["code"] == 0
        assert client.get(f"/api/v1/workflows/{workflow_uuid}/graph").json()["data"][
            "nodes"
        ] == []
        assert store.get_workflow(workflow_uuid)["revision"] == 6
    finally:
        _close_workflow(service, store)


def test_w5_duplicated_workflow_is_independent(database_mode, tmp_path: Path) -> None:
    """W5：复制工作流重建节点、父子关系和物料需求，编辑副本不影响来源。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        source_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "来源流程", "tags": [], "meta_data": {}},
        ).json()["data"]["uuid"]
        parent = client.post(
            f"/api/v1/workflows/{source_uuid}/nodes",
            json=_compute_node("父节点"),
        ).json()["data"]
        child = client.post(
            f"/api/v1/workflows/{source_uuid}/nodes",
            json=_compute_node("子节点", parent_uuid=parent["uuid"]),
        ).json()["data"]
        source_graph = store.get_graph(source_uuid)
        store.save_graph(
            source_uuid,
            revision=3,
            nodes=[
                WorkflowNodeWrite.model_validate(node) for node in source_graph["nodes"]
            ],
            edges=[],
            inventory_requirements=[
                WorkflowInventoryRequirementWrite(
                    uuid="79000000-0000-4000-8000-0000000000b1",
                    consume_node_uuid=child["uuid"],
                    requirement_key="copied-liquid",
                    target_type="current_substance",
                    required_quantity=1,
                    quantity_unit="mL",
                    meta_data={"unilab": {"material_source_node_uuid": parent["uuid"]}},
                )
            ],
        )
        copied = client.post(
            f"/api/v1/workflows/{source_uuid}/duplicate",
            json={"name": "复制流程"},
        )
        assert copied.status_code == 201, copied.text
        body = copied.json()["data"]
        assert body["workflow"]["uuid"] != source_uuid
        copied_parent = next(node for node in body["nodes"] if node["name"] == "父节点")
        copied_child = next(node for node in body["nodes"] if node["name"] == "子节点")
        assert copied_child["parent_uuid"] == copied_parent["uuid"]
        requirement = body["inventory_requirements"][0]
        assert requirement["consume_node_uuid"] == copied_child["uuid"]
        assert requirement["meta_data"]["unilab"]["material_source_node_uuid"] == (
            copied_parent["uuid"]
        )
        client.patch(
            f"/api/v1/workflow-nodes/{copied_child['uuid']}",
            json={"name": "复制图内子节点"},
        )
        source_nodes = client.get(f"/api/v1/workflows/{source_uuid}/graph").json()[
            "data"
        ]["nodes"]
        assert next(node for node in source_nodes if node["uuid"] == child["uuid"])[
            "name"
        ] == "子节点"
    finally:
        _close_workflow(service, store)


def test_w6_unknown_batch_delete_keeps_the_graph(database_mode, tmp_path: Path) -> None:
    """W6：批量删除含未知节点时不改已有图。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        workflow_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "保留图", "tags": [], "meta_data": {}},
        ).json()["data"]["uuid"]
        node = client.post(
            f"/api/v1/workflows/{workflow_uuid}/nodes",
            json=_compute_node("保留节点"),
        ).json()["data"]
        rejected = client.post(
            f"/api/v1/workflows/{workflow_uuid}/batch-delete",
            json={
                "node_uuids": ["ffffffff-ffff-4fff-8fff-ffffffffffff"],
                "edge_uuids": [],
            },
        )
        assert rejected.json()["code"] == 1000
        graph = client.get(f"/api/v1/workflows/{workflow_uuid}/graph").json()["data"]
        assert [item["uuid"] for item in graph["nodes"]] == [node["uuid"]]
    finally:
        _close_workflow(service, store)


def test_w7_stale_graph_revision_does_not_replace_nodes(
    database_mode, tmp_path: Path
) -> None:
    """W7：过期修订保存图被拒绝，已有节点保持不变。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        workflow_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "修订冲突", "tags": [], "meta_data": {}},
        ).json()["data"]["uuid"]
        node = client.post(
            f"/api/v1/workflows/{workflow_uuid}/nodes",
            json=_compute_node("原节点"),
        ).json()["data"]
        rejected = client.put(
            f"/api/v1/workflows/{workflow_uuid}/graph",
            json={"revision": 99, "nodes": [], "edges": []},
        )
        assert rejected.json()["code"] == 3003
        graph = client.get(f"/api/v1/workflows/{workflow_uuid}/graph").json()["data"]
        assert [item["uuid"] for item in graph["nodes"]] == [node["uuid"]]
    finally:
        _close_workflow(service, store)


def test_w8_experiment_operation_type_cannot_become_normal(
    database_mode, tmp_path: Path
) -> None:
    """W8：省略类型的改名保持实验操作，显式改成普通工作流被拒绝。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        created = client.post(
            "/api/v1/workflows",
            json={
                "name": "可复用操作",
                "workflow_type": "experiment_operation",
                "tags": [],
                "meta_data": {},
            },
        ).json()["data"]
        preserved = client.put(
            f"/api/v1/workflows/{created['uuid']}",
            json={"name": "可复用操作（改名）", "tags": ["manual_operation"], "meta_data": {}},
        )
        changed = client.put(
            f"/api/v1/workflows/{created['uuid']}",
            json={"name": "普通流程", "workflow_type": "normal", "tags": [], "meta_data": {}},
        )
        assert preserved.json()["data"]["workflow_type"] == "experiment_operation"
        assert changed.json()["code"] != 0
        current = client.get(f"/api/v1/workflows/{created['uuid']}").json()["data"]
        assert current["name"] == "可复用操作（改名）"
        assert current["workflow_type"] == "experiment_operation"
    finally:
        _close_workflow(service, store)


def test_w9_deleted_workflow_is_hidden_but_historical_row_remains(
    database_mode, tmp_path: Path
) -> None:
    """W9：删除后列表和详情都看不到工作流，历史行仍保留。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        created = client.post(
            "/api/v1/workflows",
            json={"name": "待删除", "tags": [], "meta_data": {}},
        ).json()["data"]
        assert client.delete(f"/api/v1/workflows/{created['uuid']}").json()["code"] == 0
        assert client.get(f"/api/v1/workflows/{created['uuid']}").json()["code"] == 3002
        assert client.get("/api/v1/workflows").json()["data"]["items"] == []
        assert store.count_rows("workflow") == 0
        assert store.count_rows("workflow", include_deleted=True) == 1
    finally:
        _close_workflow(service, store)


def test_w10_name_filter_does_not_return_other_workflows(
    database_mode, tmp_path: Path
) -> None:
    """W10：名称筛选只返回匹配工作流。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        alpha = client.post(
            "/api/v1/workflows",
            json={"name": "阿尔法流程", "tags": [], "meta_data": {}},
        ).json()["data"]
        client.post(
            "/api/v1/workflows",
            json={"name": "贝塔流程", "tags": [], "meta_data": {}},
        )
        listed = client.get("/api/v1/workflows", params={"name": "阿尔法"}).json()[
            "data"
        ]["items"]
        assert [item["uuid"] for item in listed] == [alpha["uuid"]]
    finally:
        _close_workflow(service, store)


def test_w11_step_task_is_created_without_auto_dispatch(
    database_mode, tmp_path: Path
) -> None:
    """W11：单步任务持久化为暂停，不因为创建本身派发作业。"""

    client, service, store = _workflow_app(tmp_path)
    try:
        workflow_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "单步流程", "tags": [], "meta_data": {}},
        ).json()["data"]["uuid"]
        client.put(
            f"/api/v1/workflows/{workflow_uuid}/graph",
            json={
                "revision": 1,
                "nodes": [
                    {
                        "uuid": "81000000-0000-4000-8000-0000000000c1",
                        "name": "准备",
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
        created = client.post(
            "/api/v1/workflow-tasks",
            json={"workflow_uuid": workflow_uuid, "run_mode": "step", "input": {}},
        )
        assert created.status_code == 201, created.text
        task = created.json()["data"]
        assert task["run_mode"] == "step"
        assert task["status"] == "pending"
        listed = client.get("/api/v1/workflow-tasks").json()["data"]["items"]
        assert [item["uuid"] for item in listed] == [task["uuid"]]
        jobs = client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs").json()["data"]
        assert jobs
        assert all(job["status"] == "pending" for job in jobs)
    finally:
        _close_workflow(service, store)


def test_b2_category_name_must_be_unique_and_ordered(
    database_mode, tmp_path: Path
) -> None:
    """B2：类别名称不能重复或为空，列表按 sort_order 排序。"""

    client, service, store = _domain_workflow_app(tmp_path)
    try:
        later = client.post(
            "/api/v1/experiment-operation-categories",
            json={"name": "后显示", "sort_order": 80},
        )
        earlier = client.post(
            "/api/v1/experiment-operation-categories",
            json={"name": "先显示", "sort_order": 5},
        )
        duplicate = client.post(
            "/api/v1/experiment-operation-categories",
            json={"name": "先显示", "sort_order": 6},
        )
        blank = client.post(
            "/api/v1/experiment-operation-categories",
            json={"name": "  ", "sort_order": 7},
        )
        assert later.status_code == 201
        assert earlier.status_code == 201
        assert duplicate.json()["code"] == 3003
        assert blank.json()["code"] == 1000
        names = [
            item["name"]
            for item in client.get("/api/v1/experiment-operation-categories").json()[
                "data"
            ]["items"]
        ]
        assert names.index("先显示") < names.index("后显示")
    finally:
        _close_workflow(service, store)


def test_m3_soft_deleted_material_stays_in_the_table(
    database_mode, tmp_path: Path
) -> None:
    """M3：物料软删除后接口不可见，物理行和删除时间仍在。"""

    client, store = _inventory_app(tmp_path)
    try:
        material_uuid = _container(client, "SOFT-DELETE", "待删除物料")
        assert client.delete(f"/api/v1/materials/{material_uuid}").json()["code"] == 0
        assert client.get(f"/api/v1/materials/{material_uuid}").json()["code"] == 6000
        assert client.get("/api/v1/materials").json()["data"]["items"] == []
        deleted_at = store.query_one(
            "SELECT deleted_at FROM material WHERE uuid=?",
            (material_uuid,),
        )["deleted_at"]
        assert deleted_at
    finally:
        store.close()


def test_m4_explicit_null_preserves_parent_and_zero_uuid_clears_it(
    database_mode, tmp_path: Path
) -> None:
    """M4：显式 null 保持父物料，全零 UUID 才解除父子关系。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client, "local.parent_bottle")
        parent = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "PARENT",
                "name": "父物料",
            },
        ).json()["data"]
        child = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "parent_uuid": parent["uuid"],
                "barcode": "CHILD",
                "name": "子物料",
                "description": "原描述",
                "meta_data": {"source": "test"},
            },
        ).json()["data"]
        kept = client.put(
            f"/api/v1/materials/{child['uuid']}",
            json={
                "parent_uuid": None,
                "barcode": None,
                "name": None,
                "description": None,
                "meta_data": None,
            },
        ).json()["data"]
        assert kept["parent_uuid"] == parent["uuid"]
        assert kept["barcode"] == "CHILD"
        assert kept["description"] == "原描述"
        cleared = client.put(
            f"/api/v1/materials/{child['uuid']}",
            json={"parent_uuid": "00000000-0000-0000-0000-000000000000"},
        ).json()["data"]
        assert cleared["parent_uuid"] is None
    finally:
        store.close()


def test_m5_template_and_barcode_conflicts_do_not_change_the_material(
    database_mode, tmp_path: Path
) -> None:
    """M5：不能更换模板，重复条码不创建第二行。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client, "local.identity_bottle")
        created = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "UNIQUE-BARCODE",
                "name": "唯一物料",
            },
        ).json()["data"]
        template_change = client.put(
            f"/api/v1/materials/{created['uuid']}",
            json={"resource_template_uuid": str(uuid4())},
        )
        duplicate = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "UNIQUE-BARCODE",
                "name": "重复条码",
            },
        )
        assert template_change.json()["code"] == 6002
        assert duplicate.json()["code"] == 6010
        detail = client.get(f"/api/v1/materials/{created['uuid']}").json()["data"]
        assert detail["resource_template_uuid"] == template_uuid
        assert detail["revision"] == 1
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM material WHERE barcode=?",
            ("UNIQUE-BARCODE",),
        ) == {"count": 1}
    finally:
        store.close()


def test_m6_material_graph_reads_version_and_pose_from_the_material_row(
    database_mode, tmp_path: Path
) -> None:
    """M6：物料图的修订和位姿来自物料行，不依赖已删除的位置表。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client, "local.graph_bottle")
        pose = {
            "position_x": 10,
            "position_y": 20,
            "position_z": 30,
            "depth": 1,
            "length": 2,
            "width": 3,
            "scale_x": 1,
            "scale_y": 1,
            "scale_z": 1,
            "rotation_x": 0,
            "rotation_y": 0,
            "rotation_z": 90,
        }
        created = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "GRAPH",
                "name": "图物料",
                "relative_position": pose,
            },
        ).json()["data"]
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET aggregate_version=? WHERE uuid=?",
                (7, created["uuid"]),
            )
        node = client.get("/api/v1/materials/graph").json()["data"]["nodes"][0]
        assert node["material"]["revision"] == 7
        assert {
            field: node["relative_position"][field] for field in pose
        } == pose
        assert "relative_position" not in store.db.table_names()
    finally:
        store.close()


def test_m8_site_identity_and_material_state_round_trip(
    database_mode, tmp_path: Path
) -> None:
    """M8：库位身份独立于物料，观测状态写回物料当前数据。"""

    client, store = _inventory_app(tmp_path)
    try:
        template_uuid = _template(client, "local.site_owner")
        owner = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "OWNER",
                "name": "库位主人",
            },
        ).json()["data"]
        occupant = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "barcode": "OCCUPANT",
                "name": "占用物",
                "parent_uuid": owner["uuid"],
            },
        ).json()["data"]
        site_uuid = str(uuid4())
        with store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,'{}',?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    site_uuid,
                    "2026-08-04T00:00:00Z",
                    "2026-08-04T00:00:00Z",
                    owner["uuid"],
                    "A1",
                    0,
                    "[]",
                    occupant["uuid"],
                    0,
                    0,
                    0,
                    1,
                    1,
                    1,
                ),
            )
        sites = client.get(f"/api/v1/materials/{owner['uuid']}/sites").json()["data"]
        assert sites[0]["uuid"] == site_uuid
        assert sites[0]["occupied_material_uuid"] == occupant["uuid"]
        state = client.post(
            f"/api/v1/materials/{occupant['uuid']}/states",
            json={
                "status": "observed",
                "state_data": {"temperature": 25},
                "source": "test",
            },
        )
        assert state.json()["code"] == 0
        detail = client.get(f"/api/v1/materials/{occupant['uuid']}").json()["data"]
        assert detail["data"] == {"temperature": 25}
        latest = client.get(
            f"/api/v1/materials/{occupant['uuid']}/states/latest"
        ).json()["data"]
        assert latest["state_data"] == {"temperature": 25}
    finally:
        store.close()


def test_m7_reagent_blocks_material_delete_until_the_reagent_is_gone(
    database_mode, tmp_path: Path
) -> None:
    """M7：容器上还有试剂时不能删物料，试剂删除后物料可以删除。"""

    client, store = _inventory_app(tmp_path)
    try:
        material_uuid = _container(client, "GUARDED", "受保护瓶")
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
        blocked = client.delete(f"/api/v1/materials/{material_uuid}")
        assert blocked.json()["code"] == 2005
        assert client.delete(f"/api/v1/reagents/{reagent_uuid}").json()["code"] == 0
        assert client.delete(f"/api/v1/materials/{material_uuid}").json()["code"] == 0
    finally:
        store.close()


def test_r5_unchanged_reagent_edit_does_not_bump_revision(
    database_mode, tmp_path: Path
) -> None:
    """R5：数量和说明都没变时不增加修订；说明变化后旧修订失效。"""

    client, store = _inventory_app(tmp_path)
    try:
        material_uuid = _container(client, "UNCHANGED", "未变化瓶")
        _reagent_info(client)
        reagent = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": material_uuid,
                "cas": "64-17-5",
                "quantity": 500,
                "quantity_unit": "mL",
                "description": "密封保存",
                "meta_data": {"batch": "A"},
            },
        ).json()["data"]
        same = client.put(
            f"/api/v1/reagents/{reagent['uuid']}",
            json={
                "quantity": 500,
                "quantity_unit": "mL",
                "expected_revision": 1,
                "description": "密封保存",
                "meta_data": {"batch": "A"},
            },
        ).json()["data"]
        assert same["revision"] == 1
        described = client.put(
            f"/api/v1/reagents/{reagent['uuid']}",
            json={
                "quantity": 500,
                "quantity_unit": "mL",
                "expected_revision": 1,
                "description": "冷藏保存",
            },
        ).json()["data"]
        assert described["revision"] == 2
        stale = client.put(
            f"/api/v1/reagents/{reagent['uuid']}",
            json={
                "quantity": 500,
                "quantity_unit": "mL",
                "expected_revision": 1,
                "description": "再次修改",
            },
        )
        assert stale.json()["code"] == 4002
        assert store.query_one(
            "SELECT revision, description FROM container_content WHERE uuid=?",
            (reagent["uuid"],),
        )["description"] == "冷藏保存"
    finally:
        store.close()


def test_r6_second_active_content_and_non_positive_quantity_are_rejected(
    database_mode, tmp_path: Path
) -> None:
    """R6：同一容器不能有第二份活动试剂，数量必须为正。"""

    client, store = _inventory_app(tmp_path)
    try:
        material_uuid = _container(client, "ONE-CONTENT", "单内容瓶")
        info_uuid = _reagent_info(client)
        first = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": material_uuid,
                "reagent_info_uuid": info_uuid,
                "quantity": 10,
                "quantity_unit": "mL",
                "meta_data": {},
            },
        )
        second = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": material_uuid,
                "reagent_info_uuid": info_uuid,
                "quantity": 1,
                "quantity_unit": "mL",
                "meta_data": {},
            },
        )
        negative = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": material_uuid,
                "reagent_info_uuid": info_uuid,
                "quantity": 0,
                "quantity_unit": "mL",
                "meta_data": {},
            },
        )
        assert first.status_code == 201
        assert second.json()["code"] == 4002
        assert negative.json()["code"] == 1000
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM container_content WHERE material_uuid=? AND deleted_at IS NULL",
            (material_uuid,),
        ) == {"count": 1}
    finally:
        store.close()


def test_r7_deleted_reagent_leaves_history_and_drops_off_the_active_list(
    database_mode, tmp_path: Path
) -> None:
    """R7：删除试剂后活动列表为空，历史仍可按物料读取。"""

    client, store = _inventory_app(tmp_path)
    try:
        material_uuid = _container(client, "HISTORY", "历史瓶")
        _reagent_info(client)
        reagent_uuid = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": material_uuid,
                "cas": "64-17-5",
                "quantity": 8,
                "quantity_unit": "mL",
                "meta_data": {},
            },
        ).json()["data"]["uuid"]
        assert client.delete(f"/api/v1/reagents/{reagent_uuid}").json()["code"] == 0
        page = client.get("/api/v1/reagents?page=1&page_size=20").json()["data"]
        assert page["total"] == 0
        history = client.get(
            f"/api/v1/materials/{material_uuid}/reagent-history?page=1&page_size=20"
        ).json()["data"]
        assert history["items"]
        assert client.get(f"/api/v1/reagents/{reagent_uuid}").json()["code"] != 0
    finally:
        store.close()


def test_s1_sample_excludes_reagent_on_the_same_container(
    database_mode, tmp_path: Path
) -> None:
    """S1：样品可检索和修改，同一容器不能再放试剂。"""

    client, store = _inventory_app(tmp_path)
    try:
        material_uuid = _container(client, "SAMPLE-BOTTLE", "样品瓶")
        info_uuid = _reagent_info(client)
        created = client.post(
            "/api/v1/samples",
            json={
                "material_uuid": material_uuid,
                "code": "SAMPLE-0001",
                "name": "反应液样品",
                "quantity": 5,
                "quantity_unit": "mL",
                "meta_data": {"batch": "B-01"},
            },
        )
        assert created.status_code == 201
        sample = created.json()["data"]
        page = client.get(
            "/api/v1/samples?page=1&page_size=20&keyword=0001&barcode=SAMPLE-BOTTLE"
        ).json()["data"]
        assert page["total"] == 1
        conflict = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": material_uuid,
                "reagent_info_uuid": info_uuid,
                "quantity": 5,
                "quantity_unit": "mL",
            },
        )
        assert conflict.json()["code"] == 4002
        updated = client.put(
            f"/api/v1/samples/{sample['uuid']}",
            json={
                "material_uuid": material_uuid,
                "code": "SAMPLE-0001",
                "name": "反应液样品（复核）",
                "quantity": 4,
                "quantity_unit": "mL",
                "meta_data": {},
            },
        ).json()["data"]
        assert updated["quantity"] == 4
        assert store.query_one(
            "SELECT kind, quantity FROM container_content WHERE uuid=?",
            (sample["uuid"],),
        )["kind"] == "sample"
        assert "sample" not in store.db.table_names()
        assert client.delete(f"/api/v1/samples/{sample['uuid']}").json()["code"] == 0
    finally:
        store.close()


def test_s2_current_substance_freezes_components_and_records_ledger(
    database_mode, tmp_path: Path
) -> None:
    """S2：现配物冻结来源试剂名称，修订冲突不提交，样品不能占用同一容器。"""

    client, store = _inventory_app(tmp_path)
    try:
        source_uuid = _container(client, "SOURCE", "来源瓶")
        target_uuid = _container(client, "MIXTURE", "混合瓶")
        info_uuid = _reagent_info(client)
        source_reagent = client.post(
            "/api/v1/reagents",
            json={
                "material_uuid": source_uuid,
                "reagent_info_uuid": info_uuid,
                "quantity": 200,
                "quantity_unit": "mL",
                "concentration_value": 95,
                "concentration_unit": "%",
            },
        ).json()["data"]
        create_body = {
            "material_uuid": target_uuid,
            "name": "乙醇水溶液",
            "components": [
                {
                    "reagent_uuid": source_reagent["uuid"],
                    "quantity": 15,
                    "quantity_unit": "mL",
                }
            ],
            "quantity": 100,
            "quantity_unit": "mL",
            "physical_state": "liquid",
            "meta_data": {},
        }
        created = client.post("/api/v1/current-substances", json=create_body)
        assert created.status_code == 201, created.text
        substance = created.json()["data"]
        client.put(
            f"/api/v1/reagent-infos/{info_uuid}",
            json={"name": "乙醇（身份已更名）"},
        )
        frozen = client.get(f"/api/v1/current-substances/{substance['uuid']}").json()[
            "data"
        ]
        assert frozen["components"][0]["name"] == "乙醇"
        assert client.post(
            "/api/v1/samples",
            json={
                "material_uuid": target_uuid,
                "code": "MIXTURE-CONFLICT",
                "name": "不应创建",
                "quantity": 1,
                "quantity_unit": "mL",
            },
        ).json()["code"] == 4002
        assert client.put(
            f"/api/v1/current-substances/{substance['uuid']}",
            json={**create_body, "quantity": 90, "expected_revision": 9},
        ).json()["code"] == 4002
        updated = client.put(
            f"/api/v1/current-substances/{substance['uuid']}",
            json={**create_body, "quantity": 90, "expected_revision": 1},
        ).json()["data"]
        assert updated["quantity"] == 90
        assert updated["revision"] == 2
        assert store.query_one(
            "SELECT kind FROM container_content WHERE uuid=? AND deleted_at IS NULL",
            (substance["uuid"],),
        ) == {"kind": "substance"}
        ledger = store.query_all(
            "SELECT op_type FROM inventory_ledger WHERE subject_type='current_substance' ORDER BY ledger_id"
        )
        assert [row["op_type"] for row in ledger] == [
            "current_substance.add",
            "current_substance.adjust",
        ]
    finally:
        store.close()


def test_s3_missing_substance_component_creates_nothing(
    database_mode, tmp_path: Path
) -> None:
    """S3：配方引用不存在的试剂时不创建现配物。"""

    client, store = _inventory_app(tmp_path)
    try:
        target_uuid = _container(client, "INVALID-MIXTURE", "非法混合")
        missing = client.post(
            "/api/v1/current-substances",
            json={
                "material_uuid": target_uuid,
                "quantity": 10,
                "quantity_unit": "mL",
                "physical_state": "liquid",
                "meta_data": {},
                "components": [
                    {
                        "reagent_uuid": "00000000-0000-4000-8000-000000000001",
                        "quantity": 1,
                        "quantity_unit": "mL",
                    }
                ],
            },
        )
        assert missing.json()["code"] == 1000
        assert store.query_one(
            "SELECT COUNT(*) AS count FROM container_content WHERE material_uuid=?",
            (target_uuid,),
        ) == {"count": 0}
    finally:
        store.close()


def _seed_chain(store: WorkflowStore, workflow_uuid: str) -> None:
    """写入前后依赖的两个设备作业。"""

    execution_plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": _FIRST_NODE,
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
                "material_requirements": [],
            },
            {
                "uuid": _SECOND_NODE,
                "kind": "device_action",
                "device_id": "reactor-b",
                "action_name": "finish",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
                "material_requirements": [],
            },
        ],
        "handles": [],
        "edges": [
            {
                "uuid": "71000000-0000-4000-8000-0000000000b1",
                "source_node_uuid": _FIRST_NODE,
                "target_node_uuid": _SECOND_NODE,
                "dependency_only": True,
            }
        ],
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
                _CHAIN_TASK,
                _CREATED_AT,
                _CREATED_AT,
                workflow_uuid,
                json.dumps(execution_plan),
            ),
        )
        for job_uuid, node_uuid, index in (
            (_FIRST_JOB, _FIRST_NODE, 0),
            (_SECOND_JOB, _SECOND_NODE, 1),
        ):
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status, attempt,
                    param, feedback_data, return_info, control_data, error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?,
                          'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                          '{}', '{}', '[]')
                """,
                (job_uuid, _CREATED_AT, _CREATED_AT, _CHAIN_TASK, node_uuid, index),
            )


def test_e7_successor_waits_and_repeat_submit_does_not_dispatch_again(
    database_mode, tmp_path: Path
) -> None:
    """E7：有依赖的后继作业不派发，同一桥重复提交不产生第二条命令。"""

    client, service, store = _workflow_app(tmp_path)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = importlib.import_module(
        "unilabos.workflow.task_scheduler_bridge"
    ).TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        workflow_uuid = client.post(
            "/api/v1/workflows",
            json={"name": "依赖流程", "tags": [], "meta_data": {}},
        ).json()["data"]["uuid"]
        _seed_chain(store, workflow_uuid)
        task = store.get_task(_CHAIN_TASK)
        first = bridge.submit(task)
        second = bridge.submit(task)
        assert first["task"]["status"] == "running"
        assert second["task"]["uuid"] == first["task"]["uuid"]
        assert [item["job_id"] for item in dispatcher.dispatched] == [_FIRST_JOB]
        assert store.get_job(_SECOND_JOB)["status"] == "pending"
    finally:
        bridge.close()
        _close_workflow(service, store)


def _ingress(tmp_path: Path):
    """建立两个有序入口库位和两个载体。"""

    store = InventoryStore(str(tmp_path / "ingress.db"))
    backend = BackendResourceService(store)
    templates = backend.sync_resource_templates(
        [
            {
                "id": "test.ingress-rack",
                "display_name": "入口架",
                "registry_type": "resource",
                "class": {},
            },
            {
                "id": "test.carrier",
                "display_name": "可搬运载体",
                "registry_type": "material",
                "class": {},
            },
        ]
    )["templates"]
    by_name = {row["name"]: row["uuid"] for row in templates}
    rack = backend.create_material(
        {
            "resource_template_uuid": by_name["test.ingress-rack"],
            "barcode": "INGRESS-RACK",
            "name": "工站入口",
        }
    )
    carriers = [
        backend.create_material(
            {
                "resource_template_uuid": by_name["test.carrier"],
                "barcode": f"CARRIER-{index}",
                "name": f"载体 {index}",
            }
        )
        for index in (1, 2)
    ]
    site_slow = str(uuid4())
    site_first = str(uuid4())
    timestamp = "2026-08-30T00:00:00+00:00"
    with store.transaction() as connection:
        for site_uuid, name, order in (
            (site_slow, "IN-B", 20),
            (site_first, "IN-A", 10),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,'{}',?,?,?,?,NULL,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    timestamp,
                    timestamp,
                    rack["uuid"],
                    name,
                    order,
                    json.dumps([by_name["test.carrier"]]),
                ),
            )
    authority = StationIngressAuthority(
        store,
        edge_id="edge-regression",
        lab_id="lab-regression",
        now=lambda: datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    return store, authority, {
        "rack": rack["uuid"],
        "carriers": [row["uuid"] for row in carriers],
        "sites": (site_slow, site_first),
    }


def test_i1_ingress_orders_sites_and_replays_the_same_reservation(
    database_mode, tmp_path: Path
) -> None:
    """I1：入口按库位顺序预留，同一命令重放不占第二个库位。"""

    store, authority, identities = _ingress(tmp_path)
    try:
        first = authority.reserve(
            idempotency_key="reserve-1",
            carrier_material_uuid=identities["carriers"][0],
            candidate_site_uuids=identities["sites"],
            ttl_seconds=30,
        )
        replay = authority.reserve(
            idempotency_key="reserve-1",
            carrier_material_uuid=identities["carriers"][0],
            candidate_site_uuids=identities["sites"],
            ttl_seconds=30,
        )
        second = authority.reserve(
            idempotency_key="reserve-2",
            carrier_material_uuid=identities["carriers"][1],
            candidate_site_uuids=identities["sites"],
            ttl_seconds=30,
        )
        with pytest.raises(IngressReservationError):
            authority.reserve(
                idempotency_key="reserve-3",
                carrier_material_uuid=identities["carriers"][0],
                candidate_site_uuids=identities["sites"],
                ttl_seconds=30,
            )
        assert first["site_uuid"] == identities["sites"][1]
        assert replay["uuid"] == first["uuid"]
        assert second["site_uuid"] == identities["sites"][0]
        active = SqliteStationResourceInventory(
            store, settle_material_transfer=lambda _command: {}
        )._active_ingress_site_uuids(list(identities["sites"]))
        assert active == set(identities["sites"])
        assert "station_ingress_reservation_site" not in store.db.table_names()
    finally:
        store.close()


def test_i2_receive_occupies_site_and_releases_the_ingress_hold(
    database_mode, tmp_path: Path
) -> None:
    """I2：接收后载体进入入口架，库位不再被活动入口预留占用。"""

    store, authority, identities = _ingress(tmp_path)
    try:
        reserved = authority.reserve(
            idempotency_key="receive-1",
            carrier_material_uuid=identities["carriers"][0],
            candidate_site_uuids=identities["sites"],
            ttl_seconds=30,
        )
        authority.mark_in_transit(reserved["uuid"])
        received = authority.receive(reserved["uuid"])
        site = store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?",
            (received["site_uuid"],),
        )
        carrier = store.query_one(
            "SELECT parent_uuid FROM material WHERE uuid=?",
            (received["carrier_material_uuid"],),
        )
        assert received["state"] == "received"
        assert site == {"occupied_material_uuid": identities["carriers"][0]}
        assert carrier == {"parent_uuid": identities["rack"]}
        assert SqliteStationResourceInventory(
            store, settle_material_transfer=lambda _command: {}
        )._active_ingress_site_uuids([received["site_uuid"]]) == set()
    finally:
        store.close()


def test_p1_device_property_history_is_the_only_current_value(
    database_mode, tmp_path: Path
) -> None:
    """P1：属性变化才追加历史，相同值不推进时间，设备之间互不覆盖。"""

    store = DeviceStateStore(str(tmp_path / "device.db"))
    try:
        assert store.set("reactor-a", "temperature", 25, now_ms=1000) is True
        assert store.set("reactor-a", "temperature", 25, now_ms=2000) is False
        assert store.set("reactor-a", "temperature", 30, now_ms=3000) is True
        assert store.set("reactor-b", "temperature", 18, now_ms=4000) is True
        with pytest.raises(ValueError):
            store.set("", "temperature", 1)
        latest = store.latest_for("reactor-a")
        assert latest["temperature"]["value"] == 30
        assert latest["temperature"]["updated_at"] == 3000
        assert store.latest_for("reactor-b")["temperature"]["value"] == 18
        assert store.db.table_names().isdisjoint({"device_property_latest"})
        assert "device_property_history" in store.db.table_names()
    finally:
        store.close()


def test_g1_edge_outcome_is_stored_on_the_runtime_row(
    database_mode, tmp_path: Path
) -> None:
    """G1：第一次执行结果生效，重复回调被忽略，未知命令留在运行行上。"""

    store = EdgeControlStore(str(tmp_path / "edge.db"))
    job_uuid = str(uuid4())
    try:
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": str(uuid4()),
                "node_uuid": str(uuid4()),
                "job_access_token": "temporary-secret",
                "claim_uuid": str(uuid4()),
                "attempt": 1,
                "fences": [{"lock_key": "/devices/reactor-a", "fencing_token": 1}],
            },
            str(uuid4()),
        )
        assert store.save_pending_outcome(
            job_uuid, "failed", {"ok": False}, [{"code": "E"}], ["cmd-9"]
        ) is True
        assert store.save_pending_outcome(
            job_uuid, "succeeded", {}, [], []
        ) is False
        pending = store.get_pending_outcome(job_uuid)
        assert pending is not None
        assert pending.outcome == "failed"
        assert pending.unknown_command_ids == ["cmd-9"]
        assert "edge_job_outcome_pending" not in store.db.table_names()
        store.abort_restarted_job(job_uuid)
        assert store.get_pending_outcome(job_uuid) is None
        runtime = store.db.query_one(
            "SELECT status, outcome FROM edge_job_runtime WHERE job_uuid=?",
            (job_uuid,),
        )
        assert runtime["status"] == "outcome_retired"
        assert runtime["outcome"] is None
    finally:
        store.close()


def test_l1_lab_profile_has_no_zone_routes(database_mode, tmp_path: Path) -> None:
    """L1：实验室领域可修改，分区接口不存在，布局不再返回手工分区。"""

    client, store, _service = _lab_app(tmp_path)
    try:
        assert client.get("/api/v1/lab/profile").json()["domain"] == "general"
        updated = client.put("/api/v1/lab/profile", json={"domain": "bio"})
        assert updated.json()["pack"]["name"] == "生物实验室"
        assert client.get("/api/v1/lab/profile").json()["domain"] == "bio"
        layout = client.get("/api/v1/lab/layout").json()
        assert layout["zones"] == []
        assert client.post("/api/v1/lab/zones", json={"zone_id": "z"}).status_code == 404
        assert client.get("/api/v1/lab/assembly/missing").status_code == 404
        assert "lab_zone" not in store.db.table_names()
        assert "lab_placement" not in store.db.table_names()
    finally:
        store.close()


def test_l2_lab_meta_keeps_only_the_whitelist_on_one_row(
    database_mode, tmp_path: Path
) -> None:
    """L2：实验室元数据只有一行，未知键不能写入，指纹键可以读回。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        store.set_meta("lab_name", "回归实验室")
        store.set_meta("resource_graph_bootstrap_source", "graph.json")
        store.set_meta("resource_graph_bootstrap_fingerprint", "fp-1")
        store.set_meta("resource_graph_bootstrap_fingerprint_version", "3")
        with pytest.raises(ValueError):
            store.set_meta("unrelated_note", "丢弃")
        assert store.get_meta("lab_name") == "回归实验室"
        assert store.get_meta("resource_graph_bootstrap_fingerprint") == "fp-1"
        assert store.get_meta("resource_graph_bootstrap_fingerprint_version") == "3"
        assert store.get_meta("missing_key", "默认") == "默认"
        assert store.query_one("SELECT COUNT(*) AS count FROM lab_meta") == {"count": 1}
    finally:
        store.close()


def test_l3_demo_seed_warehouse_and_assembly_are_idempotent(
    database_mode, tmp_path: Path
) -> None:
    """L3：演示种子可重复执行，仓储和组装仍可读，分区表不存在。"""

    client, store, service = _lab_app(tmp_path)
    try:
        first = client.post("/api/v1/lab/demo")
        second = client.post("/api/v1/lab/demo")
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["seeded"]["instances"] == 0
        warehouse = client.get("/api/v1/lab/warehouse").json()
        assert any(item["template_id"] == "reagent-naoh" for item in warehouse["categories"])
        assembly = client.get("/api/v1/lab/assembly/demo-deck-a").json()
        assert assembly["root"]["edge_uuid"] == "demo-deck-a"
        layout = client.get("/api/v1/lab/layout").json()
        assert layout["zones"] == []
        assert "zone-storage" in layout["storage_summary"]
        assert "lab_zone" not in store.db.table_names()
        assert service.store is store
    finally:
        store.close()
