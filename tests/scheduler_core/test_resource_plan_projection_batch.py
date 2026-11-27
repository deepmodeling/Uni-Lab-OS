"""批量投影保留完整校验及单节点的序列化结果。"""
from dataclasses import replace

import pytest

import unilabos.workflow.resource_lock_plan as resource_plan


def _plan():
    return resource_plan.ResourcePlan(
        plan_id="projection",
        resources=(resource_plan.CanonicalResource("r", "symbol:r", "resource", "r"),),
        intervals=(resource_plan.ResourceInterval(
            "i", "r", "first", "last", ("first", "last"), None, "workflow", "main",
        ),),
        acquire_sets=(resource_plan.AcquireSet("a", "first", ("r",)),),
    )


def test_batch_matches_single_node_and_validates_once(monkeypatch):
    plan = _plan()
    nodes = ["first", "last", "missing"]
    expected = {node: resource_plan.resource_plan_for_node(plan, node) for node in nodes}
    original = resource_plan.validate_resource_plan
    calls = []

    def validate(candidate):
        calls.append(candidate)
        return original(candidate)

    monkeypatch.setattr(resource_plan, "validate_resource_plan", validate)
    assert resource_plan.resource_plan_for_nodes(plan, nodes) == expected
    assert calls == [plan]
    resource_plan.resource_plan_for_node(plan, "first")
    assert calls == [plan, plan]


@pytest.mark.parametrize("nodes", [[], ["first"]])
def test_batch_rejects_invalid_complete_plan(nodes):
    plan = replace(_plan(), relations=(resource_plan.ResourceRelation("bad", "r", "missing"),))
    with pytest.raises(resource_plan.ResourcePlanError, match="未知资源"):
        resource_plan.resource_plan_for_nodes(plan, nodes)
