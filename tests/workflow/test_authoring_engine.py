"""可信工作流创作编译器（Authoring Compiler）的公共接口测试。"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.models import (
    CandidateChangeset,
    CandidateDiagnostic,
    CandidateSourceMapEntry,
)

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
PREPARE_NODE_UUID = "20000000-0000-4000-8000-000000000001"
ANALYZE_NODE_UUID = "20000000-0000-4000-8000-000000000002"
PREPARE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000001"
ANALYZE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000002"
PREPARE_SAMPLE_TARGET = "40000000-0000-4000-8000-000000000001"
PREPARE_CYCLES_TARGET = "40000000-0000-4000-8000-000000000002"
PREPARE_SAMPLE_SOURCE = "40000000-0000-4000-8000-000000000003"
PREPARE_READY_TARGET = "40000000-0000-4000-8000-000000000004"
PREPARE_READY_SOURCE = "40000000-0000-4000-8000-000000000005"
ANALYZE_SAMPLE_TARGET = "41000000-0000-4000-8000-000000000001"
ANALYZE_LABEL_TARGET = "41000000-0000-4000-8000-000000000002"
ANALYZE_REPORT_SOURCE = "41000000-0000-4000-8000-000000000003"
ANALYZE_READY_TARGET = "41000000-0000-4000-8000-000000000004"
ANALYZE_READY_SOURCE = "41000000-0000-4000-8000-000000000005"


def _handle(
    handle_uuid: str,
    *,
    node_template_uuid: str,
    key: str,
    io_type: str,
    value_type: str,
    required: bool = False,
    data_source: str = "executor",
) -> dict[str, Any]:
    """构造一个连接点（Handle）目录投影。

    参数说明：UUID 与所属模板固定身份，``key``/``io_type`` 描述业务键和方向，
    ``value_type`` 描述旧兼容类型；返回可直接进入创作目录快照的字典。
    """

    value_schema = (
        {"$slot": "ResourceSlot"}
        if value_type == "ResourceSlot"
        else {"type": value_type}
    )
    return {
        "uuid": handle_uuid,
        "workflow_node_template_uuid": node_template_uuid,
        "handle_key": key,
        "io_type": io_type,
        "display_name": key.title(),
        "type": value_type,
        "required": required,
        "data_source": data_source,
        "data_key": key,
        "description": None,
        "meta_data": {"unilab": {"value_schema": value_schema}},
    }


def _template(
    template_uuid: str,
    *,
    name: str,
    handles: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """构造动作模板及其连接点（Handle）集合。

    参数说明：``template_uuid`` 和 ``name`` 标识目录动作，``handles`` 是完整
    连接点集合；返回节点模板与连接点二元组。
    """

    return (
        {
            "uuid": template_uuid,
            "resource_template_uuid": "31000000-0000-4000-8000-000000000001",
            "name": name,
            "display_name": name.title(),
            "class": "lab.devices:Reactor",
            "description": f"{name.title()} 动作模板说明",
            "meta_data": {"owner": "test"},
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "action",
            "node_type": "compute",
            "icon": None,
            "header": None,
            "footer": None,
        },
        handles,
    )


def _engine() -> WorkflowAuthoringEngine:
    """创建只持有不可变目录快照的工作流创作编译器。"""

    prepare, prepare_handles = _template(
        PREPARE_TEMPLATE_UUID,
        name="prepare",
        handles=[
            _handle(
                PREPARE_SAMPLE_TARGET,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="sample",
                io_type="target",
                value_type="ResourceSlot",
                required=True,
            ),
            _handle(
                PREPARE_CYCLES_TARGET,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="cycles",
                io_type="target",
                value_type="integer",
                required=True,
            ),
            _handle(
                PREPARE_SAMPLE_SOURCE,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="prepared",
                io_type="source",
                value_type="ResourceSlot",
            ),
            _handle(
                PREPARE_READY_TARGET,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="ready",
                io_type="target",
                value_type="any",
                data_source="dependency",
            ),
            _handle(
                PREPARE_READY_SOURCE,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="ready",
                io_type="source",
                value_type="any",
                data_source="dependency",
            ),
        ],
    )
    analyze, analyze_handles = _template(
        ANALYZE_TEMPLATE_UUID,
        name="analyze",
        handles=[
            _handle(
                ANALYZE_SAMPLE_TARGET,
                node_template_uuid=ANALYZE_TEMPLATE_UUID,
                key="prepared",
                io_type="target",
                value_type="ResourceSlot",
                required=True,
            ),
            _handle(
                ANALYZE_LABEL_TARGET,
                node_template_uuid=ANALYZE_TEMPLATE_UUID,
                key="label",
                io_type="target",
                value_type="string",
                required=True,
            ),
            _handle(
                ANALYZE_REPORT_SOURCE,
                node_template_uuid=ANALYZE_TEMPLATE_UUID,
                key="report",
                io_type="source",
                value_type="string",
            ),
            _handle(
                ANALYZE_READY_TARGET,
                node_template_uuid=ANALYZE_TEMPLATE_UUID,
                key="ready",
                io_type="target",
                value_type="any",
                data_source="dependency",
            ),
            _handle(
                ANALYZE_READY_SOURCE,
                node_template_uuid=ANALYZE_TEMPLATE_UUID,
                key="ready",
                io_type="source",
                value_type="any",
                data_source="dependency",
            ),
        ],
    )
    catalog = AuthoringCatalogSnapshot.from_entities(
        [prepare, analyze],
        [*prepare_handles, *analyze_handles],
    )
    return WorkflowAuthoringEngine(catalog=catalog)


@pytest.fixture()
def authoring_engine() -> WorkflowAuthoringEngine:
    """向单元测试提供隔离的工作流创作编译器。"""

    return _engine()


def _applied_graph() -> dict[str, Any]:
    """构造首次编译使用的空工作流图（Workflow Graph）。"""

    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "name": "Persisted name",
            "tags": ["keep"],
            "description": "Persisted description",
            "meta_data": {"owner": "keep"},
            "revision": 7,
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def _source() -> str:
    """返回覆盖输入绑定、节点输出绑定和确定性锚点的作者源码。"""

    return f'''from typing import Annotated, Literal

from pydantic import Field
from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device()


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Sample preparation",
    description="Prepare and analyze one sample.",
)
def prepare_sample(
    *,
    sample: ResourceSlot,
    cycles: Annotated[int, Field(ge=1, le=10)] = 3,
    mode: Literal["fast", "safe"] = "safe",
):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=sample, cycles=cycles)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    analyzed = reactor.analyze(prepared=prepared.prepared, label=mode)
    return workflow_output(sample=prepared.prepared, report=analyzed.report)
'''


def _compile(
    engine: WorkflowAuthoringEngine,
    source: str | None = None,
    *,
    graph: dict[str, Any] | None = None,
):
    """经公共编译接口生成候选结果。

    参数说明：``engine`` 是被测编译器，``source`` 可覆盖标准源码，``graph``
    可覆盖当前已应用图；返回候选编译结果（CandidateCompilation）。
    """

    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source() if source is None else source,
        source_uri="package://lab/workflows/sample.py",
        applied_graph=_applied_graph() if graph is None else graph,
    )


def test_compile_builds_backend_shaped_candidate(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """静态源码应编译为后端形状候选图与完整绑定。"""

    result = _compile(authoring_engine)

    assert result.valid, result.diagnostics
    assert result.graph is not None
    assert result.graph["workflow"]["name"] == "Sample preparation"
    assert result.graph["workflow"]["tags"] == ["keep"]
    assert [node["uuid"] for node in result.graph["nodes"]] == [
        PREPARE_NODE_UUID,
        ANALYZE_NODE_UUID,
    ]
    prepare = result.graph["nodes"][0]
    analyze = result.graph["nodes"][1]
    assert prepare["meta_data"]["unilab"]["input_bindings"] == {
        PREPARE_SAMPLE_TARGET: {"parameter": "sample"},
        PREPARE_CYCLES_TARGET: {"parameter": "cycles"},
    }
    assert analyze["meta_data"]["unilab"]["input_bindings"] == {
        ANALYZE_LABEL_TARGET: {"parameter": "mode"}
    }
    assert len(result.graph["edges"]) == 1
    assert result.graph["edges"][0]["source_handle_uuid"] == PREPARE_SAMPLE_SOURCE
    assert result.graph["edges"][0]["target_handle_uuid"] == ANALYZE_SAMPLE_TARGET


def test_unannotated_node_inherits_action_template_metadata(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """未写节点注释时应继承动作模板（Action Template）的显示名和描述。"""

    result = _compile(authoring_engine)

    assert result.valid and result.graph is not None, result.diagnostics
    prepare, analyze = result.graph["nodes"]
    assert (prepare["name"], prepare["description"]) == (
        "Prepare",
        "Prepare 动作模板说明",
    )
    assert (analyze["name"], analyze["description"]) == (
        "Analyze",
        "Analyze 动作模板说明",
    )
    assert result.normalized_python_source is not None
    assert "# [" not in result.normalized_python_source


def test_compile_is_deterministic_and_emits_source_map_and_changeset(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """同一输入必须生成相同源码映射与变更集（Changeset）。"""

    first = _compile(authoring_engine)
    second = _compile(authoring_engine)

    assert first.model_dump() == second.model_dump()
    assert first.normalized_python_source is not None
    ast.parse(first.normalized_python_source)
    assert [
        CandidateSourceMapEntry.model_validate(item).workflow_node_uuid
        for item in first.source_map
    ] == [PREPARE_NODE_UUID, ANALYZE_NODE_UUID]
    changeset = CandidateChangeset.model_validate(first.changeset)
    assert changeset.kind == "graph"
    assert changeset.created_node_uuids == [PREPARE_NODE_UUID, ANALYZE_NODE_UUID]


def test_compile_never_executes_author_source(
    authoring_engine: WorkflowAuthoringEngine,
    tmp_path: Path,
) -> None:
    """不可信作者源码即使含副作用语句也绝不能被执行。"""

    marker = tmp_path / "executed"
    hostile = _source().replace(
        "reactor: Reactor = device()",
        f'open({str(marker)!r}, "w").write("executed")\nreactor: Reactor = device()',
    )

    result = _compile(authoring_engine, hostile)

    assert not result.valid
    assert not marker.exists()
    assert any(item["code"] == "unsupported_authoring_syntax" for item in result.diagnostics)


def test_compile_reports_syntax_and_anchor_errors(
    authoring_engine: WorkflowAuthoringEngine,
) -> None:
    """语法错误和非法节点锚点必须成为结构化诊断而非异常泄漏。"""

    syntax_result = _compile(authoring_engine, "def broken(:\n")
    anchor_result = _compile(
        authoring_engine,
        _source().replace(
            f"# unilab:node_uuid={PREPARE_NODE_UUID}",
            "# unilab:node_uuid=not-a-uuid",
        ),
    )

    assert any(item["code"] == "syntax_error" for item in syntax_result.diagnostics)
    assert any(item["code"] == "invalid_node_anchor" for item in anchor_result.diagnostics)
    for result in (syntax_result, anchor_result):
        for item in result.diagnostics:
            CandidateDiagnostic.model_validate(item)


def test_missing_action_catalog_identity_fails_closed() -> None:
    """目录中缺少动作身份时不得猜测或继续编译。"""

    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities([], [])
    )

    result = _compile(engine)

    assert not result.valid
    assert any(
        item["code"] == "template_catalog_mismatch" for item in result.diagnostics
    )
