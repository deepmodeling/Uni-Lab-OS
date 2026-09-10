"""F06 R3 已发布工作流调用的创作固定点 RED。"""

from __future__ import annotations

import sys
from copy import deepcopy
from typing import Any

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite import (
    CompositeAuthoring,
    project_published_workflow_contract,
)
from unilabos.workflow.models import CandidateCompilation

from .test_c1_r2_static_expansion_contract import (
    ACTION_RESOURCE_TEMPLATE_UUID,
    ACTION_VALUE_TARGET_UUID,
    CHILD_TEMPLATE_UUID,
    CHILD_WORKFLOW_UUID,
    HOST_RESOURCE_TEMPLATE_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    MemorySnapshotProvider,
    _world_components,
)
from .test_qg01_group_parallel_authoring import _group_template

CHILD_MODULE = "c1_published_lab.workflows.child"
CHILD_SYMBOL = "prepare_sample"
PRECEDING_ACTION_UUID = "11111111-1111-4111-8111-111111111121"
FOLLOWING_ACTION_UUID = "11111111-1111-4111-8111-111111111122"
GROUP_UUID = "11111111-1111-4111-8111-111111111123"
RESOURCE_BUSINESS_ID = "fixture_process_warehouse"
RESOURCE_MATERIAL_UUID = "a8000000-0000-4000-8000-000000000099"
RESOURCE_TEMPLATE_UUID = "a2000000-0000-4000-8000-000000000099"
RESOURCE_WORKFLOW_HANDLE_UUIDS = tuple(
    f"a6000000-0000-4000-8000-{index:012d}" for index in range(1, 6)
)


def _applied_parent_graph() -> dict[str, Any]:
    """构造首次编译使用的空父工作流应用图。

    参数：无。返回：修订为一的空父图。异常：无。
    """

    return {
        "workflow": {
            "uuid": PARENT_WORKFLOW_UUID,
            "revision": 1,
            "name": "Persisted parent",
            "description": None,
            "tags": [],
            "meta_data": {},
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def _source() -> str:
    """返回一个通过绝对导入调用已发布子工作流的规范作者源码。

    参数：无。返回：包含固定调用身份的 Python 源码。异常：无。
    """

    return f'''from typing import TypedDict

from {CHILD_MODULE} import {CHILD_SYMBOL}
from unilabos.workflow.authoring import workflow


class ParentResult(TypedDict):
    result: float


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Composite parent",
)
def composite_parent(*, value: float) -> ParentResult:
    # unilab:node_uuid={INVOCATION_UUID}
    result = {CHILD_SYMBOL}(value=value)
    return {{"result": result.result}}
'''


def _source_with_surrounding_actions() -> str:
    """返回把已发布工作流调用放在两个普通动作之间的作者源码。

    参数：无。返回：包含两个结构性 ready 依赖的 Python 源码。异常：无。
    """

    return f'''from typing import TypedDict

from c1_published_lab.devices import Measure
from {CHILD_MODULE} import {CHILD_SYMBOL}
from unilabos.workflow.authoring import device, workflow


class ParentResult(TypedDict):
    result: float


measure: Measure = device("{ACTION_RESOURCE_TEMPLATE_UUID}")


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Composite parent",
)
def composite_parent(*, value: float) -> ParentResult:
    # unilab:node_uuid={PRECEDING_ACTION_UUID}
    prepared = measure.measure(value=value)
    # unilab:node_uuid={INVOCATION_UUID}
    child = {CHILD_SYMBOL}(value=value)
    # unilab:node_uuid={FOLLOWING_ACTION_UUID}
    finalized = measure.measure(value=value)
    return {{"result": finalized.result}}
'''


def _source_with_grouped_composite() -> str:
    """返回把已发布工作流调用放入展示分组的规范作者源码。

    参数：无。返回：包含稳定分组和复合调用身份的 Python 源码。异常：无。
    """

    return f'''from typing import TypedDict

from {CHILD_MODULE} import {CHILD_SYMBOL}
from unilabos.workflow.authoring import group, workflow


class ParentResult(TypedDict):
    result: float


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Grouped composite parent",
)
def composite_parent(*, value: float) -> ParentResult:
    # unilab:node_uuid={GROUP_UUID}
    with group(name="Grouped child"):
        # unilab:node_uuid={INVOCATION_UUID}
        result = {CHILD_SYMBOL}(value=value)
    return {{"result": result.result}}
'''


def _engine() -> WorkflowAuthoringEngine:
    """装配固定目录与只读组合创作端口的工作流创作编译器。

    参数：无。返回：绑定冻结目录和组合端口的编译器。异常：夹具目录无效时
    由构造器抛出。
    """

    authoring, _provider, catalog, _resolver = _world_components()
    return WorkflowAuthoringEngine(
        catalog=catalog,
        composite_authoring=authoring,
    )


def _grouped_engine() -> WorkflowAuthoringEngine:
    """装配同时支持展示分组和已发布子流程调用的创作编译器。

    参数：无。返回：加入唯一展示分组模板的隔离编译器。异常：夹具目录或组合
    创作端口无效时由构造器原样抛出。
    """

    authoring, _provider, catalog, _resolver = _world_components()
    node_templates = [action.detached_template() for action in catalog.actions]
    handle_templates = [
        handle for action in catalog.actions for handle in action.detached_handles()
    ]
    grouped_catalog = AuthoringCatalogSnapshot.from_entities(
        [*node_templates, _group_template()],
        handle_templates,
    )
    return WorkflowAuthoringEngine(
        catalog=grouped_catalog,
        composite_authoring=authoring,
    )


def _blank_description_engine() -> WorkflowAuthoringEngine:
    """装配发布模板默认空描述的工作流创作编译器。

    参数：无。返回：目录里发布模板描述为 ``""`` 的隔离编译器。异常：重建
    目录或组合端口失败时由对应构造器原样抛出。
    """

    _authoring, provider, catalog, resolver = _world_components()
    templates = []
    for action in catalog.actions:
        template = action.detached_template()
        if template["uuid"] == CHILD_TEMPLATE_UUID:
            template["description"] = ""
        templates.append(template)
    blank_catalog = AuthoringCatalogSnapshot.from_entities(
        templates,
        [
            handle
            for action in catalog.actions
            for handle in action.detached_handles()
        ],
    )
    authoring = CompositeAuthoring(
        snapshot_provider=provider,
        catalog=blank_catalog,
        resolver=resolver,
    )
    return WorkflowAuthoringEngine(
        catalog=blank_catalog,
        composite_authoring=authoring,
    )


def _resource_reference_engine() -> WorkflowAuthoringEngine:
    """装配接受物料占位符（ResourceSlot）的已发布子工作流目录。

    参数：无。返回：能把部署业务资源 ID 解析为实际物料身份的隔离编译器。
    异常：测试夹具合同不一致时由目录或组合投影构造器原样抛出。
    """

    _authoring, provider, _catalog, resolver = _world_components()
    snapshot = deepcopy(provider.snapshots[CHILD_WORKFLOW_UUID])
    input_descriptor = snapshot["workflow"]["meta_data"]["unilab"][
        "input_contract"
    ]["parameters"][0]
    input_descriptor["schema"] = {"$slot": "ResourceSlot"}
    workflow_metadata = snapshot["workflow"]["meta_data"]["unilab"]
    workflow_metadata["output_contract"]["outputs"].append(
        {
            "name": "value",
            "schema": {"$slot": "ResourceSlot"},
            "implicit": True,
        }
    )
    workflow_metadata["output_bindings"]["value"] = {
        "kind": "workflow_input",
        "parameter": "value",
    }
    for handle in snapshot["handle_templates"]:
        if handle["uuid"] != ACTION_VALUE_TARGET_UUID:
            continue
        handle["type"] = "ResourceSlot"
        handle["meta_data"] = {
            "unilab": {"value_schema": {"$slot": "ResourceSlot"}}
        }
    source = resolver.resolve(CHILD_MODULE, CHILD_SYMBOL)
    projected = project_published_workflow_contract(
        source=source,
        applied_snapshot=snapshot,
        host_node_resource_template={
            "uuid": HOST_RESOURCE_TEMPLATE_UUID,
            "name": "host_node",
            "display_name": "Host Node",
        },
    )
    assert projected is not None
    workflow_template = {**projected.template, "uuid": CHILD_TEMPLATE_UUID}
    workflow_handles = [
        {
            **handle,
            "uuid": handle_uuid,
            "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
        }
        for handle, handle_uuid in zip(
            projected.handles,
            RESOURCE_WORKFLOW_HANDLE_UUIDS,
            strict=True,
        )
    ]
    catalog = AuthoringCatalogSnapshot.from_entities(
        [*snapshot["node_templates"], workflow_template],
        [*snapshot["handle_templates"], *workflow_handles],
    )
    authoring = CompositeAuthoring(
        snapshot_provider=MemorySnapshotProvider({CHILD_WORKFLOW_UUID: snapshot}),
        catalog=catalog,
        resolver=resolver,
    )
    return WorkflowAuthoringEngine(
        catalog=catalog,
        composite_authoring=authoring,
        resource_reference_resolver=lambda resource_id: (
            {
                "uuid": RESOURCE_MATERIAL_UUID,
                "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            }
            if resource_id == RESOURCE_BUSINESS_ID
            else None
        ),
    )


def _source_with_composite_resource_reference() -> str:
    """返回把稳定部署资源引用传入已发布子工作流的父流程源码。"""

    return f'''from typing import TypedDict

from {CHILD_MODULE} import {CHILD_SYMBOL}
from unilabos.workflow.authoring import resource_ref, workflow


class ParentResult(TypedDict):
    result: float


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Composite resource parent",
)
def composite_parent() -> ParentResult:
    # unilab:node_uuid={INVOCATION_UUID}
    result = {CHILD_SYMBOL}(value=resource_ref("{RESOURCE_BUSINESS_ID}"))
    return {{"result": result.result}}
'''


def _source_with_composite_resource_reference_output() -> str:
    """返回显式输出组合调用隐式物料透传值的父流程。"""

    return f'''from typing import TypedDict

from {CHILD_MODULE} import {CHILD_SYMBOL}
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import resource_ref, workflow


class ParentResult(TypedDict):
    result: ResourceSlot


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Composite resource parent",
)
def composite_parent() -> ParentResult:
    # unilab:node_uuid={INVOCATION_UUID}
    result = {CHILD_SYMBOL}(value=resource_ref("{RESOURCE_BUSINESS_ID}"))
    return {{"result": result.value}}
'''


def _source_with_composite_workflow_material_input() -> str:
    """返回把父工作流物料输入透传给组合调用的源码。"""

    return f'''from typing import TypedDict

from {CHILD_MODULE} import {CHILD_SYMBOL}
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import workflow


class ParentResult(TypedDict):
    result: ResourceSlot


@workflow(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Composite material input parent",
)
def composite_parent(*, value: ResourceSlot) -> ParentResult:
    # unilab:node_uuid={INVOCATION_UUID}
    result = {CHILD_SYMBOL}(value=value)
    return {{"result": result.value}}
'''


def _compile(
    engine: WorkflowAuthoringEngine,
    source: str,
    graph: dict[str, Any],
) -> CandidateCompilation:
    """经公共编译接口生成父工作流候选结果。

    参数：``engine`` 是编译器，``source`` 是作者源码，``graph`` 是应用基线。
    返回：结构化候选编译结果。异常：公共编译接口未收敛的异常原样传播。
    """

    return engine.compile(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        workflow_revision=1,
        python_source=source,
        source_uri="package://c1_published_lab/workflows/parent.py",
        applied_graph=graph,
    )


def test_absolute_published_workflow_call_is_a_canonical_fixed_point() -> None:
    """绝对调用静态展开后，生成源码和再次编译保持完整语义固定点。

    参数：无。返回：无；断言图、源码和来源映射固定。异常：编译或断言失败时
    由 pytest 报告。
    """

    engine = _engine()
    assert CHILD_MODULE not in sys.modules

    compiled = _compile(engine, _source(), _applied_parent_graph())

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    normalized = compiled.normalized_python_source
    assert normalized is not None
    assert f"from {CHILD_MODULE} import {CHILD_SYMBOL}" in normalized
    assert f"result = {CHILD_SYMBOL}(value=value)" in normalized
    assert CHILD_MODULE not in sys.modules
    assert [entry["workflow_node_uuid"] for entry in compiled.source_map] == [
        INVOCATION_UUID
    ]
    invocation = next(
        node for node in compiled.graph["nodes"] if node["uuid"] == INVOCATION_UUID
    )
    internal = [
        node for node in compiled.graph["nodes"] if node["uuid"] != INVOCATION_UUID
    ]
    assert internal and all(
        node["parent_uuid"] == INVOCATION_UUID for node in internal
    )
    assert invocation["meta_data"]["unilab"]["composite"][
        "child_workflow_uuid"
    ] == CHILD_WORKFLOW_UUID

    repeated = _compile(engine, normalized, compiled.graph)

    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics
    assert repeated.normalized_python_source == normalized
    assert repeated.source_map == compiled.source_map
    assert CHILD_MODULE not in sys.modules


def test_grouped_published_workflow_call_preserves_parent_fixed_point() -> None:
    """分组内复合调用保留父关系，且规范源码可再次编译。

    参数：无。返回：无；断言调用节点属于展示分组并保持固定点。异常：编译或
    断言失败时由 pytest 报告。
    """

    engine = _grouped_engine()
    compiled = _compile(
        engine,
        _source_with_grouped_composite(),
        _applied_parent_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    invocation = next(
        node for node in compiled.graph["nodes"] if node["uuid"] == INVOCATION_UUID
    )
    assert invocation["parent_uuid"] == GROUP_UUID
    normalized = compiled.normalized_python_source
    assert normalized is not None

    repeated = _compile(engine, normalized, compiled.graph)

    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics
    assert repeated.normalized_python_source == normalized


def test_published_workflow_resource_reference_is_a_canonical_fixed_point() -> None:
    """复合调用接受稳定资源引用并保持实际身份与规范源码往返。"""

    engine = _resource_reference_engine()
    source = _source_with_composite_resource_reference()

    compiled = _compile(engine, source, _applied_parent_graph())

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    invocation = next(
        node for node in compiled.graph["nodes"] if node["uuid"] == INVOCATION_UUID
    )
    assert invocation["param"]["value"] == {"uuid": RESOURCE_MATERIAL_UUID}
    assert invocation["meta_data"]["unilab"]["resource_refs"]
    normalized = compiled.normalized_python_source
    assert normalized is not None
    assert f'resource_ref("{RESOURCE_BUSINESS_ID}")' in normalized

    repeated = _compile(engine, normalized, compiled.graph)

    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics


def test_composite_implicit_material_output_keeps_provider_template_type() -> None:
    """组合调用的隐式物料输出保留实际资源模板类型。"""

    compiled = _compile(
        _resource_reference_engine(),
        _source_with_composite_resource_reference_output(),
        _applied_parent_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    output = compiled.graph["workflow"]["meta_data"]["unilab"][
        "output_contract"
    ]["outputs"][0]
    assert output["name"] == "result"
    assert output["schema"] == {
        "$slot": "ResourceSlot",
        "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_UUID],
    }


def test_composite_internal_binding_does_not_duplicate_parent_material_consumer(
) -> None:
    """组合内部执行绑定不与父调用边界重复计为物理消费。"""

    compiled = _compile(
        _resource_reference_engine(),
        _source_with_composite_workflow_material_input(),
        _applied_parent_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics


def test_breaking_child_pin_fails_closed_at_compile_seam() -> None:
    """已应用调用节点的合同摘要被篡改时不得静默重写候选图。

    参数：无。返回：无；断言篡改 pin 只产生稳定诊断。异常：编译或断言失败时
    由 pytest 报告。
    """

    engine = _engine()
    compiled = _compile(engine, _source(), _applied_parent_graph())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    stale = deepcopy(compiled.graph)
    invocation = next(
        node for node in stale["nodes"] if node["uuid"] == INVOCATION_UUID
    )
    invocation["meta_data"]["unilab"]["composite"]["contract_digest"] = (
        "sha256:" + "f" * 64
    )

    rejected = _compile(engine, _source(), stale)

    assert not rejected.valid
    assert rejected.graph is None
    assert [item["code"] for item in rejected.diagnostics] == [
        "composite_contract_stale"
    ]


def test_composite_between_actions_keeps_structural_ready_out_of_arguments() -> None:
    """组合工作流调用夹在普通动作间时仍保持可回编译固定点。

    参数：无。返回：无；断言结构性 ready 连接点（Handle）只形成边，不进入已
    发布工作流业务实参。异常：编译或断言失败时由 pytest 报告。
    """

    engine = _engine()
    compiled = _compile(
        engine,
        _source_with_surrounding_actions(),
        _applied_parent_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    normalized = compiled.normalized_python_source
    assert normalized is not None
    assert "ready=" not in normalized
    repeated = _compile(engine, normalized, compiled.graph)
    assert repeated.valid and repeated.graph == compiled.graph, repeated.diagnostics


def test_persisted_blank_workflow_description_remains_renderable() -> None:
    """持久化空描述的形状差异不得阻断已发布工作流调用回到源码。

    参数：无。返回：无；断言数据库把空字符串恢复为 ``None`` 后，图到 Python
    的公共生成接口仍接受模板默认空描述。异常：生成或断言失败时由 pytest 报告。
    """

    engine = _blank_description_engine()
    compiled = _compile(engine, _source(), _applied_parent_graph())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    persisted = deepcopy(compiled.graph)
    persisted["workflow"].pop("description", None)
    for node in persisted["nodes"]:
        node.pop("status", None)
    invocation = next(
        node for node in persisted["nodes"] if node["uuid"] == INVOCATION_UUID
    )
    invocation["description"] = None

    generated = engine.generate_python(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        workflow_revision=1,
        graph=persisted,
        source_uri="package://c1_published_lab/workflows/persisted_parent.py",
    )

    assert generated.valid, generated.diagnostics
    assert generated.normalized_python_source is not None
    validated = engine.validate(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        workflow_revision=1,
        graph=persisted,
        python_source=generated.normalized_python_source,
        source_uri="package://c1_published_lab/workflows/persisted_parent.py",
    )
    assert validated.valid, validated.diagnostics


def test_canvas_round_trip_accepts_catalog_generation_and_omitted_ready_nulls() -> (
    None
):
    """画布切换不得把目录代际和 Backend 省略空值误判为作者语义变化。

    参数：无。返回：无；先构造已应用组合图，再模拟目录其他来源变化造成的
    package 摘要换代以及 Backend ``omitempty`` 的 ready 连接点读形状，最后
    通过画布实际使用的 generate-python → validate 链证明仍是同一作者语义。
    """

    engine = _engine()
    compiled = _compile(engine, _source(), _applied_parent_graph())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    persisted = deepcopy(compiled.graph)
    child_template = next(
        template
        for template in persisted["node_templates"]
        if template["uuid"] == CHILD_TEMPLATE_UUID
    )
    child_template["meta_data"]["unilab"]["workflow_source"][
        "package_catalog_digest"
    ] = "sha256:" + "e" * 64
    for handle in persisted["handle_templates"]:
        if (
            handle["workflow_node_template_uuid"] == CHILD_TEMPLATE_UUID
            and handle["handle_key"] == "ready"
        ):
            handle.pop("description", None)
            handle.pop("data_source", None)
            handle.pop("data_key", None)

    generated = engine.generate_python(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        workflow_revision=1,
        graph=persisted,
        source_uri="package://c1_published_lab/workflows/persisted_parent.py",
    )
    assert generated.valid and generated.graph is not None, generated.diagnostics
    assert generated.normalized_python_source is not None

    validated = engine.validate(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        workflow_revision=1,
        graph=generated.graph,
        python_source=generated.normalized_python_source,
        source_uri="package://c1_published_lab/workflows/persisted_parent.py",
    )

    assert validated.valid, validated.diagnostics
