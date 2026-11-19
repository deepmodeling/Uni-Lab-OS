"""循环节点契约：LoopSpec 校验、条件比较、迭代变量占位符、容器层级的边提升。"""

from __future__ import annotations

import pytest

from unilabos.protocol.runtime.loop import (
    LoopSpec,
    compare,
    loop_context,
    parse_loop_spec,
    references_loop_placeholder,
    substitute_loop_placeholders,
)
from unilabos.protocol.utils.workflow_hierarchy import (
    HierarchyError,
    execution_parents,
    lift_edge,
    lifted_pairs,
)


def test_for_spec_requires_count_and_rejects_condition() -> None:
    spec = parse_loop_spec({"mode": "for", "count": 3})
    assert spec.describe() == "for ×3"
    with pytest.raises(ValueError, match="count"):
        parse_loop_spec({"mode": "for"})
    with pytest.raises(ValueError, match="condition"):
        parse_loop_spec(
            {
                "mode": "for",
                "count": 2,
                "condition": {"source": "device_state", "device_id": "d", "field": "f", "value": 1},
            }
        )
    with pytest.raises(ValueError):
        parse_loop_spec({"mode": "for", "count": 0})


def test_while_spec_requires_a_complete_condition() -> None:
    spec = parse_loop_spec(
        {
            "mode": "while",
            "condition": {
                "source": "device_state",
                "device_id": " heater ",
                "field": "temperature_c",
                "op": "<",
                "value": 80,
            },
            "interval_seconds": 1.5,
        }
    )
    assert spec.condition is not None
    assert spec.condition.device_id == "heater"
    assert spec.describe() == "while heater.temperature_c < 80"
    with pytest.raises(ValueError, match="device_id"):
        parse_loop_spec({"mode": "while", "condition": {"source": "device_state", "field": "x", "value": 1}})
    with pytest.raises(ValueError, match="node_uuid"):
        parse_loop_spec({"mode": "while", "condition": {"source": "node_output", "value": 1}})
    with pytest.raises(ValueError, match="value"):
        parse_loop_spec(
            {"mode": "while", "condition": {"source": "device_state", "device_id": "d", "field": "f", "op": ">"}}
        )
    # exists 不需要 value
    parse_loop_spec(
        {"mode": "while", "condition": {"source": "device_state", "device_id": "d", "field": "f", "op": "exists"}}
    )
    with pytest.raises(ValueError, match="max_iterations"):
        LoopSpec.model_validate(
            {
                "mode": "while",
                "count": 3,
                "condition": {"source": "device_state", "device_id": "d", "field": "f", "value": 1},
            }
        )


def test_compare_coerces_like_the_editor_branch() -> None:
    assert compare("<", 25.5, 80)
    assert compare(">=", "80", 80)
    assert not compare("==", "ready", "done")
    assert compare("!=", "ready", "done")
    assert compare("==", True, "yes")
    assert compare("==", False, "0")
    assert compare("contains", "site-A2", "A2")
    assert compare("contains", ["A1", "A2"], "A2")
    assert compare("exists", 0, None)
    assert not compare("exists", None, None)
    with pytest.raises(ValueError):
        compare(">", "abc", 1)


def test_loop_placeholders_keep_types_for_whole_string_matches() -> None:
    context = loop_context(2, 5)
    assert context == {"index": 2, "iteration": 3, "count": 5}
    params = {
        "index": "{{loop.index}}",
        "name": "sample-{{ loop.iteration }}",
        "nested": {"total": "{{loop.count}}", "items": ["{{loop.index}}", "x"]},
        "untouched": 7,
    }
    assert substitute_loop_placeholders(params, context) == {
        "index": 2,
        "name": "sample-3",
        "nested": {"total": 5, "items": [2, "x"]},
        "untouched": 7,
    }
    assert substitute_loop_placeholders("n={{loop.count}}", loop_context(0, None)) == "n="
    assert references_loop_placeholder(params)
    assert not references_loop_placeholder({"plain": "{{other}}"})


def test_edges_lift_to_the_lowest_common_container() -> None:
    nodes = {
        "a": {"parent_uuid": None},
        "loop": {"parent_uuid": None},
        "b1": {"parent_uuid": "loop"},
        "inner": {"parent_uuid": "loop"},
        "c": {"parent_uuid": "inner"},
        "z": {"parent_uuid": None},
        "grp": {"parent_uuid": None},
        "g1": {"parent_uuid": "grp"},
    }
    kinds = {
        "a": "device_action",
        "loop": "loop",
        "b1": "device_action",
        "inner": "loop",
        "c": "device_action",
        "z": "device_action",
        "grp": "group",
        "g1": "device_action",
    }
    parents = execution_parents(nodes, kinds, enabled=[k for k in nodes if k != "grp"])
    # 组框不是执行容器：g1 仍是顶层
    assert parents == {
        "a": None,
        "loop": None,
        "b1": "loop",
        "inner": "loop",
        "c": "inner",
        "z": None,
        "g1": None,
    }
    assert lift_edge(parents, "a", "b1") == ("a", "loop")
    assert lift_edge(parents, "c", "z") == ("loop", "z")
    assert lift_edge(parents, "b1", "c") == ("b1", "inner")
    assert lift_edge(parents, "a", "z") == ("a", "z")
    with pytest.raises(HierarchyError):
        lift_edge(parents, "loop", "b1")
    with pytest.raises(HierarchyError):
        lift_edge(parents, "c", "loop")
    # 循环体内部的边不变；提升后重复/自环的边去掉
    assert lifted_pairs(parents, [("a", "b1"), ("a", "inner"), ("b1", "inner"), ("c", "b1")]) == [
        ("a", "loop"),
        ("b1", "inner"),
        ("inner", "b1"),
    ]


def test_parent_cycles_are_rejected() -> None:
    nodes = {"x": {"parent_uuid": "y"}, "y": {"parent_uuid": "x"}}
    with pytest.raises(HierarchyError):
        execution_parents(nodes, {"x": "loop", "y": "loop"})
