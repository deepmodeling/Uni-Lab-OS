"""共同外层所有权不能把不同设备的并行分支串行化。"""

import json
from typing import Any

import pytest

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridgeError


def submit_shared_scope(runtime: CoreRuntime, *, reservation: bool = False, sequential: bool = False) -> tuple[dict[str, Any], list[str]]:
    ids = [stable_uuid(f"scope-node-{i}") for i in range(2)]
    jobs = [stable_uuid(f"scope-job-{i}") for i in range(2)]
    graph = {
        "resources": ["warehouse-a"],
        "nodes": [
            {"uuid": n, "resource_defaults": [device], "branch_id": f"branch-{i}"}
            for i, (n, device) in enumerate(zip(ids, ["reactor-a", "reactor-b"]))
        ],
    }
    edges = [{"uuid": stable_uuid("scope-edge"), "source_node_uuid": ids[0], "target_node_uuid": ids[1], "dependency_only": True}] if sequential else []
    graph["edges"] = edges
    if reservation:
        graph.pop("resources")
        graph["resource_scopes"] = [{"scope_id": "selected", "kind": "reservation", "resources": ["reactor-a", "reactor-b"], "node_uuids": ids}]
    plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(graph),
            {
                key: {"canonical_key": f"/devices/{value}", "kind": "device"}
                for key, value in runtime.device_materials.items()
            },
        )
    )
    nodes = [
        {
            "uuid": n,
            "kind": "device_action",
            "device_id": device,
            "material_uuid": runtime.device_materials[device],
            "action_name": "run",
            "action_type": "UniLabJsonCommand",
            "param": {},
            "param_schema": {
                "type": "object",
                "properties": {"goal": {"type": "object", "properties": {}}},
            },
            "execution_policy": {},
            "resource_plan_id": plan["plan_id"],
            "resource_interval_ids": [
                interval["interval_id"]
                for interval in plan["intervals"]
                if n in interval["node_uuids"]
            ],
            "resource_acquire_set_id": next(
                (a["acquire_set_id"] for a in plan["acquire_sets"] if a["node_uuid"] == n), ""
            ),
        }
        for n, device in zip(ids, ["reactor-a", "reactor-b"])
    ]
    owner = runtime.submit_frozen(
        task_name="shared-scope",
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": nodes,
            "edges": edges,
            "handles": [],
            "capabilities": plan["capabilities"],
            "resource_plan": plan,
        },
        jobs=[
            {
                "uuid": j,
                "workflow_node_uuid": n,
                "topological_index": i,
                "executor_kind": "device_action",
                "execution_policy": {},
                "param": {},
            }
            for i, (n, j) in enumerate(zip(ids, jobs))
        ],
    )
    return owner, jobs


@pytest.mark.parametrize("completion_order", [(0, 1), (1, 0)])
def test_outer_scope_allows_parallel_devices_and_releases_after_both(
    core_runtime: CoreRuntime, completion_order: tuple[int, int]
) -> None:
    runtime = core_runtime
    owner, jobs = submit_shared_scope(runtime)
    assert {p["job_id"] for p in runtime.dispatcher.dispatched} == set(jobs)
    runtime.submit(task_name="scope-waiter", devices=["warehouse-a"], priority="high")
    assert len(runtime.dispatcher.dispatched) == 2
    runtime.scheduler.on_job_finished(jobs[completion_order[0]], True, {})
    assert len(runtime.dispatcher.dispatched) == 2
    runtime.scheduler.on_job_finished(jobs[completion_order[1]], True, {})
    assert runtime.dispatcher.dispatched[-1]["job_id"] == stable_uuid("job:scope-waiter:0")
    assert runtime.workflow_store.get_task(owner["task"]["uuid"])["status"] == "succeeded"

    runtime.scheduler.on_job_finished(stable_uuid("job:scope-waiter:0"), True, {})
    assert (
        runtime.inventory_store.query_all(
            "SELECT claim_uuid FROM station_execution_claim WHERE state IN ('prepared','reserved','running','uncertain')"
        )
        == []
    )


@pytest.mark.parametrize("reservation", [False, True])
def test_each_shared_scope_job_persists_the_complete_authoritative_fences(
    core_runtime: CoreRuntime, reservation: bool,
) -> None:
    """共享 Lease 不复制行，但每个 Job 的 Claim 与物理载荷仍须含完整 Fence。"""

    runtime = core_runtime
    _owner, jobs = submit_shared_scope(runtime, reservation=reservation)
    projection = TaskRuntimeProjection(runtime.workflow_store)
    expected_fences: dict[str, dict[str, int]] = {}

    for payload in runtime.dispatcher.dispatched:
        if payload["job_id"] not in jobs:
            continue
        claim = projection.get_execution_claim(payload["job_id"])
        assert claim is not None
        claim_keys = set(claim["resource_keys"])
        claim_fences = {
            item["lock_key"]: item["fencing_token"] for item in claim["fences"]
        }
        payload_fences = {
            item["lock_key"]: item["fencing_token"] for item in payload["fences"]
        }
        assert set(claim_fences) == claim_keys
        assert payload_fences == claim_fences
        expected_fences[payload["job_id"]] = claim_fences
        inventory_claim = runtime.inventory_store.query_one(
            "SELECT resource_keys FROM station_execution_claim WHERE job_uuid=?",
            (payload["job_id"],),
        )
        assert inventory_claim is not None
        assert set(json.loads(inventory_claim["resource_keys"])) == claim_keys

    for job_id in jobs:
        runtime.scheduler.on_job_finished(job_id, True, {})
    for job_id, fences in expected_fences.items():
        terminal_claim = projection.get_execution_claim(job_id)
        assert terminal_claim is not None
        assert {
            item["lock_key"]: item["fencing_token"]
            for item in terminal_claim["fences"]
        } == fences


@pytest.mark.parametrize("completion_order", [(0, 1), (1, 0)])
@pytest.mark.parametrize("terminal_outcome", ["failed", "canceled"])
@pytest.mark.parametrize("reservation", [False, True])
def test_shared_scope_abnormal_branch_requires_operator_release(
    core_runtime: CoreRuntime, completion_order: tuple[int, int], terminal_outcome: str, reservation: bool
) -> None:
    runtime = core_runtime
    owner, jobs = submit_shared_scope(runtime, reservation=reservation)
    task_uuid = owner["task"]["uuid"]
    waiter_id = stable_uuid("job:scope-waiter:0")
    runtime.submit(task_name="scope-waiter", devices=["reactor-b" if reservation else "warehouse-a"], priority="high")
    for position, index in enumerate(completion_order):
        # 分支 0 成功；分支 1 明确失败或被设备确认取消。取消请求自身不是终态证据。
        if index == 1 and terminal_outcome == "canceled":
            runtime.scheduler.cancel_workflow(task_uuid)
            assert len(runtime.dispatcher.dispatched) == 2
        runtime.scheduler.on_job_finished(
            jobs[index], index == 0, {}, "normal" if index == 0 else terminal_outcome
        )
        if position == 0:
            assert len(runtime.dispatcher.dispatched) == 2, "另一个物理命令未结算就释放共同范围"
    assert runtime.workflow_store.get_task(task_uuid)["status"] == ("running" if terminal_outcome == "failed" else terminal_outcome)
    if terminal_outcome == "failed":
        assert runtime.workflow_store.get_task(task_uuid)["control_status"] == "error_waiting"
    assert waiter_id not in {
        payload["job_id"] for payload in runtime.dispatcher.dispatched
    }
    assert runtime.inventory_store.query_all(
        "SELECT claim_uuid FROM station_execution_claim WHERE task_uuid=? "
        "AND state IN ('prepared','reserved','running','uncertain')",
        (task_uuid,),
    )
    with runtime.workflow_store.read() as connection:
        assert connection.execute(
            "SELECT uuid FROM execution_lock_lease WHERE workflow_task_uuid=? "
            "AND state IN ('reserved','running','uncertain')",
            (task_uuid,),
        ).fetchall()

    runtime.release_error_resources_and_resume(
        task_uuid,
        command_uuid=stable_uuid(
            f"command:unlock-{terminal_outcome}-{completion_order}"
        ),
        reason="操作员确认异常分支已经停止并核对现场资源",
    )
    assert runtime.dispatcher.dispatched[-1]["job_id"] == waiter_id


@pytest.mark.parametrize("peer_outcome", ["succeeded", "failed"])
def test_shared_scope_unknown_peer_keeps_claim_until_certain_result(
    core_runtime: CoreRuntime, peer_outcome: str
) -> None:
    from tests.scheduler_core.test_failure_safety import outcome

    runtime = core_runtime
    owner, jobs = submit_shared_scope(runtime)
    runtime.submit(task_name="scope-waiter", devices=["warehouse-a"], priority="high")
    runtime.scheduler.on_job_outcome(jobs[1], outcome("failed", unknown=["scope-command"]))
    runtime.scheduler.on_job_outcome(jobs[0], outcome(peer_outcome))
    assert len(runtime.dispatcher.dispatched) == 2
    assert runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim WHERE job_uuid=?", (jobs[1],)
    ) == [{"state": "uncertain"}]
    runtime.scheduler.on_job_outcome(jobs[1], outcome("succeeded"))
    waiter_id = stable_uuid("job:scope-waiter:0")
    if peer_outcome == "succeeded":
        with pytest.raises(TaskSchedulerBridgeError, match="任务尚未进入异常终态"):
            runtime.release_error_resources_and_resume(
                owner["task"]["uuid"],
                command_uuid=stable_uuid("resume-known-unknown"),
                reason="人工确认已知未知结果可恢复",
            )
        return
    else:
        assert waiter_id not in {
            payload["job_id"] for payload in runtime.dispatcher.dispatched
        }
        runtime.release_error_resources_and_resume(
            owner["task"]["uuid"],
            command_uuid=stable_uuid("command:unlock-resolved-unknown-scope"),
            reason="操作员确认失败分支与曾不确定分支均已完成现场核对",
        )
        assert runtime.dispatcher.dispatched[-1]["job_id"] == waiter_id
    assert runtime.inventory_store.query_all(
        "SELECT claim_uuid FROM station_execution_claim WHERE task_uuid=? "
        "AND state IN ('prepared','reserved','running','uncertain')",
        (owner["task"]["uuid"],),
    ) == []


def test_shared_scope_failure_keeps_every_lock_until_operator_unlocks(
    core_runtime: CoreRuntime,
) -> None:
    """连续区间任一动作失败后整组冻结，只能由操作员显式释放。"""

    runtime = core_runtime
    owner, jobs = submit_shared_scope(runtime)
    task_uuid = str(owner["task"]["uuid"])
    waiter_job_uuid = stable_uuid("job:scope-waiter:0")
    runtime.submit(task_name="scope-waiter", devices=["warehouse-a"], priority="high")

    dispatched_owner_jobs = [
        payload["job_id"]
        for payload in runtime.dispatcher.dispatched
        if payload["job_id"] in jobs
    ]
    assert dispatched_owner_jobs
    failed_job_uuid = dispatched_owner_jobs[0]
    runtime.scheduler.on_job_finished(failed_job_uuid, False, {}, "failed")

    assert runtime.workflow_store.get_task(task_uuid)["status"] == "running"
    assert runtime.workflow_store.get_task(task_uuid)["control_status"] == "error_waiting"
    assert waiter_job_uuid not in {
        payload["job_id"] for payload in runtime.dispatcher.dispatched
    }
    assert runtime.inventory_store.query_all(
        "SELECT claim_uuid FROM station_execution_claim WHERE task_uuid=? "
        "AND state IN ('prepared','reserved','running','uncertain')",
        (task_uuid,),
    )
    with runtime.workflow_store.read() as connection:
        assert connection.execute(
            "SELECT uuid FROM execution_lock_lease WHERE workflow_task_uuid=? "
            "AND state IN ('reserved','running','uncertain')",
            (task_uuid,),
        ).fetchall()

    # 同区间已经派发的兄弟动作必须先由设备结算；它结束后失败闩锁仍不能
    # 自动打开，操作员才可以在现场核对完成后整组释放。
    running_peer_uuid = next(job_uuid for job_uuid in jobs if job_uuid != failed_job_uuid)
    runtime.scheduler.on_job_finished(running_peer_uuid, True, {})
    assert waiter_job_uuid not in {
        payload["job_id"] for payload in runtime.dispatcher.dispatched
    }

    result = runtime.release_error_resources_and_resume(
        task_uuid,
        command_uuid=stable_uuid("command:unlock-failed-shared-scope"),
        reason="操作员已确认设备停止并完成物料与库位核对",
    )

    assert result["status"] == "APPLIED"
    assert runtime.dispatcher.dispatched[-1]["job_id"] == waiter_job_uuid


def test_connection_loss_also_latches_continuous_scope(
    core_runtime: CoreRuntime,
) -> None:
    """执行断连保留实际未知执行，仍须人工明确释放逻辑占用。"""

    runtime = core_runtime
    owner, jobs = submit_shared_scope(runtime)
    task_uuid = str(owner["task"]["uuid"])
    waiter_job_uuid = stable_uuid("job:scope-waiter:0")

    runtime.scheduler.on_execution_connection_lost((jobs[0],))
    runtime.submit(task_name="scope-waiter", devices=["warehouse-a"], priority="high")

    assert runtime.workflow_store.get_task(task_uuid)["status"] == "running"
    assert runtime.workflow_store.get_task(task_uuid)["control_status"] == "error_waiting"
    assert waiter_job_uuid not in {
        payload["job_id"] for payload in runtime.dispatcher.dispatched
    }
    assert runtime.inventory_store.query_all(
        "SELECT claim_uuid FROM station_execution_claim WHERE task_uuid=? "
        "AND state IN ('prepared','reserved','running','uncertain')",
        (task_uuid,),
    )
    with runtime.workflow_store.read() as connection:
        assert connection.execute(
            "SELECT uuid FROM execution_lock_lease WHERE workflow_task_uuid=? "
            "AND state IN ('reserved','running','uncertain')",
            (task_uuid,),
        ).fetchall()

    runtime.release_error_resources_and_resume(
        task_uuid,
        command_uuid=stable_uuid("command:unlock-restarted-shared-scope"),
        reason="操作员确认执行进程重启后所有动作停止且现场资源安全",
    )
    assert runtime.dispatcher.dispatched[-1]["job_id"] == waiter_job_uuid

def test_node_set_reservation_parallel_runtime(core_runtime):
    owner, jobs = submit_shared_scope(core_runtime, reservation=True)
    assert {item["job_id"] for item in core_runtime.dispatcher.dispatched} == set(jobs)
    waiter = core_runtime.submit(task_name="reservation-waiter", devices=["reactor-a"])
    assert len(core_runtime.dispatcher.dispatched) == 2
    core_runtime.scheduler.on_job_finished(jobs[0], True, {})
    core_runtime.scheduler.on_job_finished(jobs[1], True, {})
    assert len(core_runtime.dispatcher.dispatched) == 3


@pytest.mark.parametrize("reservation", [False, True])
def test_explicit_scope_does_not_release_at_last_direct_use(core_runtime: CoreRuntime, reservation: bool) -> None:
    runtime = core_runtime
    _owner, jobs = submit_shared_scope(runtime, reservation=reservation, sequential=True)
    assert [p["job_id"] for p in runtime.dispatcher.dispatched] == jobs[:1]
    waiter = stable_uuid("job:last-use-waiter:0")
    runtime.submit(task_name="last-use-waiter", devices=["reactor-a"], priority="high")
    runtime.scheduler.on_job_finished(jobs[0], True, {})
    dispatched = {p["job_id"] for p in runtime.dispatcher.dispatched}
    assert jobs[1] in dispatched
    assert waiter not in dispatched
    runtime.scheduler.on_job_finished(jobs[1], True, {})
    assert waiter in {p["job_id"] for p in runtime.dispatcher.dispatched}
