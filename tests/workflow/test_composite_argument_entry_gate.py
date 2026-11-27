"""动态参数到达前，child 中不使用该参数的前置动作也必须等待。"""

import pytest

from tests.workflow.test_f07_composite_execution_plan import (
    INTERNAL_TARGET,
    INTERNAL_TEMPLATE,
    INTERNAL_UUID,
    INVOCATION_TARGET,
    INVOCATION_UUID,
    PRODUCER_SOURCE,
    PRODUCER_TEMPLATE,
    PRODUCER_UUID,
    SECOND_INTERNAL_UUID,
    _composite_node,
    _edge,
    _handles,
    _node,
)
from unilabos.workflow._execution_plan_graph import ExecutionPlanGraphNormalizer


@pytest.mark.parametrize("control_only", [False, True])
@pytest.mark.parametrize("entry_kind", ["ILab", "condition", "repeat_until"])
def test_child_entry_waits_even_when_only_a_later_step_uses_argument(control_only, entry_kind):
    invocation = _composite_node()
    composite = invocation["meta_data"]["unilab"]["composite"]
    composite["structural_mappings"]["entry_targets"] = [
        {
            "workflow_node_uuid": SECOND_INTERNAL_UUID,
            "target_handle_uuid": INTERNAL_TARGET,
        }
    ]
    if control_only:
        composite["target_mappings"][INVOCATION_TARGET] = []
    nodes = {
        n["uuid"]: n
        for n in (
            _node(PRODUCER_UUID, PRODUCER_TEMPLATE),
            invocation,
            _node(INTERNAL_UUID, INTERNAL_TEMPLATE),
            _node(SECOND_INTERNAL_UUID, INTERNAL_TEMPLATE),
        )
    }
    edges = [
        _edge(
            "74000000-0000-4000-8000-000000000021",
            PRODUCER_UUID,
            PRODUCER_SOURCE,
            INVOCATION_UUID,
            INVOCATION_TARGET,
        )
    ]
    nodes[SECOND_INTERNAL_UUID]["type"] = entry_kind
    flattened, _ = ExecutionPlanGraphNormalizer().flatten_composite_edges(
        nodes=nodes, edges=edges, handles={h["uuid"]: h for h in _handles()}
    )
    gate = [
        e
        for e in flattened
        if e["source_node_uuid"] == PRODUCER_UUID
        and e["target_node_uuid"] == SECOND_INTERNAL_UUID
    ]
    assert len(gate) == 1 and gate[0]["dependency_only"] is True
    assert gate[0]["source_handle_uuid"] == ""
    if entry_kind != "ILab":
        assert gate[0]["target_handle_uuid"] == ""
        planned = ExecutionPlanGraphNormalizer._direct_edge(gate[0], handles={}, runtime_handle_ids={}, target_node=nodes[SECOND_INTERNAL_UUID])
        assert planned["dependency_only"] and planned["target_node_uuid"] == SECOND_INTERNAL_UUID
    values = [e for e in flattened if e["target_node_uuid"] == INTERNAL_UUID]
    assert len(values) == (0 if control_only else 1)
    if values:
        assert values[0].get("dependency_only") is not True
