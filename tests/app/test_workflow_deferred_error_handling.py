"""错误处置路由与延迟发布的产品运行时之间的 HTTP 合同。"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.web import server
from unilabos.app.workflow_api import create_workflow_router
from unilabos.config.config import BasicConfig
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.store import WorkflowStore


def test_routes_mount_before_runtime_and_read_published_station(tmp_path, monkeypatch):
    """冷启动挂载不访问运行时；发布前返回 503，发布后原路由直接可用。"""
    monkeypatch.setattr(server, "_workflow_runtime_values", {})
    monkeypatch.setattr(server, "workflow_runtime_phase", "mounting_routes")
    monkeypatch.setattr(server, "workflow_runtime_error", None)
    monkeypatch.setattr(server, "workflow_runtime_catalog_required", False)
    monkeypatch.setattr(BasicConfig, "control_plane", "local")
    application = FastAPI()
    application.add_exception_handler(
        server.WorkflowRuntimeStarting, server.workflow_runtime_starting_handler
    )
    application.include_router(
        create_workflow_router(
            server._DeferredWorkflowRuntimePort("service"),
            wait_until_ready=server._wait_for_workflow_runtime_ready,
        )
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    service = SimpleNamespace(
        error_handling=SimpleNamespace(control=StationExecutionControl(store)),
        prune_frontend_events=store.prune_frontend_events,
    )
    try:
        with TestClient(application) as client:
            assert client.get("/api/v1/stations/local/error-handling").status_code == 503
            server._publish_workflow_runtime(
                service=service, template_projection=None,
            )
            response = client.get("/api/v1/stations/local/error-handling")
            assert response.status_code == 200
            assert response.json()["data"]["mode"] == "RUNNING"
            assert (
                client.get("/api/v1/stations/other/error-handling").status_code == 404
            )
    finally:
        store.close()
