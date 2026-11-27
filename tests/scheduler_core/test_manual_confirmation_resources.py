"""人工确认与资源计划/执行占用衔接的调度核心模拟。"""

from __future__ import annotations

import pytest

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid
from tests.scheduler_core.test_manual_confirmation import _manual_node_job
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    resource_plan_for_node,
    serialize_resource_plan,
)
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


def test_manual_confirmation_simulation_reuses_claim_and_fence_after_approval(
    core_runtime: CoreRuntime,
) -> None:
    """模拟人工批准沿用同一作业执行占用与栅栏，随后只下发一次真实动作。"""

    _pending, job_uuid = core_runtime.submit_manual(task_name="manual-claim-reuse")
    projection = TaskRuntimeProjection(core_runtime.workflow_store)
    reserved_claim = projection.get_execution_claim(job_uuid)
    assert reserved_claim is not None
    assert reserved_claim["state"] == "reserved"

    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    approved = service.decide_manual_confirmation(job_uuid, action="approve")

    running_claim = projection.get_execution_claim(job_uuid)
    assert running_claim is not None
    assert running_claim["claim_uuid"] == reserved_claim["claim_uuid"]
    assert running_claim["fences"] == reserved_claim["fences"]
    assert running_claim["state"] == "running"
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        job_uuid
    ]
    payload = core_runtime.dispatcher.dispatched[0]
    assert payload["claim_uuid"] == reserved_claim["claim_uuid"]
    assert payload["fences"] == reserved_claim["fences"]
    assert next(job for job in approved["jobs"] if job["uuid"] == job_uuid)[
        "manual_confirmation"
    ]["status"] == "approved"


def test_manual_approval_rejects_released_workflow_claim_before_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    """人工批准必须在物理边界前拒绝已释放的工作流 Claim。"""

    _pending, job_uuid = core_runtime.submit_manual(
        task_name="manual-released-workflow-credentials"
    )
    with core_runtime.workflow_store.transaction() as connection:
        connection.execute(
            "UPDATE execution_claim SET state='released', released_at=update_time "
            "WHERE workflow_node_job_uuid=?",
            (job_uuid,),
        )

    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    with pytest.raises(WorkflowConflict):
        service.decide_manual_confirmation(job_uuid, action="approve")

    assert core_runtime.dispatcher.dispatched == []


def test_manual_approval_rejects_missing_workflow_lease_before_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    """人工批准必须在物理边界前拒绝缺失的工作流 Lease。"""

    _pending, job_uuid = core_runtime.submit_manual(
        task_name="manual-missing-workflow-lease"
    )
    with core_runtime.workflow_store.transaction() as connection:
        connection.execute(
            "DELETE FROM execution_lock_lease WHERE workflow_node_job_uuid=?",
            (job_uuid,),
        )

    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    with pytest.raises(WorkflowConflict):
        service.decide_manual_confirmation(job_uuid, action="approve")

    assert core_runtime.dispatcher.dispatched == []


def test_manual_approval_rejects_released_inventory_claim_before_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    """人工批准必须在物理边界前拒绝已释放的库存 Claim。"""

    _pending, job_uuid = core_runtime.submit_manual(
        task_name="manual-released-inventory-claim"
    )
    with core_runtime.inventory_store.transaction() as connection:
        connection.execute(
            "UPDATE station_execution_claim SET state='released', "
            "released_at=update_time WHERE job_uuid=?",
            (job_uuid,),
        )

    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    with pytest.raises(WorkflowConflict):
        service.decide_manual_confirmation(job_uuid, action="approve")

    assert core_runtime.dispatcher.dispatched == []


@pytest.mark.parametrize("lease_corruption", ["released", "missing"])
def test_manual_approval_rejects_invalid_inventory_lease_before_dispatch(
    core_runtime: CoreRuntime,
    lease_corruption: str,
) -> None:
    """人工批准必须在物理边界前拒绝失活或缺失的库存 Lease。"""

    _pending, job_uuid = core_runtime.submit_manual(
        task_name="manual-inactive-inventory-lease"
    )
    with core_runtime.inventory_store.transaction() as connection:
        if lease_corruption == "released":
            connection.execute(
                "UPDATE station_execution_lock_lease SET state='released', "
                "released_at=update_time WHERE claim_uuid=("
                "SELECT claim_uuid FROM station_execution_claim WHERE job_uuid=?)",
                (job_uuid,),
            )
        else:
            connection.execute(
                "DELETE FROM station_execution_lock_lease WHERE claim_uuid=("
                "SELECT claim_uuid FROM station_execution_claim WHERE job_uuid=?)",
                (job_uuid,),
            )

    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    with pytest.raises(WorkflowConflict):
        service.decide_manual_confirmation(job_uuid, action="approve")

    assert core_runtime.dispatcher.dispatched == []


def test_manual_approval_rejects_inventory_fence_mismatch_before_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    """人工批准必须在物理边界前拒绝库存与工作流不一致的 Fence。"""

    _pending, job_uuid = core_runtime.submit_manual(
        task_name="manual-inventory-fence-mismatch"
    )
    with core_runtime.inventory_store.transaction() as connection:
        connection.execute(
            "UPDATE station_execution_lock_lease "
            "SET fencing_token=fencing_token+1 WHERE claim_uuid=("
            "SELECT claim_uuid FROM station_execution_claim WHERE job_uuid=?)",
            (job_uuid,),
        )

    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    with pytest.raises(WorkflowConflict):
        service.decide_manual_confirmation(job_uuid, action="approve")

    assert core_runtime.dispatcher.dispatched == []


def test_manual_confirmation_simulation_keeps_bound_resource_plan_identity(
    core_runtime: CoreRuntime,
) -> None:
    """模拟带绑定资源计划的人工确认，批准前后保留同一计划与区间身份。"""

    manual_node, manual_job = _manual_node_job(
        core_runtime,
        task_name="manual-resource-plan",
        index=0,
        device_id="reactor-a",
    )
    # 资源计划身份来自冻结节点和工站设备实例绑定，不从运行时重新推导。
    resource_graph = {
        "workflow_uuid": stable_uuid("workflow:manual-resource-plan"),
        "nodes": [
            {
                "uuid": manual_node["uuid"],
                "resource_defaults": ["manual-device"],
            }
        ],
        "edges": [],
    }
    template_plan = compile_template_resource_plan(resource_graph)
    bound_plan = bind_station_resource_plan(
        template_plan,
        {
            "manual-device": {
                "instance_uuid": core_runtime.device_materials["reactor-a"],
                "kind": "device",
            }
        },
    )
    node_projection = resource_plan_for_node(bound_plan, manual_node["uuid"])
    manual_node.update(
        {
            "resource_plan_id": bound_plan.plan_id,
            "resource_interval_ids": [
                item["interval_id"] for item in node_projection["intervals"]
            ],
            "resource_acquire_set_id": node_projection["acquire_sets"][0][
                "acquire_set_id"
            ],
        }
    )
    manual_job.update(
        {
            "resource_plan_id": bound_plan.plan_id,
            "resource_interval_ids": list(manual_node["resource_interval_ids"]),
            "resource_acquire_set_id": manual_node["resource_acquire_set_id"],
        }
    )
    pending = core_runtime.submit_frozen(
        task_name="manual-resource-plan",
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": [manual_node],
            "handles": [],
            "edges": [],
            "capabilities": list(bound_plan.capabilities),
            "resource_plan": serialize_resource_plan(bound_plan),
        },
        jobs=[manual_job],
    )

    assert pending["task"]["execution_plan"]["resource_plan"]["plan_id"] == (
        bound_plan.plan_id
    )
    before = core_runtime.scheduler.snapshot()["inflight_jobs"][manual_job["uuid"]]
    assert before["resource_plan_id"] == bound_plan.plan_id
    assert before["resource_interval_ids"] == manual_node["resource_interval_ids"]
    assert before["resource_acquire_set_id"] == manual_node["resource_acquire_set_id"]

    service = WorkflowService(
        core_runtime.workflow_store,
        task_scheduler_bridge=core_runtime.bridge,
    )
    approved = service.decide_manual_confirmation(
        manual_job["uuid"],
        action="approve",
    )
    after = core_runtime.scheduler.snapshot()["inflight_jobs"][manual_job["uuid"]]
    assert after["resource_plan_id"] == before["resource_plan_id"]
    assert after["resource_interval_ids"] == before["resource_interval_ids"]
    assert after["resource_acquire_set_id"] == before["resource_acquire_set_id"]
    assert next(job for job in approved["jobs"] if job["uuid"] == manual_job["uuid"])[
        "manual_confirmation"
    ]["status"] == "approved"


def test_rejected_manual_run_can_resubmit_same_workflow_and_use_same_device(core_runtime: CoreRuntime) -> None:
    """拒绝后重跑同一工作流，旧库存执行占用不能阻塞新作业。"""
    from copy import deepcopy

    from unilabos.workflow.task_input import PreparedTaskInput

    first, old_job = core_runtime.submit_manual(task_name="reject-same-workflow")
    service = WorkflowService(core_runtime.workflow_store, task_scheduler_bridge=core_runtime.bridge)
    rejected = service.decide_manual_confirmation(old_job, action="reject")
    assert rejected["task"]["status"] == "canceled"
    assert core_runtime.inventory_store.query_all(
        "SELECT DISTINCT state FROM station_execution_claim"
    ) == [{"state": "released"}]
    assert core_runtime.inventory_store.query_all(
        "SELECT DISTINCT state FROM station_execution_lock_lease"
    ) == [{"state": "released"}]

    task = first["task"]
    new_job = stable_uuid("reject-same-workflow:new-job")
    prepared = PreparedTaskInput(
        workflow_snapshot=deepcopy(task["workflow_snapshot"]),
        resolved_input={},
        execution_plan=deepcopy(task["execution_plan"]),
        jobs=[{
            "uuid": new_job,
            "workflow_node_uuid": first["jobs"][0]["workflow_node_uuid"],
            "topological_index": 0,
            "executor_kind": "manual_confirm",
            "execution_policy": {},
            "execution_timeout_seconds": 0,
            "param": {},
        }],
    )
    second = core_runtime.workflow_store.create_task_with_jobs(
        workflow_uuid=task["workflow_uuid"],
        task_uuid=stable_uuid("reject-same-workflow:new-task"),
        run_mode="normal", target_node_uuid=None, description=None,
        meta_data={}, priority="normal", plan_builder=lambda _graph: prepared, applied_graph={},
    )
    submitted = core_runtime.bridge.submit(second)
    assert submitted["task"]["workflow_uuid"] == task["workflow_uuid"]
    assert submitted["jobs"][0]["manual_confirmation"]["status"] == "pending"
    service.decide_manual_confirmation(new_job, action="approve")
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [new_job]
    core_runtime.scheduler.on_job_finished(new_job, True, {"completed": True})
    assert core_runtime.workflow_store.get_task(second["uuid"])["status"] == "succeeded"
