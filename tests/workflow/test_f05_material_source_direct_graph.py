"""F05.1 物料来源（MaterialSource）直接图保存与公共接缝回归。"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.template_projection_store import (
    RegistryTemplateProjectionStore,
)

from .test_authoring_engine import (
    PREPARE_SAMPLE_TARGET,
    PREPARE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _handle,
    _template,
)
from .test_f05_material_source_authoring import (
    FIXED_MATERIAL_UUID,
    MATERIAL_SOURCE_NODE_UUID,
    MOUNT_MATERIAL_UUID,
    PLATE_SOURCE_SYMBOL,
    PLATE_TEMPLATE_UUID,
    _material_source_template,
    _source,
)

SITE_UUID = "60000000-0000-4000-8000-000000000001"
SLOT_UUID = "60000000-0000-4000-8000-000000000002"
NONCANONICAL_MOUNT_UUID = "5ABC0000-0000-4000-8000-000000000010"


@dataclass(frozen=True, slots=True)
class _DirectGraphContext:
    """直接图公共接缝测试使用的已编译上下文。"""

    service: WorkflowService
    engine: WorkflowAuthoringEngine
    source: str
    candidate: dict[str, Any]
    applied_graph: dict[str, Any]


def _projection_handle(
    handle: dict[str, Any],
    *,
    resource_template_uuid: str,
    action_name: str,
) -> dict[str, Any]:
    """把目录连接点改成持久模板投影候选形状。

    参数说明：``handle`` 是完整连接点（Handle），其余参数组成父节点业务键。
    返回：保留显式 UUID 且可由模板投影存储解析父身份的新字典。
    """

    candidate = deepcopy(handle)
    candidate.pop("workflow_node_template_uuid", None)
    candidate["node_business_key"] = (resource_template_uuid, action_name)
    return candidate


@pytest.fixture()
def direct_graph_context(tmp_path: Path) -> Iterator[_DirectGraphContext]:
    """建立同时持久化模板和空工作流的直接图测试上下文。

    参数说明：``tmp_path`` 提供隔离 SQLite 路径。返回：逐项测试独占的服务、
    编译器、合法源码、候选图与保存前图；测试结束关闭工作流服务。
    """

    source_template, source_handle = _material_source_template()
    prepare_template, prepare_handles = _template(
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
            )
        ],
    )
    store = WorkflowStore(tmp_path / "workflow_history.db")
    projection_store = RegistryTemplateProjectionStore(store)
    nodes, handles = projection_store.replace(
        authority_id="f05-direct-graph",
        node_templates=[source_template, prepare_template],
        handle_templates=[
            _projection_handle(
                source_handle,
                resource_template_uuid=str(source_template["resource_template_uuid"]),
                action_name="material_source",
            ),
            *[
                _projection_handle(
                    handle,
                    resource_template_uuid=str(
                        prepare_template["resource_template_uuid"]
                    ),
                    action_name="prepare",
                )
                for handle in prepare_handles
            ],
        ],
    )
    catalog = AuthoringCatalogSnapshot.from_entities(
        nodes,
        handles,
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
    )
    engine = WorkflowAuthoringEngine(catalog=catalog)
    service = WorkflowService(store, compiler=engine)
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Material direct graph",
        tags=[],
        description=None,
        meta_data={},
    )
    applied_graph = service.get_graph(WORKFLOW_UUID)
    source = _source()
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/material_direct_graph.py",
        applied_graph=applied_graph,
    )
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    try:
        yield _DirectGraphContext(
            service=service,
            engine=engine,
            source=source,
            candidate=compiled.graph,
            applied_graph=applied_graph,
        )
    finally:
        service.close()


def _mutated_graph(candidate: dict[str, Any], case: str) -> dict[str, Any]:
    """生成只含一个非法物料来源选择器变更的候选图。

    参数说明：``candidate`` 是合法图，``case`` 是封闭反例名称。返回：深复制
    后的非法图；未知反例抛出 ``AssertionError``，防止测试静默失敏。
    """

    graph = deepcopy(candidate)
    source_node = next(
        node for node in graph["nodes"] if node["uuid"] == MATERIAL_SOURCE_NODE_UUID
    )
    selector = source_node["param"]
    if case == "invalid-mode":
        selector["mode"] = "later"
    elif case == "mode-list":
        selector["mode"] = ["existing"]
    elif case == "mode-object":
        selector["mode"] = {"value": "existing"}
    elif case == "missing-mount":
        selector.pop("mount")
    elif case == "extra-field":
        selector["quantity"] = 1
    elif case == "create-new-fixed":
        selector["mode"] = "create_new"
        selector["material_uuid"] = FIXED_MATERIAL_UUID
    elif case == "site-and-slot-range":
        selector["site"] = SITE_UUID
        selector["slot_range"] = [SLOT_UUID]
    elif case == "noncanonical-mount":
        selector["mount"]["uuid"] = NONCANONICAL_MOUNT_UUID
    elif case == "flow-role-list":
        selector["flow_role"] = ["primary_sample"]
    elif case == "flow-role-object":
        selector["flow_role"] = {"value": "primary_sample"}
    elif case == "custody-policy-list":
        selector["custody_policy"] = ["task_exclusive"]
    elif case == "custody-policy-unknown":
        selector["custody_policy"] = "until_first_action"
    elif case == "top-level-material":
        source_node["material_uuid"] = FIXED_MATERIAL_UUID
    else:
        raise AssertionError(f"未知物料来源反例: {case}")
    return graph


INVALID_GRAPH_CASES = (
    "invalid-mode",
    "mode-list",
    "mode-object",
    "missing-mount",
    "extra-field",
    "create-new-fixed",
    "site-and-slot-range",
    "noncanonical-mount",
    "flow-role-list",
    "flow-role-object",
    "custody-policy-list",
    "custody-policy-unknown",
    "top-level-material",
)


@pytest.mark.parametrize("case", INVALID_GRAPH_CASES)
def test_direct_save_rejects_invalid_selector_with_zero_revision_write(
    direct_graph_context: _DirectGraphContext,
    case: str,
) -> None:
    """非法选择器直接保存必须统一拒绝且保持图和修订零写入。

    参数说明：``direct_graph_context`` 是隔离权威，``case`` 选择单一反例。
    返回：无；断言错误码、完整图和修订均不改变。
    """

    graph = _mutated_graph(direct_graph_context.candidate, case)
    before = direct_graph_context.service.get_graph(WORKFLOW_UUID)

    with pytest.raises(WorkflowError) as caught:
        direct_graph_context.service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=graph["nodes"],
            edges=graph["edges"],
        )

    assert caught.value.code == "invalid_material_source"
    assert direct_graph_context.service.get_graph(WORKFLOW_UUID) == before
    assert before == direct_graph_context.applied_graph
    assert before["workflow"]["revision"] == 1


@pytest.mark.parametrize("public_seam", ("generate_python", "validate"))
@pytest.mark.parametrize("case", INVALID_GRAPH_CASES)
def test_invalid_direct_graph_has_one_stable_engine_diagnostic(
    direct_graph_context: _DirectGraphContext,
    public_seam: str,
    case: str,
) -> None:
    """生成与共同验证接缝应返回相同物料来源稳定诊断。

    参数说明：``public_seam`` 选择公共编译器操作，``case`` 选择图反例。
    返回：无；断言只有 ``invalid_material_source`` 且不泄漏图或源码。
    """

    graph = _mutated_graph(direct_graph_context.candidate, case)
    if public_seam == "generate_python":
        result = direct_graph_context.engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=graph,
            source_uri="package://lab/workflows/material_direct_graph.py",
        )
    else:
        result = direct_graph_context.engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=1,
            graph=graph,
            python_source=direct_graph_context.source,
            source_uri="package://lab/workflows/material_direct_graph.py",
        )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == ["invalid_material_source"]


@pytest.mark.parametrize(
    "source",
    (
        _source().replace("mode='existing'", "mode='later'"),
        _source().replace("        mount=resource_ref", "        other=resource_ref"),
        _source().replace(
            "        site=None,", "        site=None,\n        quantity=1,"
        ),
        _source().replace(MOUNT_MATERIAL_UUID, NONCANONICAL_MOUNT_UUID),
    ),
    ids=("invalid-mode", "missing-mount", "extra-field", "noncanonical-uuid"),
)
def test_compile_keeps_invalid_material_source_diagnostic(source: str) -> None:
    """源码编译接缝必须继续使用同一物料来源机器诊断。

    参数说明：``source`` 是含单一选择器反例的静态源码。返回：无；断言编译
    失败且不返回候选图或规范源码。
    """

    source_template, source_handle = _material_source_template()
    prepare_template, prepare_handles = _template(
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
            )
        ],
    )
    engine = WorkflowAuthoringEngine(
        catalog=AuthoringCatalogSnapshot.from_entities(
            [source_template, prepare_template],
            [source_handle, *prepare_handles],
            resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
        )
    )
    result = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://lab/workflows/material_direct_graph.py",
        applied_graph={
            "workflow": {
                "uuid": WORKFLOW_UUID,
                "name": "Material direct graph",
                "tags": [],
                "description": None,
                "meta_data": {},
                "revision": 1,
            },
            "nodes": [],
            "edges": [],
            "node_templates": [],
            "handle_templates": [],
        },
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == ["invalid_material_source"]
