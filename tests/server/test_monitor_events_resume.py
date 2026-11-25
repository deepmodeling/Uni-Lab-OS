"""/api/v1/monitor/events 的续传：Last-Event-ID、stream.reset、按页扫描，每一帧都带 id。

TestClient 会把流式响应缓冲到结束，这里直接驱动 ASGI 应用读取前几帧。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI

from unilabos.server.api.runtime.scheduling_v1 import create_scheduling_router
from unilabos.server.services.runtime.workflow.service import WorkflowService

JOB_EVENT = "workflow.node_job.changed"  # 投影到 action 通道


def _app(store) -> FastAPI:
    app = FastAPI()
    app.include_router(create_scheduling_router(store, get_scheduler=lambda: None))
    return app


def _append(service: WorkflowService, count: int, event: str = JOB_EVENT) -> None:
    for _ in range(count):
        service.append_frontend_event(event, {"status": "running"})


async def _read(app, headers: list[tuple[bytes, bytes]], steps, query: bytes = b"") -> str:
    """依次等待 steps 中每个 (predicate, action)：文本满足 predicate 后执行 action。"""

    chunks: list[str] = []
    changed = asyncio.Event()
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b"").decode("utf-8"))
            changed.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/api/v1/monitor/events", "raw_path": b"/api/v1/monitor/events",
        "query_string": query, "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        for predicate, action in steps:
            while not predicate("".join(chunks)):
                changed.clear()
                await asyncio.wait_for(changed.wait(), 5.0)
            if action is not None:
                await asyncio.to_thread(action)
    finally:
        disconnect.set()
        await asyncio.wait_for(task, 5.0)
    return "".join(chunks)


def _ids(text: str) -> list[int]:
    """事件帧的 id（不含只带 id 的水位帧）。"""

    return [int(block.split("\n", 1)[0][4:]) for block in text.split("\n\n")
            if block.startswith("id: ") and "\nevent:" in block]


@pytest.fixture
def service(monkeypatch):
    from unilabos.server.api.runtime import scheduling_v1

    monkeypatch.setattr(scheduling_v1, "shutting_down", lambda: False)
    workflow = WorkflowService(":memory:")
    yield workflow
    workflow.close()


def test_reconnect_resumes_after_last_event_id_without_replaying_the_backlog(service) -> None:
    _append(service, 50)
    app = _app(service)
    first = asyncio.run(_read(app, [], [(lambda body: "id: 50\nevent:" in body, None)]))
    assert _ids(first) == list(range(11, 51))  # 首连：最近 40 条
    _append(service, 3)
    again = asyncio.run(_read(app, [(b"last-event-id", b"50")], [(lambda body: "id: 53\nevent:" in body, None)]))
    assert _ids(again) == [51, 52, 53]  # 不重放、不重复


def test_more_than_one_page_is_delivered_in_full(service) -> None:
    app = _app(service)
    text = asyncio.run(_read(app, [], [
        (lambda body: "connected" in body, lambda: _append(service, 450)),
        (lambda body: "id: 450\nevent:" in body, None),
    ], query=b"backlog=0"))
    assert _ids(text) == list(range(1, 451))


def test_rows_outside_the_channels_still_advance_the_cursor(service) -> None:
    _append(service, 1)
    _append(service, 2, event="material.changed")
    app = _app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"0")], [(lambda body: "id: 3\n\n" in body, None)]))
    assert _ids(text) == [1]
    # 扫过的其他通知只推进水位：客户端断线后从 3 之后续传，不再重扫
    assert text.index("id: 1\nevent:") < text.index("id: 3\n\n")


class _Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RacingStore:
    """扫描读完之后、下一次读之前提交一行：模拟另一个事务恰好在扫描期间提交。"""

    def __init__(self, service: WorkflowService) -> None:
        self.service = service
        self.armed = False
        self.fired = False

    @property
    def write_lock(self):
        return self.service.write_lock

    @property
    def connection(self):
        return self

    def execute(self, sql: str, params=()):
        rows = self.service.connection.execute(sql, params).fetchall()
        if self.armed and not self.fired and "sequence >" in sql:
            self.fired = True
            _append(self.service, 1)
        return _Rows(rows)


def test_a_row_committed_during_a_scan_is_not_skipped(service) -> None:
    _append(service, 1)
    store = _RacingStore(service)
    app = _app(store)
    text = asyncio.run(_read(app, [], [
        (lambda body: "id: 1\nevent:" in body, lambda: setattr(store, "armed", True)),
        (lambda body: "id: 2\nevent:" in body, None),
    ]))
    assert store.fired and _ids(text) == [1, 2]


def _reset(text: str) -> tuple[str, dict]:
    block = next(item for item in text.split("\n\n") if "event: stream.reset" in item)
    return block.split("\n", 1)[0], json.loads(block.split("data: ", 1)[1])


def test_cursor_behind_retention_resets_as_expired(service) -> None:
    _append(service, 5)
    service.prune_frontend_events(before="0001-01-01T00:00:00.000000Z", max_count=2, batch_size=1000)
    app = _app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"1")], [
        (lambda body: "event: stream.reset" in body, lambda: _append(service, 1)),
        (lambda body: "id: 6\nevent:" in body, None),
    ]))
    assert _reset(text) == ("id: 5", {"reason": "cursor_expired"})
    assert _ids(text) == [5, 6]  # 重置帧本身带新水位，之后照常推送，不回放 4、5


def test_cursor_ahead_resets_to_the_latest(service) -> None:
    _append(service, 3)
    app = _app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"99")], [
        (lambda body: "event: stream.reset" in body, lambda: _append(service, 1)),
        (lambda body: "id: 4\nevent:" in body, None),
    ]))
    assert _reset(text) == ("id: 3", {"reason": "cursor_ahead"})


def test_cursor_at_the_window_edge_resumes_without_reset(service) -> None:
    _append(service, 5)
    service.prune_frontend_events(before="0001-01-01T00:00:00.000000Z", max_count=2, batch_size=1000)
    app = _app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"3")], [(lambda body: "id: 5\nevent:" in body, None)]))
    assert "stream.reset" not in text and _ids(text) == [4, 5]


def test_invalid_cursor_is_rejected() -> None:
    from fastapi.testclient import TestClient

    workflow = WorkflowService(":memory:")
    try:
        with TestClient(_app(workflow)) as client:
            response = client.get("/api/v1/monitor/events", headers={"Last-Event-ID": "abc"})
        assert response.status_code == 200 and response.json()["code"] == 1000
    finally:
        workflow.close()


def test_fresh_connection_sets_a_watermark_past_the_backlog(service) -> None:
    _append(service, 2)
    _append(service, 1, event="material.changed")
    app = _app(service)
    text = asyncio.run(_read(app, [], [(lambda body: "id: 3\n\n" in body, None)]))
    assert _ids(text) == [1, 2]
