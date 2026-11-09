"""生命周期 SDK：地址、token、超时、不重试管理命令和无启动副作用。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from unilabos.client.runtime import HTTPLifecycleClient, SchedulerDrainTimeout
from unilabos.config.config import BasicConfig
from unilabos.protocol.runtime.lifecycle import RestartRequest, ResetRequest, SchedulerDrainStatus
from unilabos.server.api.app import ControlTokenMiddleware
from unilabos.server.api.runtime.diagnostics import create_backend_app
from unilabos.server.backend.scheduler.service import BackendScheduler


def _status(*, active=True):
    return {
        "phase": "draining" if active else "drained",
        "active_device_job_count": 1 if active else 0,
        "active_device_job_ids": ["job-1"] if active else [],
        "requested": True,
        "dispatch_paused": True,
        "pause_reasons": ["drain"],
    }


def _mock_http(client, handler):
    config = client._http.config
    client._http._client.close()
    client._http._client = httpx.Client(base_url=config.base_url, transport=httpx.MockTransport(handler))


def test_sdk_roundtrip_reuses_authority_token_and_handles_not_ready(monkeypatch):
    monkeypatch.setattr(BasicConfig, "control_token", "test-sdk-token")
    scheduler = BackendScheduler(MagicMock(), MagicMock())
    app = create_backend_app(lambda: scheduler, lambda: SimpleNamespace())
    app.add_middleware(ControlTokenMiddleware)
    with HTTPLifecycleClient("http://testserver/api/v1") as client:
        client._http._client.close()
        client._http._client = TestClient(app, base_url="http://testserver/api/v1")
        assert client.health().status == "ok"
        assert client.readiness().status == "ready"
        assert client.drain_status().phase == "running"
        assert client.begin_drain().phase == "drained"
        assert client.readiness().status == "not_ready"
        assert client.resume_from_drain().phase == "running"


def test_offline_query_never_starts_a_process_or_retries(monkeypatch):
    requests = []
    popen = MagicMock(side_effect=AssertionError("只读查询不能启动进程"))
    monkeypatch.setattr("subprocess.Popen", popen)

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError("offline", request=request)

    with HTTPLifecycleClient("http://localhost:8002") as client:
        _mock_http(client, handler)
        with pytest.raises(httpx.ConnectError):
            client.health()
    assert len(requests) == 1
    popen.assert_not_called()


def test_drain_wait_polls_until_idle():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_status(active=len(requests) < 3))

    with HTTPLifecycleClient("http://localhost:8002") as client:
        _mock_http(client, handler)
        assert client.drain_and_wait(timeout=1, poll_interval=0.001).phase == "drained"
    assert [request.method for request in requests] == ["POST", "GET", "GET"]
    assert {request.url.path for request in requests} == {"/api/v1/scheduler/drain"}


def test_drain_timeout_keeps_pause_and_does_not_stop_runtime():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_status())

    with HTTPLifecycleClient("http://localhost:8002") as client:
        _mock_http(client, handler)
        with pytest.raises(SchedulerDrainTimeout) as exc:
            client.drain_and_wait(timeout=0.02, poll_interval=0.03)
        assert exc.value.status.active_device_job_ids == ["job-1"]
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/scheduler/drain"


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"timeout": float("inf")}, {"timeout": 1, "poll_interval": 0}])
def test_invalid_wait_budget_does_not_issue_a_command(kwargs):
    with HTTPLifecycleClient("http://localhost:8002") as client:
        calls = MagicMock()
        _mock_http(client, calls)
        with pytest.raises(ValueError):
            client.drain_and_wait(**kwargs)
        calls.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("active_device_job_count", True),
    ("active_device_job_count", 0),
    ("phase", "drained"),
])
def test_invalid_drain_status_is_not_accepted_as_safe(field, value):
    payload = _status()
    payload[field] = value
    with pytest.raises(ValidationError):
        SchedulerDrainStatus.model_validate(payload)


@pytest.mark.parametrize("operation", ["restart", "reset"])
def test_destructive_management_request_is_not_replayed_after_error(operation):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"detail": "response unavailable"})

    with HTTPLifecycleClient("http://localhost:8002") as client:
        _mock_http(client, handler)
        with pytest.raises(httpx.HTTPStatusError):
            if operation == "restart":
                client.request_restart(RestartRequest())
            else:
                client.request_reset(ResetRequest(confirmation_token="test", confirmation="清空全部数据"))
    assert len(requests) == 1
