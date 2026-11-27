"""固定实参透传输出必须使用子合同类型，不得借用同名父参数类型。"""
from types import SimpleNamespace

import pytest

from unilabos.workflow.authoring_ast import ValueBinding
from unilabos.workflow.authoring_graph import AuthoringGraphError, _record_composite_output_schemas


def _project(binding, parent_parameters):
    handles = [
        {"uuid": "input", "handle_key": "target_site", "io_type": "target", "type": "string"},
        {"uuid": "output", "handle_key": "site", "io_type": "source", "type": "string"},
    ]
    outputs = {}
    _record_composite_output_schemas(
        SimpleNamespace(arguments=(("target_site", binding),), result_name="moved"),
        invocation_node={}, action=SimpleNamespace(detached_handles=lambda: handles), catalog=None,
        expansion=SimpleNamespace(source_mappings={"output": {"kind": "workflow_input", "parameter": "target_site"}}),
        result_nodes={}, result_output_schemas=outputs,
        input_contract={"parameters": parent_parameters}, resolved_resource_references={},
    )
    return outputs[("moved", "site")]


@pytest.mark.parametrize("parent", [[], [{"name": "target_site", "schema": {"type": "integer"}}]])
def test_literal_child_input_output_uses_child_schema(parent):
    assert _project(ValueBinding("literal", "slot_09"), parent) == {"type": "string"}


def test_parent_input_reference_still_requires_parent_schema():
    assert _project(ValueBinding("workflow_input", "parent_site"), [
        {"name": "parent_site", "schema": {"type": "string", "enum": ["slot_09"]}},
    ]) == {"type": "string", "enum": ["slot_09"]}
    with pytest.raises(AuthoringGraphError):
        _project(ValueBinding("workflow_input", "missing"), [])


def test_material_passthrough_proof_survives_outer_output_binding():
    """外层组合替换参数来源后，内层透传仍保留可验证的类型证明。"""
    from unilabos.workflow.workflow_io import (
        WorkflowIOValidationError,
        node_output_value_schema,
    )

    narrow = {"$slot": "ResourceSlot", "allowed_resource_template_uuids": ["b502c389-bb04-51e2-9152-737053ea4682"]}
    handles = [
        {"uuid": "input", "handle_key": "plate", "io_type": "target",
         "workflow_node_template_uuid": "child", "type": "ResourceSlot"},
        {"uuid": "output", "handle_key": "plate", "io_type": "source",
         "workflow_node_template_uuid": "child", "type": "ResourceSlot"},
    ]
    node = {}
    _record_composite_output_schemas(
        SimpleNamespace(arguments=(("plate", ValueBinding("workflow_input", "sample")),), result_name="processed"),
        invocation_node=node, action=SimpleNamespace(detached_handles=lambda: handles), catalog=None,
        expansion=SimpleNamespace(source_mappings={"output": {"kind": "workflow_input", "parameter": "plate"}}),
        result_nodes={}, result_output_schemas={},
        input_contract={"parameters": [{"name": "sample", "schema": narrow}]},
        resolved_resource_references={},
    )
    node["meta_data"]["unilab"]["composite"] = {"source_mappings": {
        "output": {"kind": "node_output", "workflow_node_uuid": "producer", "source_handle_uuid": "material"}
    }}
    assert node_output_value_schema(
        node_uuid="invocation", handle_uuid="output", handle=handles[1],
        handles={h["uuid"]: h for h in handles}, node_meta_data={"invocation": node["meta_data"]},
    ) == narrow
    # 证明必须指向同一模板上的同名输入，不能接受另一个操作的连接点。
    handles[0]["workflow_node_template_uuid"] = "unrelated"
    with pytest.raises(WorkflowIOValidationError, match="组合工作流输出类型覆盖无效"):
        node_output_value_schema(
            node_uuid="invocation", handle_uuid="output", handle=handles[1],
            handles={h["uuid"]: h for h in handles}, node_meta_data={"invocation": node["meta_data"]},
        )


@pytest.mark.parametrize('broken', [None, 'missing_proof', 'wrong_input', 'ambiguous', 'cycle'])
def test_explicit_material_output_follows_only_proven_identity_chain(broken):
    """显式 placed.material 经 pick/place 数据边透传，不能只凭物料类型推断身份。"""
    from copy import deepcopy

    from unilabos.workflow.composite_material_passthrough import explicit_material_input_parameter

    boundary_handles = [
        {"uuid": "input", "handle_key": "material", "io_type": "target", "type": "ResourceSlot"},
        {"uuid": "output", "handle_key": "material", "io_type": "source", "type": "ResourceSlot"},
    ]
    handles = [
        {"uuid": direction, "handle_key": "material", "io_type": io,
         "workflow_node_template_uuid": "transfer", "type": "ResourceSlot"}
        for direction, io in [('in', 'target'), ('out', 'source')]]
    leaf = {"workflow_node_template_uuid": "transfer", "meta_data": {"unilab": {
        "material_passthrough_handles": {"out": "in"}}}}
    nodes = [{**deepcopy(leaf), 'uuid': name} for name in ['picked', 'placed']]
    edges = [{"source_node_uuid": "picked", "source_handle_uuid": "out",
              "target_node_uuid": "placed", "target_handle_uuid": "in"}]
    roots = {"input": [{"workflow_node_uuid": "picked", "target_handle_uuid": "in"}]}
    if broken == 'missing_proof':
        nodes[1]['meta_data']['unilab'].clear()
    elif broken == 'wrong_input':
        handles[0]['handle_key'] = 'other_material'
    elif broken == 'ambiguous':
        edges.append({**edges[0], 'source_node_uuid': 'other'})
    elif broken == 'cycle':
        roots.clear()
        edges.append({"source_node_uuid": "placed", "source_handle_uuid": "out",
                      "target_node_uuid": "picked", "target_handle_uuid": "in"})
    expansion = SimpleNamespace(nodes=nodes, edges=edges, target_mappings=roots, handle_templates=handles,
        source_mappings={"output": {"kind": "node_output", "workflow_node_uuid": "placed", "source_handle_uuid": "out"}})
    parameter = explicit_material_input_parameter(expansion, boundary_handles, expansion.source_mappings['output'])
    assert parameter == ('material' if broken is None else None)
    if broken is not None:
        return
    node = {}
    narrow = {"$slot": "ResourceSlot", "allowed_resource_template_uuids": ["b502c389-bb04-51e2-9152-737053ea4682"]}
    result_schemas = {}
    _record_composite_output_schemas(
        SimpleNamespace(arguments=(("material", ValueBinding("workflow_input", "plate")),), result_name="transported"),
        invocation_node=node, action=SimpleNamespace(detached_handles=lambda: boundary_handles), catalog=None,
        expansion=expansion, result_nodes={}, result_output_schemas=result_schemas,
        input_contract={"parameters": [{"name": "plate", "schema": narrow}]}, resolved_resource_references={},
    )
    assert result_schemas[('transported', 'material')] == narrow
    assert node['meta_data']['unilab']['material_passthrough_handles'] == {'output': 'input'}


@pytest.mark.parametrize('parent', ['nested', None])
def test_explicit_material_proof_crosses_only_ancestor_composite_routes(parent):
    """嵌套组合的物料输出必须沿真实父边界追溯，不能借用同级操作的路由。"""
    from unilabos.workflow.composite_material_passthrough import explicit_material_input_parameter

    handles = [
        {'uuid': name, 'handle_key': 'material', 'io_type': direction,
         'workflow_node_template_uuid': template, 'type': 'ResourceSlot'}
        for template, name, direction in [
            ('outer', 'input', 'target'), ('outer', 'output', 'source'),
            ('nested', 'nested-in', 'target'), ('nested', 'nested-out', 'source'),
            ('transfer', 'in', 'target'), ('transfer', 'out', 'source')]
    ]
    expansion = SimpleNamespace(
        nodes=[
            {'uuid': 'nested', 'workflow_node_template_uuid': 'nested', 'meta_data': {'unilab': {'composite': {
                'source_mappings': {'nested-out': {'kind': 'node_output', 'workflow_node_uuid': 'leaf', 'source_handle_uuid': 'out'}},
                'target_mappings': {'nested-in': [{'workflow_node_uuid': 'leaf', 'target_handle_uuid': 'in'}]},
            }}}},
            {'uuid': 'leaf', 'parent_uuid': parent, 'workflow_node_template_uuid': 'transfer',
             'meta_data': {'unilab': {'material_passthrough_handles': {'out': 'in'}}}},
        ],
        edges=[], handle_templates=handles,
        target_mappings={'input': [{'workflow_node_uuid': 'nested', 'target_handle_uuid': 'nested-in'}]},
    )
    mapping = {'kind': 'node_output', 'workflow_node_uuid': 'nested', 'source_handle_uuid': 'nested-out'}
    assert explicit_material_input_parameter(expansion, handles[:2], mapping) == ('material' if parent else None)
