"""物料 SSE 的首连边界、双订阅及重连补偿；通知后以 HTTP 为准。"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
from fastapi import FastAPI

from tests.api_envelope import error_of, ok_data
from unilabos.client.materials import bind_payload
from unilabos.protocol.materials import InventoryMutation, MaterialDelete, MaterialTreeCreate
from unilabos.server.api.materials import install_materials_api
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str, payload: dict) -> dict:
    model = {"create_material_tree": MaterialTreeCreate, "delete_material": MaterialDelete}[operation]
    return bind_payload(
        InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation),
        model.model_validate(payload),
    ).model_dump(mode="json")


async def _create(client: httpx.AsyncClient, name: str) -> str:
    response = await client.post("/api/v1/materials/trees", json=_mutation(
        "create_material_tree", {"nodes": [{"client_ref": name, "identity": {
            "resource_id": name, "name": name, "resource_type": "container",
            "class_name": "Container", "template_name": "event-test-container",
            "config": {"type": "Container"},
        }}]},
    ))
    return ok_data(response)["data"]["root_material_uuid"]


class _Stream:
    """直接消费 ASGI，不让 TestClient 把无限 SSE 缓冲到结束。"""

    def __init__(self, app, *, last_id=None, on_headers=None):
        self.app = app
        self.last_id = last_id
        self.on_headers = on_headers
        self.connected = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.rows = asyncio.Queue()
        self.received = []
        self.task = None

    async def start(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/api/v1/materials/events",
            "raw_path": b"/api/v1/materials/events", "query_string": b"",
            "root_path": "", "client": ("127.0.0.1", 1), "server": ("test", 80),
            "headers": [] if self.last_id is None else [(b"last-event-id", str(self.last_id).encode())],
        }

        async def receive():
            await self.disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
                if self.on_headers:
                    await self.on_headers()
            elif message["type"] == "http.response.body":
                body = message.get("body", b"").decode()
                if ": connected" in body:
                    self.connected.set()
                for line in body.splitlines():
                    if line.startswith("data: "):
                        row = json.loads(line[6:])
                        self.received.append(row)
                        self.rows.put_nowait(row)

        self.task = asyncio.create_task(self.app(scope, receive, send))
        await asyncio.wait_for(self.connected.wait(), 3)
        return self

    async def until(self, uuid: str):
        async def find():
            while True:
                row = await self.rows.get()
                if row["aggregate_uuid"] == uuid and row["aggregate_type"] == "material":
                    return row
        return await asyncio.wait_for(find(), 3)

    async def close(self):
        self.disconnect.set()
        if self.task:
            await asyncio.wait_for(self.task, 3)


def test_first_connection_does_not_swallow_writes_after_headers(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            old_uuid = await _create(client, "before-open")
            baseline = service.latest_ledger_sequence()
            created = []

            async def after_headers():
                # 浏览器 onopen 后的 HTTP 校准与设备写入穿插，首连不能追平掉这次写入。
                assert len(ok_data(await client.get("/api/v1/materials/instances"))) == 1
                created.append(await _create(client, "during-open"))

            stream = await _Stream(app, on_headers=after_headers).start()
            try:
                row = await stream.until(created[0])
                assert row["operation"] == "create"
                assert row["aggregate_uuid"] != old_uuid
                assert all(event["sequence"] > baseline for event in stream.received)
                assert len(ok_data(await client.get("/api/v1/materials/instances"))) == 2
            finally:
                await stream.close()

    try:
        asyncio.run(scenario())
    finally:
        service.close()


def test_two_subscribers_receive_create_delete_and_replay(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            a = await _Stream(app).start()
            b = await _Stream(app).start()
            try:
                uuid = await _create(client, "two-tabs")
                first, second = await asyncio.gather(a.until(uuid), b.until(uuid))
                assert first == second
                ok_data(await client.get(f"/api/v1/materials/instances/{uuid}"))
                await a.close()
                response = await client.request("DELETE", f"/api/v1/materials/instances/{uuid}",
                    json=_mutation("delete_material", {"material_uuid": uuid}))
                ok_data(response)
                deleted = await b.until(uuid)
                assert deleted["operation"] == "delete"
                # 删除后读取：物料不存在 6000
                error_of(await client.get(f"/api/v1/materials/instances/{uuid}"), 6000)
                replay = await _Stream(app, last_id=first["sequence"]).start()
                try:
                    assert await replay.until(uuid) == deleted
                finally:
                    await replay.close()
            finally:
                await a.close()
                await b.close()

    try:
        asyncio.run(scenario())
    finally:
        service.close()
