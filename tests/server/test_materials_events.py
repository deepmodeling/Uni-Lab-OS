"""物料 SSE 的首连边界、双订阅、重连补偿与游标重置；通知后以 HTTP 为准。"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tests.api_envelope import command_result, error_of, ok_data
from unilabos.client.materials import bind_payload
from unilabos.protocol.materials import InventoryMutation, MaterialDelete, MaterialTreeCreate
from unilabos.server.api.materials import install_materials_api
from unilabos.server.api.materials.core import format_materials_cursor, parse_materials_cursor
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str, payload: dict) -> dict:
    model = {"create_material_tree": MaterialTreeCreate, "delete_material": MaterialDelete}[operation]
    return bind_payload(
        InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation),
        model.model_validate(payload),
    ).model_dump(mode="json")


async def _create(client: httpx.AsyncClient, name: str) -> str:
    response = await client.post("/api/v1/inventory/commands", json=_mutation(
        "create_material_tree", {"nodes": [{"client_ref": name, "identity": {
            "resource_id": name, "name": name, "resource_type": "container",
            "class_name": "Container", "template_name": "event-test-container",
            "config": {"type": "Container"},
        }}]},
    ))
    return command_result(response)["data"]["root_material_uuid"]


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
        #: 每个 SSE 帧的字段（``id`` / ``event`` / ``data``），注释帧不记
        self.frames: list[dict] = []
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
                for block in body.split("\n\n"):
                    fields = dict(
                        line.split(": ", 1) for line in block.splitlines()
                        if ": " in line and not line.startswith(":")
                    )
                    if fields:
                        self.frames.append(fields)
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
                if row.get("aggregate_uuid") == uuid and row.get("aggregate_type") == "material":
                    return row
        return await asyncio.wait_for(find(), 3)

    def frame(self, event: str | None) -> dict:
        """第一帧给定事件名的帧；``None`` 取只含 ``id`` 的水位帧。"""

        return next(frame for frame in self.frames if frame.get("event") == event and "retry" not in frame)

    def ids(self, event: str) -> list[str]:
        return [frame["id"] for frame in self.frames if frame.get("event") == event]

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
                assert len(ok_data(await client.get("/api/v1/inventory/instances"))["instances"]) == 1
                created.append(await _create(client, "during-open"))

            stream = await _Stream(app, on_headers=after_headers).start()
            try:
                row = await stream.until(created[0])
                assert row["operation"] == "create"
                assert row["aggregate_uuid"] != old_uuid
                assert all(event["sequence"] > baseline for event in stream.received)
                assert len(ok_data(await client.get("/api/v1/inventory/instances"))["instances"]) == 2
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
                ok_data(await client.get(f"/api/v1/inventory/instances/{uuid}"))
                await a.close()
                response = await client.post("/api/v1/inventory/commands",
                    json=_mutation("delete_material", {"material_uuid": uuid}))
                command_result(response)
                deleted = await b.until(uuid)
                assert deleted["operation"] == "delete"
                # 删除后读取：物料不存在 6000
                error_of(await client.get(f"/api/v1/inventory/instances/{uuid}"), 6000)
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


def _app(service: MaterialsService) -> FastAPI:
    app = FastAPI()
    install_materials_api(app, service)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_cursor_format_round_trip_and_legacy_numbers():
    assert format_materials_cursor(42, "0123abcd0123abcd") == "42:0123abcd0123abcd"
    assert format_materials_cursor(0, None) == "0"
    assert parse_materials_cursor("42:0123abcd0123abcd") == (42, "0123abcd0123abcd")
    # 旧格式的纯数字游标照常接受，不带纪元
    assert parse_materials_cursor(" 42 ") == (42, None)
    assert parse_materials_cursor(None) == (None, None)
    assert parse_materials_cursor("  ") == (None, None)
    for bad in ("-1", "abc", "42:", ":abcd", "42:ab cd", "1.5"):
        with pytest.raises(ValueError):
            parse_materials_cursor(bad)


def test_ledger_epoch_is_stable_per_database_and_differs_between_databases(tmp_path):
    first = MaterialsService(tmp_path / "a" / "materials.db")
    second = MaterialsService(tmp_path / "b" / "materials.db")
    try:
        # 空账本没有纪元
        assert first.ledger_epoch() is None

        async def fill():
            async with _client(_app(first)) as client:
                await _create(client, "epoch-a1")
                epoch = first.ledger_epoch()
                await _create(client, "epoch-a2")
                # 只追加的账本：后续写入不改变纪元
                assert first.ledger_epoch() == epoch
            async with _client(_app(second)) as client:
                await _create(client, "epoch-b1")

        asyncio.run(fill())
        epoch = first.ledger_epoch()
        assert isinstance(epoch, str) and len(epoch) == 16
        assert second.ledger_epoch() not in (None, epoch)
    finally:
        first.close()
        second.close()
    reopened = MaterialsService(tmp_path / "a" / "materials.db")
    try:
        assert reopened.ledger_epoch() == epoch
    finally:
        reopened.close()


def test_fresh_connection_sends_watermark_with_epoch(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = _app(service)

    async def scenario():
        async with _client(app) as client:
            await _create(client, "before-open")
            watermark = format_materials_cursor(service.latest_ledger_sequence(), service.ledger_epoch())
            stream = await _Stream(app).start()
            try:
                uuid = await _create(client, "after-open")
                await stream.until(uuid)
                assert stream.frame(None) == {"id": watermark}
                assert "stream.reset" not in {frame.get("event") for frame in stream.frames}
                epoch = service.ledger_epoch()
                assert all(item.endswith(f":{epoch}") for item in stream.ids("materials.changed"))
            finally:
                await stream.close()

    try:
        asyncio.run(scenario())
    finally:
        service.close()


def test_empty_ledger_watermark_has_no_epoch_and_later_ids_do(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    app = _app(service)

    async def scenario():
        async with _client(app) as client:
            stream = await _Stream(app).start()
            try:
                uuid = await _create(client, "first-row")
                await stream.until(uuid)
                assert stream.frame(None) == {"id": "0"}
                epoch = service.ledger_epoch()
                assert stream.ids("materials.changed")[0] == f"1:{epoch}"
            finally:
                await stream.close()
            # 空账本时拿到的 "0" 仍从头续传（同一个库里此后的记录都是新的）
            resumed = await _Stream(app, last_id="0").start()
            try:
                await resumed.until(uuid)
                assert resumed.received[0]["sequence"] == 1
                assert "stream.reset" not in {frame.get("event") for frame in resumed.frames}
            finally:
                await resumed.close()

    try:
        asyncio.run(scenario())
    finally:
        service.close()


@pytest.mark.parametrize("with_epoch", [True, False], ids=["epoch", "legacy-number"])
def test_cursor_ahead_resets_to_latest_and_keeps_streaming(tmp_path, with_epoch):
    service = MaterialsService(tmp_path / "materials.db")
    app = _app(service)

    async def scenario():
        async with _client(app) as client:
            await _create(client, "existing")
            latest = service.latest_ledger_sequence()
            epoch = service.ledger_epoch()
            ahead = format_materials_cursor(latest + 100, epoch if with_epoch else None)
            stream = await _Stream(app, last_id=ahead).start()
            try:
                uuid = await _create(client, "after-reset")
                row = await stream.until(uuid)
                reset = stream.frame("stream.reset")
                assert reset["id"] == format_materials_cursor(latest, epoch)
                assert json.loads(reset["data"]) == {"reason": "cursor_ahead"}
                # 新水位之后的变更照常推送，不再被旧游标挡住
                assert row["sequence"] > latest
                assert stream.ids("materials.changed")[-1] == format_materials_cursor(row["sequence"], epoch)
            finally:
                await stream.close()

    try:
        asyncio.run(scenario())
    finally:
        service.close()


def test_cursor_from_another_ledger_resets_even_when_behind(tmp_path):
    """换了数据目录：新库的账本已经长过旧游标，旧游标的纪元对不上，必须重置而不是从中间续传。"""

    old = MaterialsService(tmp_path / "old" / "materials.db")
    new = MaterialsService(tmp_path / "new" / "materials.db")
    old_app, new_app = _app(old), _app(new)

    async def scenario():
        async with _client(old_app) as client:
            await _create(client, "old-root")
        old_cursor = format_materials_cursor(old.latest_ledger_sequence(), old.ledger_epoch())
        async with _client(new_app) as client:
            for index in range(3):
                await _create(client, f"new-root-{index}")
            latest = new.latest_ledger_sequence()
            assert latest > old.latest_ledger_sequence()
            stream = await _Stream(new_app, last_id=old_cursor).start()
            try:
                uuid = await _create(client, "after-restart")
                row = await stream.until(uuid)
                reset = stream.frame("stream.reset")
                assert json.loads(reset["data"]) == {"reason": "epoch_changed"}
                assert reset["id"] == format_materials_cursor(latest, new.ledger_epoch())
                # 不从旧游标的位置补发新库的历史，只推送新水位之后的变更
                assert [item["sequence"] for item in stream.received if "sequence" in item] == [row["sequence"]]
            finally:
                await stream.close()
            # 同一个库的游标（新格式）照常续传，不重置
            same = await _Stream(new_app, last_id=format_materials_cursor(latest, new.ledger_epoch())).start()
            try:
                assert (await same.until(uuid))["sequence"] == row["sequence"]
                assert "stream.reset" not in {frame.get("event") for frame in same.frames}
            finally:
                await same.close()

    try:
        asyncio.run(scenario())
    finally:
        old.close()
        new.close()


def test_cursor_into_empty_ledger_resets_as_epoch_changed(tmp_path):
    old = MaterialsService(tmp_path / "old" / "materials.db")
    new = MaterialsService(tmp_path / "new" / "materials.db")
    new_app = _app(new)

    async def scenario():
        async with _client(_app(old)) as client:
            await _create(client, "old-root")
        stream = await _Stream(new_app, last_id=format_materials_cursor(1, old.ledger_epoch())).start()
        try:
            async with _client(new_app) as client:
                uuid = await _create(client, "first-in-new")
            await stream.until(uuid)
            reset = stream.frame("stream.reset")
            assert reset["id"] == "0"
            assert json.loads(reset["data"]) == {"reason": "epoch_changed"}
            epoch = new.ledger_epoch()
            expected = [f"{sequence}:{epoch}" for sequence in range(1, new.latest_ledger_sequence() + 1)]
            assert stream.ids("materials.changed") == expected
        finally:
            await stream.close()

    try:
        asyncio.run(scenario())
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize("value", ["abc", "-1", "1:bad epoch", "1:"])
def test_invalid_last_event_id_is_rejected(tmp_path, value):
    service = MaterialsService(tmp_path / "materials.db")

    async def scenario():
        async with _client(_app(service)) as client:
            # 信封路由把参数错误换成 HTTP 200 + 1000 失败信封，不开流
            response = await client.get("/api/v1/materials/events", headers={"Last-Event-ID": value})
            assert error_of(response, 1000)["msg"] == "invalid Last-Event-ID"

    try:
        asyncio.run(scenario())
    finally:
        service.close()
