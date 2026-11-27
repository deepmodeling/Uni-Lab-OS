"""可信工作流创作的源码/候选图固定点测试。"""

from __future__ import annotations

import ast
from copy import deepcopy

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.models import CandidateChangeset

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    PREPARE_NODE_UUID,
    WORKFLOW_UUID,
    _compile,
    _engine,
    _source,
)


@pytest.fixture()
def authoring_engine() -> WorkflowAuthoringEngine:
    """向往返测试提供隔离的工作流创作编译器。"""

    return _engine()


def _typed_result_source() -> str:
    """返回使用 ``TypedDict`` 结果记录的等价作者源码。"""

    return f'''from typing import Annotated, Literal, TypedDict

from pydantic import Field
from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow


class SamplePreparationResult(TypedDict):
    sample: ResourceSlot
    report: Annotated[str, Field(unit="text")]


reactor: Reactor = device()


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Sample preparation",
    description="Prepare and analyze one sample.",
)
def prepare_sample(
    *,
    sample: ResourceSlot,
    cycles: Annotated[int, Field(unit="cycle", ge=1, le=10)] = 3,
    mode: Literal["fast", "safe"] = "safe",
) -> SamplePreparationResult:
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=sample, cycles=cycles)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    analyzed = reactor.analyze(prepared=prepared.prepared, label=mode)
    return {{"sample": prepared.prepared, "report": analyzed.report}}
'''


def test_compile_generate_compile_reaches_semantic_fixed_point(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """Python→候选图→Python→候选图必须达到语义固定点。"""

    compiled = _compile(authoring_engine)
    assert compiled.valid and compiled.graph is not None
    generated = authoring_engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        source_uri="package://lab/workflows/generated.py",
    )
    assert generated.valid and generated.normalized_python_source is not None
    repeated = _compile(
        authoring_engine,
        generated.normalized_python_source,
        graph=compiled.graph,
    )

    assert repeated.valid
    assert repeated.graph == compiled.graph
    assert repeated.normalized_python_source == generated.normalized_python_source
    assert CandidateChangeset.model_validate(repeated.changeset).kind == "source_only"


def test_typed_result_record_is_the_canonical_round_trip_form(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """显式结果记录应保留类型并成为确定性生成的规范形式。"""

    compiled = _compile(authoring_engine, _typed_result_source())

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    module = ast.parse(compiled.normalized_python_source)
    result_records = [
        statement
        for statement in module.body
        if isinstance(statement, ast.ClassDef)
    ]
    assert [record.name for record in result_records] == ["SamplePreparationResult"]
    output_contract = compiled.graph["workflow"]["meta_data"]["unilab"][
        "output_contract"
    ]
    assert output_contract["outputs"][1]["unit"] == "text"
    input_contract = compiled.graph["workflow"]["meta_data"]["unilab"][
        "input_contract"
    ]
    assert input_contract["parameters"][1]["unit"] == "cycle"
    workflow_function = next(
        statement
        for statement in module.body
        if isinstance(statement, ast.FunctionDef)
    )
    assert ast.unparse(workflow_function.returns) == "SamplePreparationResult"
    assert isinstance(workflow_function.body[-1], ast.Return)
    assert isinstance(workflow_function.body[-1].value, ast.Dict)

    repeated = _compile(
        authoring_engine,
        compiled.normalized_python_source,
        graph=compiled.graph,
    )
    assert repeated.valid
    assert repeated.graph == compiled.graph
    assert CandidateChangeset.model_validate(repeated.changeset).kind == "source_only"


def test_validate_rejects_source_graph_mismatch(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """公共验证接口必须拒绝源码与候选图的语义分叉。"""

    compiled = _compile(authoring_engine)
    assert compiled.valid and compiled.graph is not None
    forged = deepcopy(compiled.graph)
    forged["nodes"][0]["param"] = {"cycles": 9}

    result = authoring_engine.validate(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=forged,
        python_source=compiled.normalized_python_source or "",
        source_uri="package://lab/workflows/sample.py",
    )

    assert not result.valid
    assert any(item["code"] == "round_trip_mismatch" for item in result.diagnostics)

def test_node_set_reservation_roundtrip(authoring_engine):
    source = _source().replace('device, workflow, workflow_output', 'device, workflow, workflow_output, resources')
    source = source.replace('    return workflow_output(', '    with resources() as locks:\n        locks.hold("robot", nodes=[prepared, analyzed], name="Transfer")\n    return workflow_output(')
    compiled = _compile(authoring_engine, source)
    assert compiled.valid, compiled.diagnostics
    assert "locks.hold('robot', nodes=[" in compiled.normalized_python_source
    repeated = _compile(authoring_engine, compiled.normalized_python_source, graph=compiled.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


def test_hold_renderer_avoids_workflow_input_name(authoring_engine):

    source = _source().replace("workflow_output\n", "workflow_output, resources\n", 1)
    source = source.replace("    mode: Literal", '    locks: str = "",\n    mode: Literal')
    source = source.replace("    return workflow_output(", (
        '    with resources() as windows:\n'
        '        windows.hold("robot", nodes=[prepared])\n'
        '        windows.hold("site:deck/A1", nodes=[prepared, analyzed])\n'
        '    return workflow_output('
    ))
    compiled = _compile(authoring_engine, source)
    assert compiled.valid, compiled.diagnostics
    rendered = compiled.normalized_python_source
    assert rendered.count("with resources() as locks_:") == 1
    assert rendered.count("locks_.hold(") == 2
    repeated = _compile(authoring_engine, rendered, graph=compiled.graph)
    assert repeated.valid and repeated.graph == compiled.graph


def test_explicit_resource_reservation_roundtrip(authoring_engine):
    source = _source().replace('device, workflow, workflow_output', 'device, workflow, workflow_output, resources')
    source = source.replace('    return workflow_output(', '    with resources() as locks:\n        locks.hold("turntable", "shared_site", nodes=[prepared, analyzed], name="Handoff")\n    return workflow_output(')
    compiled = _compile(authoring_engine, source)
    assert compiled.valid, compiled.diagnostics
    assert "locks.hold('turntable', 'shared_site', nodes=" in compiled.normalized_python_source
    repeated = _compile(authoring_engine, compiled.normalized_python_source, graph=compiled.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


@pytest.mark.parametrize('resources', ['""', '"x", "x"', '123', 'missing_resource'])
def test_invalid_explicit_resource_reservation_rejected(authoring_engine, resources):
    source = _source().replace('device, workflow, workflow_output', 'device, workflow, workflow_output, resources')
    source = source.replace('    return workflow_output(', f'    with resources() as locks:\n        locks.hold({resources}, nodes=[prepared, analyzed])\n    return workflow_output(')
    compiled = _compile(authoring_engine, source)
    assert not compiled.valid


def test_generated_auto_prefix_action_keeps_canonical_identity_and_python_method():
    from unilabos.workflow.authoring_kernel import AuthoringCatalogError, AuthoringCatalogSnapshot
    original = _engine()._catalog
    templates = [a.detached_template() for a in original.actions]
    handles = [h for a in original.actions for h in a.detached_handles()]
    for item in templates:
        if item['name'] == 'prepare':
            item['name'] = 'auto-prepare'
    catalog = AuthoringCatalogSnapshot.from_entities(templates, handles)
    engine = WorkflowAuthoringEngine(catalog=catalog)
    compiled = _compile(engine)
    assert compiled.valid, compiled.diagnostics
    assert next(n for n in compiled.graph['nodes'] if n['uuid'] == PREPARE_NODE_UUID)['action_name'] == 'auto-prepare'
    generated = engine.generate_python(workflow_uuid=WORKFLOW_UUID, workflow_revision=7, graph=compiled.graph, source_uri='package://lab/workflows/generated.py')
    assert generated.valid, generated.diagnostics
    assert 'reactor.prepare(' in generated.normalized_python_source
    assert 'auto-prepare(' not in generated.normalized_python_source
    reopened = _compile(WorkflowAuthoringEngine(catalog=catalog), generated.normalized_python_source, graph=compiled.graph)
    assert reopened.valid, reopened.diagnostics
    assert reopened.graph == compiled.graph
    direct = next(a.detached_template() for a in original.actions if a.template['name'] == 'prepare')
    direct['uuid'] = '30000000-0000-4000-8000-000000000099'
    ambiguous = WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities([*templates, direct], handles))
    rejected = ambiguous.generate_python(workflow_uuid=WORKFLOW_UUID, workflow_revision=7, graph=compiled.graph, source_uri='package://lab/workflows/generated.py')
    assert not rejected.valid
    assert catalog.require_action('lab.devices:Reactor', 'analyze').template['name'] == 'analyze'
    with pytest.raises(AuthoringCatalogError):
        catalog.require_action('other.devices:Reactor', 'prepare')


def test_compile_preserves_resource_cycle_diagnostic(authoring_engine, monkeypatch):
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder
    from unilabos.workflow.resource_lock_plan import ResourcePlanError

    def reject_wait_cycle(self, graph, **kwargs):
        raise ResourcePlanError("resource_cycle", "A -> B -> A")

    monkeypatch.setattr(ExecutionPlanBuilder, "build", reject_wait_cycle)
    result = _compile(authoring_engine)
    assert not result.valid
    assert result.diagnostics == [
        {"severity": "error", "code": "resource_cycle", "message": "A -> B -> A"}
    ]
