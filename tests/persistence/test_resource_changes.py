"""物料页增量查询与事务通知必须同时支持 SQLite 和 PostgreSQL。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.error_handling_api import create_error_handling_router
from unilabos.app.scheduler.inventory.backend_api import create_backend_resource_router
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.persistence.resource_changes import ResourceChangeStore
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.error_handling import ErrorHandlingService
from unilabos.workflow.event_reader import DurableEventReader
from unilabos.workflow.store import WorkflowStore, utc_now


def _source(store):
    return ResourceChangeStore(store, scope="inventory" if isinstance(store, InventoryStore) else "workflow")


def _changes(store, after):
    return DurableEventReader(_source(store)).read_stream(after_sequence=after, limit=1000)


def _seed_task(store):
    workflow, task, node, job, material, binding = (str(uuid4()) for _ in range(6))
    store.create_workflow(workflow_uuid=workflow, name="监控任务", tags=[], description=None, meta_data={})
    now = utc_now()
    with store.transaction() as conn:
        conn.execute(
            """INSERT INTO workflow_task(uuid,create_time,update_time,workflow_uuid,status,
          workflow_snapshot,execution_plan,run_mode,control_status,cleanup_status,trace_context,input,output,error_info,meta_data)
          VALUES(?,?,?,?,'running','{}','{}','normal','active','none','{}','{}','{}','[]','{}')""",
            (task, now, now, workflow),
        )
        conn.execute(
            """INSERT INTO workflow_node_job(uuid,create_time,update_time,workflow_task_uuid,
          workflow_node_uuid,topological_index,executor_kind,execution_policy,status,attempt,param,feedback_data,
          return_info,control_data,error_info,expected_change_set,meta_data,feedback_sequence,execution_timeout_seconds)
          VALUES(?,?,?,?,?,0,'device_action','{}','running',1,'{}','{}','{}','{}','[]','{}','{}',0,0)""",
            (job, now, now, task, node),
        )
        conn.execute(
            """INSERT INTO workflow_task_material_claim(uuid,create_time,update_time,
          workflow_task_uuid,workflow_node_uuid,workflow_node_job_uuid,material_uuid,status,revision,acquired_at)
          VALUES(?,?,?,?,?,?,?,'active',1,?)""",
            (binding, now, now, task, node, job, material, now),
        )
    return task, job, material, binding


def test_usage_partial_query_and_release_notification(database_mode, tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        first = _seed_task(store)
        second = _seed_task(store)
        control = StationExecutionControl(store)
        service = ErrorHandlingService(store, SimpleNamespace(error_control=control), None)
        app = FastAPI()
        app.include_router(create_error_handling_router(service), prefix="/api/v1")
        with TestClient(app) as client:
            data = client.get("/api/v1/stations/local/resource-usage", params={"binding_uuid": first[3]}).json()["data"]
            assert [row["uuid"] for row in data["material_bindings"]] == [first[3]]
            assert [row["uuid"] for row in data["jobs"]] == [first[1]]
            assert data["occupancies"] == []
            assert data["task_progress"][first[0]] == {"completed": 0, "total": 1}
            cursor = _source(store).latest_event_sequence()
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE workflow_task_material_claim SET status='released',released_at=? WHERE uuid=?",
                    (utc_now(), first[3]),
                )
                conn.execute("UPDATE workflow_node_job SET status='succeeded' WHERE uuid=?", (first[1],))
            events = _changes(store, cursor)["items"]
            assert any(first[3] in row["data"].get("binding_uuids", []) for row in events)
            assert any(first[1] in row["data"].get("job_uuids", []) for row in events)
            params = [("binding_uuid", first[3]), ("job_uuid", first[1]), ("task_uuid", first[0])]
            data = client.get("/api/v1/stations/local/resource-usage", params=params).json()["data"]
            assert data["material_bindings"] == []
            assert data["jobs"] == []
            assert data["task_progress"][first[0]] == {"completed": 1, "total": 1}
            all_data = client.get("/api/v1/stations/local/resource-usage").json()["data"]
            assert [row["uuid"] for row in all_data["material_bindings"]] == [second[3]]
    finally:
        store.close()


def test_material_graph_filters_and_rollback_has_no_notification(database_mode, tmp_path):
    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service = BackendResourceService(store)
        template = service.sync_resource_templates(
            [{"id": "bottle", "display_name": "瓶", "registry_type": "resource"}]
        )["templates"][0]["uuid"]
        first = service.create_material({"resource_template_uuid": template, "name": "瓶一"})
        second = service.create_material({"resource_template_uuid": template, "name": "瓶二"})
        app = FastAPI()
        app.include_router(create_backend_resource_router(service))
        with TestClient(app) as client:
            response = client.get("/api/v1/materials/graph", params={"material_uuid": first["uuid"]})
            assert response.status_code == 200
            assert [row["material"]["uuid"] for row in response.json()["data"]["nodes"]] == [first["uuid"]]
            assert client.get("/api/v1/materials/graph", params={"material_uuid": "invalid"}).status_code == 422
        cursor = _source(store).latest_event_sequence()
        with pytest.raises(RuntimeError):
            with store.transaction() as conn:
                conn.execute("UPDATE material SET name='已回滚' WHERE uuid=?", (first["uuid"],))
                raise RuntimeError("rollback")
        assert _changes(store, cursor)["items"] == []
        with store.transaction() as conn:
            conn.execute("UPDATE material SET name='新名称' WHERE uuid=?", (first["uuid"],))
        events = _changes(store, cursor)["items"]
        assert all(row["event"] == "material.changed" for row in events)
        assert {value for row in events for value in row["data"]["material_uuids"] if value} == {first["uuid"]}
        assert service.material_graph(material_uuids=[first["uuid"]])["nodes"][0]["material"]["name"] == "新名称"
        assert service.material_graph(material_uuids=[str(uuid4())])["nodes"] == []
        assert len(service.material_graph()["nodes"]) == 2
        assert second["uuid"] != first["uuid"]
    finally:
        store.close()


def test_moving_site_notifies_owner_and_both_occupants(database_mode, tmp_path):
    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service = BackendResourceService(store)
        template = service.sync_resource_templates([{"id": "rack", "registry_type": "resource"}])["templates"][0][
            "uuid"
        ]
        owner, first, second = [
            service.create_material({"resource_template_uuid": template, "name": str(uuid4())})["uuid"]
            for _ in range(3)
        ]
        now = utc_now()
        site = str(uuid4())
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO site(uuid,create_time,update_time,material_uuid,name,occupied_material_uuid) VALUES(?,?,?,?,?,?)",
                (site, now, now, owner, "A1", first),
            )
        cursor = _source(store).latest_event_sequence()
        with store.transaction() as conn:
            conn.execute("UPDATE site SET occupied_material_uuid=? WHERE uuid=?", (second, site))
        ids = {id for row in _changes(store, cursor)["items"] for id in row["data"]["material_uuids"] if id}
        assert {owner, first, second} <= ids
        graph = {node["material"]["uuid"]: node for node in service.material_graph(material_uuids=list(ids))["nodes"]}
        assert graph[first]["current_site_uuid"] is None
        assert graph[second]["current_site_uuid"] == site
        assert graph[owner]["sites"][0]["occupied_material_uuid"] == second
    finally:
        store.close()


def test_event_retention_requests_resync_and_never_leaks_rollback(database_mode, tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO resource_usage_change_event(id,event,data,create_time) VALUES(1,'resource.usage.changed','{}',?)",
                (utc_now(),),
            )
            conn.execute(
                "INSERT INTO resource_usage_change_event(id,event,data,create_time) VALUES(10200,'resource.usage.changed','{}',?)",
                (utc_now(),),
            )
        assert _changes(store, 1)["reset_reason"] == "cursor_expired"
        assert _changes(store, 99999)["reset_reason"] == "cursor_ahead"
        page = _changes(store, 10200)
        assert page["reset_reason"] is None and page["items"] == []
    finally:
        store.close()


def test_inventory_and_workflow_cursors_do_not_skip_out_of_order_commits(database_mode, tmp_path):
    """PG 的两个业务写锁独立；不能让库存先提交的游标越过未提交的工作流事件。"""
    workflow = WorkflowStore(tmp_path / "workflow.db")
    inventory = InventoryStore(str(tmp_path / "inventory.db"))
    reader = WorkflowStore(tmp_path / "workflow.db")
    try:
        task = _seed_task(workflow)
        service = BackendResourceService(inventory)
        template = service.sync_resource_templates([{"id": "concurrent", "registry_type": "resource"}])["templates"][0][
            "uuid"
        ]
        material = service.create_material({"resource_template_uuid": template, "name": "并发容器"})["uuid"]
        workflow_cursor = _source(reader).latest_event_sequence()
        inventory_cursor = _source(inventory).latest_event_sequence()
        with workflow.transaction() as connection:
            connection.execute("UPDATE workflow_node_job SET status='succeeded' WHERE uuid=?", (task[1],))
            with inventory.transaction() as other:
                other.execute("UPDATE material SET name='先提交' WHERE uuid=?", (material,))
            assert _changes(inventory, inventory_cursor)["items"]
            before_commit = _changes(reader, workflow_cursor)
            assert before_commit["items"] == []
            assert before_commit["next_sequence"] == workflow_cursor
        assert any(task[1] in item["data"].get("job_uuids", []) for item in _changes(reader, workflow_cursor)["items"])
    finally:
        reader.close()
        inventory.close()
        workflow.close()


def test_sse_fixes_initial_watermark_before_http_snapshot_and_replays(tmp_path):
    """真实 StreamingResponse 生成器验证订阅之后提交的变更可被读取。"""
    import asyncio

    from unilabos.app.resource_change_api import resource_change_stream

    store = WorkflowStore(tmp_path / "workflow.db")
    task = _seed_task(store)

    async def disconnected():
        return False

    async def scenario():
        request = SimpleNamespace(headers={}, is_disconnected=disconnected)
        response = await resource_change_stream(request, store, scope="workflow")
        stream = response.body_iterator
        first = await anext(stream)
        assert "connected" in first
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_node_job SET status='succeeded' WHERE uuid=?", (task[1],))
        event = await anext(stream)
        assert "event: resource.usage.changed" in event
        assert task[1] in event
        await stream.aclose()

    try:
        asyncio.run(scenario())
    finally:
        store.close()
