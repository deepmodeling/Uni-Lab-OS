"""Edge scheduler REST 面冒烟测试（FastAPI TestClient）。"""

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from unilabos.app.scheduler.api import create_app  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(create_app())


def _workflow_body(workflow_id: str = "wf-api") -> dict:
    return {
        "workflow_id": workflow_id,
        "nodes": [
            {"id": "A", "device_id": "d1", "action_name": "run", "action_type": "goal"},
            {"id": "B", "device_id": "d1", "action_name": "run", "action_type": "goal"},
        ],
        "edges": [{"source_node_id": "A", "target_node_id": "B"}],
    }


def test_health(client):
    assert client.get("/api/v1/health").json() == {"status": "ok", "scheduler": "ready"}


def test_submit_and_finish_flow(client):
    r = client.post("/api/v1/workflows", json=_workflow_body())
    assert r.status_code == 200
    dispatched = r.json()["dispatched"]
    assert [d["node_id"] for d in dispatched] == ["A"]

    job_id = dispatched[0]["job_id"]
    r2 = client.post(f"/api/v1/jobs/{job_id}/finish", json={"success": True, "ret_value": {}})
    assert [d["node_id"] for d in r2.json()["dispatched"]] == ["B"]

    snap = client.get("/api/v1/workflows/wf-api").json()
    assert snap["nodes"]["A"]["state"] == "success"
    assert snap["nodes"]["B"]["state"] == "dispatched"


def test_duplicate_submit_409(client):
    assert client.post("/api/v1/workflows", json=_workflow_body("dup")).status_code == 200
    assert client.post("/api/v1/workflows", json=_workflow_body("dup")).status_code == 409


def test_cycle_422(client):
    body = {
        "workflow_id": "wf-cycle",
        "nodes": [
            {"id": "A", "device_id": "d", "action_name": "a", "action_type": "goal"},
            {"id": "B", "device_id": "d", "action_name": "a", "action_type": "goal"},
        ],
        "edges": [
            {"source_node_id": "A", "target_node_id": "B"},
            {"source_node_id": "B", "target_node_id": "A"},
        ],
    }
    assert client.post("/api/v1/workflows", json=body).status_code == 422


def test_workflow_not_found_404(client):
    assert client.get("/api/v1/workflows/ghost").status_code == 404


def test_manual_reschedule(client):
    assert client.post("/api/v1/reschedule").json() == {"dispatched": []}
