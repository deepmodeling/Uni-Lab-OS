"""停机信号：SSE 长连接在停机开始时自行结束，uvicorn 不必超时取消；监听者只通知一次。"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import pytest

from unilabos.server import lifecycle
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import WorkflowService
from unilabos.utils.fastapi import log_adapter


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    lifecycle.reset_shutdown_state()
    yield
    lifecycle.reset_shutdown_state()


def test_begin_shutdown_notifies_listeners_once_and_is_idempotent() -> None:
    calls: list[str] = []
    unsubscribe = lifecycle.on_shutdown(lambda: calls.append("host_child.request_stop"))
    lifecycle.on_shutdown(lambda: 1 / 0)  # 一个监听者出错不能拦住其它监听者 / 停机
    try:
        assert lifecycle.shutting_down() is False
        lifecycle.begin_shutdown()
        lifecycle.begin_shutdown()
        assert lifecycle.shutting_down() is True
        assert calls == ["host_child.request_stop"]
    finally:
        unsubscribe()


def test_sleep_unless_shutting_down_wakes_early() -> None:
    async def scenario() -> tuple[bool, float]:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, lifecycle.begin_shutdown)
        started = time.monotonic()
        result = await lifecycle.sleep_unless_shutting_down(5.0)
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert result is False
    assert elapsed < 1.0
    assert asyncio.run(lifecycle.sleep_unless_shutting_down(0.01)) is False  # 已在停机：立刻返回


def test_workflow_event_stream_ends_when_shutdown_begins() -> None:
    """浏览器挂着的 /api/v1/events 在停机信号后一个心跳内结束，不再等 uvicorn 超时取消。

    TestClient 会把流式响应整体缓冲到结束，观察不到"挂着"的状态，这里直接驱动 ASGI 应用。
    """

    service = WorkflowService(":memory:")
    app = create_workflow_app(service)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }

    async def scenario() -> tuple[list[bytes], float]:
        chunks: list[bytes] = []
        first_chunk = asyncio.Event()

        async def receive():
            await asyncio.sleep(3600)  # 浏览器一直挂着，不主动断开
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                first_chunk.set()

        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_chunk.wait(), 5.0)
        await asyncio.sleep(0.3)
        assert not task.done(), "没有停机信号时 SSE 不该结束"

        lifecycle.begin_shutdown()
        started = time.monotonic()
        await asyncio.wait_for(task, 3.0)
        return chunks, time.monotonic() - started

    try:
        chunks, elapsed = asyncio.run(scenario())
    finally:
        service.close()
    assert chunks[0].startswith(b"retry: 3000")
    assert elapsed < 1.5  # 一个心跳周期内收尾，远早于 uvicorn 的 5s 上限


def test_cancelled_asgi_task_report_collapses_to_one_debug_line(monkeypatch) -> None:
    """停机时 uvicorn 取消在途任务打出的 'Exception in ASGI application' 不是应用异常。"""

    emitted: list[tuple[str, str]] = []
    for name in ("debug", "info", "warning", "error", "critical", "trace"):
        monkeypatch.setattr(
            log_adapter, name, (lambda level: lambda msg, *a, **k: emitted.append((level, msg)))(name)
        )
    handler = log_adapter.UvicornToIlabosHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))

    try:
        raise asyncio.CancelledError("Task cancelled, timeout graceful shutdown exceeded")
    except asyncio.CancelledError:
        cancelled = logging.LogRecord(
            "uvicorn.error", logging.ERROR, "h11_impl.py", 421,
            "Exception in ASGI application", (), sys.exc_info(),
        )
    try:
        raise RuntimeError("real bug")
    except RuntimeError:
        genuine = logging.LogRecord(
            "uvicorn.error", logging.ERROR, "h11_impl.py", 421,
            "Exception in ASGI application", (), sys.exc_info(),
        )

    handler.emit(cancelled)
    handler.emit(genuine)
    assert emitted[0][0] == "debug"
    assert "Traceback" not in emitted[0][1] and "停机时被取消" in emitted[0][1]
    assert emitted[1][0] == "error"
    assert "Traceback" in emitted[1][1] and "real bug" in emitted[1][1]
