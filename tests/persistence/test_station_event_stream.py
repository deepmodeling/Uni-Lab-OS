"""真实库存/工作流日志经过同一 HTTP 流输出，保留双数据库事务语义。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.workflow.test_f05_task_runtime_projection import JOB_UUIDS, TASK_UUID, _seed_task
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.station_events import decode_cursor
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore


async def read_stream(app, on_open):
    disconnected = asyncio.Event()
    messages = []
    started = False
    names = set()

    async def receive():
        nonlocal started
        if not started:
            started = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            on_open()
        for line in message.get("body", b"").decode().splitlines():
            if line.startswith("event: "):
                names.add(line[7:])
        if {"workflow.task.changed", "material.changed", "resource.usage.changed"} <= names:
            disconnected.set()

    path = "/api/v1/stations/local/events/stream"
    await asyncio.wait_for(app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "http", "method": "GET", "root_path": "",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }, receive, send), timeout=3)
    assert next(item["status"] for item in messages if item["type"] == "http.response.start") == 200
    return b"".join(item.get("body", b"") for item in messages).decode()


def test_actual_station_stream_covers_changes_committed_after_open(database_mode, tmp_path):
    workflow = WorkflowStore(tmp_path / "workflow.db")
    inventory = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        _seed_task(workflow)
        materials = BackendResourceService(inventory)
        template = materials.sync_resource_templates([{"id": "bottle", "registry_type": "resource"}])["templates"][0]["uuid"]
        material = materials.create_material({"resource_template_uuid": template, "name": "before"})["uuid"]
        service = WorkflowService(workflow)
        service.error_handling = SimpleNamespace(control=StationExecutionControl(workflow))
        app = create_workflow_app(service, inventory_store_provider=lambda: inventory)
        assert {
            "/api/v1/stations/{station_id}/events",
            "/api/v1/stations/{station_id}/events/stream",
            "/api/v1/workflow-tasks/count",
        } <= app.openapi()["paths"].keys()
        stream_operation = app.openapi()["paths"]["/api/v1/stations/{station_id}/events/stream"]["get"]
        assert "text/event-stream" in stream_operation["responses"]["200"]["content"]

        def committed():
            with workflow.transaction() as connection:
                connection.execute(
                    "INSERT INTO frontend_event(event,data,create_time) VALUES (?,?,?)",
                    ("workflow.task.changed", json.dumps({"task_uuid": TASK_UUID}), "2026-11-12T00:00:00Z"),
                )
                connection.execute("UPDATE workflow_node_job SET status='succeeded' WHERE uuid=?", (JOB_UUIDS[0],))
            with inventory.transaction() as connection:
                connection.execute("UPDATE material SET name='after' WHERE uuid=?", (material,))

        body = asyncio.run(read_stream(app, committed))
        assert f'"task_uuid": "{TASK_UUID}"' in body
        assert material in body
        assert JOB_UUIDS[0] in body
        cursors = [decode_cursor(line[4:], "local") for line in body.splitlines() if line.startswith("id: ")]
        assert cursors[-1]["runtime"] > cursors[0]["runtime"]
        assert cursors[-1]["inventory"] > cursors[0]["inventory"]
        assert cursors[-1]["workflow"] > cursors[0]["workflow"]
        with TestClient(app) as client:
            assert client.get("/api/v1/stations/unknown/events/stream").status_code == 404
            assert client.get("/api/v1/stations/local/events/stream", headers={"Last-Event-ID": "bad"}).status_code == 400
            assert "items" in client.get("/api/v1/stations/local/events").json()["data"]
        unavailable = create_workflow_app(service, inventory_store_provider=lambda: None)
        with TestClient(unavailable) as client:
            assert client.get("/api/v1/stations/local/events/stream").status_code == 503
    finally:
        workflow.close()
        inventory.close()
