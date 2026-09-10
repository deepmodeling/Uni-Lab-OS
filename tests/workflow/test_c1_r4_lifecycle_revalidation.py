"""F06 R4 已发布工作流（PublishedWorkflow）生命周期重验证 RED。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite import (
    CompositeAuthoring,
    project_published_workflow_contract,
)
from unilabos.workflow.composite_compatibility import (
    classify_published_workflow_compatibility_projections,
)

from .test_c1_r2_static_expansion_contract import (
    ACTION_VALUE_TARGET_UUID,
    APPLIED_SOURCE_HASH,
    CHILD_READY_SOURCE_UUID,
    CHILD_READY_TARGET_UUID,
    CHILD_TEMPLATE_UUID,
    CHILD_VALUE_SOURCE_UUID,
    CHILD_VALUE_TARGET_UUID,
    CHILD_WORKFLOW_UUID,
    HOST_RESOURCE_TEMPLATE_UUID,
    MemorySnapshotProvider,
    _world_components,
)
from .test_c1_r3_authoring_fixed_point import (
    _applied_parent_graph,
    _compile,
    _engine,
    _source,
)

NEW_INPUT_HANDLE_UUID = "a5000000-0000-4000-8000-000000000009"
ADDITIVE_CHILD_NODE_UUID = "22222222-2222-4222-8222-222222222223"
EVOLVED_SOURCE_HASH = "sha256:" + "9" * 64


def _projection(
    *,
    digest: str,
    extra_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造兼容性分类器使用的最小冻结投影。

    参数：``digest`` 是合同摘要；``extra_input`` 是可选追加输入。返回：独立投影。
    异常：无；输入按原值复制到测试投影。
    """

    inputs = [
        {
            "name": "value",
            "schema": {"type": "number"},
            "required": True,
            "has_default": False,
            "handle_uuid": CHILD_VALUE_TARGET_UUID,
        }
    ]
    if extra_input is not None:
        inputs.append(deepcopy(extra_input))
    return {
        "template_uuid": CHILD_TEMPLATE_UUID,
        "workflow_uuid": CHILD_WORKFLOW_UUID,
        "mode": False,
        "digest": digest,
        "inputs": inputs,
        "outputs": [
            {
                "name": "result",
                "schema": {"type": "number"},
                "implicit": False,
                "handle_uuid": CHILD_VALUE_SOURCE_UUID,
            }
        ],
    }


def test_compatibility_classifier_distinguishes_exact_additive_and_breaking() -> None:
    """兼容性分类必须只接受带默认值的末尾可选输入。

    参数：无。返回：无；断言精确、可加与破坏性分类。异常：分类或断言失败时
    由 pytest 报告。
    """

    previous = _projection(digest="sha256:" + "1" * 64)
    assert (
        classify_published_workflow_compatibility_projections(previous, previous)
        == "exact"
    )
    additive = _projection(
        digest="sha256:" + "2" * 64,
        extra_input={
            "name": "threshold",
            "schema": {"type": "number"},
            "required": False,
            "has_default": True,
            "default": 2.5,
            "handle_uuid": NEW_INPUT_HANDLE_UUID,
        },
    )
    assert (
        classify_published_workflow_compatibility_projections(previous, additive)
        == "additive"
    )
    required = deepcopy(additive)
    required["inputs"][-1]["required"] = True
    assert (
        classify_published_workflow_compatibility_projections(previous, required)
        == "breaking"
    )
    reordered = deepcopy(additive)
    reordered["inputs"].reverse()
    assert (
        classify_published_workflow_compatibility_projections(previous, reordered)
        == "breaking"
    )


def _evolved_engine(kind: str) -> WorkflowAuthoringEngine:
    """构造实现、可加合同或组合模式演进后的同身份目录。

    参数：``kind`` 为 ``exact``、``additive`` 或 ``mode``。返回：绑定新目录和
    同修订快照的编译器。异常：未知分类由断言拒绝。
    """

    _old_authoring, old_provider, old_catalog, source_catalog = _world_components()
    snapshot = deepcopy(old_provider.snapshots[CHILD_WORKFLOW_UUID])
    snapshot["workflow"]["revision"] = 8
    snapshot["applied_source"]["workflow_revision"] = 8
    snapshot["applied_source"]["source_hash"] = EVOLVED_SOURCE_HASH
    if kind == "additive":
        snapshot["workflow"]["meta_data"]["unilab"]["input_contract"][
            "parameters"
        ].append(
            {
                "name": "threshold",
                "schema": {"type": "number"},
                "required": False,
                "default": 2.5,
            }
        )
        added_node = deepcopy(snapshot["nodes"][0])
        added_node["uuid"] = ADDITIVE_CHILD_NODE_UUID
        added_node["name"] = "measure_threshold"
        added_node["meta_data"]["unilab"]["input_bindings"] = {
            ACTION_VALUE_TARGET_UUID: {"parameter": "threshold"}
        }
        snapshot["nodes"].append(added_node)
    elif kind == "mode":
        snapshot["workflow"]["meta_data"]["unilab"][
            "composition_allow_transparent"
        ] = True
    else:
        assert kind == "exact"

    source = source_catalog.resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )
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
    stable_handle_uuids = {
        ("value", "target"): CHILD_VALUE_TARGET_UUID,
        ("result", "source"): CHILD_VALUE_SOURCE_UUID,
        ("ready", "target"): CHILD_READY_TARGET_UUID,
        ("ready", "source"): CHILD_READY_SOURCE_UUID,
        ("threshold", "target"): NEW_INPUT_HANDLE_UUID,
    }
    workflow_handles = [
        {
            **handle,
            "uuid": stable_handle_uuids[(handle["handle_key"], handle["io_type"])],
            "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
        }
        for handle in projected.handles
    ]
    action = next(
        item
        for item in old_catalog.actions
        if item.template["uuid"] != CHILD_TEMPLATE_UUID
    )
    catalog = AuthoringCatalogSnapshot.from_entities(
        [action.detached_template(), workflow_template],
        [*action.detached_handles(), *workflow_handles],
    )
    provider = MemorySnapshotProvider({CHILD_WORKFLOW_UUID: snapshot})
    return WorkflowAuthoringEngine(
        catalog=catalog,
        composite_authoring=CompositeAuthoring(
            snapshot_provider=provider,
            catalog=catalog,
            resolver=source_catalog,
        ),
    )


@pytest.mark.parametrize("kind", ["exact", "additive"])
def test_compatible_child_evolution_recompiles_to_current_fixed_point(kind: str) -> None:
    """实现替换和末尾可选输入应升级父候选并保持新代际固定点。

    参数：``kind`` 选择精确或可加演进。返回：无；断言父候选升级并固定。
    异常：编译或断言失败时由 pytest 报告。
    """

    original = _compile(_engine(), _source(), _applied_parent_graph())
    assert original.valid and original.graph is not None, original.diagnostics
    evolved = _compile(_evolved_engine(kind), _source(), original.graph)

    assert evolved.valid and evolved.graph is not None, evolved.diagnostics
    invocation = next(
        node
        for node in evolved.graph["nodes"]
        if node["workflow_node_template_uuid"] == CHILD_TEMPLATE_UUID
    )
    composite = invocation["meta_data"]["unilab"]["composite"]
    assert composite["child_workflow_revision"] == 8
    assert composite["child_applied_source_hash"] == EVOLVED_SOURCE_HASH
    normalized = evolved.normalized_python_source
    assert normalized is not None
    repeated = _compile(_evolved_engine(kind), normalized, evolved.graph)
    assert repeated.valid and repeated.graph == evolved.graph, repeated.diagnostics


def test_breaking_mode_change_and_tampered_previous_projection_fail_closed() -> None:
    """组合模式变化或旧连接点投影篡改必须在候选写入前拒绝。

    参数：无。返回：无；断言两种破坏性变化均关闭失败。异常：编译或断言失败
    时由 pytest 报告。
    """

    original = _compile(_engine(), _source(), _applied_parent_graph())
    assert original.valid and original.graph is not None, original.diagnostics
    mode_change = _compile(_evolved_engine("mode"), _source(), original.graph)
    assert not mode_change.valid
    assert [item["code"] for item in mode_change.diagnostics] == [
        "composite_contract_stale"
    ]

    tampered = deepcopy(original.graph)
    handle = next(
        item
        for item in tampered["handle_templates"]
        if item["uuid"] == CHILD_VALUE_TARGET_UUID
    )
    handle["required"] = False
    rejected = _compile(_evolved_engine("exact"), _source(), tampered)
    assert not rejected.valid
    assert [item["code"] for item in rejected.diagnostics] == [
        "composite_contract_stale"
    ]
    assert APPLIED_SOURCE_HASH != EVOLVED_SOURCE_HASH
