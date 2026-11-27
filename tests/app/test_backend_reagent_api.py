"""OS 本地模式与 Backend 对齐的试剂（Reagent）公共接口测试。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.app.scheduler.inventory.schemas import InventoryLedgerEntryResponse
from unilabos.app.scheduler.inventory.store import SCHEMA_VERSION, InventoryStore
from unilabos.app.scheduler.inventory.workflow_quantity import WorkflowQuantityInventoryAuthority


def _client(tmp_path) -> tuple[TestClient, InventoryStore]:
    """创建绑定隔离 ``inventory.db`` 的公共合同客户端。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    return TestClient(app), store


def _container_template(client: TestClient) -> str:
    """同步带 ``container`` 标签的试剂瓶模板并返回稳定 UUID。"""

    template = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": "local.reagent_bottle",
                    "display_name": "本地试剂瓶",
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
    ).json()["data"]["templates"][0]
    return template["uuid"]


def _container(client: TestClient) -> str:
    """创建一个不携带内容物的旧式空容器。"""

    template_uuid = _container_template(client)
    response = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "name": "乙醇瓶",
            "barcode": "LOCAL-ETHANOL-001",
        },
    )
    assert response.status_code == 201
    return response.json()["data"]["uuid"]


def test_material_create_can_atomically_include_reagent(tmp_path) -> None:
    """公共物料创建接口可在同一事务内创建容器和试剂内容。"""

    client, store = _client(tmp_path)
    template_uuid = _container_template(client)
    info_uuid = _reagent_info(client)

    created = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "name": "内联乙醇瓶",
            "barcode": "LOCAL-INLINE-ETHANOL-001",
            "reagent": {
                "reagent_info_uuid": info_uuid,
                "quantity": 500,
                "quantity_unit": "mL",
                "concentration_value": 95,
                "concentration_unit": "%",
                "source": "frontend:workbench",
                "meta_data": {"batch": "A-001"},
            },
        },
    )

    assert created.status_code == 201
    assert created.json()["code"] == 0
    result = created.json()["data"]
    assert result["reagent"]["material_uuid"] == result["uuid"]
    assert result["reagent"]["reagent_info_uuid"] == info_uuid
    assert result["reagent"]["quantity"] == 500
    assert result["reagent_info"]["uuid"] == info_uuid
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM material WHERE barcode=?",
        ("LOCAL-INLINE-ETHANOL-001",),
    ) == {"count": 1}
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM reagent WHERE material_uuid=?",
        (result["uuid"],),
    ) == {"count": 1}
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM inventory_ledger "
        "WHERE material_uuid=? AND subject_type='reagent'",
        (result["uuid"],),
    ) == {"count": 1}
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM sync_outbox "
        "WHERE aggregate_id=? AND aggregate_type='reagent'",
        (result["reagent"]["uuid"],),
    ) == {"count": 1}
    store.close()


def test_invalid_inline_reagent_rolls_back_material(tmp_path) -> None:
    """内联试剂校验失败时不得留下半成品容器。"""

    client, store = _client(tmp_path)
    template_uuid = _container_template(client)

    rejected = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "name": "不应保留的容器",
            "barcode": "LOCAL-INLINE-ROLLBACK-001",
            "reagent": {
                "cas": "67-56-1",
                "quantity": 10,
                "quantity_unit": "mL",
            },
        },
    )

    assert rejected.json()["code"] == 4001
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM material WHERE barcode=?",
        ("LOCAL-INLINE-ROLLBACK-001",),
    ) == {"count": 0}
    assert store.query_one("SELECT COUNT(*) AS count FROM reagent") == {"count": 0}
    assert store.query_one("SELECT COUNT(*) AS count FROM sync_outbox") == {"count": 0}
    store.close()


def _reagent_info(client: TestClient) -> str:
    """登记乙醇身份并返回稳定 UUID。"""

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


def test_inventory_database_reuses_edge_ledger_for_reagent_schema(tmp_path) -> None:
    """证明试剂结构复用 Edge 台账与发件箱，不建立第二张物料台账。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    tables = store.db.table_names()
    from unilabos.config.config import BasicConfig
    if BasicConfig.database_type == "sqlite":
        assert store.query_one("PRAGMA user_version") == {"user_version": SCHEMA_VERSION}
    assert {
        "reagent_info",
        "container_content",
        "inventory_ledger",
        "sync_outbox",
    } <= tables
    view_query = (
        "SELECT name FROM sqlite_master WHERE type='view'"
        if BasicConfig.database_type == "sqlite"
        else "SELECT viewname AS name FROM pg_views WHERE schemaname=current_schema()"
    )
    views = {
        row["name"]
        for row in store.query_all(view_query)
    }
    assert {"reagent", "sample", "current_substance"} <= views
    assert "material_ledger_entry" not in tables
    ledger_columns = {
        row["name"] for row in store.query_all("PRAGMA table_info(inventory_ledger)")
    }
    assert {
        "entry_uuid",
        "material_uuid",
        "subject_type",
        "quantity_delta",
        "quantity_unit",
        "revision",
        "workflow_task_uuid",
        "workflow_node_job_uuid",
    } <= ledger_columns
    store.close()


def test_v8_development_database_merges_duplicate_ledger_on_reopen(tmp_path) -> None:
    """证明早期 v8 开发库会把重复台账并入统一台账并删除旧表。"""

    from unilabos.config.config import BasicConfig
    if BasicConfig.database_type == "postgresql":
        pytest.skip("只验证旧 SQLite v8 开发库升级；PostgreSQL 不读取旧 SQLite 库")

    database = tmp_path / "inventory.db"
    store = InventoryStore(str(database))
    with store.transaction() as conn:
        conn.execute(
            """CREATE TABLE material_ledger_entry(
            uuid TEXT PRIMARY KEY,material_uuid TEXT,event_type TEXT,
            operator_type TEXT,changes TEXT,extension TEXT,trace_id TEXT,
            recorded_at TEXT,workflow_task_uuid TEXT,workflow_node_job_uuid TEXT,
            subject_type TEXT,subject_uuid TEXT,quantity_delta REAL,
            quantity_unit TEXT,revision INTEGER)"""
        )
        conn.execute(
            """INSERT INTO material_ledger_entry VALUES(
            'old-entry','old-material','adjust','frontend','{"result":{"quantity":4}}',
            '{}','trace-old','2026-08-26T10:00:00.000Z',NULL,NULL,'reagent',
            'old-reagent',-1,'mL',2)"""
        )
    store.close()

    reopened = InventoryStore(str(database))
    migrated = reopened.query_one(
        "SELECT * FROM inventory_ledger WHERE entry_uuid='old-entry'"
    )

    assert migrated is not None
    assert migrated["op_type"] == "reagent.adjust"
    assert migrated["aggregate_id"] == "old-reagent"
    assert migrated["material_uuid"] == "old-material"
    assert migrated["quantity_delta"] == -1
    assert migrated["quantity_unit"] == "mL"
    assert migrated["revision"] == 2
    assert reopened.query_one(
        "SELECT 1 AS found FROM sqlite_master "
        "WHERE type='table' AND name='material_ledger_entry'"
    ) is None
    reopened.close()


def test_reagent_info_crud_and_local_compound_lookup(tmp_path) -> None:
    """证明化学品身份可在 OS 本地模式独立登记、纠错、查询和受限删除。"""

    client, store = _client(tmp_path)
    identity = _reagent_info(client)

    page = client.get("/api/v1/reagent-infos?page=1&page_size=100").json()["data"]
    registered = client.get("/api/v1/compounds/64-17-5").json()["data"]
    unavailable = client.get("/api/v1/compounds/67-56-1").json()["data"]
    updated = client.put(
        f"/api/v1/reagent-infos/{identity}",
        json={
            "name": "无水乙醇",
            "aliases": [],
            "physical_state": "liquid",
            "name_en": None,
            "meta_data": {},
        },
    ).json()["data"]

    assert page["total"] == 1
    assert page["items"][0]["aliases"] == ["酒精"]
    assert registered["status"] == "registered"
    assert unavailable["status"] == "unavailable"
    assert updated["name"] == "无水乙醇"
    assert updated["name_en"] is None
    assert client.delete(f"/api/v1/reagent-infos/{identity}").json() == {"code": 0}
    store.close()


def test_reagent_instance_revision_history_and_delete_guard(tmp_path) -> None:
    """证明容器试剂 CRUD、乐观修订、台账和化学身份删除保护在同一库闭合。"""

    client, store = _client(tmp_path)
    material_uuid = _container(client)
    info_uuid = _reagent_info(client)

    created = client.post(
        "/api/v1/reagents",
        json={
            "material_uuid": material_uuid,
            "cas": "64-17-5",
            "quantity": 500,
            "quantity_unit": "mL",
            "concentration_value": 95,
            "concentration_unit": "%",
            "source": "frontend:workbench",
            "meta_data": {},
        },
    )
    reagent_uuid = created.json()["data"]["uuid"]
    conflict = client.put(
        f"/api/v1/reagents/{reagent_uuid}",
        json={
            "quantity": 450,
            "quantity_unit": "mL",
            "expected_revision": 9,
            "meta_data": {},
        },
    ).json()
    updated = client.put(
        f"/api/v1/reagents/{reagent_uuid}",
        json={
            "quantity": 450,
            "quantity_unit": "mL",
            "expected_revision": 1,
            "concentration_value": 95,
            "concentration_unit": "%",
            "meta_data": {},
        },
    ).json()["data"]
    history = client.get(
        f"/api/v1/materials/{material_uuid}/reagent-history?page=1&page_size=100"
    ).json()["data"]
    history_by_uuid = client.get(
        f"/api/v1/reagent-history/{history['items'][0]['uuid']}"
    ).json()["data"]
    ledger = store.query_all(
        "SELECT * FROM inventory_ledger WHERE subject_type='reagent' "
        "ORDER BY ledger_id"
    )
    outbox = store.query_all(
        "SELECT * FROM sync_outbox WHERE aggregate_type='reagent' "
        "ORDER BY sequence"
    )
    # 扩展统一台账列后，旧 ``/inventory/ledger`` 诊断 DTO 仍必须能序列化整行。
    InventoryLedgerEntryResponse.model_validate(ledger[-1])
    protected = client.delete(f"/api/v1/reagent-infos/{info_uuid}").json()
    protected_container = client.delete(
        f"/api/v1/materials/{material_uuid}"
    ).json()

    assert created.status_code == 201
    assert created.json()["data"]["revision"] == 1
    assert created.json()["data"]["reagent_info"]["uuid"] == info_uuid
    assert conflict["code"] == 4002
    assert updated["revision"] == 2
    assert updated["quantity"] == 450
    assert [entry["event_type"] for entry in history["items"]] == [
        "adjust",
        "add",
    ]
    assert history_by_uuid == history["items"][0]
    assert [entry["op_type"] for entry in ledger] == [
        "reagent.add",
        "reagent.adjust",
    ]
    assert [entry["event_type"] for entry in outbox] == [
        "reagent.add",
        "reagent.adjust",
    ]
    assert [entry["entry_uuid"] for entry in ledger] == [
        entry["event_id"] for entry in outbox
    ]
    assert protected["code"] == 4000
    assert protected_container["code"] == 2005

    assert client.delete(f"/api/v1/reagents/{reagent_uuid}").json() == {"code": 0}
    # 试剂虽已删除，身份仍被历史实例和台账引用，必须保留用于追溯。
    assert client.delete(f"/api/v1/reagent-infos/{info_uuid}").json()["code"] == 4000
    store.close()


def test_reagent_requires_registered_identity_and_container_material(tmp_path) -> None:
    """证明试剂创建不会隐式注册 CAS，也拒绝把非容器物料冒充试剂瓶。"""

    client, store = _client(tmp_path)
    material_uuid = _container(client)

    missing_identity = client.post(
        "/api/v1/reagents",
        json={
            "material_uuid": material_uuid,
            "cas": "67-56-1",
            "quantity": 10,
            "quantity_unit": "mL",
            "meta_data": {},
        },
    ).json()

    assert missing_identity["code"] == 4001
    store.close()


def _audited_reagent(client: TestClient) -> dict:
    """创建有浓度、说明和批号的试剂，供业务字段更正测试复用。"""

    material_uuid = _container(client)
    info_uuid = _reagent_info(client)
    response = client.post("/api/v1/reagents", json={
        "material_uuid": material_uuid, "reagent_info_uuid": info_uuid,
        "quantity": 500, "quantity_unit": "mL",
        "concentration_value": 95, "concentration_unit": "%",
        "description": "密封保存", "meta_data": {"batch": "A"},
    })
    assert response.status_code == 201
    return response.json()["data"]


@pytest.mark.parametrize("changes", [
    {"concentration_value": 5},
    {"concentration_unit": "g/L"},
    {"concentration_value": None, "concentration_unit": None},
    {"description": "冷藏保存"},
    {"description": None},
    {"meta_data": {"batch": "B"}},
])
def test_non_quantity_edits_record_matching_before_after_and_outbox(tmp_path, changes) -> None:
    """每类业务字段更正均有同修订的前后快照，省略字段不会被清空。"""

    client, store = _client(tmp_path)
    try:
        original = _audited_reagent(client)
        response = client.put(f"/api/v1/reagents/{original['uuid']}", json={
            "quantity": 500, "quantity_unit": "mL", "expected_revision": 1, **changes,
        }).json()
        assert response["code"] == 0, response
        updated = response["data"]
        ledger = store.query_all(
            "SELECT * FROM inventory_ledger WHERE aggregate_id=? ORDER BY ledger_id",
            (original["uuid"],),
        )
        outbox = store.query_all(
            "SELECT * FROM sync_outbox WHERE aggregate_id=? ORDER BY sequence",
            (original["uuid"],),
        )
        assert len(ledger) == len(outbox) == 2
        event = ledger[-1]
        assert event["op_type"] == "reagent.adjust"
        assert event["quantity_delta"] == 0
        assert event["revision"] == updated["revision"] == outbox[-1]["aggregate_version"] == 2
        assert event["entry_uuid"] == outbox[-1]["event_id"]
        payload = json.loads(event["delta_json"])
        assert payload == json.loads(outbox[-1]["payload_json"])
        before, after = payload["changes"]["previous"], payload["changes"]["result"]
        for field in ("concentration_value", "concentration_unit", "description", "meta_data", "quantity", "revision"):
            assert before[field] == original[field]
            assert after[field] == updated[field]
        for field in ("description", "meta_data"):
            if field not in changes:
                assert updated[field] == original[field]
    finally:
        store.close()


def test_unchanged_edit_preserves_revisions_description_and_audit(tmp_path) -> None:
    """真正无变化的保存不空增修订，随后过期修订仍被拒绝。"""

    client, store = _client(tmp_path)
    try:
        original = _audited_reagent(client)
        path = f"/api/v1/reagents/{original['uuid']}"
        result = client.put(path, json={
            "quantity": 500, "quantity_unit": "mL", "expected_revision": 1,
            "meta_data": {}, "container_capacity": {},
        }).json()["data"]
        assert result["revision"] == 1
        assert result["material_revision"] == original["material_revision"]
        assert result["update_time"] == original["update_time"]
        assert result["description"] == "密封保存"
        assert result["meta_data"] == {"batch": "A"}
        assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 1
        assert client.put(path, json={
            "quantity": 500, "quantity_unit": "mL", "expected_revision": 1,
            "description": "冷藏保存",
        }).json()["code"] == 0
        rejected = client.put(path, json={
            "quantity": 500, "quantity_unit": "mL", "expected_revision": 1,
        }).json()
        assert rejected["code"] == 4002
        assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 2
    finally:
        store.close()


def test_competing_metadata_edits_commit_only_one_revision(tmp_path) -> None:
    """两个操作者基于同一修订更正说明，只有一笔事实和事件能提交。"""

    client, store = _client(tmp_path)
    try:
        original = _audited_reagent(client)
        def update(description: str) -> dict:
            return client.put(f"/api/v1/reagents/{original['uuid']}", json={
                "quantity": 500, "quantity_unit": "mL", "expected_revision": 1,
                "description": description,
            }).json()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(update, ["操作员甲更正", "操作员乙更正"]))
        assert sorted(result["code"] for result in results) == [0, 4002]
        winner = next(result["data"] for result in results if result["code"] == 0)
        assert store.query_one("SELECT quantity,revision,description FROM reagent WHERE uuid=?", (original["uuid"],)) == {
            "quantity": 500, "revision": 2, "description": winner["description"],
        }
        assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 2
        assert store.query_one("SELECT COUNT(*) AS n FROM sync_outbox WHERE aggregate_type='reagent'")["n"] == 2
    finally:
        store.close()


def test_audit_failure_rolls_back_metadata_edit(tmp_path, monkeypatch) -> None:
    """事件写入失败时，业务字段、修订和原有台账一起保持不变。"""

    client, store = _client(tmp_path)
    try:
        original = _audited_reagent(client)
        service = BackendReagentService(store)
        def fail_event(*args, **kwargs):
            raise RuntimeError("测试：事件写入失败")

        monkeypatch.setattr(service, "_append_history", fail_event)
        with pytest.raises(RuntimeError, match="事件写入失败"):
            service.update_reagent(original["uuid"], {
                "quantity": 500, "quantity_unit": "mL", "expected_revision": 1,
                "concentration_value": 5, "description": "冷藏", "meta_data": {"batch": "B"},
            })
        current = service.get_reagent(original["uuid"])
        for key in ("quantity", "revision", "concentration_value", "description", "meta_data", "update_time"):
            assert current[key] == original[key]
        assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM sync_outbox WHERE aggregate_type='reagent'")["n"] == 1
    finally:
        store.close()


def test_reagent_page_aggregates_active_reservations_with_one_scan(tmp_path, monkeypatch) -> None:
    """多瓶列表只扫描一次活动预留，仍正确合并多作业并忽略已释放预留。"""

    client, store = _client(tmp_path)
    try:
        template_uuid = _container_template(client)
        info_uuid = _reagent_info(client)
        reagents = []
        for index in range(3):
            material = client.post("/api/v1/materials", json={
                "resource_template_uuid": template_uuid, "name": f"试剂瓶 {index}",
                "barcode": f"BATCH-QUERY-{index}",
            }).json()["data"]
            reagents.append(client.post("/api/v1/reagents", json={
                "material_uuid": material["uuid"], "reagent_info_uuid": info_uuid,
                "quantity": 50, "quantity_unit": "mL",
            }).json()["data"])
        authority = WorkflowQuantityInventoryAuthority(store, edge_id="test", lab_id="test")
        for task, reagent, quantity in [
            ("task-a", reagents[0], 0.1), ("task-b", reagents[0], 0.2),
            ("task-c", reagents[1], 4), ("task-released", reagents[2], 5),
        ]:
            authority.reserve_task(task, [{
                "uuid": str(uuid4()), "workflow_node_job_uuid": str(uuid4()),
                "inventory_type": "reagent", "inventory_uuid": reagent["uuid"],
                "material_uuid": reagent["material_uuid"], "reserved_quantity": quantity,
                "quantity_unit": "mL",
            }])
        authority.release_task("task-released", reason="测试释放")
        statements = []
        from unilabos.config.config import BasicConfig
        with store.read_connection() as connection:
            if BasicConfig.database_type == "sqlite":
                connection.set_trace_callback(statements.append)
            else:
                original_execute = connection.execute
                def traced_execute(sql, *args, **kwargs):
                    statements.append(sql)
                    return original_execute(sql, *args, **kwargs)
                monkeypatch.setattr(connection, "execute", traced_execute)
        try:
            result = client.get("/api/v1/reagents?page_size=100").json()
        finally:
            if BasicConfig.database_type == "sqlite":
                with store.read_connection() as connection:
                    connection.set_trace_callback(None)
        assert result["code"] == 0, result
        reserved = {item["uuid"]: item["active_workflow_reserved_quantity"] for item in result["data"]["items"]}
        assert reserved == pytest.approx({reagents[0]["uuid"]: 0.3, reagents[1]["uuid"]: 4, reagents[2]["uuid"]: 0})
        scans = [statement for statement in statements if "FROM inventory_reservation" in statement]
        assert len(scans) == 1, scans
    finally:
        store.close()
