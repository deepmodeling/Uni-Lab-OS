"""F09 全局服务器发送事件（SSE）的持久重连合同。"""

from __future__ import annotations

import asyncio
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
    last_event_id: int,
) -> tuple[int, str]:
    """从指定排他游标读取首个持久 SSE 事件后断开。

    参数：``app`` 是真实工作流 HTTP 应用，``last_event_id`` 是重连请求游标。
    返回：HTTP 状态与已发送帧文本。异常：两秒内没有事件时超时失败；ASGI 调用
    错误原样传播。
    """

    disconnected = asyncio.Event()
    request_sent = False
    messages: list[dict[str, Any]] = []

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

        messages.append(message)
        if b"event: workflow.authoring.changed" in message.get("body", b""):
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
        "headers": [(b"last-event-id", str(last_event_id).encode("ascii"))],
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
