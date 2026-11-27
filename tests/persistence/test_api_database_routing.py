"""根据接口文档验证实际落库、外部读写可见性和重连后的数据来源。"""

import hashlib
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_backend_reagent_api import _container_template, _reagent_info
from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.workflow_api import create_workflow_app
from unilabos.config.config import BasicConfig
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


@contextmanager
def native_database(mode, path):
    """绕过兼容层，直接用原生驱动查验被选中的数据库。"""
    if mode == "postgresql":
        import psycopg

        connection = psycopg.connect(BasicConfig.database_url, autocommit=True)
        placeholder = "%s"
    else:
        connection = sqlite3.connect(path, isolation_level=None)
        placeholder = "?"
    try:
        yield connection, placeholder
    finally:
        connection.close()


def _inventory_client(store):
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    return TestClient(app)


def test_material_reagent_api_writes_and_reads_selected_database(
    database_mode, tmp_path
):
    path = tmp_path / "inventory.db"
    # PG 模式放置一份带明显标记的 SQLite 文件，证明读写不会偷偷落回它。
    before = None
    if database_mode == "postgresql":
        with sqlite3.connect(path) as decoy:
            decoy.execute("CREATE TABLE must_remain_untouched (value TEXT)")
            decoy.execute("INSERT INTO must_remain_untouched VALUES ('not-selected')")
        before = hashlib.sha256(path.read_bytes()).digest()
    store = InventoryStore(str(path))
    client = _inventory_client(store)
    try:
        template = _container_template(client)
        reagent_info = _reagent_info(client)
        response = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template,
                "name": "文档验收源瓶",
                "barcode": "DOCUMENT-" + uuid4().hex,
                "reagent": {
                    "reagent_info_uuid": reagent_info,
                    "quantity": 500,
                    "quantity_unit": "mL",
                    "source": "document-verification",
                },
            },
        )
        assert response.status_code == 201, response.text
        material = response.json()["data"]
        material_uuid = material["uuid"]
        reagent_uuid = material["reagent"]["uuid"]
        with native_database(database_mode, path) as (connection, mark):
            assert (
                connection.execute(
                    f"SELECT name FROM material WHERE uuid={mark}", (material_uuid,)
                ).fetchone()[0]
                == "文档验收源瓶"
            )
            assert (
                connection.execute(
                    f"SELECT quantity FROM reagent WHERE uuid={mark}", (reagent_uuid,)
                ).fetchone()[0]
                == 500
            )
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM inventory_ledger WHERE material_uuid={mark}",
                    (material_uuid,),
                ).fetchone()[0]
                > 0
            )
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM sync_outbox WHERE aggregate_id={mark}",
                    (reagent_uuid,),
                ).fetchone()[0]
                > 0
            )
            connection.execute(
                f"UPDATE material SET name={mark} WHERE uuid={mark}",
                ("原生数据库修改后的名称", material_uuid),
            )
        response = client.get(f"/api/v1/materials/{material_uuid}")
        assert response.json()["data"]["name"] == "原生数据库修改后的名称"
        client.close()
        store.close()
        store = InventoryStore(str(path))
        client = _inventory_client(store)
        assert (
            client.get(f"/api/v1/materials/{material_uuid}").json()["data"]["name"]
            == "原生数据库修改后的名称"
        )
        assert (
            client.get(f"/api/v1/reagents/{reagent_uuid}").json()["data"]["quantity"]
            == 500
        )
        if before is not None:
            assert hashlib.sha256(path.read_bytes()).digest() == before
            assert not path.with_name(path.name + "-wal").exists()
    finally:
        client.close()
        store.close()


def test_task_api_uses_persistent_selection_and_keeps_definitions_in_memory(
    database_mode, tmp_path
):
    path = tmp_path / "workflow_history.db"
    runtime = WorkflowStore(path, persist_workflow_definitions=False)
    definitions = WorkflowStore(":memory:")
    service = WorkflowService(runtime, definition_store=definitions)
    client = TestClient(create_workflow_app(service))
    try:
        workflow_response = client.post(
            "/api/v1/workflows",
            json={"name": "文档验收流程", "tags": [], "meta_data": {}},
        )
        assert workflow_response.status_code == 201, workflow_response.text
        workflow = workflow_response.json()["data"]
        response = client.put(
            f"/api/v1/workflows/{workflow['uuid']}/graph",
            json={
                "revision": workflow["revision"],
                "nodes": [
                    {
                        "uuid": str(uuid4()),
                        "name": "计算节点",
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
        assert (
            response.status_code == 200 and response.json()["code"] == 0
        ), response.text
        response = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": workflow["uuid"],
                "run_mode": "normal",
                "meta_data": {},
            },
        )
        assert response.status_code == 201, response.text
        task = response.json()["data"]
        with native_database(database_mode, path) as (connection, mark):
            assert (
                connection.execute(
                    f"SELECT status FROM workflow_task WHERE uuid={mark}",
                    (task["uuid"],),
                ).fetchone()[0]
                == "pending"
            )
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM workflow_node_job WHERE workflow_task_uuid={mark}",
                    (task["uuid"],),
                ).fetchone()[0]
                == 1
            )
            if database_mode == "sqlite":
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type='table' AND name='workflow'"
                    ).fetchone()[0]
                    == 0
                )
            else:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name='workflow'"
                    ).fetchone()[0]
                    == 0
                )
            connection.execute(
                f"UPDATE workflow_task SET description={mark} WHERE uuid={mark}",
                ("从所选数据库读取", task["uuid"]),
            )
        assert (
            client.get(f"/api/v1/workflow-tasks/{task['uuid']}").json()["data"][
                "description"
            ]
            == "从所选数据库读取"
        )
        client.close()
        runtime.close()
        runtime = WorkflowStore(path, persist_workflow_definitions=False)
        client = TestClient(
            create_workflow_app(WorkflowService(runtime, definition_store=definitions))
        )
        assert (
            client.get(f"/api/v1/workflow-tasks/{task['uuid']}").json()["data"][
                "description"
            ]
            == "从所选数据库读取"
        )
        assert (
            len(
                client.get(f"/api/v1/workflow-tasks/{task['uuid']}/jobs").json()["data"]
            )
            == 1
        )
        if database_mode == "postgresql":
            assert not path.exists()
    finally:
        client.close()
        runtime.close()
        definitions.close()
