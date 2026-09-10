"""F05.4-C0d 物料来源（MaterialSource）规范源码往返合同。"""

from __future__ import annotations

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot

from .test_authoring_engine import WORKFLOW_UUID, _applied_graph, _handle, _template
from .test_f05_material_source_authoring import (
    MATERIAL_SOURCE_HANDLE_UUID,
    MATERIAL_SOURCE_NODE_UUID,
    MOUNT_MATERIAL_UUID,
    PLATE_SOURCE_SYMBOL,
    PLATE_TEMPLATE_UUID,
    PREPARE_NODE_UUID,
    PREPARE_SAMPLE_TARGET,
    PREPARE_TEMPLATE_UUID,
    _material_source_template,
)


def _goal_material_engine() -> WorkflowAuthoringEngine:
    """构造使用真实动作 goal 输入语义的工作流创作编译器。

    参数：无。返回：含一个物料来源（MaterialSource）和一个必填物料占位符
    （ResourceSlot）动作输入的公开工作流创作编译器（Authoring Compiler）。
    异常：目录实体不满足稳定身份或连接点（Handle）的稳定身份约束时，由
    ``AuthoringCatalogSnapshot`` 关闭失败。
    """

    # ``source_template`` 与 ``source_handle`` 是物料来源（MaterialSource）的
    # 稳定节点模板和唯一物料占位符（ResourceSlot）输出连接点（Handle）。
    source_template, source_handle = _material_source_template()
    # ``action_template`` 与 ``action_handles`` 模拟真实第 2 版动作合同（Action
    # Contract）投影；动作输入的数据来源是 ``goal``，不是旧测试夹具使用的
    # ``executor``。
    action_template, action_handles = _template(
        PREPARE_TEMPLATE_UUID,
        name="process_plate",
        handles=[
            _handle(
                PREPARE_SAMPLE_TARGET,
                node_template_uuid=PREPARE_TEMPLATE_UUID,
                key="plate",
                io_type="target",
                value_type="ResourceSlot",
                required=True,
                data_source="goal",
            )
        ],
    )
    # ``catalog`` 冻结本次编译使用的模板、连接点（Handle）和资源模板
    # （ResourceTemplate）源码身份，禁止往返时动态猜测。
    catalog = AuthoringCatalogSnapshot.from_entities(
        [source_template, action_template],
        [source_handle, *action_handles],
        resource_template_symbols={PLATE_SOURCE_SYMBOL: PLATE_TEMPLATE_UUID},
    )
    return WorkflowAuthoringEngine(catalog=catalog)


def _goal_material_source() -> str:
    """生成物料来源（MaterialSource）连接必填动作输入的可信工作流源码。

    参数：无。返回：通过稳定节点 UUID 和明确 ``plate=plate`` 绑定表达一条物料
    占位符（ResourceSlot）边的 Python 源码。异常：无；源码只由公开工作流创作
    编译器（Authoring Compiler）静态解析，不会执行。
    """

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


@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="Material round trip")
def material_round_trip():
    # unilab:node_uuid={MATERIAL_SOURCE_NODE_UUID}
    plate = material_source(
        resource_template=plate_96,
        mode="existing",
        mount=resource_ref("{MOUNT_MATERIAL_UUID}"),
        material_uuid=None,
        site=None,
        slot_range=None,
        flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
    )
    # unilab:node_uuid={PREPARE_NODE_UUID}
    processed_plate = reactor.process_plate(plate=plate)
    return workflow_output()
'''


def test_material_source_edge_survives_normalized_python_round_trip() -> None:
    """物料来源（MaterialSource）边必须在规范源码与再编译候选图中保持稳定。

    参数：无。返回：无。断言：公开编译生成的规范源码保留 ``plate=plate``；
    规范源码再编译后图语义、边 UUID 以及源/目标连接点（Handle）身份完全不变。
    若生成器遗漏、猜测或重绑必填物料占位符（ResourceSlot）参数，测试失败。
    """

    # ``engine`` 是一次测试内共享同一不可变目录快照的公开工作流创作编译器
    # （Authoring Compiler）。
    engine = _goal_material_engine()
    # ``compiled`` 是初始可信源码产生的候选图与规范源码。
    compiled = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_goal_material_source(),
        source_uri="package://lab/workflows/material_round_trip.py",
        applied_graph=_applied_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert compiled.normalized_python_source is not None
    assert "processed_plate = reactor.process_plate(plate=plate)" in (
        compiled.normalized_python_source
    )
    # ``original_edge`` 是由稳定节点和连接点（Handle）端点确定的物料流边事实。
    original_edge = compiled.graph["edges"][0]
    assert {
        key: original_edge[key]
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

    # ``repeated`` 是对规范源码再次编译的结果，用于证明图语义与边身份保持不变。
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=compiled.normalized_python_source,
        source_uri="package://lab/workflows/material_round_trip.py",
        applied_graph=compiled.graph,
    )

    assert repeated.valid and repeated.graph is not None, repeated.diagnostics
    assert repeated.graph == compiled.graph
    assert repeated.graph["edges"][0]["uuid"] == original_edge["uuid"]
