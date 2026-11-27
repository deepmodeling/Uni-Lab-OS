"""工作流创作图不再接受旧 material_transfer 节点类型。"""

from __future__ import annotations

import pytest

from unilabos.registry.decorators import NodeType
from unilabos.workflow._execution_plan_graph import executor_kind
from unilabos.workflow.graph_validation import GraphValidationError, validate_graph
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.store import StoreConflict

TEMPLATE_UUID = "10000000-0000-4000-8000-000000000051"
NODE_UUID = "20000000-0000-4000-8000-000000000051"


def test_graph_validation_rejects_material_transfer_node_type() -> None:
    node = WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=TEMPLATE_UUID,
        name="旧物料转移节点",
        type="material_transfer",
        param={},
    )

    with pytest.raises(GraphValidationError, match="不支持的节点执行类型"):
        validate_graph(
            nodes=[node],
            edges=[],
            templates={
                TEMPLATE_UUID: {
                    "uuid": TEMPLATE_UUID,
                    "node_type": "material_transfer",
                    "schema": {"type": "object"},
                }
            },
            handles={},
            effective_params={NODE_UUID: {}},
            workflow_meta_data={},
            node_meta_data={NODE_UUID: {}},
        )


def test_execution_plan_rejects_material_transfer_node_type() -> None:
    with pytest.raises(StoreConflict, match="unsupported workflow node type"):
        executor_kind("material_transfer")


def test_registry_node_type_excludes_material_transfer() -> None:
    with pytest.raises(ValueError):
        NodeType("material_transfer")
