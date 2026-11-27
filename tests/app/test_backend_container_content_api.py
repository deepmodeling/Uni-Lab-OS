"""OS 本地模式的 Backend 同形样品与当前内容物公共接口测试。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import SCHEMA_VERSION, InventoryStore
from unilabos.config.config import BasicConfig


def _client(tmp_path) -> tuple[TestClient, InventoryStore]:
    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    return TestClient(app), store


def _container(client: TestClient, suffix: str) -> str:
    template_response = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": "local.container",
                    "display_name": "本地容器",
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
    template = template_response.json()["data"]["templates"][0]
    response = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template["uuid"],
            "name": f"容器-{suffix}",
            "barcode": f"LOCAL-CONTAINER-{suffix}",
        },
    )
    assert response.status_code == 201
    return response.json()["data"]["uuid"]


def _reagent_info(client: TestClient) -> str:
    response = client.post(
        "/api/v1/reagent-infos",
        json={
            "cas": "64-17-5",
            "name": "乙醇",
            "physical_state": "liquid",
        },
    )
    assert response.status_code == 201
    return response.json()["data"]["uuid"]


def test_v9_database_adds_sample_and_current_substance_tables(tmp_path) -> None:
    """既有试剂版数据库重开后幂等升级到容器内容 v10 与模板引用 v11。"""

    if BasicConfig.database_type == "postgresql":
        pytest.skip("该测试直接构造 SQLite 旧文件并检查 PRAGMA 版本")

    database = tmp_path / "inventory.db"
    store = InventoryStore(str(database))
    with store.transaction() as connection:
        connection.execute("DROP VIEW IF EXISTS current_substance")
        connection.execute("DROP VIEW IF EXISTS sample")
        connection.execute("DROP TABLE IF EXISTS container_content")
        connection.execute("PRAGMA user_version = 9")
    store.close()

    reopened = InventoryStore(str(database))
    tables = {
        row["name"]
        for row in reopened.query_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert reopened.query_one("PRAGMA user_version") == {
        "user_version": SCHEMA_VERSION
    }
    assert "container_content" in tables
    views = {
        row["name"]
        for row in reopened.query_all(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )
    }
    assert {"sample", "current_substance"} <= views
    reopened.close()


def test_sample_crud_search_and_container_content_exclusion(tmp_path) -> None:
    """样品可独立 CRUD，并与同一容器中的试剂、当前内容物互斥。"""

    client, store = _client(tmp_path)
    material_uuid = _container(client, "SAMPLE")
    info_uuid = _reagent_info(client)

    created_response = client.post(
        "/api/v1/samples",
        json={
            "material_uuid": material_uuid,
            "code": "SAMPLE-0001",
            "name": "反应液样品",
            "quantity": 5,
            "quantity_unit": "mL",
            "description": "反应结束后留样",
            "meta_data": {"batch": "B-01"},
        },
    )
    created = created_response.json()["data"]
    sample_uuid = created["uuid"]

    assert created_response.status_code == 201
    assert created["material_uuid"] == material_uuid
    assert created["code"] == "SAMPLE-0001"
    assert created["meta_data"] == {"batch": "B-01"}
    assert client.get(f"/api/v1/samples/{sample_uuid}").json()["data"] == created

    page = client.get(
        "/api/v1/samples?page=1&page_size=20&keyword=0001&barcode=CONTAINER-SAMPLE"
    ).json()["data"]
    assert page["total"] == 1
    assert page["items"][0]["uuid"] == sample_uuid

    conflict = client.post(
        "/api/v1/reagents",
        json={
            "material_uuid": material_uuid,
            "reagent_info_uuid": info_uuid,
            "quantity": 5,
            "quantity_unit": "mL",
        },
    ).json()
    assert conflict["code"] == 4002

    updated = client.put(
        f"/api/v1/samples/{sample_uuid}",
        json={
            "material_uuid": material_uuid,
            "code": "SAMPLE-0001",
            "name": "反应液样品（复核）",
            "quantity": 4,
            "quantity_unit": "mL",
            "meta_data": {},
        },
    ).json()["data"]
    assert updated["name"] == "反应液样品（复核）"
    assert updated["quantity"] == 4

    # Backend 当前合同中 Sample 只有 CRUD，不进入数量台账与同步发件箱。
    assert (
        store.query_one(
            "SELECT 1 AS found FROM inventory_ledger WHERE subject_type='sample'"
        )
        is None
    )
    assert (
        store.query_one(
            "SELECT 1 AS found FROM sync_outbox WHERE aggregate_type='sample'"
        )
        is None
    )

    assert client.delete(f"/api/v1/samples/{sample_uuid}").json() == {"code": 0}
    store.close()


def test_current_substance_snapshot_revision_history_and_outbox(tmp_path) -> None:
    """当前内容物冻结来源试剂快照，并以统一台账和发件箱记录数量变化。"""

    client, store = _client(tmp_path)
    source_material_uuid = _container(client, "SOURCE")
    target_material_uuid = _container(client, "MIXTURE")
    info_uuid = _reagent_info(client)
    source_reagent = client.post(
        "/api/v1/reagents",
        json={
            "material_uuid": source_material_uuid,
            "reagent_info_uuid": info_uuid,
            "quantity": 200,
            "quantity_unit": "mL",
            "concentration_value": 95,
            "concentration_unit": "%",
        },
    ).json()["data"]

    create_body = {
        "material_uuid": target_material_uuid,
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
        "meta_data": {"formula": "v1"},
    }
    created_response = client.post("/api/v1/current-substances", json=create_body)
    created = created_response.json()["data"]
    substance_uuid = created["uuid"]

    assert created_response.status_code == 201
    assert created["revision"] == 1
    assert created["components"] == [
        {
            "reagent_uuid": source_reagent["uuid"],
            "reagent_info_uuid": info_uuid,
            "name": "乙醇",
            "quantity": 15,
            "quantity_unit": "mL",
            "sort_order": 0,
        }
    ]
    assert created["composition"][0]["concentration_value"] == 95
    client.put(
        f"/api/v1/reagent-infos/{info_uuid}",
        json={"name": "乙醇（身份已更名）"},
    )
    frozen = client.get(f"/api/v1/current-substances/{substance_uuid}").json()["data"]
    assert frozen["components"][0]["name"] == "乙醇"
    by_material = client.get(
        f"/api/v1/materials/{target_material_uuid}/current-substance"
    ).json()["data"]
    assert by_material["uuid"] == substance_uuid
    sample_conflict = client.post(
        "/api/v1/samples",
        json={
            "material_uuid": target_material_uuid,
            "code": "MIXTURE-CONFLICT",
            "name": "不应创建的样品",
            "quantity": 1,
            "quantity_unit": "mL",
        },
    ).json()
    assert sample_conflict["code"] == 4002

    stale = client.put(
        f"/api/v1/current-substances/{substance_uuid}",
        json={**create_body, "quantity": 90, "expected_revision": 9},
    ).json()
    assert stale["code"] == 4002

    updated = client.put(
        f"/api/v1/current-substances/{substance_uuid}",
        json={**create_body, "quantity": 90, "expected_revision": 1},
    ).json()["data"]
    assert updated["quantity"] == 90
    assert updated["revision"] == 2

    history = client.get(
        f"/api/v1/materials/{target_material_uuid}/substance-history"
        "?page=1&page_size=20"
    ).json()["data"]
    assert [item["revision"] for item in history["items"]] == [2, 1]
    assert [item["quantity_delta"] for item in history["items"]] == [-10, 100]
    assert (
        client.get(f"/api/v1/substance-history/{history['items'][0]['uuid']}").json()[
            "data"
        ]
        == history["items"][0]
    )

    ledger = store.query_all(
        "SELECT * FROM inventory_ledger "
        "WHERE subject_type='current_substance' ORDER BY ledger_id"
    )
    outbox = store.query_all(
        "SELECT * FROM sync_outbox "
        "WHERE aggregate_type='current_substance' ORDER BY sequence"
    )
    assert [row["op_type"] for row in ledger] == [
        "current_substance.add",
        "current_substance.adjust",
    ]
    assert [row["quantity_delta"] for row in ledger] == [100, -10]
    assert [row["entry_uuid"] for row in ledger] == [row["event_id"] for row in outbox]

    assert client.delete(f"/api/v1/current-substances/{substance_uuid}").json() == {
        "code": 0
    }
    history_after_delete = client.get(
        f"/api/v1/materials/{target_material_uuid}/substance-history"
    ).json()["data"]
    assert len(history_after_delete["items"]) == 2
    assert client.delete(f"/api/v1/materials/{target_material_uuid}").json() == {
        "code": 0
    }
    store.close()


def test_current_substance_rejects_duplicate_or_missing_component(tmp_path) -> None:
    """配方成分必须唯一且引用仍然存在的试剂实例。"""

    client, store = _client(tmp_path)
    target_material_uuid = _container(client, "INVALID-MIXTURE")
    missing_uuid = "00000000-0000-4000-8000-000000000001"
    component = {
        "reagent_uuid": missing_uuid,
        "quantity": 1,
        "quantity_unit": "mL",
    }
    base = {
        "material_uuid": target_material_uuid,
        "quantity": 10,
        "quantity_unit": "mL",
        "physical_state": "liquid",
        "meta_data": {},
    }

    duplicate = client.post(
        "/api/v1/current-substances",
        json={**base, "components": [component, component]},
    ).json()
    missing = client.post(
        "/api/v1/current-substances",
        json={**base, "components": [component]},
    ).json()

    assert duplicate["code"] == 1000
    assert missing["code"] == 1000
    store.close()
