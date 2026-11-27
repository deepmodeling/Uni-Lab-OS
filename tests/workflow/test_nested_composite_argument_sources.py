"""再次组合已发布的子图时，恢复其规范输入绑定，不能只读取静态 param。"""

from copy import deepcopy

import pytest

from tests.workflow.test_c1_r2_static_expansion_contract import (
    CHILD_WORKFLOW_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    _nested_world,
)


def test_nested_invocation_restores_workflow_input_from_handle_binding():
    """已应用图把父输入放在 input_bindings 中，静态 param 保持为空。"""
    authoring, provider = _nested_world()
    child = provider.snapshots[CHILD_WORKFLOW_UUID]
    child["nodes"][0]["param"] = {}
    result = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={"value": 7.5},
    )
    assert not result.diagnostics, result.diagnostics
    assert result.invocation_node is not None
    assert len(result.nodes) == 2
    assert child["nodes"][0]["param"] == {}


def test_nested_argument_data_edge_is_restored_without_overwriting_conflicts():
    """数据连线保留生产者身份，同一参数的不同来源必须拒绝。"""
    from unilabos.workflow.composite_expansion import (
        _CompositeFailure,
        _node_keyword_arguments,
    )

    node = {"uuid": "child", "param": {}, "meta_data": {}}
    handle = {"uuid": "target", "handle_key": "value", "io_type": "target"}
    edge = {"target_node_uuid": "child", "target_handle_uuid": "target",
            "source_node_uuid": "producer", "source_handle_uuid": "result"}
    original = deepcopy(node)
    expected = {"kind": "node_output", "workflow_node_uuid": "producer", "source_handle_uuid": "result"}
    assert _node_keyword_arguments(node, handles=[handle], edges=[edge]) == {"value": expected}
    assert node == original
    node["param"] = {"value": expected}
    assert _node_keyword_arguments(node, handles=[handle], edges=[edge]) == {"value": expected}
    with pytest.raises(_CompositeFailure) as error:
        _node_keyword_arguments(node, handles=[handle], edges=[{**edge, "source_node_uuid": "other"}])
    assert error.value.message == "组合调用的同一参数存在冲突来源"


def test_nested_argument_follows_ancestor_boundary_route():
    """外层节点输出替换子参数后，内层调用通过祖先边界获取同一来源。"""
    from unilabos.workflow.composite_expansion import _node_keyword_arguments

    target = {"uuid": "target", "handle_key": "value", "io_type": "target"}
    outer_handle = {"uuid": "outer-target", "handle_key": "offset", "io_type": "target"}
    node = {"uuid": "child", "parent_uuid": "outer", "param": {}, "meta_data": {}}
    outer = {
        "uuid": "outer", "param": {}, "meta_data": {"unilab": {"composite": {
            "target_mappings": {"outer-target": [{"workflow_node_uuid": "child", "target_handle_uuid": "target"}]}
        }}}
    }
    edge = {"target_node_uuid": "outer", "target_handle_uuid": "outer-target",
            "source_node_uuid": "measurement", "source_handle_uuid": "offset-result"}
    kwargs = dict(handles=[target], edges=[edge], nodes={"child": node, "outer": outer},
                  node_handles={"child": [target], "outer": [outer_handle]})
    assert _node_keyword_arguments(node, **kwargs) == {"value": {
        "kind": "node_output", "workflow_node_uuid": "measurement", "source_handle_uuid": "offset-result"
    }}
    # 同级操作的映射不能越过调用边界为 child 注入来源。
    detached = {**node, "parent_uuid": None}
    assert _node_keyword_arguments(detached, **kwargs) == {}
