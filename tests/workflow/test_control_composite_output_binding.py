"""循环携带值引用 child 输出时必须冻结到真实结果来源。"""

from copy import deepcopy

import pytest

from unilabos.workflow.workflow_boundary_binding import (
    WorkflowBoundaryBindingError,
    flatten_control_result_bindings,
)


def test_loop_seed_resolves_child_output_alias_without_modifying_graph():
    binding = {"kind": "node_result", "node_uuid": "child", "result_path": ["amount"]}
    nodes = [
        {
            "uuid": "child",
            "type": "workflow",
            "workflow_node_template_uuid": "child-template",
            "meta_data": {
                "unilab": {
                    "composite": {
                        "source_mappings": {
                            "boundary": {
                                "kind": "node_output",
                                "workflow_node_uuid": "measure",
                                "source_handle_uuid": "actual",
                            }
                        }
                    }
                }
            },
        },
        {"uuid": "measure", "type": "ILab"},
        {
            "uuid": "loop",
            "type": "repeat_until",
            "param": {"initial_carry": {"amount": binding}},
        },
    ]
    handles = [
        {
            "uuid": "boundary",
            "workflow_node_template_uuid": "child-template",
            "io_type": "source",
            "data_key": "amount",
        },
        {"uuid": "actual", "data_key": "volume"},
    ]
    original = deepcopy(nodes)
    result = flatten_control_result_bindings(
        graph_nodes=nodes, handles=handles, planned_node_uuids={"loop", "measure"}
    )
    assert result[-1]["param"]["initial_carry"]["amount"] == {
        "kind": "node_result",
        "node_uuid": "measure",
        "result_path": ["volume"],
    }
    assert nodes == original
    with pytest.raises(WorkflowBoundaryBindingError):
        flatten_control_result_bindings(
            graph_nodes=nodes,
            handles=handles[:1],
            planned_node_uuids={"loop", "measure"},
        )


def test_condition_waits_for_child_branch_exits_including_skipped_alternative():
    graph = [
        {"uuid": "outer", "type": "repeat_until"},
        {
            "uuid": "child",
            "type": "workflow",
            "parent_uuid": "outer",
            "meta_data": {
                "unilab": {
                    "composite": {
                        "structural_mappings": {
                            "completion_sources": [
                                {"workflow_node_uuid": "then"},
                                {"workflow_node_uuid": "else"},
                            ]
                        },
                    }
                }
            },
        },
        {"uuid": "choice", "type": "condition", "parent_uuid": "child"},
        {"uuid": "then", "type": "ILab", "parent_uuid": "choice"},
        {"uuid": "else", "type": "ILab", "parent_uuid": "choice"},
        {
            "uuid": "next",
            "type": "condition",
            "parent_uuid": "outer",
            "param": {"predecessor_node_uuids": ["child"]},
        },
    ]
    result = flatten_control_result_bindings(
        graph_nodes=graph,
        handles=[],
        planned_node_uuids={"outer", "choice", "then", "else", "next"},
    )
    assert result[-1]["param"]["predecessor_node_uuids"] == ["then", "else"]
    assert graph[-1]["param"]["predecessor_node_uuids"] == ["child"]


def test_child_completion_cannot_reference_an_unrelated_physical_job():
    graph = [
        {
            "uuid": "child",
            "type": "workflow",
            "meta_data": {
                "unilab": {
                    "composite": {
                        "structural_mappings": {
                            "completion_sources": [{"workflow_node_uuid": "unrelated"}]
                        },
                    }
                }
            },
        },
        {"uuid": "unrelated", "type": "ILab"},
        {
            "uuid": "next",
            "type": "condition",
            "param": {"predecessor_node_uuids": ["child"]},
        },
    ]
    with pytest.raises(WorkflowBoundaryBindingError):
        flatten_control_result_bindings(
            graph_nodes=graph, handles=[], planned_node_uuids={"unrelated", "next"}
        )


def test_child_entry_waits_for_its_condition_before_selecting_any_branch():
    """组合入口落到子条件自身，不能让子条件越过调用方的分支守卫提前求值。"""
    from unilabos.workflow.workflow_boundary_binding import structural_boundary_nodes

    nodes = {
        "child": {"uuid": "child", "type": "workflow", "meta_data": {"unilab": {"composite": {
            "structural_mappings": {"entry_targets": [{"workflow_node_uuid": "motion"}],
                                    "completion_sources": [{"workflow_node_uuid": "motion"}]}
        }}}},
        "choice": {"uuid": "choice", "type": "condition", "parent_uuid": "child"},
        "motion": {"uuid": "motion", "type": "ILab", "parent_uuid": "choice"},
    }
    assert structural_boundary_nodes(nodes, "child", "entry_targets") == ["choice"]
    assert structural_boundary_nodes(nodes, "child", "completion_sources") == ["motion"]


def test_condition_branch_expands_child_members_and_structural_boundaries():
    graph = [
        {
            "uuid": "region",
            "type": "condition",
            "param": {
                "branches": [
                    {
                        "node_uuids": ["child"],
                        "entry_node_uuids": ["child"],
                        "exit_node_uuids": ["child"],
                    }
                ]
            },
        },
        {
            "uuid": "child",
            "type": "workflow",
            "parent_uuid": "region",
            "meta_data": {
                "unilab": {
                    "composite": {
                        "structural_mappings": {
                            "entry_targets": [{"workflow_node_uuid": "choice"}],
                            "completion_sources": [
                                {"workflow_node_uuid": "yes"},
                                {"workflow_node_uuid": "no"},
                            ],
                        },
                    }
                }
            },
        },
        {"uuid": "choice", "type": "condition", "parent_uuid": "child"},
        {"uuid": "yes", "type": "ILab", "parent_uuid": "choice"},
        {"uuid": "no", "type": "ILab", "parent_uuid": "choice"},
    ]
    result = flatten_control_result_bindings(
        graph_nodes=graph,
        handles=[],
        planned_node_uuids={"region", "choice", "yes", "no"},
    )
    assert result[0]["param"]["branches"][0] == {
        "node_uuids": ["choice", "yes", "no"],
        "entry_node_uuids": ["choice"],
        "exit_node_uuids": ["yes", "no"],
    }
    assert graph[0]["param"]["branches"][0]["node_uuids"] == ["child"]


def test_duplicate_original_branch_members_are_still_rejected():
    graph = [
        {
            "uuid": "region",
            "type": "condition",
            "param": {"branches": [{"node_uuids": ["leaf", "leaf"]}]},
        },
        {"uuid": "leaf", "type": "ILab", "parent_uuid": "region"},
    ]
    with pytest.raises(WorkflowBoundaryBindingError):
        flatten_control_result_bindings(
            graph_nodes=graph, handles=[], planned_node_uuids={"region", "leaf"}
        )
