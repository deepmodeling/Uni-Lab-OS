"""资源锁目标合同：图结构、整站无环与实际所有权。"""

from __future__ import annotations

import pytest

from unilabos.workflow.resource_lock_plan import (
    ResourcePlanError,
    bind_station_resource_plan,
    compile_template_resource_plan,
)


def edge(source, target):
    return {"source_node_uuid": source, "target_node_uuid": target}


def node(name, resources=(), branch="main", **kwargs):
    return {"uuid": name, "resource_defaults": list(resources), "branch_id": branch, **kwargs}


def intervals(plan, alias):
    resource = next(r for r in plan.resources if r.alias == alias)
    return [i for i in plan.intervals if i.resource_id == resource.resource_id]


def test_continuity_across_join_wait():
    plan = compile_template_resource_plan(
        {
            "nodes": [
                node("pick", ["robot"], "left"),
                node("prepare", ["camera"], "right"),
                node("place", ["robot"]),
            ],
            "edges": [edge("pick", "place"), edge("prepare", "place")],
        }
    )
    assert [i.node_uuids for i in intervals(plan, "robot")] == [("pick", "place")]


def test_outer_scope_is_one_owner_across_parallel():
    plan = compile_template_resource_plan(
        {
            "resources": ["station"],
            "nodes": [node("left", branch="left"), node("right", branch="right"), node("end")],
            "edges": [edge("left", "end"), edge("right", "end")],
        }
    )
    assert len(intervals(plan, "station")) == 1
    assert set(intervals(plan, "station")[0].node_uuids) == {"left", "right", "end"}


def test_join_competitor_breaks_only_safe_default_continuity():
    plan = compile_template_resource_plan(
        {
            "nodes": [
                node("left", ["robot"], "left"),
                node("right", ["robot"], "right"),
                node("end", ["robot"]),
            ],
            "edges": [edge("left", "end"), edge("right", "end")],
        }
    )
    assert len(intervals(plan, "robot")) == 3
    assert any(d["code"] == "join_continuity_released" for d in plan.diagnostics)


def test_shortcut_edge_cannot_hold_across_intermediate_release_boundary():
    plan = compile_template_resource_plan(
        {
            "nodes": [
                node("dose", ["tecan"]),
                node("move", ["tecan"], resource_default_boundary=True),
                node("pick", ["tecan"]),
            ],
            "edges": [edge("dose", "move"), edge("move", "pick"), edge("dose", "pick")],
        }
    )
    assert sorted(i.node_uuids for i in intervals(plan, "tecan")) == [("dose",), ("move",), ("pick",)]


def test_shortcut_edge_still_continues_through_intermediate_use():
    plan = compile_template_resource_plan(
        {
            "nodes": [node("dose", ["tecan"]), node("shake", ["tecan"]), node("pick", ["tecan"])],
            "edges": [edge("dose", "shake"), edge("shake", "pick"), edge("dose", "pick")],
        }
    )
    assert [i.node_uuids for i in intervals(plan, "tecan")] == [("dose", "shake", "pick")]


def test_resource_cycle_across_three_workflows_is_rejected():
    bindings = {r: {"canonical_key": f"/devices/{r}", "kind": "device"} for r in "ABC"}
    plans = []
    for a, b in [("A", "B"), ("B", "C"), ("C", "A")]:
        plans.append(
            bind_station_resource_plan(
                compile_template_resource_plan(
                    {
                        "workflow_uuid": a + b,
                        "nodes": [node("first", [a]), node("second", [a, b])],
                        "edges": [edge("first", "second")],
                    }
                ),
                bindings,
            )
        )
    with pytest.raises(ResourcePlanError, match="A.*B.*C.*A"):
        bind_station_resource_plan(plans[0], bindings, concurrency=plans[1:])


def test_aliases_bound_to_one_instance_reuse_ownership():
    plan = compile_template_resource_plan(
        {
            "resources": ["arm"],
            "nodes": [node("first", ["robot"]), node("last", ["robot"])],
            "edges": [edge("first", "last")],
        }
    )
    bound = bind_station_resource_plan(
        plan, {r: {"canonical_key": "/devices/robot", "kind": "device"} for r in ["arm", "robot"]}
    )
    assert len({r.resource_id for r in bound.resources}) == 1
    assert len(bound.intervals) == 1


def test_parallel_sensitive_actions_require_explicit_order():
    with pytest.raises(ResourcePlanError, match="顺序"):
        compile_template_resource_plan(
            {
                "nodes": [
                    node("open", ["robot"], "left", order_sensitive=True),
                    node("close", ["robot"], "right", order_sensitive=True),
                ],
            }
        )


def test_held_material_cannot_be_released_at_join_conflict():
    with pytest.raises(ResourcePlanError, match="交接"):
        compile_template_resource_plan(
            {
                "nodes": [
                    node("pick", ["robot"], "left", physical_hold_resources=["robot"]),
                    node("other", ["robot"], "right"),
                    node("place", ["robot"]),
                ],
                "edges": [edge("pick", "place"), edge("other", "place")],
            }
        )


def test_nonresource_action_breaks_unsafe_hold_without_explicit_scope():
    with pytest.raises(ResourcePlanError, match="交接"):
        compile_template_resource_plan(
            {
                "nodes": [
                    node("pick", ["robot"], physical_hold_resources=["robot"]),
                    node("inspect", ["camera"]),
                    node("place", ["robot"]),
                ],
                "edges": [edge("pick", "inspect"), edge("inspect", "place")],
            }
        )


def transfer_nodes():
    return [
        node(
            "pick",
            ["robot", "source"],
            transfer_step={
                "operation": "pick",
                "material": "plate",
                "endpoint_resource": "source",
                "endpoint_site": "source/site",
                "carrier_resources": ["robot", "rail"],
            },
        ),
        node(
            "place",
            ["robot", "target"],
            transfer_step={
                "operation": "place",
                "material": "plate",
                "endpoint_resource": "target",
                "endpoint_site": "target/site",
                "carrier_resources": ["robot", "rail"],
            },
        ),
    ]


def test_transfer_acquires_destination_before_pick_and_releases_source_early():
    plan = compile_template_resource_plan(
        {"nodes": transfer_nodes(), "edges": [edge("pick", "place")]}
    )
    names = {r.resource_id: r.alias for r in plan.resources}
    acquire = next(a for a in plan.acquire_sets if a.node_uuid == "pick")
    assert {names[r] for r in acquire.resource_ids} == {
        "robot",
        "rail",
        "source",
        "source/site",
        "target",
        "target/site",
    }
    assert not any(a.node_uuid == "place" for a in plan.acquire_sets)
    assert intervals(plan, "source")[0].release_node_uuid == "pick"
    assert intervals(plan, "target")[0].release_node_uuid == "place"


def test_transfer_missing_place_is_rejected():
    with pytest.raises(ResourcePlanError, match="配对"):
        compile_template_resource_plan({"nodes": transfer_nodes()[:1]})


def test_explicit_dependencies_exclude_nonoverlapping_reverse_relations():
    plan = compile_template_resource_plan(
        {
            "nodes": [node("a"), node("b", ["B"]), node("c"), node("d", ["A"])],
            "edges": [edge("a", "b"), edge("b", "c"), edge("c", "d")],
            "resource_scopes": [
                {"scope_id": "first", "kind": "with", "resources": ["A"], "node_uuids": ["a", "b"]},
                {
                    "scope_id": "second",
                    "kind": "with",
                    "resources": ["B"],
                    "node_uuids": ["c", "d"],
                },
            ],
        }
    )
    assert plan.relations == ()
    aliases_by_id = {resource.resource_id: resource.alias for resource in plan.resources}
    assert [
        {aliases_by_id[resource_id] for resource_id in scope.resource_ids}
        for scope in plan.scopes
        if scope.scope_id in {"first", "second"}
    ] == [{"A", "B"}, {"A", "B"}]


def test_resource_binding_changes_plan_identity():
    template = compile_template_resource_plan({"nodes": [node("a", ["robot"])]})
    a = bind_station_resource_plan(template, {"robot": {"canonical_key": "/devices/a"}})
    b = bind_station_resource_plan(template, {"robot": {"canonical_key": "/devices/b"}})
    assert a.plan_id != b.plan_id


def test_executor_default_does_not_hide_auxiliary_action_resources():
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder

    plan = ExecutionPlanBuilder._resource_plan(
        graph={"resource_bindings": {"rail": {"kind": "device", "canonical_key": "/devices/rail"}}},
        planned_nodes=[
            {
                "uuid": "move",
                "kind": "device_action",
                "material_uuid": "00000000-0000-4000-8000-000000000001",
                "action_resource_contract": {
                    "version": 2,
                    "resource_params": [{"param": "rail", "role": "motion"}],
                },
            }
        ],
        planned_edges=[],
    )
    assert {r.canonical_key for r in plan.resources} == {
        "/devices/00000000-0000-4000-8000-000000000001",
        "/devices/rail",
    }


def test_transfer_role_lists_are_part_of_static_footprint():
    plan = compile_template_resource_plan(
        {
            "nodes": [
                {
                    "uuid": "move",
                    "resource_defaults": ["robot"],
                    "action_resource_contract": {
                        "version": 2,
                        "transfer": {
                            "motion_resource_roles": ["rail"],
                            "tool_resource_roles": ["gripper"],
                        },
                    },
                }
            ]
        }
    )
    assert {r.alias for r in plan.resources} == {"robot", "rail", "gripper"}


def test_contract_aliases_bound_to_same_resource_are_merged():
    plan = compile_template_resource_plan(
        {
            "nodes": [
                {
                    "uuid": "move",
                    "resource_defaults": ["robot"],
                    "action_resource_contract": {
                        "version": 2,
                        "resource_params": [{"param": "executor", "role": "device"}],
                    },
                }
            ]
        }
    )
    bound = bind_station_resource_plan(
        plan,
        {r: {"canonical_key": "/devices/robot", "kind": "device"} for r in ("robot", "executor")},
    )
    assert len(bound.resources) == len(bound.intervals) == 1


def test_runtime_accepts_transfer_role_lists_and_rejects_unsettled_step():
    from types import SimpleNamespace

    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.app.scheduler.transfer_resource_set import TransferResourceSetError

    transfer = dict.fromkeys(
        (
            "material_param",
            "target_owner_param",
            "target_site_uuid_param",
            "target_site_name_param",
            "gripper_site_role",
        ),
        "param",
    )
    contract = {
        "version": 2,
        "transfer": {
            **transfer,
            "motion_resource_roles": ["rail"],
            "tool_resource_roles": ["gripper"],
        },
    }
    assert (
        EdgeScheduler._transfer_resource_contract(
            SimpleNamespace(action_resource_contract=contract)
        )["material_param"]
        == "param"
    )
    with pytest.raises(TransferResourceSetError, match="合同"):
        EdgeScheduler._transfer_resource_contract(
            SimpleNamespace(action_resource_contract={"transfer_step": {"operation": "pick"}})
        )
    assert (
        EdgeScheduler._transfer_resource_contract(
            SimpleNamespace(
                action_name="transfer_resource",
                executor_kind="material_transfer",
                action_resource_contract={},
            )
        )
        is None
    )
    with pytest.raises(TransferResourceSetError, match="合同"):
        EdgeScheduler._transfer_resource_contract(
            SimpleNamespace(
                action_name="add_liquid_atomic",
                executor_kind="material_transfer",
                action_resource_contract={},
            )
        )


def test_parallel_scopes_merge_complete_sets_to_eliminate_incremental_cycle():
    """并行作用域在入口原子取得完整集合，不再生成中途反向取得环。"""
    graph = {
        "nodes": [node("a0"), node("a1"), node("b0", ["B"]), node("c0"), node("c1", ["A"])],
        "edges": [edge("a0", "a1"), edge("a1", "c0"), edge("c0", "c1")],
        "resource_scopes": [
            {
                "scope_id": "A_scope",
                "kind": "with",
                "resources": ["A"],
                "node_uuids": ["a0", "a1", "b0"],
            },
            {"scope_id": "B_scope", "kind": "with", "resources": ["B"], "node_uuids": ["c0", "c1"]},
        ],
    }
    plan = compile_template_resource_plan(graph)
    aliases_by_id = {resource.resource_id: resource.alias for resource in plan.resources}
    assert plan.relations == ()
    assert all(
        {aliases_by_id[resource_id] for resource_id in scope.resource_ids}
        == {"A", "B"}
        for scope in plan.scopes
        if scope.scope_id in {"A_scope", "B_scope"}
    )


@pytest.mark.parametrize("outer_scope", [False, True])
@pytest.mark.parametrize("restart_during_first_round", [False, True])
def test_repeat_second_iteration_keeps_persisted_resource_claim(
    core_runtime, monkeypatch, outer_scope, restart_during_first_round
):
    """旧轮次的 adjust 完成记录不能使第二轮 measure 提前释放真实库存租约。"""
    from tests.scheduler_core.conftest import stable_uuid
    from tests.scheduler_core.test_control_flow import dependency, device_node, job
    from unilabos.workflow.resource_lock_plan import serialize_resource_plan

    runtime = core_runtime
    repeat, measure, adjust, final = [
        stable_uuid(f"lease-repeat:{n}") for n in ("repeat", "measure", "adjust", "final")
    ]
    repeat_job, final_job = stable_uuid("lease-repeat:control-job"), stable_uuid(
        "lease-repeat:final-job"
    )
    region = {
        "predecessor_node_uuids": [],
        "successor_node_uuids": [final],
        "loop_variable": "loop",
        "max_iterations": 3,
        "initial_carry": {},
        "next_carry": {},
        "until": {"field": {"var": "measurement"}, "name": "qualified"},
        "bindings": {"measurement": {"kind": "node_result", "node_uuid": measure}},
        "node_uuids": [measure, adjust],
        "entry_node_uuids": [measure],
        "exit_node_uuids": [adjust],
    }
    nodes = [
        {
            "uuid": repeat,
            "parent_uuid": None,
            "kind": "repeat_until",
            "param": region,
            "control_region": region,
            "execution_policy": {},
            "action_resource_contract": {},
        }
    ]
    for identifier, name, parent, device in [
        (measure, "measure", repeat, "reactor-a"),
        (adjust, "adjust", repeat, "reactor-a"),
        (final, "finish", None, "robot-a"),
    ]:
        nodes.append(
            device_node(
                node_uuid=identifier,
                parent_uuid=parent,
                device_id=device,
                material_uuid=runtime.device_materials[device],
                action=name,
            )
        )
    edges = [
        dependency(measure, adjust, name="lease-body"),
        dependency(repeat, measure, name="lease-entry"),
        dependency(repeat, final, name="lease-exit"),
    ]
    resource_plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(
                {
                    "nodes": [
                        dict(n, resource_defaults=[n["device_id"]] if n.get("device_id") else [])
                        for n in nodes
                    ],
                    "edges": edges,
                    **({"resources": ["warehouse-a"]} if outer_scope else {}),
                }
            ),
            {
                k: {"canonical_key": f"/devices/{v}", "kind": "device"}
                for k, v in runtime.device_materials.items()
            },
        )
    )
    for n in nodes:
        n["resource_plan_id"] = resource_plan["plan_id"]
        n["resource_interval_ids"] = [
            i["interval_id"] for i in resource_plan["intervals"] if n["uuid"] in i["node_uuids"]
        ]
        n["resource_acquire_set_id"] = next(
            (
                a["acquire_set_id"]
                for a in resource_plan["acquire_sets"]
                if a["node_uuid"] == n["uuid"]
            ),
            "",
        )
    kept_claims = []
    inventory = runtime.scheduler.station_resource_inventory
    original_retain = inventory.retain_dispatch_permit_resources

    def record_retain(claim_uuid, **kwargs):
        kept_claims.append(claim_uuid)
        return original_retain(claim_uuid, **kwargs)

    monkeypatch.setattr(inventory, "retain_dispatch_permit_resources", record_retain)
    aggregate = runtime.submit_frozen(
        task_name="lease-repeat",
        execution_plan={
            "version": 2,
            "run_mode": "normal",
            "nodes": nodes,
            "edges": edges,
            "handles": [],
            "resource_plan": resource_plan,
            "capabilities": [
                "condition_expression_v1",
                "control_regions_v1",
                "dynamic_iteration_jobs_v1",
                *resource_plan["capabilities"],
            ],
        },
        jobs=[
            job(job_uuid=repeat_job, node_uuid=repeat, index=0, kind="repeat_until", param=region),
            job(job_uuid=final_job, node_uuid=final, index=3, kind="device_action"),
        ],
    )
    for iteration in range(2):
        first = runtime.dispatcher.dispatched[-1]
        assert first["action"] == "measure"
        claim = runtime.bridge._projection.get_execution_claim(first["job_id"])
        runtime.scheduler.on_job_finished(first["job_id"], True, {"qualified": iteration == 1})
        assert claim["claim_uuid"] in kept_claims
        second = runtime.dispatcher.dispatched[-1]
        assert second["action"] == "adjust"
        if restart_during_first_round and iteration == 0:
            dispatched_count = len(runtime.dispatcher.dispatched)
            runtime.restart_scheduler()
            assert len(runtime.dispatcher.dispatched) == dispatched_count
        runtime.scheduler.on_job_finished(second["job_id"], True, {})
    assert runtime.dispatcher.dispatched[-1]["job_id"] == final_job
    runtime.scheduler.on_job_finished(final_job, True, {})
    assert runtime.workflow_store.get_task(aggregate["task"]["uuid"])["status"] == "succeeded"
    assert (
        runtime.inventory_store.query_all(
            "SELECT claim_uuid FROM station_execution_claim WHERE state IN ('prepared','reserved','running','uncertain')"
        )
        == []
    )


def test_root_scope_merges_transfer_carrier_through_place():
    def step(operation, resource, site):
        return {
            "operation": operation,
            "material": "plate",
            "endpoint_resource": resource,
            "endpoint_site": site,
            "carrier_resources": ["robot"],
        }

    plan = compile_template_resource_plan(
        {
            "resources": ["target", "target-site"],
            "nodes": [
                node("pick", transfer_step=step("pick", "source", "source-site")),
                node("move", ["robot"]),
                node("camera", ["camera"]),
                node("place", transfer_step=step("place", "target", "target-site")),
            ],
            "edges": [edge("pick", "move"), edge("move", "camera"), edge("camera", "place")],
        }
    )
    assert intervals(plan, "robot")[0].node_uuids == (
        "pick",
        "move",
        "camera",
        "place",
    )


def test_bound_builder_merges_aliases_before_rejecting_symbolic_cycle():
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder

    graph = {
        "resource_scopes": [
            {"scope_id": "one", "kind": "with", "resources": ["first"], "node_uuids": ["a", "b"]},
            {"scope_id": "two", "kind": "with", "resources": ["second"], "node_uuids": ["b", "c"]},
        ],
        "resource_bindings": {
            alias: {"canonical_key": "/devices/R", "kind": "device"}
            for alias in ("first", "second")
        },
    }
    plan = ExecutionPlanBuilder._resource_plan(
        graph=graph,
        planned_nodes=[node("a"), node("b"), node("c", ["first"])],
        planned_edges=[edge("a", "b"), edge("b", "c")],
    )
    assert len(plan.resources) == len(plan.intervals) == 1
    assert plan.intervals[0].node_uuids == ("a", "b", "c")


def test_bound_kinds_share_the_same_runtime_device_identity():
    instance = "00000000-0000-4000-8000-000000000111"
    template = compile_template_resource_plan({"nodes": [node("move", ["motor", "rail"])]})
    bound = bind_station_resource_plan(
        template,
        {
            "motor": {"instance_uuid": instance, "kind": "device"},
            "rail": {"instance_uuid": instance, "kind": "motion"},
        },
    )
    assert len(bound.resources) == len(bound.intervals) == 1
    assert bound.resources[0].canonical_key == f"/devices/{instance}"


def test_transfer_descriptor_alias_binding_does_not_reintroduce_merged_alias():
    def step(name, operation, owner, site):
        return node(
            name,
            param={"arm": "robot", "owner": owner, "site": site, "material": "plate"},
            action_resource_contract={
                "resource_params": [{"param": "arm"}],
                "transfer_step": {
                    "operation": operation,
                    "material_param": "material",
                    "owner_param": "owner",
                    "site_param": "site",
                    "carrier_params": ["arm"],
                },
            },
        )

    template = compile_template_resource_plan(
        {
            "nodes": [step("p", "pick", "source", "s"), step("q", "place", "target", "t")],
            "edges": [edge("p", "q")],
        }
    )
    bound = bind_station_resource_plan(
        template,
        {
            r.alias: {
                "canonical_key": "/devices/" + ("robot" if r.alias == "arm" else r.alias),
                "kind": "device",
            }
            for r in template.resources
        },
    )
    assert len([r for r in bound.resources if r.canonical_key == "/devices/robot"]) == 1


def test_material_transfer_hold_ends_before_later_ordinary_actions():
    """搬运物料只续持到配对 place，后续普通动作仍逐 Job 释放。"""

    sample_uuid = "00000000-0000-4000-8000-000000000099"
    material_alias = f"material:{sample_uuid}"
    transfer = transfer_nodes()
    for transfer_node in transfer:
        transfer_node["resource_defaults"].append(material_alias)
        transfer_node["transfer_step"]["material"] = sample_uuid
    graph = {
        "nodes": [
            *transfer,
            node("ordinary-1", [material_alias]),
            node("ordinary-2", [material_alias]),
        ],
        "edges": [
            edge("pick", "place"),
            edge("place", "ordinary-1"),
            edge("ordinary-1", "ordinary-2"),
        ],
    }
    template = compile_template_resource_plan(graph)
    bindings = {}
    for index, resource in enumerate(template.resources, start=1):
        is_material = resource.alias == material_alias
        bindings[resource.alias] = {
            "instance_uuid": (
                sample_uuid
                if is_material
                else f"00000000-0000-4000-8000-{index:012d}"
            ),
            "kind": "material" if is_material else "device",
        }

    bound = bind_station_resource_plan(template, bindings)

    assert {interval.node_uuids for interval in intervals(bound, material_alias)} == {
        ("ordinary-1",),
        ("ordinary-2",),
        ("pick", "place"),
    }


@pytest.mark.parametrize("terminal", ["failed", "skipped", "canceled", "timeout"])
def test_failed_or_skipped_place_cannot_release_successful_pick(terminal):
    from unilabos.workflow.resource_lock_plan import (
        completed_resource_nodes_for_job,
        continuing_resource_interval_ids,
        serialize_resource_plan,
    )

    def transfer(operation, owner, site):
        return {
            "operation": operation,
            "material": "plate",
            "endpoint_resource": owner,
            "endpoint_site": site,
            "carrier_resources": ["robot"],
        }

    plan = serialize_resource_plan(
        compile_template_resource_plan(
            {
                "nodes": [
                    node("pick", transfer_step=transfer("pick", "source", "s")),
                    node("place", transfer_step=transfer("place", "target", "t")),
                ],
                "edges": [edge("pick", "place")],
            }
        )
    )
    jobs = [
        {"workflow_node_uuid": "pick", "status": "succeeded"},
        {"workflow_node_uuid": "place", "status": terminal},
    ]
    completed = completed_resource_nodes_for_job(jobs[0], jobs, plan)
    assert "place" not in completed
    robot = next(r["resource_id"] for r in plan["resources"] if r["alias"] == "robot")
    ids = [i["interval_id"] for i in plan["intervals"] if i["resource_id"] == robot]
    assert continuing_resource_interval_ids(plan, ids, "pick", completed) == tuple(ids)
    assert continuing_resource_interval_ids(
        plan, ids, "place", completed, current_completed=False
    ) == tuple(ids)
    from unilabos.workflow.task_scheduler_bridge import _retained_interval_ids_for_result

    jobs[1]["control_data"] = {"resource_interval_ids": ids}
    assert _retained_interval_ids_for_result({"execution_plan": plan}, jobs[1], jobs) == tuple(
        ids
    )


def test_shared_scopes_acquired_at_same_ancestor_have_no_synthetic_cycle():
    plan = compile_template_resource_plan(
        {
            "nodes": [node(str(i)) for i in range(4)],
            "edges": [edge("0", str(i)) for i in range(1, 4)],
            "resource_scopes": [
                {
                    "scope_id": "A",
                    "kind": "with",
                    "resources": ["A"],
                    "node_uuids": ["0", "1", "2"],
                },
                {
                    "scope_id": "B",
                    "kind": "with",
                    "resources": ["B"],
                    "node_uuids": ["0", "1", "2", "3"],
                },
            ],
        }
    )
    assert plan.relations == ()


@pytest.mark.parametrize("role", ["device", "motion", "tool"])
def test_builder_rejects_v2_binding_different_from_frozen_parameter(role):
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder, ExecutionPlanBuildError

    one, two = "00000000-0000-4000-8000-000000000111", "00000000-0000-4000-8000-000000000222"
    with pytest.raises(ExecutionPlanBuildError, match="不一致"):
        ExecutionPlanBuilder._resource_plan(
            graph={"resource_bindings": {"rail": {"instance_uuid": one, "kind": role}}},
            planned_nodes=[
                node(
                    "move",
                    param={"rail": {"uuid": two}},
                    action_resource_contract={
                        "version": 2,
                        "resource_params": [{"param": "rail", "role": role}],
                    },
                )
            ],
            planned_edges=[],
        )


def test_v2_motion_contract_locks_actual_inventory_device(core_runtime):
    from tests.scheduler_core.conftest import stable_uuid
    from tests.scheduler_core.test_control_flow import device_node, job
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder
    from unilabos.workflow.execution_resource_policy import merge_action_resource_policy
    from unilabos.workflow.resource_lock_plan import serialize_resource_plan

    runtime = core_runtime
    identifier, job_id = stable_uuid("v2-motion-node"), stable_uuid("v2-motion-job")
    rail = runtime.device_materials["warehouse-a"]
    contract = {"version": 2, "resource_params": [{"param": "rail", "role": "motion"}]}
    planned = device_node(
        node_uuid=identifier,
        parent_uuid=None,
        device_id="reactor-a",
        material_uuid=runtime.device_materials["reactor-a"],
        action="move",
    )
    planned["param"] = {"rail": {"uuid": rail}}
    planned["action_resource_contract"] = contract
    planned["execution_policy"] = merge_action_resource_policy(contract, {})
    planned["param_schema"] = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "rail": {
                        "type": "object",
                        "properties": {"uuid": {"type": "string", "format": "uuid"}},
                        "required": ["uuid"],
                        "x-unilabos-material-lock": False,
                    }
                },
            }
        },
    }
    plan = serialize_resource_plan(
        ExecutionPlanBuilder._resource_plan(graph={}, planned_nodes=[planned], planned_edges=[])
    )
    planned["resource_plan_id"] = plan["plan_id"]
    planned["resource_interval_ids"] = [i["interval_id"] for i in plan["intervals"]]
    planned["resource_acquire_set_id"] = plan["acquire_sets"][0]["acquire_set_id"]
    runtime.submit_frozen(
        task_name="v2-motion",
        execution_plan={
            "version": 1,
            "run_mode": "normal",
            "nodes": [planned],
            "edges": [],
            "handles": [],
            "resource_plan": plan,
            "capabilities": plan["capabilities"],
        },
        jobs=[
            dict(
                job(job_uuid=job_id, node_uuid=identifier, index=0, kind="device_action"),
                param=planned["param"],
                execution_policy=planned["execution_policy"],
            )
        ],
    )
    assert runtime.dispatcher.dispatched[-1]["job_id"] == job_id
    runtime.submit(task_name="v2-motion-waiter", devices=["warehouse-a"], priority="high")
    assert len(runtime.dispatcher.dispatched) == 1
    runtime.scheduler.on_job_finished(job_id, True, {})
    assert runtime.dispatcher.dispatched[-1]["job_id"] == stable_uuid("job:v2-motion-waiter:0")


def test_transfer_static_role_alias_binds_without_a_goal_parameter():
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder

    instance = "00000000-0000-4000-8000-000000000111"
    plan = ExecutionPlanBuilder._resource_plan(
        graph={
            "resource_bindings": {"station:rail": {"instance_uuid": instance, "kind": "motion"}}
        },
        planned_nodes=[
            node(
                "move",
                param={},
                action_resource_contract={
                    "version": 2,
                    "transfer": {"motion_resource_roles": ["station:rail"]},
                },
            )
        ],
        planned_edges=[],
    )
    assert [r.canonical_key for r in plan.resources] == [f"/devices/{instance}"]


def test_outer_iteration_sees_its_nested_rounds_without_other_outer_iteration():
    from unilabos.workflow.resource_lock_plan import completed_resource_nodes_for_job

    def candidate(node_id, status, path, iteration, runtime):
        return {
            "workflow_node_uuid": node_id,
            "status": status,
            "meta_data": {
                "unilab": {
                    "control_path": path,
                    "iteration_index": iteration,
                    "runtime_node_id": runtime,
                }
            },
        }

    current = candidate("after-inner", "succeeded", "outer", 0, "after-0")
    jobs = [
        current,
        candidate("inner", "succeeded", "outer", 0, "inner-0"),
        candidate("body", "succeeded", "inner-0", 0, "body-0"),
        candidate("body", "succeeded", "inner-0", 1, "body-1"),
        candidate("inner", "pending", "outer", 1, "inner-1"),
        candidate("body", "pending", "inner-1", 0, "body-2"),
    ]
    assert completed_resource_nodes_for_job(current, jobs) == {"after-inner", "inner", "body"}
    jobs[3]["status"] = "running"
    assert "body" not in completed_resource_nodes_for_job(current, jobs)

def test_parallel_scope_peers_remain_atomic_when_transfer_spans_next_scope():
    """跨词法范围的搬运区间仍保留共同原子入口，不产生兄弟准备动作假环。"""
    pick, place = transfer_nodes()
    plan = compile_template_resource_plan({
        "nodes": [node("outbound", ["source"]), node("prepare"), pick, place],
        "edges": [edge("outbound", "pick"), edge("pick", "place"), edge("prepare", "place")],
        "resource_scopes": [
            {"scope_id": "prepare-stage", "kind": "with", "resources": ["stack", "turntable"],
             "node_uuids": ["outbound", "prepare", "pick"]},
            {"scope_id": "place-stage", "kind": "with", "resources": ["turntable"],
             "node_uuids": ["place"]},
        ],
    })
    robot, = intervals(plan, "robot")
    rail, = intervals(plan, "rail")
    assert robot.scope_id == "prepare-stage"
    assert robot.acquire_node_uuid == rail.acquire_node_uuid
    assert not any(
        {relation.from_resource_id, relation.to_resource_id} == {robot.resource_id, rail.resource_id}
        for relation in plan.relations
    )


def test_reservation_handoff_and_extended_custody_share_atomic_entry():
    """复现 Stage B：外部出库交接与跨预留出口的持料不能制造同批取得假环。"""
    from copy import deepcopy

    from tests.scheduler_core.test_physical_hold_scope_boundary import graph as physical_graph
    workflow = deepcopy(physical_graph)
    workflow['edges'] = [edge for edge in workflow['edges']
                         if (edge['source_node_uuid'], edge['target_node_uuid']) != ('01', '03')]
    workflow['resource_scopes'] = [{
        'scope_id': 'resource-scope', 'kind': 'reservation',
        'resources': ['stack', 'stack-site', 'center-arm', 'center-site', 'turntable',
                      'plate-site', 'west-arm', 'west-site', 'g3', 'g3-input'],
        'node_uuids': ['02', '03', '04', '05', '06', '07'],
    }]
    for start, end, resources in [
        ('01', '02', ['stack', 'stack-site']),
        ('02', '04', ['center-arm', 'center-site']),
        ('04', '07', ['turntable', 'plate-site']),
        ('07', '08', ['west-arm', 'west-site']),
        ('08', '09', ['g3', 'g3-input']),
    ]:
        workflow['resource_scopes'].append({
            'scope_id': f'site-handoff:{start}:{end}', 'kind': 'transfer',
            'resources': resources, 'node_uuids': [start, end],
            'source': 'inventory.material_site_handoff',
        })
    # 真实失败图的独立准备 UUID 排在出库之前，二者共享同一原子取得入口。
    for item in workflow['nodes']:
        if item['uuid'] == '03':
            item['uuid'] = '00-prepare'
    for edge in workflow['edges']:
        for key in ('source_node_uuid', 'target_node_uuid'):
            if edge[key] == '03':
                edge[key] = '00-prepare'
    workflow['resource_scopes'][0]['node_uuids'][1] = '00-prepare'
    plan = compile_template_resource_plan(workflow)
    by_alias = {resource.alias: resource.resource_id for resource in plan.resources}
    held = {alias: next(item for item in plan.intervals if item.resource_id == by_alias[alias])
            for alias in ('stack-site', 'west-arm', 'g3-input')}
    assert {item.acquire_node_uuid for item in held.values()} == {'00-prepare'}
    assert '08' in held['west-arm'].node_uuids
    assert '09' in held['g3-input'].node_uuids
    assert '08' not in held['stack-site'].node_uuids
    # 出库可独立先持有 stack-site，因此该资源到后续预约资源的取得关系仍然
    # 合法；仅凭代表入口 UUID 相同就删除它，会掩盖其他图的真实取得环。
    assert any(relation.from_resource_id == held['stack-site'].resource_id
               and relation.to_resource_id == held['west-arm'].resource_id
               for relation in plan.relations)
    assert not any(relation.from_resource_id in {held['west-arm'].resource_id, held['g3-input'].resource_id}
                   and relation.to_resource_id == held['stack-site'].resource_id
                   for relation in plan.relations)
