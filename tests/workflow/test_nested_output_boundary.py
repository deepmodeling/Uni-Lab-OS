"""嵌套组合透传输出必须沿父调用输入边界解析。"""

import pytest

from unilabos.workflow.workflow_boundary_binding import (
    WorkflowBoundaryBindingError,
    flatten_output_bindings,
)


def _fixture():
    def composite(uuid, parent, source, targets, parameters, completion):
        return {"uuid": uuid, "parent_uuid": parent, "meta_data": {"unilab": {
            "input_bindings": {}, "composite": {
                "source_mappings": source, "target_mappings": targets,
                "contract_compatibility": {"parameters": parameters},
                "structural_mappings": {"completion_sources": [{"workflow_node_uuid": completion}]},
            },
        }}}
    nodes = [
        {"uuid": "source"},
        {"uuid": "action", "parent_uuid": "child"},
        composite("parent", None, {"out": {"kind": "node_output", "workflow_node_uuid": "child", "source_handle_uuid": "out"}},
                  {"parent-in": [{"workflow_node_uuid": "child", "target_handle_uuid": "child-in"}]},
                  [{"name": "base", "handle_uuid": "parent-in"}], "child"),
        composite("child", "parent", {"out": {"kind": "workflow_input", "parameter": "base"}}, {},
                  [{"name": "base", "handle_uuid": "child-in"}], "action"),
    ]
    return dict(graph_nodes=nodes, graph_edges=[{
        "source_node_uuid": "source", "source_handle_uuid": "material",
        "target_node_uuid": "parent", "target_handle_uuid": "parent-in",
    }], output_bindings={"base": {"kind": "node_output", "workflow_node_uuid": "parent", "source_handle_uuid": "out"}},
        planned_node_uuids={"source", "action"})


def test_nested_passthrough_resolves_parent_input_edge():
    assert flatten_output_bindings(**_fixture())["base"] == {
        "kind": "node_output", "workflow_node_uuid": "source",
        "source_handle_uuid": "material", "completion_node_uuids": ["action"],
    }


def test_nested_passthrough_rejects_ambiguous_provider():
    fixture = _fixture()
    fixture["graph_edges"] *= 2
    with pytest.raises(WorkflowBoundaryBindingError):
        flatten_output_bindings(**fixture)


@pytest.mark.parametrize("binding_key", ["parent-in", "base"])
@pytest.mark.parametrize("binding", [
    {"parameter": "root_base"},
    {"kind": "workflow_input", "parameter": "root_base"},
])
def test_nested_passthrough_preserves_root_input_and_completion(binding_key, binding):
    fixture = _fixture()
    fixture["graph_edges"] = []
    fixture["graph_nodes"][2]["meta_data"]["unilab"]["input_bindings"] = {
        binding_key: binding,
    }
    assert flatten_output_bindings(**fixture)["base"] == {
        "kind": "workflow_input", "parameter": "root_base",
        "completion_node_uuids": ["action"],
    }
    assert fixture["graph_nodes"][2]["meta_data"]["unilab"]["input_bindings"][binding_key] == binding


@pytest.mark.parametrize("binding", [
    {}, "root_base", {"parameter": ""}, {"parameter": 1},
    {"kind": "node_output", "parameter": "root_base"},
    {"kind": "workflow_input"},
    {"parameter": "root_base", "unexpected": True},
])
def test_nested_passthrough_rejects_invalid_root_input_binding(binding):
    fixture = _fixture()
    fixture["graph_edges"] = []
    fixture["graph_nodes"][2]["meta_data"]["unilab"]["input_bindings"] = {
        "parent-in": binding,
    }
    with pytest.raises(WorkflowBoundaryBindingError):
        flatten_output_bindings(**fixture)


def test_nested_passthrough_rejects_direct_and_parent_providers():
    fixture = _fixture()
    fixture["graph_edges"].append({
        "source_node_uuid": "source", "source_handle_uuid": "material",
        "target_node_uuid": "child", "target_handle_uuid": "child-in",
    })
    with pytest.raises(WorkflowBoundaryBindingError):
        flatten_output_bindings(**fixture)


def test_nested_passthrough_uses_typed_frozen_literal_and_waits_for_child_completion():
    fixture = _fixture()
    fixture["graph_edges"] = []
    parent = fixture["graph_nodes"][2]
    parent["param"] = {"base": "cold-site-02"}
    parent["meta_data"]["unilab"]["composite"]["contract_compatibility"]["parameters"][0]["schema"] = {"type": "string", "minLength": 1}
    assert flatten_output_bindings(**fixture)["base"] == {
        "kind": "literal", "value": "cold-site-02", "completion_node_uuids": ["action"],
    }


@pytest.mark.parametrize('schema,value', [
    ({"type": "string", "minLength": 1}, ""),
    ({"$slot": "ResourceSlot"}, {"uuid": "plate"}),
])
def test_nested_passthrough_rejects_invalid_literal_or_unproved_material(schema, value):
    fixture = _fixture()
    fixture["graph_edges"] = []
    parent = fixture["graph_nodes"][2]
    parent["param"] = {"base": value}
    parent["meta_data"]["unilab"]["composite"]["contract_compatibility"]["parameters"][0]["schema"] = schema
    with pytest.raises(WorkflowBoundaryBindingError):
        flatten_output_bindings(**fixture)
