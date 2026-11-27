from dataclasses import replace

import pytest

from unilabos.workflow import resource_lock_plan as locks


def _plan():
    names = ("a", "b", "c", "d")
    intervals = tuple(
        locks.ResourceInterval(key, key, key, key, (key,), None, "local", "")
        for key in names
    )
    return locks.ResourcePlan(
        plan_id="test",
        resources=tuple(locks.CanonicalResource(k, "symbol:" + k, "device", k) for k in names),
        intervals=intervals,
        relations=tuple(
            locks.ResourceRelation(a + b, a, b, source_interval_id=a)
            for a in names for b in names if a != b
        ),
        metadata={"dependency_edges": list(zip(names, names[1:]))},
    )


def test_serial_intervals_share_one_dependency_closure(monkeypatch):
    calls = 0
    original = locks._reachability

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(locks, "_reachability", counted)
    locks.validate_resource_plan(_plan())
    assert calls == 1


def test_external_candidate_can_still_complete_a_real_cycle():
    plan = _plan()
    extra = replace(plan.relations[0], relation_id="external", reason="Workflow other")
    with pytest.raises(locks.ResourcePlanError) as caught:
        locks.validate_resource_plan(replace(plan, relations=plan.relations + (extra,)))
    assert caught.value.code == "resource_cycle"


def test_serial_reacquisition_after_release_compiles():
    steps = [
        ("first_a", ["A"]),
        ("take_b", ["A", "B"]),
        ("keep_b", ["B"]),
        ("retake_a", ["A", "B"]),
    ]
    graph = {
        "nodes": [{"uuid": name, "resource_defaults": keys} for name, keys in steps],
        "edges": [
            {"source_node_uuid": left[0], "target_node_uuid": right[0]}
            for left, right in zip(steps, steps[1:])
        ],
    }
    plan = locks.compile_template_resource_plan(graph)
    assert {rel.source_node_uuid for rel in plan.relations} == {"take_b", "retake_a"}


@pytest.mark.parametrize("other_request", [None, "", "outside", "early"])
def test_release_proof_is_specific_to_the_request_node(other_request):
    # The holders overlap, but B is released before the late A -> B request.
    nodes = ["hold_a", "hold_b", "early", "release_b", "late", "release_a"]
    plan = locks.ResourcePlan(
        plan_id="request-time",
        resources=tuple(locks.CanonicalResource(k, "symbol:" + k, "device", k) for k in "ab"),
        intervals=(
            locks.ResourceInterval("ia", "a", "hold_a", "release_a", ("hold_a", "release_a"), None, "local", ""),
            locks.ResourceInterval("ib", "b", "hold_b", "release_b", ("hold_b", "release_b"), None, "local", ""),
        ),
        relations=(
            locks.ResourceRelation("ab", "a", "b", "ia", "late"),
            locks.ResourceRelation("ba", "b", "a", "ib", "early"),
        ),
        metadata={"dependency_edges": list(zip(nodes, nodes[1:]))},
    )
    if other_request is None:
        locks.validate_resource_plan(plan)
        return
    alternative = replace(
        plan.relations[0],
        relation_id="alternative",
        source_node_uuid="late" if other_request == "outside" else other_request,
        reason="Workflow external" if other_request == "outside" else "local",
    )
    with pytest.raises(locks.ResourcePlanError) as error:
        locks.validate_resource_plan(replace(plan, relations=plan.relations + (alternative,)))
    assert error.value.code == "resource_cycle"


@pytest.mark.parametrize("keep_first", [False, True])
def test_parallel_branches_visit_same_resources_in_reverse(keep_first):
    nodes, edges = [], []
    for branch, first, second in [("left", "A", "B"), ("right", "B", "A")]:
        start, end = f"{branch}_start", f"{branch}_end"
        nodes.extend([
            {"uuid": start, "branch_id": branch, "resource_defaults": [first]},
            {"uuid": end, "branch_id": branch,
             "resource_defaults": [first, second] if keep_first else [second]},
        ])
        edges.append({"source_node_uuid": start, "target_node_uuid": end})
    graph = {"nodes": nodes, "edges": edges}
    if keep_first:
        with pytest.raises(locks.ResourcePlanError) as error:
            locks.compile_template_resource_plan(graph)
        assert error.value.code == "resource_cycle"
    else:
        assert not locks.compile_template_resource_plan(graph).relations
