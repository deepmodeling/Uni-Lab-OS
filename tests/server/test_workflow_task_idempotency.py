"""整图任务的幂等提交：同一幂等键只建一个任务、只提交一次，键按执行类型隔离。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from unilabos.server.services.runtime.workflow.service import (
    WorkflowConflict,
    WorkflowService,
)


def _workflow(service: WorkflowService, count: int = 2) -> str:
    workflow = service.create_workflow(name="幂等提交", tags=[], description=None, meta_data={})
    nodes = []
    for index in range(count):
        nodes.append({
            "uuid": str(uuid4()), "name": f"动作{index}", "type": "device_action",
            "material_uuid": str(uuid4()), "action_name": "record", "action_type": "UniLabJsonCommand",
            "param": {"index": index}, "pose": {"x": index, "y": 0},
            "execution_policy": {"depends_on": [nodes[-1]["uuid"]] if nodes else []},
            "meta_data": {"target_device_id": f"device-{index}"},
        })
    service.save_graph(workflow["uuid"], revision=workflow["revision"], nodes=nodes, edges=[])
    return workflow["uuid"]


def _create(service: WorkflowService, workflow_uuid: str, *, key=None, run_mode="normal"):
    return service.create_workflow_task(
        workflow_uuid=workflow_uuid, run_mode=run_mode, target_node_uuid=None,
        input_value={}, description=None, meta_data={}, idempotency_key=key,
    )


@pytest.fixture
def service():
    service = WorkflowService(":memory:")
    yield service
    service.close()


def test_same_key_returns_the_first_task_and_submits_once(service) -> None:
    submitted: list[str] = []
    service.set_task_submitter(submitted.append)
    workflow_uuid = _workflow(service)

    first = _create(service, workflow_uuid, key="click-1")
    replay = _create(service, workflow_uuid, key="click-1")

    assert replay["uuid"] == first["uuid"]
    assert submitted == [first["uuid"]]
    with pytest.raises(WorkflowConflict):
        _create(service, workflow_uuid, key="click-1", run_mode="step")


def test_without_a_key_every_submission_is_a_new_task(service) -> None:
    workflow_uuid = _workflow(service)
    assert _create(service, workflow_uuid)["uuid"] != _create(service, workflow_uuid)["uuid"]


def test_keys_are_scoped_per_execution_kind(service) -> None:
    workflow_uuid = _workflow(service)
    task = _create(service, workflow_uuid, key="shared-key")
    ad_hoc = service.create_ad_hoc_device_action_task(
        device_id="device-0", action_name="record", param={}, idempotency_key="shared-key",
        description=None, meta_data={},
    )
    assert ad_hoc["uuid"] != task["uuid"]
