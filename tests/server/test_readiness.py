"""GET /api/v1/readiness：能派发任务才 200，否则 503。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.api.runtime.diagnostics import create_backend_router


class _Backend:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def host_ready(self) -> bool:
        return self.ready


def _client(scheduler, backend) -> TestClient:
    app = FastAPI()
    app.include_router(create_backend_router(lambda: scheduler, lambda: backend))
    return TestClient(app)


def test_ready_when_the_execution_adapter_is_registered() -> None:
    response = _client(object(), _Backend(True)).get("/api/v1/readiness")
    assert response.status_code == 200
    # V1 合同字段 + dev 探针摘要（扩展字段）；不包信封
    assert response.json() == {
        "status": "ready",
        "capabilities": {
            "can_edit_definitions": True,
            "can_view_drafts": True,
            "can_run_unpublished": True,
            "can_debug": True,
            "can_execute_device_actions": True,
        },
        "phase": "ready",
        "workflowRuntime": "disabled",
        "scheduler": "local",
        "execution": "ready",
        "reasons": [],
    }


def test_not_ready_while_devices_are_still_initializing() -> None:
    backend = _Backend(False)
    client = _client(object(), backend)
    response = client.get("/api/v1/readiness")
    assert response.status_code == 503
    # V1 没有 not_ready：未就绪统一为 starting，dev 的执行面状态与原因保留在扩展字段
    assert response.json()["status"] == "starting" and response.json()["phase"] == "execution_starting"
    assert response.json()["execution"] == "starting"
    assert response.json()["reasons"]
    # 存活探针不受影响
    assert client.get("/api/v1/health").status_code == 200

    backend.ready = True
    assert client.get("/api/v1/readiness").status_code == 200


def test_split_topology_follows_the_host_control_connection(monkeypatch) -> None:
    import unilabos.server.api.edge_proxy as edge_proxy
    import unilabos.server.backend.edge_control as edge_control

    class _Service:
        connected = False

    service = _Service()
    monkeypatch.setattr(edge_proxy, "edge_proxy_enabled", lambda: True)
    monkeypatch.setattr(edge_control, "get_edge_control_service", lambda: service)
    client = _client(object(), None)

    response = client.get("/api/v1/readiness")
    assert response.status_code == 503
    assert response.json()["execution"] == "restarting" and response.json()["phase"] == "host_restarting"

    service.connected = True
    assert client.get("/api/v1/readiness").status_code == 200


def test_pure_scheduler_authority_is_ready(monkeypatch) -> None:
    import unilabos.server.api.edge_proxy as edge_proxy

    monkeypatch.setattr(edge_proxy, "edge_proxy_enabled", lambda: False)
    response = _client(object(), None).get("/api/v1/readiness")
    assert response.status_code == 200
    assert response.json()["execution"] == "disabled"
