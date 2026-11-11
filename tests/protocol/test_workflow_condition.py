"""条件协议拒绝模糊真值、可执行字符串以及不可见分支引用。"""

from __future__ import annotations

import pytest

from unilabos.protocol.runtime.condition import (
    condition_references, evaluate_condition, validate_condition_graph,
)

SOURCE = "10000000-0000-4000-8000-000000000001"
CONDITION = "10000000-0000-4000-8000-000000000002"
BRANCH = "10000000-0000-4000-8000-000000000003"
JOIN = "10000000-0000-4000-8000-000000000004"


@pytest.mark.parametrize("value", [0, 1, "false", "", None, {}, []])
def test_condition_requires_actual_boolean(value):
    expression = {"field": {"node_uuid": SOURCE}, "name": "ready"}
    with pytest.raises(ValueError, match="布尔"):
        evaluate_condition({"version": 1, "expression": expression}, lambda _: {"ready": value})


@pytest.mark.parametrize("expression", [
    "__import__('os')", {"op": "eval", "args": [{"lit": True}, {"lit": True}]},
    {"lit": float("nan")}, {"lit": [True]}, {"not": {"lit": True}, "extra": True},
])
def test_condition_rejects_unstructured_or_unsupported_expressions(expression):
    with pytest.raises(ValueError):
        condition_references({"version": 1, "expression": expression})


def test_condition_short_circuit_does_not_read_missing_field():
    expression = {"op": "and", "args": [
        {"lit": False}, {"field": {"node_uuid": SOURCE}, "name": "missing"},
    ]}
    assert evaluate_condition({"version": 1, "expression": expression}, lambda _: {}) is False


def test_condition_does_not_compare_bool_as_number():
    with pytest.raises(ValueError, match="同类型"):
        evaluate_condition({"version": 1, "expression": {
            "op": "eq", "args": [{"lit": True}, {"lit": 1}],
        }}, lambda _: {})


def test_condition_graph_adds_result_and_guard_dependencies_and_rejects_leakage():
    guard = {"node_uuid": CONDITION, "equals": True}
    nodes = {SOURCE: {}, CONDITION: {}, BRANCH: {"execution_policy": {"condition_guards": [guard]}}}
    kinds = {SOURCE: "device_action", CONDITION: "condition", BRANCH: "device_action"}
    params = {SOURCE: {}, CONDITION: {"version": 1, "expression": {"field": {"node_uuid": SOURCE}, "name": "ready"}}, BRANCH: {}}
    refs = validate_condition_graph(nodes, kinds, {}, params)
    assert refs[CONDITION] == {SOURCE} and refs[BRANCH] == {CONDITION}
    nodes[JOIN] = {}
    kinds[JOIN] = "condition"
    params[JOIN] = {"version": 1, "expression": {"node_uuid": BRANCH}}
    with pytest.raises(ValueError, match="其他分支"):
        validate_condition_graph(nodes, kinds, {}, params)


def test_condition_branch_inventory_is_rejected_before_reservation():
    nodes = {CONDITION: {}, BRANCH: {
        "execution_policy": {"condition_guards": [{"node_uuid": CONDITION, "equals": False}]},
        "meta_data": {"inventory_requirements": [{"key": "water"}]},
    }}
    with pytest.raises(ValueError, match="库存"):
        validate_condition_graph(nodes, {CONDITION: "condition", BRANCH: "device_action"}, {},
                                 {CONDITION: {"version": 1, "expression": {"lit": True}}, BRANCH: {}})
