"""/api/v1/events 订阅语义，对齐 v1.0 ADR 0006（前端事件订阅）。

TestClient 会把流式响应缓冲到结束，这里直接驱动 ASGI 应用读取前几帧。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from unilabos.server.api.runtime import events
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.server.services.runtime.workflow.store import WorkflowStore

NOW = "2026-11-04T00:00:00Z"


def _append(service: WorkflowService, count: int) -> None:
    with service.transaction() as conn:
        for index in range(count):
            WorkflowStore._append_event(
                conn, event="workflow.task.changed", data={"workflow_task_uuid": f"t{index}"}, now=NOW,
            )


async def _read(app, headers: list[tuple[bytes, bytes]], steps) -> str:
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
        "scheme": "http", "path": "/api/v1/events", "raw_path": b"/api/v1/events", "query_string": b"",
        "root_path": "", "headers": headers, "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    try:
        for predicate, action in steps:
            while not predicate("".join(chunks)):
                changed.clear()
                await asyncio.wait_for(changed.wait(), 5.0)
            if action is not None:
                action()
    finally:
        disconnect.set()
        await asyncio.wait_for(task, 5.0)
    return "".join(chunks)


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(events, "shutting_down", lambda: False)
    workflow = WorkflowService(":memory:")
    yield workflow
    workflow.close()


def test_fresh_connection_starts_at_watermark_without_replaying_history(service) -> None:
    _append(service, 3)
    assert service.latest_event_sequence() == 3
    app = create_workflow_app(service)

    text = asyncio.run(_read(app, [], [
        (lambda body: "id: 3\n\n" in body, lambda: _append(service, 1)),
        (lambda body: "id: 4\nevent: workflow.task.changed" in body, None),
    ]))

    assert text.startswith("retry: 3000")
    for old in (1, 2, 3):
        assert f"id: {old}\nevent:" not in text


def test_empty_database_watermark_is_zero(service) -> None:
    app = create_workflow_app(service)
    text = asyncio.run(_read(app, [], [(lambda body: "id: 0\n\n" in body, None)]))
    assert "event:" not in text


def test_explicit_zero_replays_retained_history(service) -> None:
    _append(service, 3)
    app = create_workflow_app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"0")], [
        (lambda body: "id: 3\nevent:" in body, None),
    ]))
    assert "id: 1\nevent: workflow.task.changed" in text
    assert "id: 0\n\n" not in text  # 显式游标不发水位帧


def test_cursor_ahead_resets_to_latest_and_keeps_streaming(service) -> None:
    _append(service, 3)
    app = create_workflow_app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"99")], [
        (lambda body: "event: stream.reset" in body, lambda: _append(service, 1)),
        (lambda body: "id: 4\nevent: workflow.task.changed" in body, None),
    ]))
    reset = next(frame for frame in text.split("\n\n") if "event: stream.reset" in frame)
    assert reset.startswith("id: 3\n")
    assert json.loads(reset.split("data: ", 1)[1]) == {"reason": "cursor_ahead"}


def test_resume_cursor_streams_only_later_events(service) -> None:
    _append(service, 3)
    app = create_workflow_app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"2")], [
        (lambda body: "id: 3\nevent:" in body, None),
    ]))
    assert "id: 2\nevent:" not in text and "stream.reset" not in text


def _prune_to_last(service, keep: int) -> None:
    service.prune_frontend_events(before="0001-01-01T00:00:00.000000Z", max_count=keep, batch_size=1000)


def test_cursor_behind_retention_window_resets_as_expired(service) -> None:
    _append(service, 5)
    _prune_to_last(service, 2)  # 保留窗口 4..5
    app = create_workflow_app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"1")], [
        (lambda body: "event: stream.reset" in body, lambda: _append(service, 1)),
        (lambda body: "id: 6\nevent: workflow.task.changed" in body, None),
    ]))
    reset = next(frame for frame in text.split("\n\n") if "event: stream.reset" in frame)
    assert reset.startswith("id: 5\n")
    assert json.loads(reset.split("data: ", 1)[1]) == {"reason": "cursor_expired"}
    assert "id: 4\nevent:" not in text


def test_cursor_at_window_edge_resumes_without_reset(service) -> None:
    _append(service, 5)
    _prune_to_last(service, 2)
    app = create_workflow_app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"3")], [
        (lambda body: "id: 5\nevent:" in body, None),
    ]))
    assert "id: 4\nevent:" in text and "stream.reset" not in text


def test_explicit_zero_replays_retained_window_without_reset(service) -> None:
    _append(service, 5)
    _prune_to_last(service, 2)
    app = create_workflow_app(service)
    text = asyncio.run(_read(app, [(b"last-event-id", b"0")], [
        (lambda body: "id: 5\nevent:" in body, None),
    ]))
    assert "id: 4\nevent:" in text and "stream.reset" not in text
