"""GET /api/v1/ping：HTTP 版 ping-pong，供链路时延 / 时钟偏差诊断（host_node.test_latency 与前端）。"""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.runtime.diagnostics import create_backend_router


def _client(scheduler=None) -> TestClient:
    app = FastAPI()
    app.include_router(create_backend_router(lambda: scheduler, lambda: None))
    return TestClient(app)


def test_ping_echoes_client_timestamp_with_server_clock() -> None:
    client = _client()
    before = time.time()
    body = ok_data(client.get("/api/v1/ping", params={"client_timestamp": 1.5}))
    after = time.time()
    assert body["client_timestamp"] == 1.5
    assert before <= body["server_timestamp"] <= after
    assert body["scheduler"] == "remote"


def test_ping_without_client_timestamp_and_local_scheduler() -> None:
    body = ok_data(_client(scheduler=object()).get("/api/v1/ping"))
    assert body["client_timestamp"] is None
    assert isinstance(body["server_timestamp"], float)
    assert body["scheduler"] == "local"
