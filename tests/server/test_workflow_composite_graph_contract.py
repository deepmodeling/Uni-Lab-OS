"""组合展示节点能进入持久图，但不改变执行节点语义。"""

from __future__ import annotations

from unilabos.server.workflow.graph_validation import validate_graph
from unilabos.server.workflow.models import WorkflowNodeWrite
from unilabos.server.workflow.service import WorkflowService
from unilabos.server.workflow.store import WorkflowStore

INVOCATION_UUID = "75000000-0000-4000-8000-000000000001"
TEMPLATE_UUID = "75000000-0000-4000-8000-000000000002"
LEAF_UUID = "75000000-0000-4000-8000-000000000003"
LEAF_TEMPLATE_UUID = "75000000-0000-4000-8000-000000000004"
INVOCATION_READY_TARGET_UUID = "75000000-0000-4000-8000-000000000005"
INVOCATION_READY_SOURCE_UUID = "75000000-0000-4000-8000-000000000006"
LEAF_READY_TARGET_UUID = "75000000-0000-4000-8000-000000000007"
LEAF_READY_SOURCE_UUID = "75000000-0000-4000-8000-000000000008"


def test_graph_validation_accepts_workflow_invocation_node_kind() -> None:
    node = WorkflowNodeWrite(
        uuid=INVOCATION_UUID,
        workflow_node_template_uuid=TEMPLATE_UUID,
        name="child",
        type="workflow",
        param={},
    )

    validate_graph(
        nodes=[node],
        edges=[],
        templates={
            TEMPLATE_UUID: {
                "uuid": TEMPLATE_UUID,
                "node_type": "workflow",
                "type": "workflow",
            }
        },
        handles={},
        effective_params={INVOCATION_UUID: {}},
        workflow_meta_data={},
        node_meta_data={INVOCATION_UUID: {}},
    )


def test_disabled_composite_suppresses_all_expanded_descendant_jobs() -> None:
    graph = {
        "workflow": {"uuid": "75000000-0000-4000-8000-000000000009"},
        "nodes": [
            {
                "uuid": INVOCATION_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "parent_uuid": None,
                "name": "child",
                "type": "workflow",
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": True,
                "minimized": False,
                "meta_data": {
                    "unilab": {
                        "composite": {
                            "target_mappings": {},
                            "source_mappings": {},
                            "structural_mappings": {
                                "entry_targets": [
                                    {
                                        "workflow_node_uuid": LEAF_UUID,
                                        "target_handle_uuid": LEAF_READY_TARGET_UUID,
                                    }
                                ],
                                "completion_sources": [
                                    {
                                        "workflow_node_uuid": LEAF_UUID,
                                        "source_handle_uuid": LEAF_READY_SOURCE_UUID,
                                    }
                                ],
                            },
                            "contract_compatibility": {"inputs": []},
                        }
                    }
                },
            },
            {
                "uuid": LEAF_UUID,
                "workflow_node_template_uuid": LEAF_TEMPLATE_UUID,
                "parent_uuid": INVOCATION_UUID,
                "name": "leaf",
                "type": "device",
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {},
            },
        ],
        "edges": [],
        "node_templates": [
            {"uuid": TEMPLATE_UUID, "node_type": "workflow", "type": "workflow"},
            {
                "uuid": LEAF_TEMPLATE_UUID,
                "node_type": "device",
                "type": "action",
            },
        ],
        "handle_templates": [
            {
                "uuid": INVOCATION_READY_TARGET_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "io_type": "target",
                "handle_key": "ready",
            },
            {
                "uuid": INVOCATION_READY_SOURCE_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "io_type": "source",
                "handle_key": "ready",
            },
            {
                "uuid": LEAF_READY_TARGET_UUID,
                "workflow_node_template_uuid": LEAF_TEMPLATE_UUID,
                "io_type": "target",
                "handle_key": "ready",
            },
            {
                "uuid": LEAF_READY_SOURCE_UUID,
                "workflow_node_template_uuid": LEAF_TEMPLATE_UUID,
                "io_type": "source",
                "handle_key": "ready",
            },
        ],
    }
    service = WorkflowService(WorkflowStore(":memory:"))
    try:
        plan, jobs = service._build_execution_plan(  # noqa: SLF001
            graph,
            run_mode="normal",
            target_node_uuid=None,
        )
    finally:
        service.close()

    assert plan["nodes"] == []
    assert jobs == []
