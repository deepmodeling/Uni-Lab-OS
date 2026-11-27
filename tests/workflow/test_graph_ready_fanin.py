"""结构性就绪边（Ready Edge）的公共工作流图校验测试。"""

from __future__ import annotations

import pytest

from unilabos.workflow.graph_validation import (
    GraphValidationError,
    _is_composite_descendant,
    validate_graph,
)
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


def test_composite_boundary_accepts_nested_control_descendant() -> None:
    """组合参数可以进入 RepeatUntil 内部动作，但不能越过调用根。"""

    root = WorkflowNodeWrite(
        uuid=NODE_UUIDS[0],
        workflow_node_template_uuid=TEMPLATE_UUID,
        name="调用",
        type="workflow",
        param={},
    )
    control = WorkflowNodeWrite(
        uuid=NODE_UUIDS[1],
        workflow_node_template_uuid=TEMPLATE_UUID,
        name="循环",
        type="repeat_until",
        parent_uuid=NODE_UUIDS[0],
        param={},
    )
    action = WorkflowNodeWrite(
        uuid=NODE_UUIDS[2],
        workflow_node_template_uuid=TEMPLATE_UUID,
        name="搅拌",
        type="compute",
        parent_uuid=NODE_UUIDS[1],
        param={},
    )
    nodes = {node.uuid: node for node in (root, control, action)}
    assert _is_composite_descendant(NODE_UUIDS[2], NODE_UUIDS[0], nodes)
    assert not _is_composite_descendant(NODE_UUIDS[0], NODE_UUIDS[2], nodes)


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


def test_empty_condition_branch_is_rejected_before_it_can_replace_a_graph() -> None:
    """空的条件分支在图校验阶段拒绝，不能作为当前定义保存。"""

    condition = "20000000-0000-4000-8000-000000000011"
    action = "20000000-0000-4000-8000-000000000012"
    nodes = [
        WorkflowNodeWrite(
            uuid=condition,
            name="有料判断",
            type="condition",
            param={
                "branches": [
                    {
                        "node_uuids": [],
                        "entry_node_uuids": [],
                        "exit_node_uuids": [],
                    }
                ]
            },
        ),
        WorkflowNodeWrite(
            uuid=action,
            name="补料",
            type="compute",
            parent_uuid=condition,
            param={},
        ),
    ]
    with pytest.raises(GraphValidationError, match="条件分支成员或边界无效"):
        validate_graph(
            nodes=nodes,
            edges=[],
            templates={},
            handles={},
            effective_params={node.uuid: dict(node.param or {}) for node in nodes},
            workflow_meta_data={},
            node_meta_data={node.uuid: {} for node in nodes},
        )


def test_condition_branch_with_members_passes_graph_validation() -> None:
    """非空分支仍可通过图校验。"""

    condition = "20000000-0000-4000-8000-000000000021"
    action = "20000000-0000-4000-8000-000000000022"
    nodes = [
        WorkflowNodeWrite(
            uuid=condition,
            name="有料判断",
            type="condition",
            param={
                "branches": [
                    {
                        "node_uuids": [action],
                        "entry_node_uuids": [action],
                        "exit_node_uuids": [action],
                    }
                ]
            },
        ),
        WorkflowNodeWrite(
            uuid=action,
            name="补料",
            type="compute",
            parent_uuid=condition,
            param={},
        ),
    ]
    validate_graph(
        nodes=nodes,
        edges=[],
        templates={},
        handles={},
        effective_params={node.uuid: dict(node.param or {}) for node in nodes},
        workflow_meta_data={},
        node_meta_data={node.uuid: {} for node in nodes},
    )


def test_data_target_still_rejects_multiple_value_providers() -> None:
    """普通数据目标仍只能有一个值提供者，避免解除数据单写者约束。"""

    with pytest.raises(GraphValidationError, match="同一目标 Handle"):
        _validate([_edge(1, ready=False), _edge(2, ready=False)])


@pytest.mark.parametrize('foreign_parent,disabled_target', [(False, False), (True, False), (False, True)])
def test_composite_boundary_traverses_display_group_without_accepting_foreign_or_disabled_targets(
    foreign_parent: bool, disabled_target: bool,
) -> None:
    """展示分组保留调用祖先关系，但不放宽跨调用或禁用目标检查。"""
    other_uuid = '20000000-0000-4000-8000-000000000004'
    action_template = '10000000-0000-4000-8000-000000000006'
    nodes = [
        WorkflowNodeWrite(uuid=NODE_UUIDS[0], name='调用', type='workflow',
                          workflow_node_template_uuid=TEMPLATE_UUID, param={'value': 7}),
        WorkflowNodeWrite(uuid=NODE_UUIDS[1], name='展示分组', type='group',
                          parent_uuid=other_uuid if foreign_parent else NODE_UUIDS[0]),
        WorkflowNodeWrite(uuid=NODE_UUIDS[2], name='内部动作', type='compute',
                          workflow_node_template_uuid=action_template,
                          parent_uuid=NODE_UUIDS[1], disabled=disabled_target),
        WorkflowNodeWrite(uuid=other_uuid, name='另一个调用', type='workflow',
                          workflow_node_template_uuid=TEMPLATE_UUID, param={'value': 9}),
    ]
    metadata = {node.uuid: {} for node in nodes}
    metadata[NODE_UUIDS[0]] = {'unilab': {'composite': {'target_mappings': {
        READY_TARGET_UUID: [{'workflow_node_uuid': NODE_UUIDS[2], 'target_handle_uuid': DATA_TARGET_UUID}],
    }}}}
    kwargs = dict(
        nodes=nodes, edges=[],
        templates={
            TEMPLATE_UUID: {'uuid': TEMPLATE_UUID, 'node_type': 'workflow', 'schema': {'type': 'object'}},
            action_template: {'uuid': action_template, 'node_type': 'compute',
                              'schema': {'type': 'object', 'properties': {'value': {'type': 'number'}}, 'required': ['value']}},
        },
        handles={
            READY_TARGET_UUID: {'uuid': READY_TARGET_UUID, 'workflow_node_template_uuid': TEMPLATE_UUID,
                                'handle_key': 'value', 'data_key': 'value', 'io_type': 'target', 'type': 'number'},
            DATA_TARGET_UUID: {'uuid': DATA_TARGET_UUID, 'workflow_node_template_uuid': action_template,
                               'handle_key': 'value', 'data_key': 'value', 'io_type': 'target', 'type': 'number', 'required': True},
        },
        effective_params={node.uuid: dict(node.param or {}) for node in nodes},
        workflow_meta_data={}, node_meta_data=metadata,
    )
    if foreign_parent or disabled_target:
        with pytest.raises(GraphValidationError, match='内部目标越过调用边界'):
            validate_graph(**kwargs)
    else:
        validate_graph(**kwargs)
