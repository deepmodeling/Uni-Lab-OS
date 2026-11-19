"""结构化结果引用的封闭格式、循环/条件作用域和字面值边界。"""
from uuid import uuid4

import pytest

from unilabos.protocol.runtime.value_binding import (
    result_references, remap_result_bindings, resolve_result_bindings, validate_result_binding_graph,
)
from unilabos.server.backend.scheduler.parameters import json_get_exists

SOURCE, TARGET, LOOP, CONDITION = [str(uuid4()) for _ in range(4)]


def binding(source=SOURCE, path="value"):
    return {"kind": "node_output", "workflow_node_uuid": source, "data_key": path}


@pytest.mark.parametrize("invalid", [
    {"kind": "node_output"}, {"kind": "node_output", "node_key": "step-0", "data_key": "x"},
    {**binding(), "expression": "eval"}, binding(source="not-uuid"), binding(path=1),
])
def test_reject_malformed_reference(invalid):
    with pytest.raises((ValueError, TypeError)):
        result_references({"nested": [invalid]})


def test_nested_remap_and_resolve_preserve_literal_returned_data():
    template = {"list": [{"kind": "node_output", "node_key": "s", "data_key": ""}]}
    mapped = remap_result_bindings(template, lambda key: SOURCE, template=True)
    returned = {"value": "{{loop.index}}", "nested": binding(source=TARGET)}
    assert resolve_result_bindings(mapped, lambda source: returned, read_path=json_get_exists) == {"list": [returned]}
    assert template["list"][0]["node_key"] == "s"


@pytest.mark.parametrize("case", ["branch", "loop", "missing", "self"])
def test_graph_rejects_invisible_results(case):
    nodes = {SOURCE: {}, TARGET: {}}
    kinds = {SOURCE: "device_action", TARGET: "device_action"}
    parents = {}
    source = SOURCE
    if case == "branch":
        nodes[SOURCE] = {"execution_policy": {"condition_guards": [{"node_uuid": CONDITION, "equals": True}]}}
    elif case == "loop":
        parents[SOURCE] = LOOP
    elif case == "missing":
        source = str(uuid4())
    else:
        source = TARGET
    with pytest.raises(ValueError):
        validate_result_binding_graph(nodes, kinds, parents, {TARGET: {"value": binding(source)}})


def test_graph_accepts_outer_result_inside_loop_and_records_dependency():
    result = validate_result_binding_graph({SOURCE: {}, TARGET: {}, LOOP: {}},
        {SOURCE: "device_action", TARGET: "device_action", LOOP: "loop"},
        {TARGET: LOOP}, {TARGET: {"value": binding()}})
    assert result[TARGET] == {SOURCE}
