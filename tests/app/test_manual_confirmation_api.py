"""Edge 人工确认 HTTP 与持久 SSE 契约。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.scheduler_core.conftest import build_core_runtime
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService


def test_manual_confirmation_uses_job_uuid_strict_body_and_specialized_events(
    tmp_path: Path,
) -> None:
    """新接口只收 action；读模型携带确认状态，SSE 只发轻量失效身份。"""

    runtime = build_core_runtime(tmp_path / "runtime")
    try:
        pending, job_uuid = runtime.submit_manual(task_name="manual-api")
        service = WorkflowService(
            runtime.workflow_store,
            task_scheduler_bridge=runtime.bridge,
        )
        client = TestClient(create_workflow_app(service))
        try:
            before = service.list_events(after_sequence=0, limit=100)["items"]
            required = [
                item
                for item in before
                if item["event"] == "manual_confirmation.required"
            ]
            assert len(required) == 1
            assert required[0]["data"] == {
                "task_uuid": pending["task"]["uuid"],
                "job_uuid": job_uuid,
            }

            invalid = client.post(
                f"/api/v1/workflow-node-jobs/{job_uuid}/manual-confirmation",
                json={"action": "approve", "param": {"temperature": 30}},
            )
            assert invalid.status_code == 400
            assert invalid.json()["code"] == 1000
            assert "请求参数不符合接口要求" in invalid.json()["error"]["msg"]
            assert runtime.dispatcher.dispatched == []

            missing = client.post(
                f"/api/v1/workflow-node-jobs/{uuid4()}/manual-confirmation",
                json={"action": "approve"},
            )
            assert missing.status_code == 404
            assert missing.json()["code"] == 3002
            assert "资源不存在" in missing.json()["error"]["msg"]

            response = client.post(
                f"/api/v1/workflow-node-jobs/{job_uuid}/manual-confirmation",
                json={"action": "approve"},
            )
            assert response.status_code == 200
            aggregate = response.json()["data"]
            job = next(item for item in aggregate["jobs"] if item["uuid"] == job_uuid)
            assert job["manual_confirmation"]["status"] == "approved"
            assert job["executor_kind"] == "device_action"

            after = service.list_events(after_sequence=0, limit=100)["items"]
            resolved = [
                item
                for item in after
                if item["event"] == "manual_confirmation.resolved"
            ]
            assert len(resolved) == 1
            assert resolved[0]["data"] == {
                "task_uuid": pending["task"]["uuid"],
                "job_uuid": job_uuid,
            }
        finally:
            client.close()
    finally:
        runtime.close()

def test_manual_confirmation_conflicts_are_http_409(tmp_path: Path) -> None:
    """普通 Job 与相反决定都明确返回 HTTP 409。"""

    runtime = build_core_runtime(tmp_path / "runtime-conflict")
    try:
        _normal = runtime.submit(task_name="ordinary", devices=["reactor-b"])
        normal_job_uuid = _normal["jobs"][0]["uuid"]
        _pending, manual_job_uuid = runtime.submit_manual(
            task_name="manual-conflict",
            device_id="reactor-a",
        )
        client = TestClient(
            create_workflow_app(
                WorkflowService(
                    runtime.workflow_store,
                    task_scheduler_bridge=runtime.bridge,
                )
            )
        )
        try:
            ordinary = client.post(
                f"/api/v1/workflow-node-jobs/{normal_job_uuid}/manual-confirmation",
                json={"action": "approve"},
            )
            assert ordinary.status_code == 409
            assert ordinary.json()["code"] == 3003

            rejected = client.post(
                f"/api/v1/workflow-node-jobs/{manual_job_uuid}/manual-confirmation",
                json={"action": "reject"},
            )
            assert rejected.status_code == 200
            late = client.post(
                f"/api/v1/workflow-node-jobs/{manual_job_uuid}/manual-confirmation",
                json={"action": "approve"},
            )
            assert late.status_code == 409
            assert late.json()["code"] == 3003
        finally:
            client.close()
    finally:
        runtime.close()
