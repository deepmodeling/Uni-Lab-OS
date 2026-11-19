"""单点设备动作任务（execution_kind=ad_hoc_device_action）契约测试。

微前端设备页/画布的单点动作通过 POST /api/v1/workflow-tasks 直接创建
单 job 任务，复用整图任务的调度、历史与异常链路。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.server.backend.scheduler.authority import SchedulerAuthorityProfile
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.services.runtime.workflow.service import (
    WorkflowConflict,
    WorkflowError,
    WorkflowService,
)


class _ExecutorStub:
    def add_job_finished_listener(self, listener: Any) -> None:  # noqa: D401
        self.listener = listener


@pytest.fixture()
def service() -> WorkflowService:
    instance = WorkflowService(":memory:")
    try:
        yield instance
    finally:
        instance.close()


def _create(service: WorkflowService, **overrides: Any) -> dict:
    payload: dict[str, Any] = {
        "device_id": "host_node",
        "action_name": "transfer_resource",
        "action_type": "UniLabJsonCommand",
        "param": {"resource": {"uuid": "m-1"}, "site": "site-1"},
        "execution_policy": {"always_free": True},
        "description": "单点转移",
        "meta_data": {},
    }
    payload.update(overrides)
    return service.create_ad_hoc_device_action_task(**payload)


def test_create_ad_hoc_task_persists_task_and_single_job(
    service: WorkflowService,
) -> None:
    submitted: list[str] = []
    service.set_task_submitter(submitted.append)
    task = _create(service)

    assert task["execution_kind"] == "ad_hoc_device_action"
    assert task["workflow_uuid"] is None
    assert task["status"] == "pending"
    assert task["run_mode"] == "normal"
    assert task["idempotency_key"]
    assert task["request_fingerprint"].startswith("sha256:")
    assert submitted == [task["uuid"]]

    # snapshot/plan 形状与调度器 _build_dag 消费面对齐
    snapshot_nodes = task["workflow_snapshot"]["nodes"]
    plan_nodes = task["execution_plan"]["nodes"]
    assert len(snapshot_nodes) == len(plan_nodes) == 1
    assert snapshot_nodes[0]["action_name"] == "transfer_resource"
    assert snapshot_nodes[0]["meta_data"]["target_device_id"] == "host_node"
    assert plan_nodes[0]["param"] == {
        "resource": {"uuid": "m-1"},
        "site": "site-1",
    }

    # 单点任务 = 一个节点运行 + attempt 1
    (run,) = service.list_workflow_node_runs(task["uuid"])
    assert run["executor_kind"] == "device_action"
    assert run["workflow_node_uuid"] == snapshot_nodes[0]["uuid"]
    assert run["param"] == plan_nodes[0]["param"]
    assert run["execution_policy"] == {"always_free": True}
    (attempt,) = run["attempts"]
    assert attempt["attempt_no"] == 1 and attempt["uuid"] == run["current_job_uuid"]
    assert [job["uuid"] for job in service.list_workflow_node_jobs(task["uuid"])] == [attempt["uuid"]]


def test_ad_hoc_task_idempotency_and_fingerprint_conflict(
    service: WorkflowService,
) -> None:
    submitted: list[str] = []
    service.set_task_submitter(submitted.append)
    first = _create(service, idempotency_key="key-1")
    replay = _create(service, idempotency_key="key-1")
    assert replay["uuid"] == first["uuid"]
    # 幂等复用不重复提交调度
    assert submitted == [first["uuid"]]

    with pytest.raises(WorkflowConflict):
        _create(
            service,
            idempotency_key="key-1",
            param={"resource": {"uuid": "m-2"}},
        )


def test_ad_hoc_task_validation_and_authority_gate() -> None:
    service = WorkflowService(
        ":memory:",
        authority_profile=SchedulerAuthorityProfile.BACKEND_CONTROLLED,
    )
    try:
        with pytest.raises(WorkflowError) as exc:
            service.create_ad_hoc_device_action_task(
                device_id="host_node",
                action_name="transfer_resource",
                param={},
                description=None,
                meta_data={},
            )
        assert exc.value.code == "local_task_authority_forbidden"
    finally:
        service.close()


def test_ad_hoc_task_requires_device_and_action(service: WorkflowService) -> None:
    for overrides in ({"device_id": " "}, {"action_name": ""}):
        with pytest.raises(WorkflowError) as exc:
            _create(service, **overrides)
        assert exc.value.code == "invalid_input"


def test_scheduler_build_dag_consumes_ad_hoc_task(service: WorkflowService) -> None:
    task = _create(service)
    prepared = service.prepare_workflow_task_execution(task["uuid"])
    assert prepared["state"] == "ready"

    scheduler = BackendScheduler(service, _ExecutorStub())
    dag, specs = scheduler._build_dag(  # noqa: SLF001 - 契约验证
        prepared["task"], prepared["runs"]
    )
    assert len(dag.nodes) == 1
    node = next(iter(dag.nodes.values()))
    # DAG 节点键 = 节点运行 uuid（稳定），执行器 job 是 attempt
    assert node.node_id == prepared["runs"][0]["uuid"]
    assert node.device_id == "host_node"
    assert node.action == "transfer_resource"
    assert node.action_args == {"resource": {"uuid": "m-1"}, "site": "site-1"}
    assert node.always_free is True
    assert not dag.edges
    assert set(specs) == set(dag.nodes)


def test_api_create_ad_hoc_task_and_rejects_unknown_kind(
    service: WorkflowService,
) -> None:
    client = TestClient(create_workflow_app(service))
    response = client.post(
        "/api/v1/workflow-tasks",
        json={
            "execution_kind": "ad_hoc_device_action",
            "device_id": "host_node",
            "action_name": "set_substance",
            "param": {"resource": {"uuid": "m-9"}, "substance_names": ["water"]},
            "idempotency_key": "api-key-1",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["data"]["execution_kind"] == "ad_hoc_device_action"
    assert body["data"]["idempotency_key"] == "api-key-1"

    replay = client.post(
        "/api/v1/workflow-tasks",
        json={
            "execution_kind": "ad_hoc_device_action",
            "device_id": "host_node",
            "action_name": "set_substance",
            "param": {"resource": {"uuid": "m-9"}, "substance_names": ["water"]},
            "idempotency_key": "api-key-1",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["data"]["uuid"] == body["data"]["uuid"]

    # Backend 信封：业务错误 HTTP 恒 200，code=1000 表示 invalid_input
    unknown = client.post(
        "/api/v1/workflow-tasks",
        json={"execution_kind": "nonsense", "workflow_uuid": "w-1"},
    )
    assert unknown.status_code == 200
    assert unknown.json()["code"] == 1000
