"""组合执行计划对冻结边界映射采用 fail-closed 语义。"""

from __future__ import annotations

import pytest

from unilabos.server.services.runtime.workflow.execution_plan_graph import (
    CompositeExecutionPlanError,
    CompositeExecutionPlanNormalizer,
)

INVOCATION_UUID = "73000000-0000-4000-8000-000000000001"
PROVIDER_UUID = "73000000-0000-4000-8000-000000000002"
WORKFLOW_TEMPLATE_UUID = "73000000-0000-4000-8000-000000000003"
ACTION_TEMPLATE_UUID = "73000000-0000-4000-8000-000000000004"
INPUT_HANDLE_UUID = "73000000-0000-4000-8000-000000000005"
READY_TARGET_UUID = "73000000-0000-4000-8000-000000000006"
READY_SOURCE_UUID = "73000000-0000-4000-8000-000000000007"
PROVIDER_SOURCE_UUID = "73000000-0000-4000-8000-000000000008"
EDGE_UUID = "73000000-0000-4000-8000-000000000009"
LEAF_A_UUID = "73000000-0000-4000-8000-00000000000a"
LEAF_B_UUID = "73000000-0000-4000-8000-00000000000b"
LEAF_A_TEMPLATE_UUID = "73000000-0000-4000-8000-00000000000c"
LEAF_B_TEMPLATE_UUID = "73000000-0000-4000-8000-00000000000d"
LEAF_B_TARGET_UUID = "73000000-0000-4000-8000-00000000000e"
CONSUMER_UUID = "73000000-0000-4000-8000-00000000000f"
CONSUMER_TARGET_UUID = "73000000-0000-4000-8000-000000000010"
READY_IN_EDGE_UUID = "73000000-0000-4000-8000-000000000011"
READY_OUT_EDGE_UUID = "73000000-0000-4000-8000-000000000012"
INNER_INVOCATION_UUID = "73000000-0000-4000-8000-000000000013"
INNER_TEMPLATE_UUID = "73000000-0000-4000-8000-000000000014"
INNER_INPUT_UUID = "73000000-0000-4000-8000-000000000015"
INNER_READY_TARGET_UUID = "73000000-0000-4000-8000-000000000016"
INNER_READY_SOURCE_UUID = "73000000-0000-4000-8000-000000000017"
LEAF_TARGET_UUID = "73000000-0000-4000-8000-000000000018"


def test_execution_plan_rejects_incoming_edge_missing_from_frozen_mapping() -> None:
    invocation = {
        "uuid": INVOCATION_UUID,
        "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
        "parent_uuid": None,
        "param": {},
        "meta_data": {
            "unilab": {
                "composite": {
                    "target_mappings": {},
                    "source_mappings": {},
                    "structural_mappings": {
                        "entry_targets": [],
                        "completion_sources": [],
                    },
                    "contract_compatibility": {
                        "inputs": [
                            {
                                "handle_uuid": INPUT_HANDLE_UUID,
                                "name": "sample",
                            }
                        ]
                    },
                }
            }
        },
    }
    handles = {
        INPUT_HANDLE_UUID: {
            "uuid": INPUT_HANDLE_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "sample",
        },
        READY_TARGET_UUID: {
            "uuid": READY_TARGET_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "ready",
        },
        READY_SOURCE_UUID: {
            "uuid": READY_SOURCE_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "ready",
        },
        PROVIDER_SOURCE_UUID: {
            "uuid": PROVIDER_SOURCE_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "sample",
        },
    }
    edge = {
        "uuid": EDGE_UUID,
        "source_node_uuid": PROVIDER_UUID,
        "source_handle_uuid": PROVIDER_SOURCE_UUID,
        "target_node_uuid": INVOCATION_UUID,
        "target_handle_uuid": INPUT_HANDLE_UUID,
    }

    with pytest.raises(CompositeExecutionPlanError):
        CompositeExecutionPlanNormalizer().flatten_composite_edges(
            nodes={
                PROVIDER_UUID: {
                    "uuid": PROVIDER_UUID,
                    "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
                    "parent_uuid": None,
                    "meta_data": {},
                },
                INVOCATION_UUID: invocation,
            },
            edges=[edge],
            handles=handles,
        )


def test_flatten_rejects_mapping_handle_not_owned_by_mapped_node() -> None:
    invocation = {
        "uuid": INVOCATION_UUID,
        "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
        "parent_uuid": None,
        "param": {},
        "meta_data": {
            "unilab": {
                "composite": {
                    "target_mappings": {
                        INPUT_HANDLE_UUID: [
                            {
                                "workflow_node_uuid": LEAF_A_UUID,
                                "target_handle_uuid": LEAF_B_TARGET_UUID,
                            }
                        ]
                    },
                    "source_mappings": {},
                    "structural_mappings": {
                        "entry_targets": [],
                        "completion_sources": [],
                    },
                    "contract_compatibility": {
                        "inputs": [
                            {
                                "handle_uuid": INPUT_HANDLE_UUID,
                                "name": "sample",
                            }
                        ]
                    },
                }
            }
        },
    }
    handles = {
        INPUT_HANDLE_UUID: {
            "uuid": INPUT_HANDLE_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "sample",
        },
        READY_TARGET_UUID: {
            "uuid": READY_TARGET_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "ready",
        },
        READY_SOURCE_UUID: {
            "uuid": READY_SOURCE_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "ready",
        },
        PROVIDER_SOURCE_UUID: {
            "uuid": PROVIDER_SOURCE_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "sample",
        },
        LEAF_B_TARGET_UUID: {
            "uuid": LEAF_B_TARGET_UUID,
            "workflow_node_template_uuid": LEAF_B_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "sample",
        },
    }
    nodes = {
        PROVIDER_UUID: {
            "uuid": PROVIDER_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "parent_uuid": None,
            "meta_data": {},
        },
        INVOCATION_UUID: invocation,
        LEAF_A_UUID: {
            "uuid": LEAF_A_UUID,
            "workflow_node_template_uuid": LEAF_A_TEMPLATE_UUID,
            "parent_uuid": INVOCATION_UUID,
            "meta_data": {},
        },
        LEAF_B_UUID: {
            "uuid": LEAF_B_UUID,
            "workflow_node_template_uuid": LEAF_B_TEMPLATE_UUID,
            "parent_uuid": INVOCATION_UUID,
            "meta_data": {},
        },
    }
    edge = {
        "uuid": EDGE_UUID,
        "source_node_uuid": PROVIDER_UUID,
        "source_handle_uuid": PROVIDER_SOURCE_UUID,
        "target_node_uuid": INVOCATION_UUID,
        "target_handle_uuid": INPUT_HANDLE_UUID,
    }

    with pytest.raises(CompositeExecutionPlanError):
        CompositeExecutionPlanNormalizer().flatten_composite_edges(
            nodes=nodes,
            edges=[edge],
            handles=handles,
        )


def test_empty_composite_preserves_ready_dependency() -> None:
    invocation = {
        "uuid": INVOCATION_UUID,
        "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
        "parent_uuid": None,
        "param": {},
        "meta_data": {
            "unilab": {
                "composite": {
                    "target_mappings": {},
                    "source_mappings": {},
                    "structural_mappings": {
                        "entry_targets": [],
                        "completion_sources": [],
                    },
                    "contract_compatibility": {"inputs": []},
                }
            }
        },
    }
    nodes = {
        PROVIDER_UUID: {
            "uuid": PROVIDER_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "parent_uuid": None,
        },
        INVOCATION_UUID: invocation,
        CONSUMER_UUID: {
            "uuid": CONSUMER_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "parent_uuid": None,
        },
    }
    handles = {
        READY_TARGET_UUID: {
            "uuid": READY_TARGET_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "ready",
        },
        READY_SOURCE_UUID: {
            "uuid": READY_SOURCE_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "ready",
        },
        PROVIDER_SOURCE_UUID: {
            "uuid": PROVIDER_SOURCE_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "ready",
        },
        CONSUMER_TARGET_UUID: {
            "uuid": CONSUMER_TARGET_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "ready",
        },
    }
    edges = [
        {
            "uuid": READY_IN_EDGE_UUID,
            "source_node_uuid": PROVIDER_UUID,
            "source_handle_uuid": PROVIDER_SOURCE_UUID,
            "target_node_uuid": INVOCATION_UUID,
            "target_handle_uuid": READY_TARGET_UUID,
        },
        {
            "uuid": READY_OUT_EDGE_UUID,
            "source_node_uuid": INVOCATION_UUID,
            "source_handle_uuid": READY_SOURCE_UUID,
            "target_node_uuid": CONSUMER_UUID,
            "target_handle_uuid": CONSUMER_TARGET_UUID,
        },
    ]

    flattened, _params = CompositeExecutionPlanNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=edges,
        handles=handles,
    )

    assert [
        (edge["source_node_uuid"], edge["target_node_uuid"])
        for edge in flattened
    ] == [(PROVIDER_UUID, CONSUMER_UUID)]


def test_nested_composite_outer_boundary_reaches_leaf() -> None:
    outer = {
        "uuid": INVOCATION_UUID,
        "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
        "parent_uuid": None,
        "param": {},
        "meta_data": {
            "unilab": {
                "composite": {
                    "target_mappings": {
                        INPUT_HANDLE_UUID: [
                            {
                                "workflow_node_uuid": INNER_INVOCATION_UUID,
                                "target_handle_uuid": INNER_INPUT_UUID,
                            }
                        ]
                    },
                    "source_mappings": {},
                    "structural_mappings": {
                        "entry_targets": [],
                        "completion_sources": [],
                    },
                    "contract_compatibility": {
                        "inputs": [
                            {"handle_uuid": INPUT_HANDLE_UUID, "name": "sample"}
                        ]
                    },
                }
            }
        },
    }
    inner = {
        "uuid": INNER_INVOCATION_UUID,
        "workflow_node_template_uuid": INNER_TEMPLATE_UUID,
        "parent_uuid": INVOCATION_UUID,
        "param": {},
        "meta_data": {
            "unilab": {
                "composite": {
                    "target_mappings": {
                        INNER_INPUT_UUID: [
                            {
                                "workflow_node_uuid": LEAF_A_UUID,
                                "target_handle_uuid": LEAF_TARGET_UUID,
                            }
                        ]
                    },
                    "source_mappings": {},
                    "structural_mappings": {
                        "entry_targets": [],
                        "completion_sources": [],
                    },
                    "contract_compatibility": {
                        "inputs": [
                            {"handle_uuid": INNER_INPUT_UUID, "name": "sample"}
                        ]
                    },
                }
            }
        },
    }
    handles = {
        INPUT_HANDLE_UUID: {
            "uuid": INPUT_HANDLE_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "sample",
        },
        READY_TARGET_UUID: {
            "uuid": READY_TARGET_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "ready",
        },
        READY_SOURCE_UUID: {
            "uuid": READY_SOURCE_UUID,
            "workflow_node_template_uuid": WORKFLOW_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "ready",
        },
        INNER_INPUT_UUID: {
            "uuid": INNER_INPUT_UUID,
            "workflow_node_template_uuid": INNER_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "sample",
        },
        INNER_READY_TARGET_UUID: {
            "uuid": INNER_READY_TARGET_UUID,
            "workflow_node_template_uuid": INNER_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "ready",
        },
        INNER_READY_SOURCE_UUID: {
            "uuid": INNER_READY_SOURCE_UUID,
            "workflow_node_template_uuid": INNER_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "ready",
        },
        LEAF_TARGET_UUID: {
            "uuid": LEAF_TARGET_UUID,
            "workflow_node_template_uuid": LEAF_A_TEMPLATE_UUID,
            "io_type": "target",
            "handle_key": "sample",
        },
        PROVIDER_SOURCE_UUID: {
            "uuid": PROVIDER_SOURCE_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "io_type": "source",
            "handle_key": "sample",
        },
    }
    nodes = {
        PROVIDER_UUID: {
            "uuid": PROVIDER_UUID,
            "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
            "parent_uuid": None,
        },
        INVOCATION_UUID: outer,
        INNER_INVOCATION_UUID: inner,
        LEAF_A_UUID: {
            "uuid": LEAF_A_UUID,
            "workflow_node_template_uuid": LEAF_A_TEMPLATE_UUID,
            "parent_uuid": INNER_INVOCATION_UUID,
        },
    }
    edge = {
        "uuid": EDGE_UUID,
        "source_node_uuid": PROVIDER_UUID,
        "source_handle_uuid": PROVIDER_SOURCE_UUID,
        "target_node_uuid": INVOCATION_UUID,
        "target_handle_uuid": INPUT_HANDLE_UUID,
    }

    flattened, params = CompositeExecutionPlanNormalizer().flatten_composite_edges(
        nodes=nodes,
        edges=[edge],
        handles=handles,
    )

    assert params == {}
    assert [
        (item["source_node_uuid"], item["target_node_uuid"])
        for item in flattened
    ] == [(PROVIDER_UUID, LEAF_A_UUID)]
