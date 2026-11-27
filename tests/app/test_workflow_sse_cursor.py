"""F09 全局服务器发送事件（SSE）的持久重连合同。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "92000000-0000-4000-8000-000000000001"


def _append_authoring_event(
    store: WorkflowStore,
    *,
    marker: str,
) -> int:
    """通过创作事务追加一个小型持久失效通知。

    参数：``store`` 是当前唯一工作流写模型，``marker`` 区分事件代际。返回：
    数据库签发的全局事件序号。异常：工作流缺失或事务失败时传播。
    """

    return store.record_draft_compilation(
        workflow_uuid=WORKFLOW_UUID,
        draft_hash="sha256:" + marker * 64,
        draft_update_time=f"2026-08-05T00:00:0{marker}Z",
        diagnostics=[],
        candidate_hash=None,
        candidate=None,
        event_data={"workflow_uuid": WORKFLOW_UUID, "cause": f"event-{marker}"},
    )


async def _read_sse_event(
    app: FastAPI,
    *,
    last_event_id: int | str | None = None,
    on_connected: Callable[[], None] | None = None,
    on_frame: Callable[[bytes], None] | None = None,
    event_count: int = 1,
    stop_on_keepalive: bool = False,
) -> tuple[int, str]:
    """从指定排他游标读取首个持久 SSE 事件后断开。

    参数：``app`` 是真实工作流 HTTP 应用，``last_event_id`` 是重连请求游标。
    返回：HTTP 状态与已发送帧文本。异常：两秒内没有事件时超时失败；ASGI 调用
    错误原样传播。
    """

    disconnected = asyncio.Event()
    request_sent = False
    messages: list[dict[str, Any]] = []
    received_events = 0

    async def receive() -> dict[str, Any]:
        """模拟一次请求体并等待测试主动断开。

        参数：无。返回：首轮为请求，之后为断开。异常：不抛异常。
        """

        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        """收集 ASGI 响应并在出现工作流事件时关闭连接。

        参数：``message`` 是单个 ASGI 响应消息。返回：记录完成后无值。异常：
        不抛异常。
        """

        nonlocal received_events
        messages.append(message)
        if message["type"] == "http.response.start" and on_connected:
            on_connected()
        body = message.get("body", b"")
        if on_frame:
            on_frame(body)
        if b"event: workflow." in body or b"event: stream.reset" in body:
            received_events += 1
        if received_events >= event_count or (stop_on_keepalive and b": keepalive" in body):
            disconnected.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "root_path": "",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"",
        "headers": [] if last_event_id is None else [
            (b"last-event-id", str(last_event_id).encode("ascii"))
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    ).decode("utf-8")
    return status, body


@pytest.mark.parametrize("header", [None, "", "   "])
def test_new_connection_skips_history_and_delivers_change_after_connect(
    tmp_path: Path, header: str | None,
) -> None:
    """首次订阅跳过历史，但建连期间发生的最后一次变更必须送达。"""
    store = WorkflowStore(tmp_path / "fresh.db")
    try:
        store.append_forwarded_events([{
            "event": "workflow.runtime.changed", "data": {"cause": "old"},
            "create_time": "2026-10-14T00:00:00Z",
        }] * 201)
        def append_current() -> None:
            store.append_forwarded_events([{
                "event": "workflow.runtime.changed", "data": {"cause": "current"},
                "create_time": "2026-10-14T00:00:01Z",
            }])
        status, body = asyncio.run(_read_sse_event(
            create_workflow_app(WorkflowService(store)), last_event_id=header,
            on_connected=append_current,
        ))
        assert status == 200
        assert '"cause":"old"' not in body
        assert '"cause":"current"' in body
        assert "id: 202\n" in body
    finally:
        store.close()


@pytest.mark.parametrize("cursor", [0, 1])
def test_explicit_cursor_drains_multiple_pages_without_one_second_per_page(
    tmp_path: Path, cursor: int,
) -> None:
    """完整 HTTP 流在两秒期限内追平超过两页的积压，显式零仍可回放。"""
    store = WorkflowStore(tmp_path / "backlog.db")
    try:
        store.append_forwarded_events([{
            "event": "workflow.runtime.changed", "data": {},
            "create_time": "2026-10-14T00:00:00Z",
        }] * 202)
        _, body = asyncio.run(_read_sse_event(
            create_workflow_app(WorkflowService(store)), last_event_id=cursor,
            event_count=202 - cursor,
        ))
        ids = [int(line[4:]) for line in body.splitlines() if line.startswith("id: ")]
        assert ids == list(range(cursor + 1, 203))
    finally:
        store.close()


@pytest.mark.parametrize("cursor, reason", [(1, "cursor_expired"), (999, "cursor_ahead")])
def test_unavailable_cursor_requests_rest_resync_then_delivers_new_events(
    tmp_path: Path, cursor: int, reason: str,
) -> None:
    """过期和数据库重置后的超前游标都先通知重读，再继续正常推送。"""
    store = WorkflowStore(tmp_path / "reset.db")
    event = {"event": "workflow.runtime.changed", "data": {},
             "create_time": "2026-10-14T00:00:00Z"}
    try:
        store.append_forwarded_events([event] * 5)
        store.prune_frontend_events(before="2000", max_count=2, batch_size=10)
        def after_reset(frame: bytes) -> None:
            if b"event: stream.reset" in frame:
                store.append_forwarded_events([event])
        _, body = asyncio.run(_read_sse_event(
            create_workflow_app(WorkflowService(store)), last_event_id=cursor,
            on_frame=after_reset, event_count=2,
        ))
        assert f'"reason":"{reason}"' in body
        assert "id: 5\nevent: stream.reset" in body
        assert "id: 6\nevent: workflow.runtime.changed" in body
        assert "id: 4\n" not in body
    finally:
        store.close()


def test_retention_overtaking_an_open_connection_emits_reset(tmp_path: Path) -> None:
    """清理在传输中越过慢连接时，下一页也必须检测丢失窗口。"""
    store = WorkflowStore(tmp_path / "slow.db")
    try:
        store.append_forwarded_events([{
            "event": "workflow.runtime.changed", "data": {},
            "create_time": "2026-10-14T00:00:00Z",
        }] * 201)
        def prune_after_first(frame: bytes) -> None:
            if frame.startswith(b"id: 1\n"):
                store.prune_frontend_events(before="2000", max_count=1, batch_size=1000)
        _, body = asyncio.run(_read_sse_event(
            create_workflow_app(WorkflowService(store)), last_event_id=0,
            on_frame=prune_after_first, event_count=101,
        ))
        assert '"reason":"cursor_expired"' in body
        assert "id: 201\nevent: stream.reset" in body
        assert "id: 101\n" not in body
    finally:
        store.close()


def test_new_connection_forwards_pending_definition_events_before_watermark(tmp_path: Path) -> None:
    """内存定义目录中积压的旧通知也属于新连接应跳过的历史。"""
    store = WorkflowStore(tmp_path / "runtime.db")
    definitions = WorkflowStore(":memory:")
    try:
        definitions.append_forwarded_events([{
            "event": "workflow.authoring.changed", "data": {"cause": "old-definition"},
            "create_time": "2026-10-14T00:00:00Z",
        }])
        _, body = asyncio.run(_read_sse_event(
            create_workflow_app(WorkflowService(store, definition_store=definitions)),
            stop_on_keepalive=True,
        ))
        assert "old-definition" not in body
        assert "id: 1\n\n" in body
        assert ": keepalive" in body
    finally:
        store.close()
        definitions.close()


def test_last_event_id_replays_only_later_event_after_restart(
    tmp_path: Path,
) -> None:
    """重启后的 SSE 只重放严格晚于 Last-Event-ID 的持久通知。

    参数：``tmp_path`` 是隔离数据库目录。返回：无；断言首事件不重复、次事件不
    丢失且载荷只有失效身份。异常：存储、应用或异步流失败由测试暴露。
    """

    database = tmp_path / "f09-sse.db"
    first_store = WorkflowStore(database)
    try:
        first_store.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="F09 SSE",
            tags=[],
            description=None,
            meta_data={},
        )
        first_sequence = _append_authoring_event(first_store, marker="1")
    finally:
        first_store.close()

    reopened_store = WorkflowStore(database)
    try:
        second_sequence = _append_authoring_event(reopened_store, marker="2")
        status, body = asyncio.run(
            _read_sse_event(
                create_workflow_app(WorkflowService(reopened_store)),
                last_event_id=first_sequence,
            )
        )
    finally:
        reopened_store.close()

    assert status == 200
    assert f"id: {second_sequence}\n" in body
    assert f"id: {first_sequence}\n" not in body
    assert "event: workflow.authoring.changed\n" in body
    assert f'"workflow_uuid":"{WORKFLOW_UUID}"' in body
    assert "python_source" not in body
    assert "graph" not in body


@pytest.mark.parametrize(
    "cursor",
    ["-1", "1.0", str(1 << 63), "not-an-integer"],
)
def test_last_event_id_rejects_invalid_int64_without_streaming(
    tmp_path: Path,
    cursor: str,
) -> None:
    """非法 SSE 游标必须在建立流前返回稳定输入错误。

    参数：``tmp_path`` 是隔离数据库目录，``cursor`` 是非法 int64 文本。返回：
    无。异常：HTTP 或错误信封回归由断言暴露。
    """

    store = WorkflowStore(tmp_path / "f09-invalid-sse.db")
    client = TestClient(create_workflow_app(WorkflowService(store)))
    try:
        response = client.get(
            "/api/v1/events",
            headers={"Last-Event-ID": cursor},
        )
    finally:
        client.close()
        store.close()

    assert response.status_code == 200
    assert response.json()["code"] == 1000
