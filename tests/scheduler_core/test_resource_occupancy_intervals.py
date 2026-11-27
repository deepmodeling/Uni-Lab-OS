"""资源占用区间在独立调度内核中的行为合同。"""

from __future__ import annotations

from typing import Any

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)


def _continuous_task(
    runtime: CoreRuntime,
    *,
    task_name: str,
    device_id: str,
) -> tuple[dict[str, Any], tuple[str, str]]:
    """提交一个由同一设备上两个连续 Action 组成的冻结任务。"""

    node_uuids = tuple(
        stable_uuid(f"node:{task_name}:{index}") for index in range(2)
    )
    job_uuids = tuple(stable_uuid(f"job:{task_name}:{index}") for index in range(2))
    device_material_uuid = runtime.device_materials[device_id]
    nodes = [
        {
            "uuid": node_uuid,
            "kind": "device_action",
            "device_id": device_id,
            "material_uuid": device_material_uuid,
            "action_name": f"run-{index}",
            "action_type": "UniLabJsonCommand",
            "param": {},
            "param_schema": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    }
                },
                "additionalProperties": False,
            },
            "execution_policy": {},
            "action_resource_contract": {},
            "material_requirements": [],
        }
        for index, node_uuid in enumerate(node_uuids)
    ]
    jobs = [
        {
            "uuid": job_uuid,
            "workflow_node_uuid": node_uuid,
            "topological_index": index,
            "executor_kind": "device_action",
            "execution_policy": {},
            "execution_timeout_seconds": 0,
            "param": {},
        }
        for index, (node_uuid, job_uuid) in enumerate(zip(node_uuids, job_uuids))
    ]
    aggregate = runtime.submit_frozen(
        task_name=task_name,
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": nodes,
            "handles": [],
            "edges": [
                {
                    "uuid": stable_uuid(f"edge:{task_name}:0:1"),
                    "source_node_uuid": node_uuids[0],
                    "target_node_uuid": node_uuids[1],
                    "source_handle_uuid": "",
                    "target_handle_uuid": "",
                    "dependency_only": True,
                    "source_data_key": "",
                    "target_data_key": "",
                    "source_type": "",
                    "target_type": "",
                }
            ],
            "resource_occupancy_plan": {
                "version": 1,
                "static_acyclic": True,
                "intervals": [
                    {
                        "uuid": stable_uuid(f"resource-interval:{task_name}"),
                        "parent_uuid": None,
                        "source": "automatic_continuity",
                        "resource_locks": [
                            {
                                "lock_key": f"/devices/{device_material_uuid}",
                                "scope": "device",
                                "material_uuid": device_material_uuid,
                            }
                        ],
                        "member_node_uuids": list(node_uuids),
                        "entry_node_uuids": [node_uuids[0]],
                        "exit_node_uuids": [node_uuids[1]],
                    }
                ],
            },
        },
        jobs=jobs,
    )
    return aggregate, job_uuids


def _three_job_scoped_task(
    runtime: CoreRuntime,
    *,
    task_name: str,
) -> tuple[dict[str, Any], tuple[str, str, str], dict[str, Any]]:
    """提交三个 Job 各自准入、共同持有一个根资源的冻结任务。"""

    node_uuids = tuple(
        stable_uuid(f"node:{task_name}:{index}") for index in range(3)
    )
    job_uuids = tuple(stable_uuid(f"job:{task_name}:{index}") for index in range(3))
    device_ids = ("robot-a", "reactor-a", "robot-a")
    resource_defaults = (
        ("robot-a",),
        ("reactor-a", "reactor-b"),
        ("robot-a",),
    )
    graph_edges = [
        {
            "source_node_uuid": node_uuids[index],
            "target_node_uuid": node_uuids[index + 1],
        }
        for index in range(2)
    ]
    resource_plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(
                {
                    "workflow_uuid": stable_uuid(f"workflow:{task_name}"),
                    "resources": ["warehouse-a"],
                    "nodes": [
                        {
                            "uuid": node_uuid,
                            "resource_defaults": list(node_resources),
                        }
                        for node_uuid, node_resources in zip(
                            node_uuids, resource_defaults
                        )
                    ],
                    "edges": graph_edges,
                }
            ),
            {
                alias: {
                    "canonical_key": f"/devices/{material_uuid}",
                    "kind": "device",
                }
                for alias, material_uuid in runtime.device_materials.items()
            },
        )
    )
    nodes = []
    for node_uuid, device_id, node_resources in zip(
        node_uuids, device_ids, resource_defaults
    ):
        nodes.append(
            {
                "uuid": node_uuid,
                "kind": "device_action",
                "device_id": device_id,
                "material_uuid": runtime.device_materials[device_id],
                "action_name": "run",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        }
                    },
                    "additionalProperties": False,
                },
                "execution_policy": {},
                "action_resource_contract": {},
                "material_requirements": [],
                "resource_defaults": list(node_resources),
                "resource_plan_id": resource_plan["plan_id"],
                "resource_interval_ids": [
                    interval["interval_id"]
                    for interval in resource_plan["intervals"]
                    if node_uuid in interval["node_uuids"]
                ],
                    "resource_acquire_set_id": next(
                        (
                            acquire_set["acquire_set_id"]
                            for acquire_set in resource_plan["acquire_sets"]
                            if acquire_set["node_uuid"] == node_uuid
                        ),
                        "",
                    ),
            }
        )
    edges = [
        {
            "uuid": stable_uuid(f"edge:{task_name}:{index}:{index + 1}"),
            "source_node_uuid": edge["source_node_uuid"],
            "target_node_uuid": edge["target_node_uuid"],
            "source_handle_uuid": "",
            "target_handle_uuid": "",
            "dependency_only": True,
            "source_data_key": "",
            "target_data_key": "",
            "source_type": "",
            "target_type": "",
        }
        for index, edge in enumerate(graph_edges)
    ]
    owner = runtime.submit_frozen(
        task_name=task_name,
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "target_node_uuid": None,
            "nodes": nodes,
            "handles": [],
            "edges": edges,
            "capabilities": resource_plan["capabilities"],
            "resource_plan": resource_plan,
        },
        jobs=[
            {
                "uuid": job_uuid,
                "workflow_node_uuid": node_uuid,
                "topological_index": index,
                "executor_kind": "device_action",
                "execution_policy": {},
                "execution_timeout_seconds": 0,
                "param": {},
            }
            for index, (node_uuid, job_uuid) in enumerate(zip(node_uuids, job_uuids))
        ],
    )
    return owner, job_uuids, resource_plan


def _active_task_leases(runtime: CoreRuntime, task_uuid: str) -> list[dict[str, Any]]:
    """读取某 Task 在 Inventory 公共投影中的活动 Claim/Lease。"""

    return runtime.inventory_store.query_all(
        "SELECT claim.claim_uuid,claim.job_uuid,lease.lock_key,lease.fencing_token "
        "FROM station_execution_lock_lease lease "
        "JOIN station_execution_claim claim USING(claim_uuid) "
        "WHERE claim.task_uuid=? "
        "AND lease.state IN ('prepared','reserved','running','uncertain') "
        "ORDER BY lease.lock_key",
        (task_uuid,),
    )


def _fences(payload: dict[str, Any]) -> dict[str, int]:
    """把派发 Fence 投影成可直接比较的规范键到 token 映射。"""

    return {
        str(fence["lock_key"]): int(fence["fencing_token"])
        for fence in payload["fences"]
    }


def test_continuous_interval_keeps_resource_across_jobs_before_priority_waiter(
    core_runtime: CoreRuntime,
) -> None:
    """连续占用不能在两个 Action 之间让更高优先级任务插入。"""

    owner, owner_jobs = _continuous_task(
        core_runtime,
        task_name="continuous-owner",
        device_id="reactor-a",
    )
    waiter = core_runtime.submit(
        task_name="continuous-priority-waiter",
        devices=["reactor-a"],
        priority="high",
    )
    waiter_job = stable_uuid("job:continuous-priority-waiter:0")

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        owner_jobs[0]
    ]

    core_runtime.scheduler.on_job_finished(owner_jobs[0], True, {"step": 1})

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        owner_jobs[0],
        owner_jobs[1],
    ]
    assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "pending"

    core_runtime.scheduler.on_job_finished(owner_jobs[1], True, {"step": 2})
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        owner_jobs[0],
        owner_jobs[1],
        waiter_job,
    ]
    core_runtime.scheduler.on_job_finished(waiter_job, True, {"done": True})

    assert core_runtime.workflow_store.get_task(owner["task"]["uuid"])["status"] == (
        "succeeded"
    )
    assert core_runtime.workflow_store.get_task(waiter["task"]["uuid"])["status"] == (
        "succeeded"
    )


def test_scope_preacquires_complete_set_before_first_action(
    core_runtime: CoreRuntime,
) -> None:
    """后续动作的资源在首动作前原子取得，区间内不再增量补锁。"""

    blocker = core_runtime.submit(
        task_name="scoped-successor-blocker",
        devices=["reactor-b"],
    )
    blocker_job = stable_uuid("job:scoped-successor-blocker:0")
    owner, owner_jobs, resource_plan = _three_job_scoped_task(
        core_runtime,
        task_name="scoped-three-job-owner",
    )
    waiter = core_runtime.submit(
        task_name="scoped-common-priority-waiter",
        devices=["warehouse-a"],
        priority="high",
    )
    waiter_job = stable_uuid("job:scoped-common-priority-waiter:0")
    owner_task_uuid = owner["task"]["uuid"]

    resource_alias_by_id = {
        item["resource_id"]: item["alias"] for item in resource_plan["resources"]
    }
    acquire_by_node = {
        item["node_uuid"]: (
            frozenset(resource_alias_by_id[value] for value in item["resource_ids"]),
            frozenset(
                resource_alias_by_id[value]
                for value in item["preheld_resource_ids"]
            ),
            item["atomic"],
        )
        for item in resource_plan["acquire_sets"]
    }
    owner_nodes = tuple(
        stable_uuid(f"node:scoped-three-job-owner:{index}") for index in range(3)
    )
    assert acquire_by_node == {
        owner_nodes[0]: (
            frozenset({"reactor-a", "reactor-b", "robot-a", "warehouse-a"}),
            frozenset(),
            True,
        )
    }

    # reactor-b 被占用时 owner 连第一个动作都不能开始；随后提交的 warehouse
    # waiter 可先运行，证明 owner 没有边等边占一部分资源。
    assert [payload["job_id"] for payload in core_runtime.dispatcher.dispatched] == [
        blocker_job,
        waiter_job,
    ]
    core_runtime.scheduler.on_job_finished(waiter_job, True, {"done": True})
    core_runtime.scheduler.on_job_finished(blocker_job, True, {"released": True})
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == owner_jobs[0]

    common_key = f"/devices/{core_runtime.device_materials['warehouse-a']}"
    exclusive_key = f"/devices/{core_runtime.device_materials['reactor-b']}"
    first_executor_key = f"/devices/{core_runtime.device_materials['robot-a']}"
    second_executor_key = f"/devices/{core_runtime.device_materials['reactor-a']}"
    expected_keys = {
        common_key,
        exclusive_key,
        first_executor_key,
        second_executor_key,
    }
    first_payload = core_runtime.dispatcher.dispatched[-1]
    first_fences = _fences(first_payload)
    assert set(first_fences) == expected_keys

    core_runtime.scheduler.on_job_finished(owner_jobs[0], True, {"step": 1})
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == owner_jobs[1]
    assert set(_fences(core_runtime.dispatcher.dispatched[-1])) == expected_keys

    core_runtime.scheduler.on_job_finished(owner_jobs[1], True, {"step": 2})
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == owner_jobs[2]
    assert set(_fences(core_runtime.dispatcher.dispatched[-1])) == expected_keys

    core_runtime.scheduler.on_job_finished(owner_jobs[2], True, {"step": 3})
    assert _active_task_leases(core_runtime, owner_task_uuid) == []
    assert core_runtime.inventory_store.query_all(
        "SELECT claim_uuid FROM station_execution_claim WHERE task_uuid=? "
        "AND state IN ('prepared','reserved','running','uncertain')",
        (owner_task_uuid,),
    ) == []

    assert core_runtime.workflow_store.get_task(blocker["task"]["uuid"])["status"] == (
        "succeeded"
    )
    assert core_runtime.workflow_store.get_task(owner_task_uuid)["status"] == "succeeded"
    assert core_runtime.workflow_store.get_task(waiter["task"]["uuid"])["status"] == (
        "succeeded"
    )
