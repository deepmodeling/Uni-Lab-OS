"""人工确认包装必须跨画布保存、Python 编译和冷启动保持。"""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _applied_graph
from tests.workflow.test_domain_workflow_source_sync import _empty_domain_package, _service
from tests.workflow.test_f05_authoring_fixed_executor_projection import (
    ACTION_NODE_UUID,
    DEVICE_MATERIAL_UUID,
    _catalog,
    _source,
)
from tests.workflow.test_structured_condition_authoring import _condition_template
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.models import CandidateCompilation


def _engine() -> WorkflowAuthoringEngine:
    """使用与公共 API 相同的模板 schema 形状。"""
    template = _catalog().actions[0].detached_template()
    template["schema"] = None
    return WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities([template], []))


def _compile(
    engine: WorkflowAuthoringEngine,
    source: str,
    graph: dict[str, Any] | None = None,
) -> CandidateCompilation:
    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="memory://manual.py",
        applied_graph=graph or _applied_graph(),
    )


@pytest.mark.parametrize("timeout", [1, 45, 86400])
@pytest.mark.parametrize("disabled", [False, True])
def test_manual_wrapper_roundtrip_and_removal(timeout: int, disabled: bool) -> None:
    """开关和超时不是动作参数；开启、冷编译和关闭均保持明确语义。"""
    engine = _engine()
    initial = _compile(engine, _source(DEVICE_MATERIAL_UUID))
    assert initial.valid, initial.diagnostics
    graph = deepcopy(initial.graph)
    node = graph["nodes"][0]
    node.update(
        type="manual_confirm",
        manual_confirmation={"timeout_seconds": timeout},
        disabled=disabled,
    )
    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri="memory://manual.py",
    )
    assert generated.valid, generated.diagnostics
    for baseline in (graph, _applied_graph()):
        rebuilt = _compile(engine, generated.normalized_python_source, baseline)
        assert rebuilt.valid, rebuilt.diagnostics
        actual = rebuilt.graph["nodes"][0]
        assert actual["type"] == "manual_confirm"
        assert actual["manual_confirmation"] == {"timeout_seconds": timeout}
        assert bool(actual.get("disabled")) == disabled
        assert actual["param"] == node["param"]
    enabled_source = generated.normalized_python_source.replace(
        f" manual_confirmation_timeout_seconds={timeout}", ""
    )
    removed = _compile(engine, enabled_source, rebuilt.graph)
    assert removed.valid, removed.diagnostics
    assert removed.graph["nodes"][0]["type"] == "ILab"
    assert not removed.graph["nodes"][0].get("manual_confirmation")


@pytest.mark.parametrize("timeout", ["0", "86401", "-1", "true", "1.5"])
def test_invalid_manual_timeout_fails_closed(timeout: str) -> None:
    engine = _engine()
    source = _source(DEVICE_MATERIAL_UUID).replace(
        f"node_uuid={ACTION_NODE_UUID}",
        f"node_uuid={ACTION_NODE_UUID} manual_confirmation_timeout_seconds={timeout}",
    )
    assert not _compile(engine, source).valid


def test_manual_wrapper_requires_fixed_device() -> None:
    engine = _engine()
    source = _source(None).replace(
        f"node_uuid={ACTION_NODE_UUID}",
        f"node_uuid={ACTION_NODE_UUID} manual_confirmation_timeout_seconds=45",
    )
    assert not _compile(engine, source).valid


def test_condition_only_selected_action_keeps_manual_wrapper() -> None:
    """同一动作模板只包装 False 分支，不污染 True 分支或条件控制器。"""
    action = _engine()._catalog.actions[0].detached_template()
    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities([action, _condition_template()], [])
    )
    source = _source(DEVICE_MATERIAL_UUID).replace(
        "def fixed_executor_projection():", "def fixed_executor_projection(*, value: bool = False):"
    ).replace(
        f"    # unilab:node_uuid={ACTION_NODE_UUID}\n    prepared = reactor.prepare()",
        "    # unilab:node_uuid=20000000-0000-4000-8000-000000000021\n"
        "    if value:\n"
        "        # unilab:node_uuid=20000000-0000-4000-8000-000000000022\n"
        "        true_result = reactor.prepare()\n"
        "    else:\n"
        f"        # unilab:node_uuid={ACTION_NODE_UUID} manual_confirmation_timeout_seconds=45\n"
        "        prepared = reactor.prepare()",
    )
    result = _compile(engine, source)
    assert result.valid, result.diagnostics
    assert [
        n["uuid"] for n in result.graph["nodes"] if n["type"] == "manual_confirm"
    ] == [ACTION_NODE_UUID]
    rebuilt = _compile(engine, result.normalized_python_source)
    assert rebuilt.valid, rebuilt.diagnostics
    assert rebuilt.graph == result.graph
    bad_source = source.replace(
        "node_uuid=20000000-0000-4000-8000-000000000021",
        "node_uuid=20000000-0000-4000-8000-000000000021 manual_confirmation_timeout_seconds=45",
    )
    rejected = _compile(engine, bad_source)
    assert not rejected.valid
    assert rejected.diagnostics[0]["code"] == "invalid_manual_confirmation"


def test_native_manual_action_has_stable_default_config() -> None:
    """原生人工确认动作不需要额外源码标记，生成源码后仍达到固定点。"""
    action = _engine()._catalog.actions[0].detached_template()
    action["node_type"] = "manual_confirm"
    engine = WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities([action], []))
    result = _compile(engine, _source(DEVICE_MATERIAL_UUID))
    assert result.valid, result.diagnostics
    repeated = _compile(engine, result.normalized_python_source, result.graph)
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == result.graph


def test_save_manual_wrapper_survives_domain_restart(tmp_path: Path) -> None:
    """真实保存入口写回源码，销毁内存目录后仍生成等待人工确认的 Job。"""
    root = tmp_path / "domain"
    _empty_domain_package(root)
    database = tmp_path / "runtime.db"
    engine = _engine()
    service, _, _ = _service(database_path=database, selected_root=root, engine=engine)
    try:
        service.import_python_workflow(
            file_name="manual.py", python_source=_source(DEVICE_MATERIAL_UUID)
        )
        graph = service.get_graph(WORKFLOW_UUID)
        graph["nodes"][0].update(
            type="manual_confirm", manual_confirmation={"timeout_seconds": 45}
        )
        saved = service.save_graph(
            WORKFLOW_UUID,
            revision=graph["workflow"]["revision"],
            nodes=graph["nodes"],
            edges=graph["edges"],
        )
        assert saved["nodes"][0]["type"] == "manual_confirm"
        assert saved["nodes"][0]["manual_confirmation"] == {"timeout_seconds": 45}
    finally:
        service.close()
    reopened, _, _ = _service(database_path=database, selected_root=root, engine=engine)
    try:
        graph = reopened.get_graph(WORKFLOW_UUID)
        assert graph["nodes"][0]["type"] == "manual_confirm"
        plan, jobs = ExecutionPlanBuilder().build(graph, run_mode="normal", target_node_uuid=None)
        assert jobs[0]["executor_kind"] == "manual_confirm"
        assert plan["nodes"][0]["manual_confirmation"] == {"timeout_seconds": 45}
    finally:
        reopened.close()
