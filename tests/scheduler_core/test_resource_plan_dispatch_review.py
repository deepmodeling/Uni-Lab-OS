"""冻结的无环证明不能被实际派发资源漂移或缺失投影绕过。"""

import pytest

from tests.scheduler_core.test_resource_interval_runtime import _spec
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.resource_lock_plan import ResourcePlanError


def test_plan_rejects_actual_executor_outside_declared_footprint() -> None:
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _spec("review-executor", node_ids=["a"], priority="normal")
    spec.nodes[0].device_id = "unexpected"
    scheduler.submit_workflow(spec)
    assert dispatcher.dispatched == []


def test_plan_cannot_be_bypassed_by_clearing_node_interval_projection() -> None:
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _spec("review-projection", node_ids=["a"], priority="normal")
    spec.nodes[0].resource_interval_ids = []
    scheduler.submit_workflow(spec)
    assert dispatcher.dispatched == []


def test_plan_resource_cannot_disappear_from_inventory_projection() -> None:
    from tests.scheduler_core.test_resource_interval_runtime import _graph_spec

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _graph_spec(
        "review-opaque-resource",
        {
            "resources": ["opaque"],
            "nodes": [{"uuid": "a", "device_id": "robot", "resource_defaults": ["robot"]}],
        },
    )
    for resource in spec.resource_plan["resources"]:
        if resource["alias"] == "opaque":
            resource["canonical_key"] = "resource:opaque"
            resource["kind"] = "resource"
    # 内容寻址校验可能在资源键语法校验之前发现篡改；两者都必须关闭派发。
    with pytest.raises(ResourcePlanError, match="资源锁键格式|内容与 plan_id 不一致"):
        scheduler.submit_workflow(spec)
    assert dispatcher.dispatched == []


def test_certain_failure_latches_interval_with_unstarted_successor(core_runtime) -> None:
    from tests.scheduler_core.conftest import stable_uuid
    from tests.scheduler_core.test_resource_occupancy_intervals import _continuous_task

    owner, jobs = _continuous_task(
        core_runtime, task_name="review-failed-sequence", device_id="reactor-a"
    )
    core_runtime.submit(task_name="review-failed-waiter", devices=["reactor-a"])
    core_runtime.scheduler.on_job_finished(jobs[0], False, {})
    waiter_job_uuid = stable_uuid("job:review-failed-waiter:0")
    assert waiter_job_uuid not in {
        payload["job_id"] for payload in core_runtime.dispatcher.dispatched
    }
    core_runtime.release_error_resources_and_resume(
        owner["task"]["uuid"],
        command_uuid=stable_uuid("command:review-failed-sequence-unlock"),
        reason="操作员确认失败动作已停止且连续区间资源安全",
    )
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == waiter_job_uuid


def test_failed_physical_handoff_keeps_inventory_and_mirror_until_recovery(
    core_runtime, monkeypatch
) -> None:
    from tests.scheduler_core.test_resource_occupancy_intervals import _continuous_task

    submit = core_runtime.submit_frozen
    key = f"/devices/{core_runtime.device_materials['reactor-a']}"

    def physical_submit(**values):
        values["execution_plan"]["nodes"][0]["physical_hold_resources"] = [key]
        return submit(**values)

    monkeypatch.setattr(core_runtime, "submit_frozen", physical_submit)
    owner, jobs = _continuous_task(
        core_runtime, task_name="review-physical-abort", device_id="reactor-a"
    )
    core_runtime.scheduler.on_job_finished(jobs[0], True, {})
    core_runtime.scheduler.on_job_finished(jobs[1], False, {})
    task_uuid = owner["task"]["uuid"]
    assert core_runtime.inventory_store.query_all(
        "SELECT lease.lock_key FROM station_execution_lock_lease lease JOIN station_execution_claim claim USING(claim_uuid) WHERE claim.task_uuid=? AND lease.state!='released'",
        (task_uuid,),
    )
    with core_runtime.workflow_store.read() as connection:
        assert connection.execute(
            "SELECT lock_key FROM execution_lock_lease WHERE workflow_task_uuid=? AND state!='released'",
            (task_uuid,),
        ).fetchall()
    assert core_runtime.workflow_store.get_task(task_uuid)["cleanup_status"] != "settled"
