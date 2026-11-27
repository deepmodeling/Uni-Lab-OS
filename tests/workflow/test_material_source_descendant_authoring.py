"""递归来源选择是显式布尔声明，源码、图和执行计划保持同一合同。"""

import pytest

from unilabos.workflow.execution_plan import ExecutionPlanBuilder

from .test_authoring_engine import WORKFLOW_UUID, _applied_graph
from .test_f05_material_source_authoring import (
    MATERIAL_SOURCE_NODE_UUID,
    _engine,
    _source,
)


@pytest.mark.parametrize("value", ["True", "False", "1", "'true'"])
def test_recursive_source_roundtrip_and_plan(value):
    engine = _engine()
    source = _source().replace(
        "mode='existing',", f"mode='existing', search_descendants={value},"
    )

    def compile(text, graph=None):
        return engine.compile(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            python_source=text,
            source_uri="package://lab/workflows/source.py",
            applied_graph=graph or _applied_graph(),
        )

    result = compile(source)
    assert result.valid == (value in {"True", "False"}), result.diagnostics
    if not result.valid:
        return
    rebuilt = compile(result.normalized_python_source, result.graph)
    assert rebuilt.valid, rebuilt.diagnostics
    assert rebuilt.graph == result.graph
    source_node = next(
        n for n in result.graph["nodes"] if n["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    assert source_node["param"].get("search_descendants", False) == (value == "True")
    plan, _ = ExecutionPlanBuilder().build(
        result.graph, run_mode="normal", target_node_uuid=None
    )
    planned = next(n for n in plan["nodes"] if n["uuid"] == MATERIAL_SOURCE_NODE_UUID)
    assert planned["material_requirements"][0].get("search_descendants", False) == (
        value == "True"
    )
