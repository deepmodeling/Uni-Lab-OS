"""SDK watch 经 SSE 订阅：帧解析、真实连接器、stream.reset 与弃用参数。"""

from __future__ import annotations

import json
import threading
import time
import warnings
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from unilabos.client.runtime.workflow import (
    HTTPWorkflowClient,
    WorkflowClientError,
    _SSENoticeStream,
    _iter_text_lines,
    _parse_sse_lines,
)


def test_sse_lines_split_crlf_across_chunks_without_extra_blank_lines() -> None:
    chunks = ["id: 1\r", "\nevent: a\r\ndata: {}\r", "\n\r", "\n", "data: x\rdata: y\r\r", "\n"]
    assert list(_iter_text_lines(chunks)) == ["id: 1", "event: a", "data: {}", "", "data: x", "data: y", ""]


def test_sse_parser_keeps_watermark_frames_and_does_not_inherit_ids() -> None:
    lines = [
        "retry: 3000", ": connected", "",
        "id: 7", "",
        "id: 8", "event: workflow.task.changed", 'data: {"workflow_task_uuid":', "data: \"t\"}", "",
        "event: runtime.logs.changed", 'data: {"source_ids":["host"]}', "",
    ]
    assert list(_parse_sse_lines(lines)) == [
        {"event": "message", "id": "7", "data": None},
        {"event": "workflow.task.changed", "id": "8", "data": '{"workflow_task_uuid":\n"t"}'},
        {"event": "runtime.logs.changed", "id": None, "data": '{"source_ids":["host"]}'},
    ]


class _ScriptedSSE(BaseHTTPRequestHandler):
    script: list[bytes] = []
    content_type = "text/event-stream"
    seen_headers: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        type(self).seen_headers.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.end_headers()
        for chunk in self.script:
            self.wfile.write(chunk)
            self.wfile.flush()
            time.sleep(0.05)
        time.sleep(2)  # 保持连接，验证 recv 超时而不是读到 EOF

    def log_message(self, *_args: Any) -> None:
        return None


@pytest.fixture
def sse_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedSSE)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _ScriptedSSE.seen_headers = []
    yield f"http://127.0.0.1:{server.server_address[1]}/api/v1/events"
    server.shutdown()
    server.server_close()


def test_real_sse_stream_delivers_notices_and_times_out(sse_server: str) -> None:
    _ScriptedSSE.content_type = "text/event-stream"
    _ScriptedSSE.script = [
        b"retry: 3000\n: connected\n\n",
        b"id: 5\n\n",
        b'id: 6\nevent: workflow.task.changed\ndata: {"workflow_task_uuid":"t"}\n\n',
    ]
    with _SSENoticeStream(sse_server, {"Accept": "text/event-stream", "Last-Event-ID": "4"}, 5) as stream:
        assert json.loads(stream.recv(timeout=2)) == {"action": "message", "id": 5, "data": {}}
        assert json.loads(stream.recv(timeout=2)) == {
            "action": "workflow.task.changed", "id": 6, "data": {"workflow_task_uuid": "t"},
        }
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            stream.recv(timeout=0.3)
        assert time.monotonic() - started < 1.5
    assert _ScriptedSSE.seen_headers[0]["Last-Event-ID"] == "4"


def test_real_sse_stream_rejects_json_error_envelope(sse_server: str) -> None:
    _ScriptedSSE.content_type = "application/json"
    _ScriptedSSE.script = [b'{"code":1000,"error":{"msg":"invalid_input"}}']
    try:
        with pytest.raises(WorkflowClientError) as caught:
            with _SSENoticeStream(sse_server, {}, 5):
                pass
    finally:
        _ScriptedSSE.content_type = "text/event-stream"
    assert caught.value.code == "watch_rejected"
    assert "1000" in caught.value.details


class _Tasks:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.reads = 0

    def get(self, path: str, **_kwargs: Any) -> Any:
        if path.endswith("/jobs"):
            return []
        status = self.statuses[min(self.reads, len(self.statuses) - 1)]
        self.reads += 1
        return {"uuid": "task-1", "status": status}


class _Notices(AbstractContextManager):
    def __init__(self, notices: list[dict[str, Any]]) -> None:
        self.notices = iter(json.dumps(notice) for notice in notices)

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def recv(self, timeout: float | None = None) -> str:
        del timeout
        try:
            return next(self.notices)
        except StopIteration:
            raise TimeoutError from None


def test_watch_follows_stream_reset_and_skips_unchanged_snapshots() -> None:
    # 首次快照、连接后补读、无变化通知、reset 后读到终态。
    http = _Tasks(["running", "running", "running", "succeeded"])
    notices = [
        {"action": "message", "id": 40, "data": {}},
        {"action": "workflow.node_job.changed", "id": 41, "data": {"workflow_node_job_uuid": "j"}},
        {"action": "workflow.task.changed", "id": 42, "data": {"workflow_task_uuid": "other"}},
        {"action": "stream.reset", "id": 3, "data": {"reason": "cursor_ahead"}},
    ]
    client = HTTPWorkflowClient("http://edge:8002", http_client=http, notice_connector=lambda *_: _Notices(notices))
    events = list(client.watch_task("task-1", timeout=1))

    assert [(event["kind"], event["task"]["status"], event["cursor"]) for event in events] == [
        ("task_snapshot", "running", 0),
        ("task_changed", "succeeded", 3),
    ]


def test_schedule_addr_is_deprecated_and_not_used_for_watch() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client = HTTPWorkflowClient("http://edge:8002", schedule_addr="ws://edge:8002/api/v1/ws/schedule", http_client=object())
    assert any(issubclass(item.category, DeprecationWarning) for item in caught)
    assert client.notification_url == "http://edge:8002/api/v1/events"
