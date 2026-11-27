import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.web.run_artifacts import router
from unilabos.config.config import BasicConfig
from unilabos.utils.run_artifacts import RunArtifactStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    return RunArtifactStore.configured()


def archive(store, **overrides):
    return store.archive(
        "报告.pdf",
        b"%PDF-1.4 test",
        media_type="application/pdf",
        kind="report",
        source="smb://host/Reports/a.pdf",
        **({"task_uuid": str(uuid.uuid4()), "job_uuid": str(uuid.uuid4())} | overrides),
    )


def test_per_run_copy_survives_restart_and_cleanup_is_isolated(store):
    first, second = archive(store), archive(store)
    assert first["id"] != second["id"]
    reopened = RunArtifactStore(store.root)
    assert reopened.read(first["id"])[1] == b"%PDF-1.4 test"
    assert reopened.list(first["task_uuid"]) == [first]
    reopened.delete(first["id"])
    with pytest.raises(FileNotFoundError):
        reopened.read(first["id"])
    assert reopened.list(first["task_uuid"])[0]["deleted_at"]
    assert reopened.read(second["id"])[1] == b"%PDF-1.4 test"


def test_same_node_reuses_identical_copy_and_detects_corruption(store):
    first = archive(store)
    assert archive(store, task_uuid=first["task_uuid"], job_uuid=first["job_uuid"]) == first
    (store.root / first["task_uuid"] / first["job_uuid"] / first["id"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="校验"):
        store.read(first["id"])
    with pytest.raises(ValueError):
        store.metadata("../../other")


def test_api_pdf_preview_and_cleanup_requires_terminal_task(store, monkeypatch):
    item = archive(store)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    eligible = False
    monkeypatch.setattr(
        "unilabos.workflow.composition.get_workflow_service",
        lambda: SimpleNamespace(get_workflow_task_delete_eligibility=lambda _: {"can_delete": eligible}),
    )
    with TestClient(app) as client:
        response = client.get(item["content_url"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert (
            client.get("/api/v1/run-artifacts", params={"task_uuid": item["task_uuid"]}).json()["data"]["size_bytes"]
            == item["size_bytes"]
        )
        endpoint = "/api/v1/run-artifacts/" + item["id"]
        assert client.delete(endpoint).status_code == 409
        eligible = True
        assert client.delete(endpoint).status_code == 200
        assert client.get(item["content_url"]).status_code == 404
        assert client.delete(endpoint).status_code == 200


def test_workspace_backend_previews_edge_files_across_generations(tmp_path, monkeypatch):
    workbench = tmp_path / "runtime" / "workbench"
    edge = workbench / "edge" / str(uuid.uuid4())
    backend = workbench / "backend" / "local-domain"
    legacy = RunArtifactStore(edge / "run_artifacts")
    old = archive(legacy)
    monkeypatch.setattr(BasicConfig, "working_dir", str(edge))
    current = archive(RunArtifactStore.configured(), task_uuid=old["task_uuid"])
    monkeypatch.setattr(BasicConfig, "working_dir", str(backend))
    store = RunArtifactStore.configured()
    assert {item["id"] for item in store.list(old["task_uuid"])} == {old["id"], current["id"]}
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(
        "unilabos.workflow.composition.get_workflow_service",
        lambda: SimpleNamespace(get_workflow_task_delete_eligibility=lambda _: {"can_delete": True}),
    )
    with TestClient(app) as client:
        for item in (old, current):
            response = client.get(item["content_url"])
            assert response.status_code == 200
            assert response.content == b"%PDF-1.4 test"
            assert response.headers["content-disposition"].startswith("inline;")
        assert client.delete("/api/v1/run-artifacts/" + old["id"]).status_code == 200
        assert client.get(old["content_url"]).status_code == 404
        assert client.get(current["content_url"]).status_code == 200
    monkeypatch.setattr(BasicConfig, "working_dir", str(workbench / "edge" / str(uuid.uuid4())))
    assert RunArtifactStore.configured().read(current["id"])[1] == b"%PDF-1.4 test"


def test_concurrent_retries_publish_one_artifact(store):
    task, job = str(uuid.uuid4()), str(uuid.uuid4())
    ready = Barrier(4)

    def submit(_):
        ready.wait(timeout=10)
        return archive(store, task_uuid=task, job_uuid=job)["id"]

    with ThreadPoolExecutor(max_workers=4) as workers:
        ids = list(workers.map(submit, range(4)))
    assert len(set(ids)) == 1
    assert [item["id"] for item in store.list(task)] == [ids[0]]
