"""过载退避须保持原请求，通知不能缩短期限，停止仍可立即退出。"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from tests.app.center_client.test_center_client import config
from tests.app.center_client.test_runtime import Center, Service, runtime
from tests.app.center_client.test_websocket import setup_receiver
from unilabos.app.center_client import transport
from unilabos.app.center_client.transport import CenterTransport, CenterTransportError, retry_after_seconds


@pytest.mark.parametrize(("value", "expected"), [
    (None, None), ("", None), ("invalid", None), ("-1", None), ("1.5", None),
    ("NaN", None), ("inf", None), ("1" * 129, None), ("0", 0.0), (" 60 ", 60.0),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 60.0), ("Wed, 21 Oct 2015 07:26:00 GMT", 0.0),
])
def test_retry_after_header_parsing(monkeypatch, value, expected):
    monkeypatch.setattr(transport, "time", SimpleNamespace(time=lambda: 1445412420.0))
    assert retry_after_seconds(value) == expected


@pytest.mark.parametrize("status", [429, 503])
def test_transport_does_not_send_again_before_retry_after(tmp_path, monkeypatch, status):
    now = [100.0]
    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: now[0]))
    response = requests.Response()
    response.status_code = status
    response._content = b'{"code":"OVERLOADED"}'
    response.headers["Retry-After"] = "60"
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs["json"]))
        return response

    client = CenterTransport(config(tmp_path))
    monkeypatch.setattr(client, "_session", lambda: SimpleNamespace(request=request))
    body = {"event_uuid": "persistent-identity", "value": 3}
    with pytest.raises(CenterTransportError) as first:
        client.request("POST", "/events", json_body=body)
    assert first.value.retry_after_seconds == 60
    now[0] += 59
    with pytest.raises(CenterTransportError) as waiting:
        client.request("POST", "/events", json_body=body)
    assert waiting.value.retry_after_seconds == 1
    assert len(calls) == 1
    now[0] += 1
    response.status_code, response._content = 202, b'{}'
    assert client.request("POST", "/events", json_body=body) == {}
    assert calls[1] == calls[0]


def test_http_runtime_notifications_cannot_shorten_backoff_but_stop_can(tmp_path, monkeypatch):
    run = runtime(tmp_path, Center(), Service())
    run.config = replace(run.config, websocket_enabled=False, poll_seconds=0.01, max_backoff_seconds=0.05)
    attempted = threading.Event()
    count = []

    def poll():
        count.append(1)
        attempted.set()
        raise CenterTransportError("过载", status_code=503, retryable=True, retry_after_seconds=60)

    monkeypatch.setattr(run, "poll_once", poll)
    try:
        run.start()
        assert attempted.wait(1)
        for _ in range(10):
            run._wake.set()
            time.sleep(0.01)
        assert len(count) == 1
        before = time.monotonic()
        run.stop()
        assert time.monotonic() - before < 1
    finally:
        run.close()


@pytest.mark.parametrize('retry_after', [None, 60])
def test_successful_reenrollment_wakes_local_backoff_but_preserves_retry_after(tmp_path, monkeypatch, retry_after):
    center = Center()
    run = runtime(tmp_path, center, Service())
    run.config = replace(run.config, websocket_enabled=False)
    first_attempt, resumed = threading.Event(), threading.Event()
    monkeypatch.setattr(center, 'reload_credentials', lambda config: None, raising=False)

    def poll():
        if not first_attempt.is_set():
            first_attempt.set()
            raise CenterTransportError('旧凭据失效', retryable=True, retry_after_seconds=retry_after)
        resumed.set()

    monkeypatch.setattr(run, 'poll_once', poll)
    try:
        run.start()
        assert first_attempt.wait(1)
        run.reload_credentials(run.config, recover_control=True)
        assert resumed.wait(0.5) is (retry_after is None)
    finally:
        run.close()


def test_http_runtime_exponential_delay_resets_after_success(tmp_path, monkeypatch):
    from unilabos.app.center_client import runtime as runtime_module

    run = runtime(tmp_path, Center(), Service())
    now, pauses, attempts = [0.0], [], []
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(runtime_module.random, "uniform", lambda _low, _high: 1.0)

    def wait(seconds):
        pauses.append(seconds)
        now[0] += seconds
        return False

    def poll():
        attempts.append(1)
        if len(attempts) in {7, 9}:
            if len(attempts) == 9:
                run._stop.set()
            return
        raise CenterTransportError("断网", retryable=True)

    monkeypatch.setattr(run._retry_wake, "wait", wait)
    monkeypatch.setattr(run._wake, "wait", lambda seconds: None)
    monkeypatch.setattr(run, "poll_once", poll)
    try:
        run._run()
        assert pauses == [1, 2, 4, 8, 16, 30, 1]
    finally:
        run.close()


@pytest.mark.parametrize("status", [429, 503])
def test_wss_handshake_respects_retry_after(tmp_path, monkeypatch, status):
    _server, _client, store, receiver = setup_receiver(tmp_path)
    pauses = []

    async def connect(_session):
        raise InvalidStatus(Response(status, "Busy", Headers({"Retry-After": "60"})))

    async def sleep(seconds):
        pauses.append(seconds)
        receiver._stop.set()

    monkeypatch.setattr(receiver, "_connect", connect)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    try:
        asyncio.run(receiver._run_async())
        assert pauses == [60]
    finally:
        store.close()
