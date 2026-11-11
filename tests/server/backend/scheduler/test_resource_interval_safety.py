"""资源锁目标合同：图结构、整站无环与实际所有权。"""


from __future__ import annotations


import pytest


from unilabos.server.backend.scheduler.resource_plan import (
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


def test_outer_iteration_sees_its_nested_rounds_without_other_outer_iteration():
    from unilabos.server.backend.scheduler.resource_plan import completed_resource_nodes_for_job

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
