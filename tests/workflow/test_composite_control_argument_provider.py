"""父节点计算的参数进入 child 条件/循环时仍引用真实的上游结果。"""

from copy import deepcopy

import pytest

from unilabos.workflow.composite_graph_rewrite import (
    CompositeGraphRewriteError,
    materialize_control_arguments,
)
from unilabos.workflow.composite_invocation import _materialize_published_arguments


@pytest.mark.parametrize("control", ["condition", "repeat_until"])
@pytest.mark.parametrize("path", ["shared", "published"])
def test_computed_argument_becomes_control_result_and_waits_for_provider(control, path):
    field = "bindings" if control == "condition" else "initial_carry"
    source = {
        "uuid": "child-condition",
        "type": control,
        "param": {
            field: {"slot": {"kind": "workflow_input", "parameter": "slot_id"}},
            "predecessor_node_uuids": ["prior-child-step"],
        },
    }
    nodes = [deepcopy(source)]
    args = {
        "slot_id": {
            "kind": "node_output",
            "workflow_node_uuid": "actual-slot",
            "source_handle_uuid": "slot-handle",
        }
    }
    handles = [{"uuid": "slot-handle", "io_type": "source", "data_key": "value"}]
    if path == "shared":
        materialize_control_arguments(
            nodes, keyword_arguments=args, source_handles=handles
        )
    else:
        _materialize_published_arguments(
            expanded_nodes=nodes,
            source_nodes=[source],
            contract={
                "input_contract": {"parameters": []},
                "boundary_mapping": {"target_mappings": {}},
                "graph_snapshot": {},
            },
            normalized_param=args,
            parent_handles=handles,
        )
    assert nodes[0]["param"][field]["slot"] == {
        "kind": "node_result",
        "node_uuid": "actual-slot",
        "result_path": ["value"],
    }
    assert nodes[0]["param"]["predecessor_node_uuids"] == [
        "prior-child-step",
        "actual-slot",
    ]
    assert source["param"][field]["slot"]["kind"] == "workflow_input"
    first = deepcopy(nodes)
    materialize_control_arguments(nodes, keyword_arguments=args, source_handles=handles)
    assert nodes == first
    for invalid in ([], handles * 2, [{**handles[0], "io_type": "target"}]):
        with pytest.raises(CompositeGraphRewriteError):
            materialize_control_arguments(
                [deepcopy(source)], keyword_arguments=args, source_handles=invalid
            )
