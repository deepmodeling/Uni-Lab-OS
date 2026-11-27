"""F06 R3 物料占位符（ResourceSlot）边界约束传播 RED。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.composite import (
    CompositeAuthoring,
    project_published_workflow_contract,
)

from .test_c1_r2_static_expansion_contract import (
    CHILD_TEMPLATE_UUID,
    CHILD_WORKFLOW_UUID,
    HOST_RESOURCE_TEMPLATE_UUID,
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    MemorySnapshotProvider,
    _action_handles,
    _action_template,
    _applied_snapshot,
    _source_catalog,
)

MATERIAL_A_UUID = "a2000000-0000-4000-8000-000000000011"
MATERIAL_B_UUID = "a2000000-0000-4000-8000-000000000012"
MATERIAL_C_UUID = "a2000000-0000-4000-8000-000000000013"


def _slot(*allowlist: str) -> dict[str, object]:
    """构造省略或显式限制资源模板的物料占位符（ResourceSlot）Schema。

    参数：``allowlist`` 是允许的资源模板 UUID。返回：物料占位符 Schema。
    异常：无。
    """

    schema: dict[str, object] = {"$slot": "ResourceSlot"}
    if allowlist:
        schema["allowed_resource_template_uuids"] = list(allowlist)
    return schema


def _wrapper(*allowlist: str) -> dict[str, object]:
    """构造可空物料占位符（ResourceSlot）数组 Schema。

    参数：``allowlist`` 是数组元素允许的资源模板 UUID。返回：可空数组 Schema。
    异常：无。
    """

    return {
        "anyOf": [
            {"type": "array", "items": _slot(*allowlist)},
            {"type": "null"},
        ]
    }


def _resource_world(
    child_schema: dict[str, object],
) -> tuple[CompositeAuthoring, MemorySnapshotProvider]:
    """装配输入边界承载物料占位符（ResourceSlot）的发布工作流。

    参数：``child_schema`` 是子工作流边界 Schema。返回：组合创作接口与快照
    端口。异常：发布合同或目录夹具不一致时由构造器抛出。
    """

    snapshot = _applied_snapshot()
    unilab = snapshot["workflow"]["meta_data"]["unilab"]
    child_parameter = unilab["input_contract"]["parameters"][0]
    child_parameter["schema"] = deepcopy(child_schema)
    if "anyOf" in child_schema:
        child_parameter["required"] = False
        child_parameter["default"] = None
    unilab["output_contract"]["outputs"].append(
        {
            "name": "value",
            "schema": deepcopy(child_schema),
            "implicit": True,
        }
    )
    unilab["output_bindings"]["value"] = {
        "kind": "workflow_input",
        "parameter": "value",
    }
    action_handles = _action_handles()
    target = next(
        handle
        for handle in action_handles
        if handle["handle_key"] == "value" and handle["io_type"] == "target"
    )
    target["type"] = "ResourceSlot"
    target["required"] = "anyOf" not in child_schema
    target["meta_data"]["unilab"]["value_schema"] = deepcopy(child_schema)
    target["meta_data"]["unilab"]["allowed_resource_template_uuids"] = (
        deepcopy(child_schema).get("allowed_resource_template_uuids")
    )
    snapshot["handle_templates"] = action_handles
    source_catalog = _source_catalog()
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
    workflow_handles = [
        {
            **handle,
            "uuid": f"a5000000-0000-4000-8000-{index:012x}",
            "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
        }
        for index, handle in enumerate(projected.handles, start=1)
    ]
    catalog = AuthoringCatalogSnapshot.from_entities(
        [_action_template(), workflow_template],
        [*action_handles, *workflow_handles],
    )
    provider = MemorySnapshotProvider({CHILD_WORKFLOW_UUID: snapshot})
    return (
        CompositeAuthoring(
            snapshot_provider=provider,
            catalog=catalog,
            resolver=source_catalog,
        ),
        provider,
    )


@pytest.mark.parametrize(
    ("parent_schema", "child_schema", "expected"),
    [
        (_slot(), _slot(MATERIAL_A_UUID), _slot(MATERIAL_A_UUID)),
        (_slot(MATERIAL_A_UUID), _slot(), _slot(MATERIAL_A_UUID)),
        (
            _slot(MATERIAL_A_UUID, MATERIAL_B_UUID),
            _slot(MATERIAL_B_UUID, MATERIAL_C_UUID),
            _slot(MATERIAL_B_UUID),
        ),
        (
            _wrapper(MATERIAL_A_UUID, MATERIAL_B_UUID),
            _wrapper(MATERIAL_B_UUID),
            _wrapper(MATERIAL_B_UUID),
        ),
    ],
)
def test_resource_slot_constraints_intersect_without_losing_wrapper_shape(
    parent_schema: dict[str, object],
    child_schema: dict[str, object],
    expected: dict[str, object],
) -> None:
    """省略表示全集，显式集合求交，并保留数组与可空包装。

    参数：父/子 ``Schema`` 与 ``expected`` 由参数化夹具提供。返回：无；断言
    有效合同交集。异常：展开或断言失败时由 pytest 报告。
    """

    authoring, _provider = _resource_world(child_schema)
    expansion = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={
            "value": {"kind": "workflow_input", "parameter": "sample"}
        },
        parent_input_contract={
            "version": 1,
            "parameters": [
                {
                    "name": "sample",
                    "schema": parent_schema,
                    "required": "anyOf" not in parent_schema,
                    **({"default": None} if "anyOf" in parent_schema else {}),
                }
            ],
        },
    )

    assert expansion.diagnostics == ()
    assert expansion.effective_parent_input_contract["parameters"][0][
        "schema"
    ] == expected


def test_empty_resource_slot_intersection_fails_closed() -> None:
    """互斥物料模板集合只返回稳定诊断且不产生部分候选。

    参数：无。返回：无；断言空交集关闭失败且快照不变。异常：展开或断言失败
    时由 pytest 报告。
    """

    authoring, provider = _resource_world(_slot(MATERIAL_B_UUID))
    before = deepcopy(provider.snapshots)
    expansion = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={
            "value": {"kind": "workflow_input", "parameter": "sample"}
        },
        parent_input_contract={
            "version": 1,
            "parameters": [
                {
                    "name": "sample",
                    "schema": _slot(MATERIAL_A_UUID),
                    "required": True,
                }
            ],
        },
    )

    assert expansion.invocation_node is None
    assert [item["code"] for item in expansion.diagnostics] == [
        "composite_resource_constraint_empty"
    ]
    assert provider.snapshots == before
