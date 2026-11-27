"""冻结条件分支与 RepeatUntil 的调度核心合同。"""

from __future__ import annotations

from typing import Any

import pytest

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    resource_plan_for_node,
    serialize_resource_plan,
)


def device_node(
    *,
    node_uuid: str,
    parent_uuid: str | None,
    device_id: str,
    material_uuid: str,
    action: str,
    param: dict[str, Any] | None = None,
    carry_bindings: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """构造静态冻结计划中的最小设备动作节点。"""

    return {
        "uuid": node_uuid,
        "parent_uuid": parent_uuid,
        "kind": "device_action",
        "param": dict(param or {}),
        "execution_policy": {},
        "action_resource_contract": {},
        "device_id": device_id,
        "material_uuid": material_uuid,
        "action_name": action,
        "action_type": "UniLabJsonCommand",
        "param_schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                }
            },
            "additionalProperties": False,
        },
        "material_requirements": [],
        "carry_bindings": dict(carry_bindings or {}),
    }


def job(
    *,
    job_uuid: str,
    node_uuid: str,
    index: int,
    kind: str,
    param: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造冻结计划已创建的持久 Job。"""

    return {
        "uuid": job_uuid,
        "workflow_node_uuid": node_uuid,
        "topological_index": index,
        "executor_kind": kind,
        "execution_policy": {},
        "execution_timeout_seconds": 0,
        "param": dict(param or {}),
    }


def dependency(source: str, target: str, *, name: str) -> dict[str, Any]:
    """构造不传值的冻结依赖边。"""

    return {
        "uuid": stable_uuid(f"control-edge:{name}"),
        "source_node_uuid": source,
        "target_node_uuid": target,
        "source_handle_uuid": "",
        "target_handle_uuid": "",
        "dependency_only": True,
    }


def attach_resource_plan(
    core_runtime: CoreRuntime,
    *,
    name: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    resource_scopes: list[dict[str, Any]],
) -> dict[str, Any]:
    """给控制流计划附加一份绑定到测试站点的资源计划。"""

    aliases = {
        str(node.get("device_id") or "")
        for node in nodes
        if node.get("device_id")
    }
    aliases.update(
        str(resource)
        for scope in resource_scopes
        for resource in scope.get("resources", ())
    )
    plan = bind_station_resource_plan(
        compile_template_resource_plan(
            {
                "workflow_uuid": stable_uuid(f"workflow:{name}"),
                "nodes": [
                    {
                        "uuid": node["uuid"],
                        "resource_defaults": (
                            [node["device_id"]] if node.get("device_id") else []
                        ),
                    }
                    for node in nodes
                ],
                "edges": [
                    {
                        "source_node_uuid": edge["source_node_uuid"],
                        "target_node_uuid": edge["target_node_uuid"],
                    }
                    for edge in edges
                ],
                "resource_scopes": resource_scopes,
            }
        ),
        {
            alias: {
                "canonical_key": f"/devices/{core_runtime.device_materials[alias]}",
                "kind": "device",
            }
            for alias in aliases
        },
    )
    serialized = serialize_resource_plan(plan)
    for node in nodes:
        projection = resource_plan_for_node(plan, node["uuid"])
        node["resource_plan_id"] = plan.plan_id
        node["resource_interval_ids"] = [
            interval["interval_id"] for interval in projection["intervals"]
        ]
        node["resource_acquire_set_id"] = next(
            (
                acquire_set["acquire_set_id"]
                for acquire_set in projection["acquire_sets"]
            ),
            "",
        )
    return serialized


def active_resource_keys(
    core_runtime: CoreRuntime,
    *,
    task_uuid: str,
) -> tuple[set[str], set[str]]:
    """读取同一 Task 在库存权威与工作流镜像中的活动锁键。"""

    inventory_keys = {
        row["lock_key"]
        for row in core_runtime.inventory_store.query_all(
            "SELECT lease.lock_key FROM station_execution_claim claim "
            "JOIN station_execution_lock_lease lease USING(claim_uuid) "
            "WHERE claim.task_uuid=? "
            "AND lease.state IN ('prepared','reserved','running','uncertain')",
            (task_uuid,),
        )
    }
    with core_runtime.workflow_store.read() as connection:
        workflow_keys = {
            row["lock_key"]
            for row in connection.execute(
                "SELECT lease.lock_key FROM execution_claim claim "
                "JOIN execution_lock_lease lease USING(claim_uuid) "
                "WHERE claim.workflow_task_uuid=? "
                "AND lease.state IN ('reserved','running','uncertain')",
                (task_uuid,),
            ).fetchall()
        }
    return inventory_keys, workflow_keys


def test_frozen_condition_selects_one_branch_and_persists_the_other_as_skipped(
    core_runtime: CoreRuntime,
) -> None:
    """条件节点只在本地求值，Fake Dispatcher 只能看到命中分支。"""

    condition_uuid = stable_uuid("condition:selector")
    selected_uuid = stable_uuid("condition:selected")
    fallback_uuid = stable_uuid("condition:fallback")
    condition_job = stable_uuid("job:condition:selector")
    selected_job = stable_uuid("job:condition:selected")
    fallback_job = stable_uuid("job:condition:fallback")
    region = {
        "predecessor_node_uuids": [],
        "bindings": {
            "should_analyze": {
                "kind": "workflow_input",
                "parameter": "should_analyze",
            }
        },
        "branches": [
            {
                "label": "if",
                "condition": {"var": "should_analyze"},
                "node_uuids": [selected_uuid],
                "entry_node_uuids": [selected_uuid],
                "exit_node_uuids": [selected_uuid],
            },
            {
                "label": "else",
                "condition": None,
                "node_uuids": [fallback_uuid],
                "entry_node_uuids": [fallback_uuid],
                "exit_node_uuids": [fallback_uuid],
            },
        ],
    }
    plan = {
        "version": 2,
        "run_mode": "normal",
        "capabilities": ["condition_expression_v1", "control_regions_v1"],
        "nodes": [
            {
                "uuid": condition_uuid,
                "parent_uuid": None,
                "kind": "condition",
                "param": region,
                "control_region": region,
                "execution_policy": {},
                "action_resource_contract": {},
            },
            device_node(
                node_uuid=selected_uuid,
                parent_uuid=condition_uuid,
                device_id="reactor-a",
                material_uuid=core_runtime.device_materials["reactor-a"],
                action="selected",
            ),
            device_node(
                node_uuid=fallback_uuid,
                parent_uuid=condition_uuid,
                device_id="reactor-b",
                material_uuid=core_runtime.device_materials["reactor-b"],
                action="fallback",
            ),
        ],
        "handles": [],
        "edges": [
            dependency(condition_uuid, selected_uuid, name="selected"),
            dependency(condition_uuid, fallback_uuid, name="fallback"),
        ],
    }
    aggregate = core_runtime.submit_frozen(
        task_name="condition",
        execution_plan=plan,
        jobs=[
            job(
                job_uuid=condition_job,
                node_uuid=condition_uuid,
                index=0,
                kind="condition",
                param=region,
            ),
            job(
                job_uuid=selected_job,
                node_uuid=selected_uuid,
                index=1,
                kind="device_action",
            ),
            job(
                job_uuid=fallback_job,
                node_uuid=fallback_uuid,
                index=2,
                kind="device_action",
            ),
        ],
        resolved_input={"should_analyze": True},
    )

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        selected_job
    ]
    assert core_runtime.workflow_store.get_job(condition_job)["status"] == "succeeded"
    assert core_runtime.workflow_store.get_job(fallback_job)["status"] == "skipped"

    core_runtime.scheduler.on_job_finished(selected_job, True, {"selected": True})
    assert (
        core_runtime.workflow_store.get_task(aggregate["task"]["uuid"])["status"]
        == "succeeded"
    )


def test_condition_skip_closes_completed_resource_interval_before_other_task(
    core_runtime: CoreRuntime,
) -> None:
    """条件跳过区间尾节点后，两套持久锁都必须立即结束而非等 Task 终态。"""

    first_uuid = stable_uuid("condition-resource:first")
    condition_uuid = stable_uuid("condition-resource:selector")
    skipped_uuid = stable_uuid("condition-resource:skipped")
    selected_uuid = stable_uuid("condition-resource:selected")
    first_job = stable_uuid("job:condition-resource:first")
    condition_job = stable_uuid("job:condition-resource:selector")
    skipped_job = stable_uuid("job:condition-resource:skipped")
    selected_job = stable_uuid("job:condition-resource:selected")
    condition_region = {
        "predecessor_node_uuids": [first_uuid],
        "bindings": {
            "take_locked_branch": {
                "kind": "workflow_input",
                "parameter": "take_locked_branch",
            }
        },
        "branches": [
            {
                "label": "if",
                "condition": {"var": "take_locked_branch"},
                "node_uuids": [skipped_uuid],
                "entry_node_uuids": [skipped_uuid],
                "exit_node_uuids": [skipped_uuid],
            },
            {
                "label": "else",
                "condition": None,
                "node_uuids": [selected_uuid],
                "entry_node_uuids": [selected_uuid],
                "exit_node_uuids": [selected_uuid],
            },
        ],
    }
    nodes = [
        device_node(
            node_uuid=first_uuid,
            parent_uuid=None,
            device_id="reactor-a",
            material_uuid=core_runtime.device_materials["reactor-a"],
            action="first",
        ),
        {
            "uuid": condition_uuid,
            "parent_uuid": None,
            "kind": "condition",
            "param": condition_region,
            "control_region": condition_region,
            "execution_policy": {},
            "action_resource_contract": {},
        },
        device_node(
            node_uuid=skipped_uuid,
            parent_uuid=condition_uuid,
            device_id="reactor-a",
            material_uuid=core_runtime.device_materials["reactor-a"],
            action="skipped",
        ),
        device_node(
            node_uuid=selected_uuid,
            parent_uuid=condition_uuid,
            device_id="reactor-b",
            material_uuid=core_runtime.device_materials["reactor-b"],
            action="selected",
        ),
    ]
    edges = [
        dependency(first_uuid, condition_uuid, name="first-condition"),
        dependency(condition_uuid, skipped_uuid, name="condition-skipped"),
        dependency(condition_uuid, selected_uuid, name="condition-selected"),
    ]
    template = compile_template_resource_plan(
        {
            "workflow_uuid": stable_uuid("workflow:condition-resource"),
            "nodes": [
                {"uuid": first_uuid, "resource_defaults": ["reactor-a"]},
                {"uuid": condition_uuid},
                {"uuid": skipped_uuid, "resource_defaults": ["reactor-a"]},
                {"uuid": selected_uuid, "resource_defaults": ["reactor-b"]},
            ],
            "edges": [
                {
                    "source_node_uuid": edge["source_node_uuid"],
                    "target_node_uuid": edge["target_node_uuid"],
                }
                for edge in edges
            ],
        }
    )
    resource_plan = bind_station_resource_plan(
        template,
        {
            alias: {
                "canonical_key": f"/devices/{core_runtime.device_materials[alias]}",
                "kind": "device",
            }
            for alias in ("reactor-a", "reactor-b")
        },
    )
    serialized_plan = serialize_resource_plan(resource_plan)
    for node in nodes:
        projection = resource_plan_for_node(resource_plan, node["uuid"])
        node["resource_plan_id"] = resource_plan.plan_id
        node["resource_interval_ids"] = [
            interval["interval_id"] for interval in projection["intervals"]
        ]
        node["resource_acquire_set_id"] = next(
            (
                acquire_set["acquire_set_id"]
                for acquire_set in projection["acquire_sets"]
            ),
            "",
        )

    owner = core_runtime.submit_frozen(
        task_name="condition-resource-owner",
        execution_plan={
            "version": 2,
            "run_mode": "normal",
            "capabilities": [
                "condition_expression_v1",
                "control_regions_v1",
                *serialized_plan["capabilities"],
            ],
            "nodes": nodes,
            "handles": [],
            "edges": edges,
            "resource_plan": serialized_plan,
        },
        jobs=[
            job(job_uuid=first_job, node_uuid=first_uuid, index=0, kind="device_action"),
            job(job_uuid=condition_job, node_uuid=condition_uuid, index=1, kind="condition"),
            job(job_uuid=skipped_job, node_uuid=skipped_uuid, index=2, kind="device_action"),
            job(job_uuid=selected_job, node_uuid=selected_uuid, index=3, kind="device_action"),
        ],
        resolved_input={"take_locked_branch": False},
    )
    owner_task_uuid = owner["task"]["uuid"]
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_job
    ]

    core_runtime.scheduler.on_job_finished(first_job, True, {"done": True})

    assert core_runtime.workflow_store.get_job(skipped_job)["status"] == "skipped"
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_job,
        selected_job,
    ]
    waiter_job = stable_uuid("job:condition-resource-waiter:0")
    core_runtime.submit(
        task_name="condition-resource-waiter",
        devices=["reactor-a"],
        priority="high",
    )

    assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "running"
    selected_lock_key = (
        f"/devices/{core_runtime.device_materials['reactor-b']}"
    )
    assert core_runtime.inventory_store.query_all(
        "SELECT claim.job_uuid,lease.lock_key FROM station_execution_claim claim "
        "JOIN station_execution_lock_lease lease USING(claim_uuid) "
        "WHERE claim.task_uuid=? "
        "AND lease.state IN ('prepared','reserved','running','uncertain')",
        (owner_task_uuid,),
    ) == [{"job_uuid": selected_job, "lock_key": selected_lock_key}]
    with core_runtime.workflow_store.read() as connection:
        workflow_leases = [
            dict(row)
            for row in connection.execute(
                "SELECT claim.workflow_node_job_uuid AS job_uuid,lease.lock_key "
                "FROM execution_claim claim "
                "JOIN execution_lock_lease lease USING(claim_uuid) "
                "WHERE claim.workflow_task_uuid=? "
                "AND lease.state IN ('reserved','running','uncertain')",
                (owner_task_uuid,),
            ).fetchall()
        ]
    assert workflow_leases == [
        {"job_uuid": selected_job, "lock_key": selected_lock_key}
    ]


def test_condition_tail_reconciles_persistent_resource_interval_without_skip(
    core_runtime: CoreRuntime,
) -> None:
    """条件本身作为区间尾时，即使没有跳过分支也同步释放两套持久锁。"""

    first_uuid = stable_uuid("condition-tail:first")
    condition_uuid = stable_uuid("condition-tail:condition")
    selected_uuid = stable_uuid("condition-tail:selected")
    parallel_uuid = stable_uuid("condition-tail:parallel")
    first_job = stable_uuid("job:condition-tail:first")
    condition_job = stable_uuid("job:condition-tail:condition")
    selected_job = stable_uuid("job:condition-tail:selected")
    parallel_job = stable_uuid("job:condition-tail:parallel")
    region = {
        "predecessor_node_uuids": [first_uuid],
        "bindings": {
            "selected": {"kind": "workflow_input", "parameter": "selected"}
        },
        "branches": [
            {
                "label": "if",
                "condition": {"var": "selected"},
                "node_uuids": [selected_uuid],
                "entry_node_uuids": [selected_uuid],
                "exit_node_uuids": [selected_uuid],
            }
        ],
    }
    nodes = [
        device_node(
            node_uuid=first_uuid,
            parent_uuid=None,
            device_id="reactor-a",
            material_uuid=core_runtime.device_materials["reactor-a"],
            action="first",
        ),
        {
            "uuid": condition_uuid,
            "parent_uuid": None,
            "kind": "condition",
            "param": region,
            "control_region": region,
            "execution_policy": {},
            "action_resource_contract": {},
        },
        device_node(
            node_uuid=selected_uuid,
            parent_uuid=condition_uuid,
            device_id="reactor-b",
            material_uuid=core_runtime.device_materials["reactor-b"],
            action="selected",
        ),
        device_node(
            node_uuid=parallel_uuid,
            parent_uuid=None,
            device_id="robot-a",
            material_uuid=core_runtime.device_materials["robot-a"],
            action="parallel",
        ),
    ]
    edges = [
        dependency(first_uuid, condition_uuid, name="condition-tail-entry"),
        dependency(condition_uuid, selected_uuid, name="condition-tail-exit"),
    ]
    resource_plan = attach_resource_plan(
        core_runtime,
        name="condition-tail",
        nodes=nodes,
        edges=edges,
        resource_scopes=[
            {
                "scope_id": "condition-tail-shared",
                "kind": "with",
                "resources": ["warehouse-a"],
                "node_uuids": [first_uuid, condition_uuid],
            }
        ],
    )
    owner = core_runtime.submit_frozen(
        task_name="condition-tail-owner",
        execution_plan={
            "version": 2,
            "run_mode": "normal",
            "capabilities": [
                "condition_expression_v1",
                "control_regions_v1",
                *resource_plan["capabilities"],
            ],
            "nodes": nodes,
            "handles": [],
            "edges": edges,
            "resource_plan": resource_plan,
        },
        jobs=[
            job(job_uuid=first_job, node_uuid=first_uuid, index=0, kind="device_action"),
            job(job_uuid=condition_job, node_uuid=condition_uuid, index=1, kind="condition"),
            job(job_uuid=selected_job, node_uuid=selected_uuid, index=2, kind="device_action"),
            job(job_uuid=parallel_job, node_uuid=parallel_uuid, index=3, kind="device_action"),
        ],
        resolved_input={"selected": True},
    )
    owner_task_uuid = owner["task"]["uuid"]
    shared_key = f"/devices/{core_runtime.device_materials['warehouse-a']}"
    inventory_keys, workflow_keys = active_resource_keys(
        core_runtime,
        task_uuid=owner_task_uuid,
    )
    assert shared_key in inventory_keys
    assert shared_key in workflow_keys

    waiter_job = stable_uuid("job:condition-tail-waiter:0")
    core_runtime.submit(
        task_name="condition-tail-waiter",
        devices=["warehouse-a"],
        priority="high",
    )
    assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "pending"

    core_runtime.scheduler.on_job_finished(first_job, True, {"done": True})

    assert core_runtime.workflow_store.get_job(condition_job)["status"] == "succeeded"
    assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "running"
    inventory_keys, workflow_keys = active_resource_keys(
        core_runtime,
        task_uuid=owner_task_uuid,
    )
    assert shared_key not in inventory_keys
    assert shared_key not in workflow_keys


def test_step_advances_only_the_selected_local_condition(
    core_runtime: CoreRuntime,
) -> None:
    """同一层多个本地条件就绪时，一次 Step 不能连续求值其他条件。"""

    condition_a = stable_uuid("step-condition:a")
    condition_b = stable_uuid("step-condition:b")
    action_a = stable_uuid("step-condition:action-a")
    action_b = stable_uuid("step-condition:action-b")

    def region(flag: str, action_uuid: str) -> dict[str, Any]:
        return {
            "predecessor_node_uuids": [],
            "bindings": {flag: {"kind": "workflow_input", "parameter": flag}},
            "branches": [
                {
                    "label": "if",
                    "condition": {"var": flag},
                    "node_uuids": [action_uuid],
                    "entry_node_uuids": [action_uuid],
                    "exit_node_uuids": [action_uuid],
                }
            ],
        }

    region_a = region("flag_a", action_a)
    region_b = region("flag_b", action_b)
    aggregate = core_runtime.submit_frozen(
        task_name="step-local-conditions",
        execution_plan={
            "version": 2,
            "run_mode": "step",
            "capabilities": ["condition_expression_v1", "control_regions_v1"],
            "nodes": [
                {
                    "uuid": condition_a,
                    "kind": "condition",
                    "param": region_a,
                    "control_region": region_a,
                    "execution_policy": {},
                    "action_resource_contract": {},
                },
                {
                    "uuid": condition_b,
                    "kind": "condition",
                    "param": region_b,
                    "control_region": region_b,
                    "execution_policy": {},
                    "action_resource_contract": {},
                },
                device_node(
                    node_uuid=action_a,
                    parent_uuid=condition_a,
                    device_id="reactor-a",
                    material_uuid=core_runtime.device_materials["reactor-a"],
                    action="action-a",
                ),
                device_node(
                    node_uuid=action_b,
                    parent_uuid=condition_b,
                    device_id="reactor-b",
                    material_uuid=core_runtime.device_materials["reactor-b"],
                    action="action-b",
                ),
            ],
            "handles": [],
            "edges": [
                dependency(condition_a, action_a, name="step-a"),
                dependency(condition_b, action_b, name="step-b"),
            ],
        },
        jobs=[
            job(
                job_uuid=stable_uuid("step-condition:job-a"),
                node_uuid=condition_a,
                index=0,
                kind="condition",
                param=region_a,
            ),
            job(
                job_uuid=stable_uuid("step-condition:job-b"),
                node_uuid=condition_b,
                index=1,
                kind="condition",
                param=region_b,
            ),
            job(
                job_uuid=stable_uuid("step-condition:action-job-a"),
                node_uuid=action_a,
                index=2,
                kind="device_action",
            ),
            job(
                job_uuid=stable_uuid("step-condition:action-job-b"),
                node_uuid=action_b,
                index=3,
                kind="device_action",
            ),
        ],
        resolved_input={"flag_a": True, "flag_b": True},
    )
    task_uuid = aggregate["task"]["uuid"]

    core_runtime.bridge.step(task_uuid, target_node_uuid=condition_a)

    assert core_runtime.workflow_store.get_job(
        stable_uuid("step-condition:job-a")
    )["status"] == "succeeded"
    assert core_runtime.workflow_store.get_job(
        stable_uuid("step-condition:job-b")
    )["status"] == "pending"
    assert core_runtime.dispatcher.dispatched == []
    candidates = core_runtime.bridge.step_state(task_uuid)["candidates"]
    assert {item["node_id"] for item in candidates} == {condition_b, action_a}


def submit_repeat_task(
    core_runtime: CoreRuntime,
) -> dict:
    """RepeatUntil 每轮使用新 Job 身份，满足严格布尔条件后才派发后继。"""

    repeat_uuid = stable_uuid("repeat:control")
    measure_uuid = stable_uuid("repeat:measure")
    adjust_uuid = stable_uuid("repeat:adjust")
    final_uuid = stable_uuid("repeat:final")
    repeat_job = stable_uuid("job:repeat:control")
    final_job = stable_uuid("job:repeat:final")
    dose_handle = stable_uuid("handle:repeat:dose")
    region = {
        "predecessor_node_uuids": [],
        "successor_node_uuids": [final_uuid],
        "loop_variable": "loop",
        "max_iterations": 3,
        "initial_carry": {"dose": {"kind": "literal", "value": 1}},
        "next_carry": {
            "dose": {
                "kind": "node_result",
                "node_uuid": adjust_uuid,
                "result_path": ["next_dose"],
            }
        },
        "until": {"field": {"var": "measurement"}, "name": "qualified"},
        "bindings": {"measurement": {"kind": "node_result", "node_uuid": measure_uuid}},
        "node_uuids": [measure_uuid, adjust_uuid],
        "entry_node_uuids": [measure_uuid],
        "exit_node_uuids": [adjust_uuid],
    }
    plan = {
        "version": 2,
        "run_mode": "normal",
        "capabilities": [
            "condition_expression_v1",
            "control_regions_v1",
            "dynamic_iteration_jobs_v1",
        ],
        "nodes": [
            {
                "uuid": repeat_uuid,
                "parent_uuid": None,
                "kind": "repeat_until",
                "param": region,
                "control_region": region,
                "execution_policy": {},
                "action_resource_contract": {},
            },
            device_node(
                node_uuid=measure_uuid,
                parent_uuid=repeat_uuid,
                device_id="reactor-a",
                material_uuid=core_runtime.device_materials["reactor-a"],
                action="measure",
            ),
            device_node(
                node_uuid=adjust_uuid,
                parent_uuid=repeat_uuid,
                device_id="reactor-b",
                material_uuid=core_runtime.device_materials["reactor-b"],
                action="adjust",
                carry_bindings={
                    dose_handle: {
                        "control_region_uuid": repeat_uuid,
                        "key": "dose",
                    }
                },
            ),
            device_node(
                node_uuid=final_uuid,
                parent_uuid=None,
                device_id="robot-a",
                material_uuid=core_runtime.device_materials["robot-a"],
                action="finish",
            ),
        ],
        "handles": [
            {
                "uuid": dose_handle,
                "node_uuid": adjust_uuid,
                "data_source": "executor",
                "handle_key": "dose",
                "data_key": "dose",
                "io_type": "target",
            }
        ],
        "edges": [
            dependency(measure_uuid, adjust_uuid, name="repeat-body"),
            dependency(repeat_uuid, measure_uuid, name="repeat-entry"),
            dependency(repeat_uuid, final_uuid, name="repeat-exit"),
        ],
    }
    aggregate = core_runtime.submit_frozen(
        task_name="repeat",
        execution_plan=plan,
        jobs=[
            job(
                job_uuid=repeat_job,
                node_uuid=repeat_uuid,
                index=0,
                kind="repeat_until",
                param=region,
            ),
            job(
                job_uuid=final_job,
                node_uuid=final_uuid,
                index=3,
                kind="device_action",
            ),
        ],
    )

    return aggregate


def test_repeat_until_persists_each_iteration_before_dispatch(core_runtime):
    """RepeatUntil 每轮使用新 Job 身份，满足严格布尔条件后才派发后继。"""
    aggregate = submit_repeat_task(core_runtime)
    final_job = stable_uuid("job:repeat:final")
    first_measure = core_runtime.dispatcher.dispatched[-1]
    assert first_measure["action"] == "measure"
    core_runtime.scheduler.on_job_finished(
        first_measure["job_id"], True, {"qualified": False}
    )
    first_adjust = core_runtime.dispatcher.dispatched[-1]
    assert first_adjust["action"] == "adjust"
    assert first_adjust["action_args"]["dose"] == 1

    core_runtime.scheduler.on_job_finished(
        first_adjust["job_id"], True, {"next_dose": 2}
    )
    second_measure = core_runtime.dispatcher.dispatched[-1]
    assert second_measure["action"] == "measure"
    assert second_measure["job_id"] != first_measure["job_id"]
    core_runtime.scheduler.on_job_finished(
        second_measure["job_id"], True, {"qualified": True}
    )
    second_adjust = core_runtime.dispatcher.dispatched[-1]
    assert second_adjust["action"] == "adjust"
    assert second_adjust["action_args"]["dose"] == 2

    core_runtime.scheduler.on_job_finished(
        second_adjust["job_id"], True, {"next_dose": 3}
    )
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == final_job
    core_runtime.scheduler.on_job_finished(final_job, True, {"done": True})

    task_uuid = aggregate["task"]["uuid"]
    persisted_jobs = core_runtime.workflow_store.list_jobs(task_uuid)
    assert core_runtime.workflow_store.get_task(task_uuid)["status"] == "succeeded"
    assert len(persisted_jobs) == 6
    assert {item["status"] for item in persisted_jobs} == {"succeeded"}
    assert len({item["job_id"] for item in core_runtime.dispatcher.dispatched}) == 5


@pytest.mark.parametrize(
    ("qualified", "max_iterations", "repeat_status"),
    [(True, 2, "succeeded"), (False, 1, "failed")],
    ids=("exit", "iteration-limit"),
)
def test_repeat_tail_reconciles_or_latches_persistent_resource_interval(
    core_runtime: CoreRuntime,
    qualified: bool,
    max_iterations: int,
    repeat_status: str,
) -> None:
    """RepeatUntil 正常退出释放区间，失败则保留到人工整组解锁。"""

    repeat_uuid = stable_uuid("repeat-tail:control")
    measure_uuid = stable_uuid("repeat-tail:measure")
    final_uuid = stable_uuid("repeat-tail:final")
    parallel_uuid = stable_uuid("repeat-tail:parallel")
    repeat_job = stable_uuid("job:repeat-tail:control")
    final_job = stable_uuid("job:repeat-tail:final")
    parallel_job = stable_uuid("job:repeat-tail:parallel")
    region = {
        "predecessor_node_uuids": [],
        "successor_node_uuids": [final_uuid],
        "loop_variable": "loop",
        "max_iterations": max_iterations,
        "initial_carry": {},
        "next_carry": {},
        "until": {"field": {"var": "measurement"}, "name": "qualified"},
        "bindings": {
            "measurement": {
                "kind": "node_result",
                "node_uuid": measure_uuid,
            }
        },
        "node_uuids": [measure_uuid],
        "entry_node_uuids": [measure_uuid],
        "exit_node_uuids": [measure_uuid],
    }
    nodes = [
        {
            "uuid": repeat_uuid,
            "parent_uuid": None,
            "kind": "repeat_until",
            "param": region,
            "control_region": region,
            "execution_policy": {},
            "action_resource_contract": {},
        },
        device_node(
            node_uuid=measure_uuid,
            parent_uuid=repeat_uuid,
            device_id="reactor-a",
            material_uuid=core_runtime.device_materials["reactor-a"],
            action="measure",
        ),
        device_node(
            node_uuid=final_uuid,
            parent_uuid=None,
            device_id="reactor-b",
            material_uuid=core_runtime.device_materials["reactor-b"],
            action="final",
        ),
        device_node(
            node_uuid=parallel_uuid,
            parent_uuid=None,
            device_id="robot-a",
            material_uuid=core_runtime.device_materials["robot-a"],
            action="parallel",
        ),
    ]
    edges = [
        dependency(repeat_uuid, measure_uuid, name="repeat-tail-entry"),
        dependency(repeat_uuid, final_uuid, name="repeat-tail-exit"),
    ]
    resource_plan = attach_resource_plan(
        core_runtime,
        name="repeat-tail",
        nodes=nodes,
        edges=edges,
        resource_scopes=[
            {
                "scope_id": "repeat-tail-shared",
                "kind": "with",
                "resources": ["warehouse-a"],
                "node_uuids": [repeat_uuid, measure_uuid],
            }
        ],
    )
    owner = core_runtime.submit_frozen(
        task_name="repeat-tail-owner",
        execution_plan={
            "version": 2,
            "run_mode": "normal",
            "capabilities": [
                "condition_expression_v1",
                "control_regions_v1",
                "dynamic_iteration_jobs_v1",
                *resource_plan["capabilities"],
            ],
            "nodes": nodes,
            "handles": [],
            "edges": edges,
            "resource_plan": resource_plan,
        },
        jobs=[
            job(
                job_uuid=repeat_job,
                node_uuid=repeat_uuid,
                index=0,
                kind="repeat_until",
                param=region,
            ),
            job(job_uuid=final_job, node_uuid=final_uuid, index=2, kind="device_action"),
            job(
                job_uuid=parallel_job,
                node_uuid=parallel_uuid,
                index=3,
                kind="device_action",
            ),
        ],
    )
    owner_task_uuid = owner["task"]["uuid"]
    measure_job = next(
        item["job_id"]
        for item in core_runtime.dispatcher.dispatched
        if item["action"] == "measure"
    )
    shared_key = f"/devices/{core_runtime.device_materials['warehouse-a']}"
    inventory_keys, workflow_keys = active_resource_keys(
        core_runtime,
        task_uuid=owner_task_uuid,
    )
    assert shared_key in inventory_keys
    assert shared_key in workflow_keys

    waiter_job = stable_uuid("job:repeat-tail-waiter:0")
    core_runtime.submit(
        task_name="repeat-tail-waiter",
        devices=["warehouse-a"],
        priority="high",
    )
    assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "pending"

    core_runtime.scheduler.on_job_finished(
        measure_job,
        True,
        {"qualified": qualified},
    )

    assert core_runtime.workflow_store.get_job(repeat_job)["status"] == repeat_status
    if repeat_status == "failed":
        assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "pending"
        core_runtime.scheduler.on_job_finished(parallel_job, True, {})
        core_runtime.release_error_resources_and_resume(
            owner_task_uuid,
            command_uuid=stable_uuid("command:repeat-tail-failure-unlock"),
            reason="操作员确认循环失败后的设备、物料和库位均已核对",
        )
    assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "running"
    inventory_keys, workflow_keys = active_resource_keys(
        core_runtime,
        task_uuid=owner_task_uuid,
    )
    assert shared_key not in inventory_keys
    assert shared_key not in workflow_keys


def test_step_repeat_until_auto_evaluates_rounds_without_consuming_body_steps(
    core_runtime: CoreRuntime,
) -> None:
    """循环入口和 body 节点逐步执行，轮末 until/续轮物化由调度器自动完成。"""

    repeat_uuid = stable_uuid("step-repeat:control")
    measure_uuid = stable_uuid("step-repeat:measure")
    adjust_uuid = stable_uuid("step-repeat:adjust")
    final_uuid = stable_uuid("step-repeat:final")
    dose_handle = stable_uuid("step-handle:repeat:dose")
    region = {
        "predecessor_node_uuids": [],
        "successor_node_uuids": [final_uuid],
        "loop_variable": "loop",
        "max_iterations": 3,
        "initial_carry": {"dose": {"kind": "literal", "value": 1}},
        "next_carry": {
            "dose": {
                "kind": "node_result",
                "node_uuid": adjust_uuid,
                "result_path": ["next_dose"],
            }
        },
        "until": {"field": {"var": "measurement"}, "name": "qualified"},
        "bindings": {
            "measurement": {"kind": "node_result", "node_uuid": measure_uuid}
        },
        "node_uuids": [measure_uuid, adjust_uuid],
        "entry_node_uuids": [measure_uuid],
        "exit_node_uuids": [adjust_uuid],
    }
    plan = {
        "version": 2,
        "run_mode": "step",
        "capabilities": [
            "condition_expression_v1",
            "control_regions_v1",
            "dynamic_iteration_jobs_v1",
        ],
        "nodes": [
            {
                "uuid": repeat_uuid,
                "parent_uuid": None,
                "kind": "repeat_until",
                "param": region,
                "control_region": region,
                "execution_policy": {},
                "action_resource_contract": {},
            },
            device_node(
                node_uuid=measure_uuid,
                parent_uuid=repeat_uuid,
                device_id="reactor-a",
                material_uuid=core_runtime.device_materials["reactor-a"],
                action="measure",
            ),
            device_node(
                node_uuid=adjust_uuid,
                parent_uuid=repeat_uuid,
                device_id="reactor-b",
                material_uuid=core_runtime.device_materials["reactor-b"],
                action="adjust",
                carry_bindings={
                    dose_handle: {
                        "control_region_uuid": repeat_uuid,
                        "key": "dose",
                    }
                },
            ),
            device_node(
                node_uuid=final_uuid,
                parent_uuid=None,
                device_id="robot-a",
                material_uuid=core_runtime.device_materials["robot-a"],
                action="finish",
            ),
        ],
        "handles": [
            {
                "uuid": dose_handle,
                "node_uuid": adjust_uuid,
                "data_source": "executor",
                "handle_key": "dose",
                "data_key": "dose",
                "io_type": "target",
            }
        ],
        "edges": [
            dependency(measure_uuid, adjust_uuid, name="repeat-body"),
            dependency(repeat_uuid, measure_uuid, name="repeat-entry"),
            dependency(repeat_uuid, final_uuid, name="repeat-exit"),
        ],
    }
    aggregate = core_runtime.submit_frozen(
        task_name="step-repeat",
        execution_plan=plan,
        jobs=[
            job(
                job_uuid=stable_uuid("job:step-repeat:control"),
                node_uuid=repeat_uuid,
                index=0,
                kind="repeat_until",
                param=region,
            ),
            job(
                job_uuid=stable_uuid("job:step-repeat:final"),
                node_uuid=final_uuid,
                index=3,
                kind="device_action",
            ),
        ],
    )
    task_uuid = aggregate["task"]["uuid"]
    assert core_runtime.dispatcher.dispatched == []

    core_runtime.bridge.step(task_uuid, target_node_uuid=repeat_uuid)
    first_measure = core_runtime.bridge.step_state(task_uuid)["candidates"][0]
    assert first_measure["action_name"] == "measure"
    core_runtime.bridge.step(task_uuid, target_node_uuid=first_measure["node_id"])
    first_measure_job = core_runtime.dispatcher.dispatched[-1]
    core_runtime.scheduler.on_job_finished(
        first_measure_job["job_id"], True, {"qualified": False}
    )

    first_adjust = core_runtime.bridge.step_state(task_uuid)["candidates"][0]
    assert first_adjust["action_name"] == "adjust"
    core_runtime.bridge.step(task_uuid, target_node_uuid=first_adjust["node_id"])
    first_adjust_job = core_runtime.dispatcher.dispatched[-1]
    core_runtime.scheduler.on_job_finished(
        first_adjust_job["job_id"], True, {"next_dose": 2}
    )

    second_measure = core_runtime.bridge.step_state(task_uuid)["candidates"][0]
    assert second_measure["action_name"] == "measure"
    assert second_measure["node_id"] != first_measure["node_id"]
    core_runtime.bridge.step(task_uuid, target_node_uuid=second_measure["node_id"])
    second_measure_job = core_runtime.dispatcher.dispatched[-1]
    core_runtime.scheduler.on_job_finished(
        second_measure_job["job_id"], True, {"qualified": True}
    )

    second_adjust = core_runtime.bridge.step_state(task_uuid)["candidates"][0]
    core_runtime.bridge.step(task_uuid, target_node_uuid=second_adjust["node_id"])
    second_adjust_job = core_runtime.dispatcher.dispatched[-1]
    core_runtime.scheduler.on_job_finished(
        second_adjust_job["job_id"], True, {"next_dose": 3}
    )

    successor = core_runtime.bridge.step_state(task_uuid)["candidates"][0]
    assert successor["node_id"] == final_uuid
    persisted_task = core_runtime.workflow_store.get_task(task_uuid)
    assert persisted_task["status"] == "running"
    assert persisted_task["execution_mode"] == "step"
    assert persisted_task["control_status"] == "paused"


def submit_repeat_task_with_outer_value(core_runtime: CoreRuntime) -> dict:
    """循环体动作除 carry 外还消费区域外节点的输出值（跨区域值边）。

    pTLC 全流程 2026-09-17 复测：s9 的 ``review_candidate`` 在 ``条件→重复直到``
    内，同一组合参数 ``before_path``（来自 s4 拍照输出）在循环外的
    ``prepare_candidate`` 有值、循环内缺失，派发前被 Schema 拒绝。原因是规格编译
    只把区域内部边放进循环模板、顶层边只保留顶层端点，跨区域值边两边都不要。
    """

    prepare_uuid = stable_uuid("repeat-outer:prepare")
    repeat_uuid = stable_uuid("repeat-outer:control")
    measure_uuid = stable_uuid("repeat-outer:measure")
    adjust_uuid = stable_uuid("repeat-outer:adjust")
    final_uuid = stable_uuid("repeat-outer:final")
    prepare_job = stable_uuid("job:repeat-outer:prepare")
    repeat_job = stable_uuid("job:repeat-outer:control")
    final_job = stable_uuid("job:repeat-outer:final")
    dose_handle = stable_uuid("handle:repeat-outer:dose")
    path_source = stable_uuid("handle:repeat-outer:prepare.path")
    path_target = stable_uuid("handle:repeat-outer:adjust.before_path")
    region = {
        "predecessor_node_uuids": [prepare_uuid],
        "successor_node_uuids": [final_uuid],
        "loop_variable": "loop",
        "max_iterations": 3,
        "initial_carry": {"dose": {"kind": "literal", "value": 1}},
        "next_carry": {
            "dose": {"kind": "node_result", "node_uuid": adjust_uuid, "result_path": ["next_dose"]}
        },
        "until": {"field": {"var": "measurement"}, "name": "qualified"},
        "bindings": {"measurement": {"kind": "node_result", "node_uuid": measure_uuid}},
        "node_uuids": [measure_uuid, adjust_uuid],
        "entry_node_uuids": [measure_uuid],
        "exit_node_uuids": [adjust_uuid],
    }
    plan = {
        "version": 2,
        "run_mode": "normal",
        "capabilities": ["condition_expression_v1", "control_regions_v1", "dynamic_iteration_jobs_v1"],
        "nodes": [
            device_node(
                node_uuid=prepare_uuid, parent_uuid=None, device_id="robot-a",
                material_uuid=core_runtime.device_materials["robot-a"], action="prepare",
            ),
            {
                "uuid": repeat_uuid, "parent_uuid": None, "kind": "repeat_until",
                "param": region, "control_region": region,
                "execution_policy": {}, "action_resource_contract": {},
            },
            device_node(
                node_uuid=measure_uuid, parent_uuid=repeat_uuid, device_id="reactor-a",
                material_uuid=core_runtime.device_materials["reactor-a"], action="measure",
            ),
            device_node(
                node_uuid=adjust_uuid, parent_uuid=repeat_uuid, device_id="reactor-b",
                material_uuid=core_runtime.device_materials["reactor-b"], action="adjust",
                carry_bindings={dose_handle: {"control_region_uuid": repeat_uuid, "key": "dose"}},
            ),
            device_node(
                node_uuid=final_uuid, parent_uuid=None, device_id="robot-a",
                material_uuid=core_runtime.device_materials["robot-a"], action="finish",
            ),
        ],
        "handles": [
            {"uuid": dose_handle, "node_uuid": adjust_uuid, "data_source": "executor",
             "handle_key": "dose", "data_key": "dose", "io_type": "target"},
            {"uuid": path_source, "node_uuid": prepare_uuid, "data_source": "executor",
             "handle_key": "path", "data_key": "path", "io_type": "source"},
            {"uuid": path_target, "node_uuid": adjust_uuid, "data_source": "executor",
             "handle_key": "before_path", "data_key": "before_path", "io_type": "target"},
        ],
        "edges": [
            dependency(prepare_uuid, repeat_uuid, name="repeat-outer-entry"),
            {
                "uuid": stable_uuid("value-edge:repeat-outer:path"),
                "source_node_uuid": prepare_uuid, "target_node_uuid": adjust_uuid,
                "source_handle_uuid": path_source, "target_handle_uuid": path_target,
            },
            dependency(measure_uuid, adjust_uuid, name="repeat-outer-body"),
            dependency(repeat_uuid, measure_uuid, name="repeat-outer-body-entry"),
            dependency(repeat_uuid, final_uuid, name="repeat-outer-exit"),
        ],
    }
    return core_runtime.submit_frozen(
        task_name="repeat-outer",
        execution_plan=plan,
        jobs=[
            job(job_uuid=prepare_job, node_uuid=prepare_uuid, index=0, kind="device_action"),
            job(job_uuid=repeat_job, node_uuid=repeat_uuid, index=1, kind="repeat_until", param=region),
            job(job_uuid=final_job, node_uuid=final_uuid, index=4, kind="device_action"),
        ],
    )


def test_repeat_body_receives_outer_node_output_on_every_iteration(core_runtime):
    """跨区域值边必须在每一轮循环体里都绑定到动作参数，并进入持久化轮次作业。"""

    aggregate = submit_repeat_task_with_outer_value(core_runtime)
    prepare = core_runtime.dispatcher.dispatched[-1]
    assert prepare["action"] == "prepare"
    core_runtime.scheduler.on_job_finished(prepare["job_id"], True, {"path": "/img/before.jpg"})

    first_measure = core_runtime.dispatcher.dispatched[-1]
    assert first_measure["action"] == "measure"
    core_runtime.scheduler.on_job_finished(first_measure["job_id"], True, {"qualified": False})
    first_adjust = core_runtime.dispatcher.dispatched[-1]
    assert first_adjust["action"] == "adjust"
    assert first_adjust["action_args"]["dose"] == 1
    assert first_adjust["action_args"]["before_path"] == "/img/before.jpg"

    core_runtime.scheduler.on_job_finished(first_adjust["job_id"], True, {"next_dose": 2})
    second_measure = core_runtime.dispatcher.dispatched[-1]
    core_runtime.scheduler.on_job_finished(second_measure["job_id"], True, {"qualified": True})
    second_adjust = core_runtime.dispatcher.dispatched[-1]
    assert second_adjust["action"] == "adjust"
    assert second_adjust["action_args"]["dose"] == 2
    assert second_adjust["action_args"]["before_path"] == "/img/before.jpg"
    core_runtime.scheduler.on_job_finished(second_adjust["job_id"], True, {"next_dose": 3})
    final = core_runtime.dispatcher.dispatched[-1]
    assert final["action"] == "finish"
    core_runtime.scheduler.on_job_finished(final["job_id"], True, {"done": True})

    task_uuid = aggregate["task"]["uuid"]
    persisted = core_runtime.workflow_store.list_jobs(task_uuid)
    adjust_params = [
        item["param"] for item in persisted
        if item["workflow_node_uuid"] == stable_uuid("repeat-outer:adjust")
    ]
    assert len(adjust_params) == 2
    assert all(param.get("before_path") == "/img/before.jpg" for param in adjust_params)
    assert core_runtime.workflow_store.get_task(task_uuid)["status"] == "succeeded"


def test_spec_compiler_records_inbound_value_bindings_for_repeat_regions(core_runtime):
    """规格编译器把跨区域值边记成循环模板的 inbound_bindings，只依赖边不算。"""

    from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler

    aggregate = submit_repeat_task_with_outer_value(core_runtime)
    task_uuid = aggregate["task"]["uuid"]
    spec = WorkflowSpecCompiler().compile(
        core_runtime.workflow_store.get_task(task_uuid),
        core_runtime.workflow_store.list_jobs(task_uuid),
    )
    region = spec.repeat_regions[stable_uuid("repeat-outer:control")]
    assert region.inbound_bindings == {
        stable_uuid("repeat-outer:adjust"): [
            {
                "source_node_id": stable_uuid("repeat-outer:prepare"),
                "source_data_key": "path",
                "target_data_key": "before_path",
            }
        ]
    }


def test_explicit_interval_spanning_skipped_nested_repeat_is_released(core_runtime):
    """显式资源区间跨过"被条件跳过的分支里的嵌套循环"，分支跳过后区间仍须按时释放。

    pTLC 2026-09-17 v3 复测：s9 `photoscrape_process` 的显式 photoscrape/control
    作用域覆盖了人工复核循环（含嵌套对位内环）；`mode=auto` 时该分支被条件跳过，
    循环从未物化，内层控制节点没有任何内存状态。内存侧
    `_resource_interval_member_completed` 只看离成员最近的控制节点，判定成员
    未完成，区间在内存里永久持有；数据库租约早已释放，s10 首个动作等到超时。
    """

    acquire_uuid = stable_uuid("skipped-loop:acquire")
    condition_uuid = stable_uuid("skipped-loop:condition")
    repeat_uuid = stable_uuid("skipped-loop:repeat")
    inner_uuid = stable_uuid("skipped-loop:inner")
    review_uuid = stable_uuid("skipped-loop:review")
    auto_uuid = stable_uuid("skipped-loop:auto")
    release_uuid = stable_uuid("skipped-loop:release")
    tail_uuid = stable_uuid("skipped-loop:tail")
    acquire_job = stable_uuid("job:skipped-loop:acquire")
    condition_job = stable_uuid("job:skipped-loop:condition")
    repeat_job = stable_uuid("job:skipped-loop:repeat")
    auto_job = stable_uuid("job:skipped-loop:auto")
    release_job = stable_uuid("job:skipped-loop:release")
    tail_job = stable_uuid("job:skipped-loop:tail")
    inner_region = {
        "predecessor_node_uuids": [],
        "successor_node_uuids": [],
        "loop_variable": "inner",
        "max_iterations": 3,
        "initial_carry": {},
        "next_carry": {},
        "until": {"field": {"var": "verdict"}, "name": "done"},
        "bindings": {"verdict": {"kind": "node_result", "node_uuid": review_uuid}},
        "node_uuids": [review_uuid],
        "entry_node_uuids": [review_uuid],
        "exit_node_uuids": [review_uuid],
    }
    # 外层循环体只有一个嵌套循环：跳过外层后，内层控制节点从未物化、没有任何内存状态。
    repeat_region = {
        "predecessor_node_uuids": [],
        "successor_node_uuids": [],
        "loop_variable": "loop",
        "max_iterations": 3,
        "initial_carry": {},
        "next_carry": {},
        "until": {"field": {"var": "verdict"}, "name": "done"},
        "bindings": {"verdict": {"kind": "node_result", "node_uuid": review_uuid}},
        "node_uuids": [inner_uuid],
        "entry_node_uuids": [inner_uuid],
        "exit_node_uuids": [inner_uuid],
    }
    condition_region = {
        "predecessor_node_uuids": [acquire_uuid],
        "bindings": {"manual": {"kind": "workflow_input", "parameter": "manual"}},
        "branches": [
            {
                "label": "if",
                "condition": {"var": "manual"},
                "node_uuids": [repeat_uuid],
                "entry_node_uuids": [repeat_uuid],
                "exit_node_uuids": [repeat_uuid],
            },
            {
                "label": "else",
                "condition": None,
                "node_uuids": [auto_uuid],
                "entry_node_uuids": [auto_uuid],
                "exit_node_uuids": [auto_uuid],
            },
        ],
    }
    nodes = [
        device_node(
            node_uuid=acquire_uuid, parent_uuid=None, device_id="reactor-a",
            material_uuid=core_runtime.device_materials["reactor-a"], action="press",
        ),
        {
            "uuid": condition_uuid, "parent_uuid": None, "kind": "condition",
            "param": condition_region, "control_region": condition_region,
            "execution_policy": {}, "action_resource_contract": {},
        },
        {
            "uuid": repeat_uuid, "parent_uuid": condition_uuid, "kind": "repeat_until",
            "param": repeat_region, "control_region": repeat_region,
            "execution_policy": {}, "action_resource_contract": {},
        },
        {
            "uuid": inner_uuid, "parent_uuid": repeat_uuid, "kind": "repeat_until",
            "param": inner_region, "control_region": inner_region,
            "execution_policy": {}, "action_resource_contract": {},
        },
        device_node(
            node_uuid=review_uuid, parent_uuid=inner_uuid, device_id="robot-a",
            material_uuid=core_runtime.device_materials["robot-a"], action="review",
        ),
        device_node(
            node_uuid=auto_uuid, parent_uuid=condition_uuid, device_id="reactor-b",
            material_uuid=core_runtime.device_materials["reactor-b"], action="auto",
        ),
        device_node(
            node_uuid=release_uuid, parent_uuid=None, device_id="reactor-a",
            material_uuid=core_runtime.device_materials["reactor-a"], action="overlay",
        ),
        # 区间之后任务仍在运行（对应 pTLC 的 s10），持有者不能靠任务终态清理来释放。
        device_node(
            node_uuid=tail_uuid, parent_uuid=None, device_id="robot-a",
            material_uuid=core_runtime.device_materials["robot-a"], action="tail",
        ),
    ]
    edges = [
        dependency(acquire_uuid, condition_uuid, name="skipped-loop-enter"),
        dependency(condition_uuid, repeat_uuid, name="skipped-loop-if"),
        dependency(repeat_uuid, inner_uuid, name="skipped-loop-body"),
        dependency(inner_uuid, review_uuid, name="skipped-loop-inner-body"),
        dependency(condition_uuid, auto_uuid, name="skipped-loop-else"),
        dependency(repeat_uuid, release_uuid, name="skipped-loop-if-exit"),
        dependency(auto_uuid, release_uuid, name="skipped-loop-else-exit"),
        dependency(release_uuid, tail_uuid, name="skipped-loop-tail"),
    ]
    resource_plan = attach_resource_plan(
        core_runtime,
        name="skipped-loop",
        nodes=nodes,
        edges=edges,
        resource_scopes=[
            {
                "scope_id": "skipped-loop-shared",
                "kind": "with",
                "resources": ["warehouse-a"],
                "node_uuids": [acquire_uuid, repeat_uuid, inner_uuid, review_uuid, auto_uuid, release_uuid],
            }
        ],
    )
    owner = core_runtime.submit_frozen(
        task_name="skipped-loop-owner",
        execution_plan={
            "version": 2, "run_mode": "normal",
            "capabilities": [
                "condition_expression_v1", "control_regions_v1",
                "dynamic_iteration_jobs_v1", *resource_plan["capabilities"],
            ],
            "nodes": nodes, "handles": [], "edges": edges, "resource_plan": resource_plan,
        },
        jobs=[
            job(job_uuid=acquire_job, node_uuid=acquire_uuid, index=0, kind="device_action"),
            job(job_uuid=condition_job, node_uuid=condition_uuid, index=1, kind="condition", param=condition_region),
            job(job_uuid=repeat_job, node_uuid=repeat_uuid, index=2, kind="repeat_until", param=repeat_region),
            job(job_uuid=auto_job, node_uuid=auto_uuid, index=3, kind="device_action"),
            job(job_uuid=release_job, node_uuid=release_uuid, index=4, kind="device_action"),
            job(job_uuid=tail_job, node_uuid=tail_uuid, index=5, kind="device_action"),
        ],
        resolved_input={"manual": False},
    )
    owner_task_uuid = owner["task"]["uuid"]
    shared_key = f"/devices/{core_runtime.device_materials['warehouse-a']}"

    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == acquire_job
    core_runtime.scheduler.on_job_finished(acquire_job, True, {})
    assert core_runtime.workflow_store.get_job(repeat_job)["status"] == "skipped"
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == auto_job
    core_runtime.scheduler.on_job_finished(auto_job, True, {})
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == release_job
    inventory_keys, workflow_keys = active_resource_keys(core_runtime, task_uuid=owner_task_uuid)
    assert shared_key in workflow_keys

    waiter_job = stable_uuid("job:skipped-loop-waiter:0")
    core_runtime.submit(task_name="skipped-loop-waiter", devices=["warehouse-a"], priority="high")
    assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "pending"

    core_runtime.scheduler.on_job_finished(release_job, True, {})
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == tail_job
    assert core_runtime.workflow_store.get_task(owner_task_uuid)["status"] == "running"
    # 区间最后一个真实成员已完成、分支里的嵌套循环被跳过：共享键必须释放，等待者得到派发。
    assert core_runtime.workflow_store.get_job(waiter_job)["status"] == "running"
    inventory_keys, workflow_keys = active_resource_keys(core_runtime, task_uuid=owner_task_uuid)
    assert shared_key not in inventory_keys
    assert shared_key not in workflow_keys
    core_runtime.scheduler.on_job_finished(tail_job, True, {})
    assert core_runtime.workflow_store.get_task(owner_task_uuid)["status"] == "succeeded"
