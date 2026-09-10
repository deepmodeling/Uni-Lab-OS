"""F05.1 物料来源（MaterialSource）静态编译与往返合同。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

from .test_authoring_engine import (
    PREPARE_SAMPLE_TARGET,
    PREPARE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _applied_graph,
    _handle,
    _template,
)

MATERIAL_SOURCE_NODE_UUID = "20000000-0000-4000-8000-000000000010"
PREPARE_NODE_UUID = "20000000-0000-4000-8000-000000000011"
MATERIAL_SOURCE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000010"
MATERIAL_SOURCE_HANDLE_UUID = "40000000-0000-4000-8000-000000000010"
PLATE_TEMPLATE_UUID = "32000000-0000-4000-8000-000000000010"
MOUNT_MATERIAL_UUID = "50000000-0000-4000-8000-000000000010"
FIXED_MATERIAL_UUID = "50000000-0000-4000-8000-000000000011"
PLATE_SOURCE_SYMBOL = "lab.resources:plate_96"


def _material_source_template() -> tuple[dict[str, Any], dict[str, Any]]:
    """构造框架物料来源（MaterialSource）模板与唯一物料占位符。

    参数：无。返回：可进入不可变创作目录快照的节点模板与 source
    物料占位符（ResourceSlot）二元组。
    """

    template = {
        "uuid": MATERIAL_SOURCE_TEMPLATE_UUID,
        "resource_template_uuid": "31000000-0000-4000-8000-000000000001",
        "name": "material_source",
        "display_name": "Material Source",
        "class": "unilabos.workflow.authoring:material_source",
        "description": "声明工作流进入边界的物料来源。",
        "meta_data": {"framework": "material_source"},
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "schema": None,
        "type": "material_source",
        "node_type": "material_source",
        "icon": None,
        "header": None,
        "footer": None,
    }
    handle = _handle(
        MATERIAL_SOURCE_HANDLE_UUID,
        node_template_uuid=MATERIAL_SOURCE_TEMPLATE_UUID,
        key="material",
        io_type="source",
        value_type="ResourceSlot",
    )
    return template, handle


def _engine() -> WorkflowAuthoringEngine:
    """创建含物料来源与消费动作的纯创作编译器。

    参数：无。返回：冻结资源模板源码符号双向身份的工作流创作编译器
    （Authoring Compiler）。
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
    catalog = AuthoringCatalogSnapshot.from_entities(
        [source_template, prepare_template],
        [source_handle, *prepare_handles],
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
    )
    return WorkflowAuthoringEngine(catalog=catalog)


def _source(
    *,
    mode: str = "existing",
    material_uuid: str | None = None,
    flow_role: str = "PRIMARY_SAMPLE",
) -> str:
    """生成一个物料来源向动作线性传递的作者源码。

    参数说明：``mode``、``material_uuid`` 与 ``flow_role`` 分别控制选择器
    模式、固定物料身份和物料流角色（MaterialFlowRole）。返回：只含静态
    创作语法的 Python 文本。
    """

    material_literal = "None" if material_uuid is None else repr(material_uuid)
    return f'''from lab.devices import Reactor
from lab.resources import plate_96
from unilabos.workflow.authoring import (
    MaterialFlowRole,
    device,
    material_source,
    resource_ref,
    workflow,
    workflow_output,
)


reactor: Reactor = device()


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Material assay")
def material_assay():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    assay_plate = material_source(
        resource_template=plate_96,
        mode={mode!r},
        mount=resource_ref("{MOUNT_MATERIAL_UUID}"),
        material_uuid={material_literal},
        site=None,
        slot_range=None,
        flow_role=MaterialFlowRole.{flow_role},
    )
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=assay_plate)
    return workflow_output()
'''


def _compile(source: str):
    """通过公共接口编译物料来源作者源码。

    参数说明：``source`` 是待验证的静态 Python 文本。返回：候选编译结果
    （CandidateCompilation），测试只经公共接口观察诊断和图。
    """

    return _engine().compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="package://lab/workflows/material_assay.py",
        applied_graph=_applied_graph(),
    )


def test_material_source_compiles_to_backend_shaped_selector_and_edge() -> None:
    """物料来源应生成规范选择器并通过物料占位符连接消费动作。

    参数：无。返回：无；断言节点模板身份、完整选择器和确定性边端点。
    """

    compiled = _compile(_source())

    assert compiled.valid, compiled.diagnostics
    assert compiled.graph is not None
    nodes = {node["uuid"]: node for node in compiled.graph["nodes"]}
    source_node = nodes[MATERIAL_SOURCE_NODE_UUID]
    assert source_node["workflow_node_template_uuid"] == MATERIAL_SOURCE_TEMPLATE_UUID
    assert source_node["type"] == "material_source"
    assert source_node["material_uuid"] is None
    assert source_node["param"] == {
        "mode": "existing",
        "resource_template_uuid": PLATE_TEMPLATE_UUID,
        "mount": {"uuid": MOUNT_MATERIAL_UUID},
        "material_uuid": None,
        "site": None,
        "slot_range": None,
        "flow_role": "primary_sample",
    }
    assert len(compiled.graph["edges"]) == 1
    assert {
        key: compiled.graph["edges"][0][key]
        for key in (
            "source_node_uuid",
            "source_handle_uuid",
            "target_node_uuid",
            "target_handle_uuid",
        )
    } == {
        "source_node_uuid": MATERIAL_SOURCE_NODE_UUID,
        "source_handle_uuid": MATERIAL_SOURCE_HANDLE_UUID,
        "target_node_uuid": PREPARE_NODE_UUID,
        "target_handle_uuid": PREPARE_SAMPLE_TARGET,
    }


@pytest.mark.parametrize(
    ("mode", "material_uuid", "role_member", "role_value"),
    [
        ("existing", FIXED_MATERIAL_UUID, "ALIQUOT_SAMPLE", "aliquot_sample"),
        ("existing", None, "REAGENT", "reagent"),
        ("create_new", None, "CONSUMABLE", "consumable"),
    ],
)
def test_material_source_selector_matrix_reaches_python_graph_fixed_point(
    mode: str,
    material_uuid: str | None,
    role_member: str,
    role_value: str,
) -> None:
    """合法选择器矩阵应确定生成源码并重新编译为同一候选图。

    参数说明：四个参数分别描述模式、固定物料、枚举成员和 wire 值。
    返回：无；断言选择器、规范源码和 Python↔图语义固定点。
    """

    engine = _engine()
    source = _source(
        mode=mode,
        material_uuid=material_uuid,
        flow_role=role_member,
    )
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=source,
        source_uri="package://lab/workflows/material_assay.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    source_node = next(
        node for node in compiled.graph["nodes"] if node["type"] == "material_source"
    )
    assert source_node["param"]["flow_role"] == role_value
    assert source_node["param"]["material_uuid"] == material_uuid
    assert compiled.normalized_python_source is not None
    assert f"flow_role=MaterialFlowRole.{role_member}" in (
        compiled.normalized_python_source
    )
    assert "resource_template=plate_96" in compiled.normalized_python_source
    assert f'mount=resource_ref("{MOUNT_MATERIAL_UUID}")' in (
        compiled.normalized_python_source
    )
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=compiled.normalized_python_source,
        source_uri="package://lab/workflows/material_assay.py",
        applied_graph=compiled.graph,
    )
    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compiled.graph


@pytest.mark.parametrize(
    "case",
    [
        "mode",
        "create-new-fixed",
        "free-string-role",
        "missing-field",
        "extra-field",
    ],
)
def test_invalid_material_source_selector_returns_stable_diagnostic(
    case: str,
) -> None:
    """非法物料来源选择器必须关闭失败且只返回稳定机器诊断。

    参数说明：``case`` 选择把合法静态选择器改成哪一种单一非法情形。
    返回：无；断言不泄漏候选图或规范源码。
    """

    # ``mutations`` 是测试情形到确定性文本变换的显式映射，不执行作者源码。
    mutations = {
        "mode": ("mode='existing'", "mode='later'"),
        "create-new-fixed": (
            "mode='existing',\n        mount=",
            (
                "mode='create_new',\n"
                f"        material_uuid='{FIXED_MATERIAL_UUID}',\n"
                "        mount="
            ),
        ),
        "free-string-role": (
            "MaterialFlowRole.PRIMARY_SAMPLE",
            "'primary_sample'",
        ),
        "missing-field": ("        site=None,\n", ""),
        "extra-field": (
            "        site=None,",
            "        site=None,\n        quantity=1,",
        ),
    }
    old, new = mutations[case]
    source = _source().replace(old, new)
    if case == "create-new-fixed":
        # 上面的定点插入保留原 ``material_uuid=None``，此处移除该重复字段。
        source = source.replace("        material_uuid=None,\n", "")
    result = _compile(source)

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert [item["code"] for item in result.diagnostics] == [
        "invalid_material_source"
    ]


def test_unknown_resource_template_symbol_fails_closed_without_importing() -> None:
    """目录未冻结的资源模板源码符号不得被导入、猜测或动态查询。

    参数：无。返回：无；断言模板目录不匹配诊断和空候选结果。
    """

    source = _source().replace(
        "from lab.resources import plate_96",
        "from lab.resources import unknown_plate",
    ).replace("resource_template=plate_96", "resource_template=unknown_plate")
    result = _compile(source)

    assert not result.valid
    assert result.graph is None
    assert [item["code"] for item in result.diagnostics] == [
        "template_catalog_mismatch"
    ]
