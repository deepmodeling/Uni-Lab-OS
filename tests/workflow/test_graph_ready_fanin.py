"""结构性就绪边（Ready Edge）的公共工作流图校验测试。"""

from __future__ import annotations

import pytest

from unilabos.workflow.graph_validation import GraphValidationError, validate_graph
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite

TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
READY_SOURCE_UUID = "10000000-0000-4000-8000-000000000002"
READY_TARGET_UUID = "10000000-0000-4000-8000-000000000003"
DATA_SOURCE_UUID = "10000000-0000-4000-8000-000000000004"
DATA_TARGET_UUID = "10000000-0000-4000-8000-000000000005"
NODE_UUIDS = (
    "20000000-0000-4000-8000-000000000001",
    "20000000-0000-4000-8000-000000000002",
    "20000000-0000-4000-8000-000000000003",
)


def _validate(edges: list[WorkflowEdgeWrite]) -> None:
    """用三个同模板动作验证给定边集。

    参数说明：``edges`` 是待验证边集。返回：公共全图校验通过时无返回值；
    图不满足连接点（Handle）合同则抛出 ``GraphValidationError``。
    """

    nodes = [
        WorkflowNodeWrite(
            uuid=node_uuid,
            workflow_node_template_uuid=TEMPLATE_UUID,
            name=f"动作 {index}",
            type="compute",
            param={},
        )
        for index, node_uuid in enumerate(NODE_UUIDS)
    ]
    validate_graph(
        nodes=nodes,
        edges=edges,
        templates={
            TEMPLATE_UUID: {
                "uuid": TEMPLATE_UUID,
                "node_type": "compute",
                "schema": {"type": "object"},
            }
        },
        handles={
            READY_SOURCE_UUID: {
                "uuid": READY_SOURCE_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "handle_key": "ready",
                "io_type": "source",
                "type": "default",
                "required": False,
            },
            READY_TARGET_UUID: {
                "uuid": READY_TARGET_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "handle_key": "ready",
                "io_type": "target",
                "type": "default",
                "required": False,
            },
            DATA_SOURCE_UUID: {
                "uuid": DATA_SOURCE_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "handle_key": "value",
                "data_key": "value",
                "data_source": "executor",
                "io_type": "source",
                "type": "number",
                "required": False,
            },
            DATA_TARGET_UUID: {
                "uuid": DATA_TARGET_UUID,
                "workflow_node_template_uuid": TEMPLATE_UUID,
                "handle_key": "value",
                "data_key": "value",
                "io_type": "target",
                "type": "number",
                "required": False,
            },
        },
        effective_params={node_uuid: {} for node_uuid in NODE_UUIDS},
        workflow_meta_data={},
        node_meta_data={node_uuid: {} for node_uuid in NODE_UUIDS},
    )


def _edge(index: int, *, ready: bool) -> WorkflowEdgeWrite:
    """构造从两个前置节点汇入第三节点同一连接点（Handle）的边。"""

    return WorkflowEdgeWrite(
        uuid=f"30000000-0000-4000-8000-00000000000{index}",
        source_node_uuid=NODE_UUIDS[index - 1],
        target_node_uuid=NODE_UUIDS[2],
        source_handle_uuid=READY_SOURCE_UUID if ready else DATA_SOURCE_UUID,
        target_handle_uuid=READY_TARGET_UUID if ready else DATA_TARGET_UUID,
    )


def test_ready_target_accepts_multiple_structural_dependencies() -> None:
    """多个前置动作可共同汇入同一个 ``ready`` 目标形成真实汇合。"""

    _validate([_edge(1, ready=True), _edge(2, ready=True)])


def test_data_target_still_rejects_multiple_value_providers() -> None:
    """普通数据目标仍只能有一个值提供者，避免解除数据单写者约束。"""

    with pytest.raises(GraphValidationError, match="同一目标 Handle"):
        _validate([_edge(1, ready=False), _edge(2, ready=False)])
