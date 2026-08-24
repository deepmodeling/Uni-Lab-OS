"""动作参数物料锁的调度与执行合同测试。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from unilabos.server.scheduler.backend import (
    JobExecutionBackend,
    make_device_materials_need_lock_resolver,
)
from unilabos.server.scheduler.dispatch import RecordingDispatcher
from unilabos.server.scheduler.material_locks import extract_material_uuids
from unilabos.server.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.server.scheduler.service import EdgeScheduler


@dataclass
class _ResourceReference:
    unilabos_uuid: str


def test_material_uuid_extraction_accepts_wire_and_resolved_resource_shapes() -> None:
    assert extract_material_uuids("material-a") == {"material-a"}
    assert extract_material_uuids({"material_uuid": "material-a"}) == {
        "material-a"
    }
    assert extract_material_uuids({"uuid": "material-a"}) == {"material-a"}
    assert extract_material_uuids(
        [{"uuid": "material-a"}, {"data": {"unilabos_uuid": "material-b"}}]
    ) == {"material-a", "material-b"}
    assert extract_material_uuids(_ResourceReference("material-a")) == {
        "material-a"
    }
    assert extract_material_uuids({"id": "display-id", "name": "name"}) == set()


class _Adapter:
    def __init__(self, mappings: dict[str, Any]) -> None:
        self._action_value_mappings = mappings
        self.devices_instances: dict[str, Any] = {}


class _RecordingExecutionAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__({})
        self.sent: list[Any] = []

    def send_goal(self, item: Any, **_kwargs: Any) -> None:
        self.sent.append(item)

    def cancel_goal(self, _job_id: str) -> None:
        return None


def _node(node_id: str, device_id: str, material: Any) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        device_id=device_id,
        action_name="process",
        action_type="goal",
        param={"plate": material},
    )


def test_scheduler_serializes_same_declared_material_across_devices() -> None:
    adapter = _Adapter(
        {
            "device-a": {
                "process": {"materials_need_lock": ["plate"]},
            },
            "device-b": {
                "process": {"materials_need_lock": ["plate"]},
            },
        }
    )
    resolver = make_device_materials_need_lock_resolver(lambda: adapter)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        materials_need_lock_resolver=resolver,
    )

    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="workflow",
            nodes=[
                _node("a", "device-a", {"uuid": "material-shared"}),
                _node("b", "device-b", _ResourceReference("material-shared")),
            ],
        )
    )

    assert len(result["dispatched"]) == 1
    (job_id, inflight), = scheduler.snapshot()["inflight_jobs"].items()
    assert inflight["resource_locks"] == ["res:material-shared"]
    follow_up = scheduler.on_job_finished(job_id, success=True)
    assert len(follow_up["dispatched"]) == 1
    assert {payload["node_id"] for payload in dispatcher.dispatched} == {"a", "b"}


def test_undeclared_actions_remain_unlocked_and_parallel() -> None:
    adapter = _Adapter(
        {
            "device-a": {"process": {}},
            "device-b": {"process": {}},
        }
    )
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        materials_need_lock_resolver=make_device_materials_need_lock_resolver(
            lambda: adapter
        ),
    )

    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="workflow-unlocked",
            nodes=[
                _node("a", "device-a", {"uuid": "material-shared"}),
                _node("b", "device-b", {"uuid": "material-shared"}),
            ],
        )
    )

    assert len(result["dispatched"]) == 2


def test_execution_contract_rejects_unresolvable_declared_material() -> None:
    with pytest.raises(ValueError, match="无法解析权威物料 UUID"):
        JobExecutionBackend._material_uuids_from_arguments(
            ["plate"],
            {"plate": {"id": "not-authoritative"}},
        )


def test_execution_backend_holds_and_releases_declared_material_lock() -> None:
    adapter = _RecordingExecutionAdapter()
    backend = JobExecutionBackend(
        host_node_getter=lambda: adapter,
        queue_conflicts=True,
    )
    backend.start()
    try:
        for index, device_id in enumerate(("device-a", "device-b"), start=1):
            backend.dispatch(
                {
                    "job_id": f"job-{index}",
                    "task_id": "task",
                    "node_id": f"node-{index}",
                    "device_id": device_id,
                    "action": "process",
                    "action_type": "goal",
                    "action_args": {"plate": {"uuid": "material-shared"}},
                    "materials_need_lock": ["plate"],
                }
            )

        assert backend.wait_idle()
        assert [item.job_id for item in adapter.sent] == ["job-1"]

        backend.publish_job_status(
            {},
            adapter.sent[0],
            "success",
            {"return_value": None, "suc_type": "normal"},
        )

        assert backend.wait_idle()
        assert [item.job_id for item in adapter.sent] == ["job-1", "job-2"]
    finally:
        backend.stop()
