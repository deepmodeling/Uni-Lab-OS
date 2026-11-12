"""V1 ``/api/v1/history/*``（工作流与作业运行历史）契约；dev 事件存储移到 ``/api/v1/edge/history``。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from tests.api_envelope import error_of, ok_data
from tests.server.v1_lifecycle_support import Stack, wait_for


@pytest.fixture
def stack():
    value = Stack()
    yield value
    value.close()


def test_history_workflows_jobs_and_detail_follow_v1_shapes(stack: Stack) -> None:
    task = stack.create_task(["device-a", "device-b"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    stack.executor.finish(0)
    wait_for(lambda: len(stack.executor.dispatched) == 2)
    stack.executor.finish(1)
    wait_for(lambda: stack.task(task)["status"] == "succeeded")

    listing = ok_data(stack.client.get("/api/v1/history/workflows"))
    assert set(listing) == {"runs", "stats"}
    run = next(item for item in listing["runs"] if item["workflow_id"] == task["uuid"])
    assert set(run) == {
        "workflow_id", "task_id", "lab_id", "priority", "node_count", "state",
        "submitted_at", "started_at", "finished_at", "duration_s",
    }
    assert run["state"] == "success" and run["node_count"] == 2
    assert isinstance(run["submitted_at"], float)
    assert listing["stats"]["total_runs"] >= 1 and listing["stats"]["runs_by_state"]["success"] >= 1
    assert listing["stats"]["total_jobs"] >= 2

    filtered = ok_data(stack.client.get("/api/v1/history/workflows", params={"state": "failed"}))
    assert all(item["state"] == "failed" for item in filtered["runs"])
    with_spec = ok_data(stack.client.get("/api/v1/history/workflows", params={"with_spec": "true"}))
    assert "spec" in with_spec["runs"][0]

    detail = ok_data(stack.client.get(f"/api/v1/history/workflows/{task['uuid']}"))
    assert detail["workflow_id"] == task["uuid"] and isinstance(detail["spec"], dict)
    assert len(detail["jobs"]) == 2 and {job["state"] for job in detail["jobs"]} == {"success"}
    assert all(job["workflow_id"] == task["uuid"] for job in detail["jobs"])

    jobs = ok_data(stack.client.get("/api/v1/history/jobs", params={"workflow_id": task["uuid"], "limit": 1}))
    assert list(jobs) == ["jobs"] and len(jobs["jobs"]) == 1
    assert {"job_id", "workflow_id", "node_id", "device_id", "action_name", "state", "actual_s"} <= set(jobs["jobs"][0])


def test_history_detail_of_unknown_run_is_workflow_not_found(stack: Stack) -> None:
    error_of(stack.client.get("/api/v1/history/workflows/missing"), 3002)


def test_dev_event_store_moved_to_edge_history(tmp_path) -> None:
    from unilabos.server.api import install_server_apis
    from unilabos.server.composition import ServerServices
    from unilabos.server.database.layout import ServerDatabasePaths

    services = ServerServices.open(ServerDatabasePaths.resolve(tmp_path))
    app = FastAPI()
    install_server_apis(app, services)
    try:
        paths = set(app.openapi()["paths"])
        assert "/api/v1/edge/history/events" in paths
        assert "/api/v1/edge/history/payloads/{payload_uuid}" in paths
        # /api/v1/history/* 归 V1 运行历史，不再由 dev 事件存储占用
        assert not any(path.startswith("/api/v1/history/") for path in paths)
    finally:
        services.close()


def test_edge_proxy_forwards_only_the_moved_event_store() -> None:
    from unilabos.server.api.edge_proxy import EDGE_ROUTE_PREFIXES, create_edge_proxy_router

    assert "history" not in EDGE_ROUTE_PREFIXES and "edge/history" in EDGE_ROUTE_PREFIXES
    paths = {route.path for route in create_edge_proxy_router().routes}
    assert "/api/v1/edge/history/{path:path}" in paths
    assert not any(path.startswith("/api/v1/history") for path in paths)


def test_history_client_targets_edge_history() -> None:
    from unilabos.client.history import HTTPHistoryClient

    assert HTTPHistoryClient("http://h:8002").base_url == "http://h:8002/api/v1/edge/history"
    assert HTTPHistoryClient("http://h:8002/api/v1").base_url == "http://h:8002/api/v1/edge/history"
    assert HTTPHistoryClient("http://h/api/v1/edge/history").base_url == "http://h/api/v1/edge/history"
