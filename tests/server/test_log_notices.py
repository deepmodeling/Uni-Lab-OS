"""日志轻通知：空闲静默、突发合并、来源鉴别、SSE 游标及 WS 快速通道。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from unilabos.protocol.runtime.logs import RuntimeLogNotice
from unilabos.server.api.runtime import events
from unilabos.utils import log_notices as notices


def test_hub_coalesces_off_writer_thread_and_stays_idle():
    hub = notices.LogNoticeHub(batch_seconds=.03)
    called = threading.Event()
    received = []
    writer = threading.get_ident()

    def listen(notice):
        received.append((notice, threading.get_ident()))
        called.set()

    release = hub.subscribe(listen)
    try:
        assert not called.wait(.06)  # 没有日志时不主动查询/广播。
        for _ in range(100):
            hub.changed("host")
            hub.changed("slave:a", sources_changed=True)
        assert called.wait(1)
        assert len(received) == 1
        assert received[0][0].source_ids == ["host", "slave:a"]
        assert received[0][0].sources_changed
        assert received[0][1] != writer
        called.clear()
        assert not called.wait(.06)
        release()
        hub.changed("host")
        assert not called.wait(.06)
    finally:
        release()
        hub.close()


def test_notice_size_is_bounded_and_contains_no_body():
    merged = notices.merge_notices(
        RuntimeLogNotice(source_ids=[f"slave:{i}" for i in range(256)]),
        RuntimeLogNotice(source_ids=["host"], sources_changed=True),
    )
    assert merged.all_sources and merged.sources_changed and not merged.source_ids
    for invalid in ({"source_ids": ["a" * 513]}, {"lines": ["secret"]}, {"source_ids": ["x"] * 257}):
        with pytest.raises(ValidationError):
            RuntimeLogNotice.model_validate(invalid)


def test_log_append_ignores_observer_transport_and_not_normal_driver_errors(monkeypatch):
    hub = Mock()
    monkeypatch.setattr(notices, "log_notices", hub)
    handler = notices.LogAppendHandler()
    for name, message in [
        ("uvicorn.access", "GET /anything 200"), ("httpx", "HTTP request"),
        ("unilabos.utils.log.server", "process.log.read failed"),
        ("control", "runtime_logs_changed failed"),
    ]:
        handler.emit(logging.LogRecord(name, logging.INFO, "", 0, message, (), None))
    hub.changed.assert_not_called()
    recursive = logging.LogRecord("control", logging.ERROR, "", 0, "通知发送队列已满", (), None)
    recursive.threadName = "RuntimeLogNotices"
    handler.emit(recursive)
    hub.changed.assert_not_called()
    handler.emit(logging.LogRecord("driver", logging.ERROR, "", 0, "泵异常：%s", ("断连",), None))
    hub.changed.assert_called_once_with("host")
    monkeypatch.setattr(notices, "_local_source", None)
    handler.emit(logging.LogRecord("backend", logging.INFO, "", 0, "权威日志不是 Host 日志", (), None))
    assert hub.changed.call_count == 1


def test_hostlink_notice_uses_registered_connection_identity(monkeypatch):
    from unilabos.backend.hostlink.server import HostLinkServer

    hub = Mock()
    monkeypatch.setattr(notices, "log_notices", hub)
    server = HostLinkServer("127.0.0.1", 0)
    peer = {"node_id": "slave-a", "machine_name": "slave-a"}
    assert server._handle_log_changed({}, peer) == {"accepted": True}
    hub.changed.assert_called_once_with("slave:slave-a")
    with pytest.raises(ValueError):
        server._handle_log_changed({"source_id": "host"}, peer)
    with pytest.raises(ValueError):
        server._handle_log_changed({}, {})


def test_slave_sends_empty_notice_only_for_own_log():
    from unilabos.backend.hostlink.client import HostLinkClient
    from unilabos.backend.hostlink.protocol import ActionType

    client = HostLinkClient("127.0.0.1", 1, machine_name="test")
    # 不启动网络；仅验证本进程 -> 已有连接的通知边界。
    client._online.set()
    client.request = Mock()
    try:
        client._notify_log_append(RuntimeLogNotice(source_ids=["slave:other"]))
        client.request.assert_not_called()
        client._notify_log_append(RuntimeLogNotice(source_ids=["host"]))
        client.request.assert_called_once_with(ActionType.LOG_CHANGED, {}, timeout=1)
    finally:
        client.close()


def test_edge_control_notice_is_ephemeral_and_resyncs_on_attach_detach(monkeypatch):
    from unilabos.server.backend.edge_control import EdgeControlService

    hub = Mock()
    monkeypatch.setattr(notices, "log_notices", hub)
    service = EdgeControlService(payload_client=Mock())
    epoch, _ = service.attach_connection()
    hub.changed.assert_called_with(sources_changed=True, all_sources=True)
    assert service.handle_message("runtime_logs_changed", {"source_ids": ["slave:a"]}) is None
    hub.publish.assert_called_once_with(RuntimeLogNotice(source_ids=["slave:a"]))
    assert service.outgoing.empty() and service.active_job_ids() == []
    service.detach_connection(epoch)
    assert hub.changed.call_count == 2


def test_sse_notice_merges_slow_reader_without_changing_workflow_cursor(monkeypatch):
    class Hub:
        listener = None

        def subscribe(self, listener):
            self.listener = listener
            return lambda: setattr(self, "listener", None)

    hub = Hub()
    monkeypatch.setattr(events, "log_notices", hub)
    monkeypatch.setattr(events, "shutting_down", lambda: False)

    class Request:
        async def is_disconnected(self):
            return False

    workflow = Mock()
    workflow.list_events.return_value = {"items": [{"id": 42, "event": "workflow.task.changed", "data": {"uuid": "t"}}]}
    workflow.latest_event_sequence.return_value = 42

    async def scenario():
        stream = events.runtime_event_stream(Request(), workflow, cursor=41)
        assert "connected" in await anext(stream)
        assert (await anext(stream)).startswith("id: 42\n")
        # 慢读者停在 yield 期间，重复通知只保留合并后的一个槽位。
        for _ in range(100):
            hub.listener(RuntimeLogNotice(source_ids=["host"]))
            hub.listener(RuntimeLogNotice(source_ids=["slave:a"], sources_changed=True))
        await asyncio.sleep(0)
        frame = await asyncio.wait_for(anext(stream), 1)
        assert "event: runtime.logs.changed\n" in frame and "id:" not in frame
        payload = json.loads(frame.split("data: ")[1])
        assert payload["source_ids"] == ["host", "slave:a"] and payload["sources_changed"]
        workflow.list_events.assert_called_once_with(after_id=41, limit=100)
        await stream.aclose()
        assert hub.listener is None

    asyncio.run(scenario())
