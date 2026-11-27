from copy import deepcopy

import pytest

from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.task_input import TaskInputError, prepare_task_input
from unilabos.workflow.workflow_io import (
    WorkflowIOValidationError,
    validate_workflow_graph_io,
)

from .test_authoring_engine import WORKFLOW_UUID, _applied_graph
from .test_f05_material_source_authoring import (
    MATERIAL_SOURCE_NODE_UUID,
    MOUNT_MATERIAL_UUID,
    _compile,
    _engine,
    _source,
)

SITE_UUID = "60000000-0000-4000-8000-000000000010"


def selected_graph():
    """编译最小启动库位来源，保持公共作者入口可验证。"""
    source = (
        _source()
        .replace("def material_assay():", "def material_assay(*, source_site: str):")
        .replace("site=None,", "site=source_site,")
    )
    compiled = _compile(source)
    assert compiled.valid, compiled.diagnostics
    return compiled.graph


@pytest.mark.parametrize("reference", ["staging-a", "deck-a/staging-a"])
def test_static_site_reference_round_trips_and_freezes_at_task_creation(reference):
    source = _source().replace("site=None,", f"site={reference!r},")
    engine = _engine()
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="memory://static-site.py",
        applied_graph=_applied_graph(),
    )
    assert compiled.valid, compiled.diagnostics
    node = next(
        node for node in compiled.graph["nodes"] if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    assert node["param"]["site"] is None
    assert node["meta_data"]["unilab"]["material_source_site_binding"] == {
        "reference": reference
    }
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=compiled.normalized_python_source,
        source_uri="memory://static-site.py",
        applied_graph=compiled.graph,
    )
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics

    plan, jobs = ExecutionPlanBuilder().build(
        compiled.graph, run_mode="full", target_node_uuid=None
    )
    requests = []

    def resolve_site(request):
        requests.append(request)
        return {"site_uuids": [SITE_UUID], "fingerprint": "sha256:test"}

    prepared = prepare_task_input(
        graph=compiled.graph,
        raw_input={},
        execution_plan=plan,
        jobs=jobs,
        site_selection_resolver=resolve_site,
    )
    assert requests == [
        {
            "version": 1,
            "owner_material_uuid": MOUNT_MATERIAL_UUID,
            "occupant_material_uuid": "",
            "group_key": "",
            "exact_site_reference": reference,
            "strategy": "sort_order",
        }
    ]
    frozen = next(
        node
        for node in prepared.execution_plan["nodes"]
        if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    assert frozen["param"]["site"] == SITE_UUID
    assert frozen["material_requirements"][0]["site_uuid"] == SITE_UUID
    frozen_job = next(
        job
        for job in prepared.jobs
        if job["workflow_node_uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    assert frozen_job["param"]["site"] == SITE_UUID
    frozen_snapshot = next(
        node
        for node in prepared.workflow_snapshot["nodes"]
        if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    assert frozen_snapshot["param"]["site"] == SITE_UUID
    assert "material_source_site_binding" not in frozen_snapshot[
        "meta_data"
    ]["unilab"]
    original = next(
        node
        for node in compiled.graph["nodes"]
        if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    assert original["param"]["site"] is None


@pytest.mark.parametrize(
    "bad_input", [{}, {"source_site": ""}, {"source_site": None}, {"source_site": 3}]
)
def test_missing_or_invalid_site_never_creates_a_broad_source(bad_input):
    graph = selected_graph()
    plan, jobs = ExecutionPlanBuilder().build(
        graph, run_mode="full", target_node_uuid=None
    )
    original = deepcopy(plan)
    with pytest.raises(TaskInputError):
        prepare_task_input(
            graph=graph,
            raw_input=bad_input,
            execution_plan=plan,
            jobs=jobs,
            site_selection_resolver=lambda _: {"site_uuids": [SITE_UUID]},
        )
    assert plan == original


@pytest.mark.parametrize(
    "binding",
    [
        {"parameter": "unknown"},
        {"parameter": 1},
        {"parameter": "source_site", "extra": True},
        {"reference": ""},
        {"parameter": "source_site", "reference": "staging-a"},
    ],
)
def test_direct_graph_rejects_invalid_source_site_binding(binding):
    graph = selected_graph()
    node = next(
        node for node in graph["nodes"] if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    node["meta_data"]["unilab"]["material_source_site_binding"] = binding
    with pytest.raises(WorkflowIOValidationError):
        validate_workflow_graph_io(graph)


def test_selected_site_cannot_escape_declared_candidate_range():
    graph = selected_graph()
    node = next(
        node for node in graph["nodes"] if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    node["param"]["slot_range"] = ["60000000-0000-4000-8000-000000000011"]
    plan, jobs = ExecutionPlanBuilder().build(
        graph, run_mode="full", target_node_uuid=None
    )
    with pytest.raises(TaskInputError, match="不在工作流允许范围"):
        prepare_task_input(
            graph=graph,
            raw_input={"source_site": "A1"},
            execution_plan=plan,
            jobs=jobs,
            site_selection_resolver=lambda _: {"site_uuids": [SITE_UUID]},
        )


def test_unresolved_site_fails_before_task_creation():
    graph = selected_graph()
    plan, jobs = ExecutionPlanBuilder().build(
        graph, run_mode="full", target_node_uuid=None
    )
    with pytest.raises(TaskInputError, match="缺少库存权威"):
        prepare_task_input(
            graph=graph, raw_input={"source_site": "A1"}, execution_plan=plan, jobs=jobs
        )
