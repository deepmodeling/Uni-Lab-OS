"""F05.2-C 工作流边界物料图（Material Graph）不变量的 RED 合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_identity import authoring_edge_uuid
from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.store import StoreAuthoringConflict

from .f05_material_graph_fixtures import (
    INCOMPATIBLE_TEMPLATE_UUID,
    PASSTHROUGH_NODE_UUID,
    PASSTHROUGH_SOURCE_UUID,
    PLATE_TEMPLATE_UUID,
    SECOND_CONSUMER_NODE_UUID,
    compile_material_source_graph,
    fan_out_candidate,
    material_graph_engine,
    opened_material_graph_store,
    passthrough_chain_source,
    single_chain_source,
)
from .test_authoring_engine import (
    PREPARE_SAMPLE_TARGET,
    WORKFLOW_UUID,
    _applied_graph,
)
from .test_f05_material_source_authoring import PREPARE_NODE_UUID

_SOURCE_URI = "package://lab/workflows/f05_material_graph_boundaries.py"


def _diagnostic_codes(result: CandidateCompilation) -> list[str]:
    """提取公共创作结果的稳定机器诊断码。

    参数说明：``result`` 是编译、源码生成或共同验证返回的候选结果。
    返回：保持公共接口顺序的诊断码列表；不读取内部异常或实现状态。
    """

    return [diagnostic["code"] for diagnostic in result.diagnostics]


def _workflow_input_source(*, fan_out: bool, ordered: bool = False) -> str:
    """构造工作流输入（Workflow Input）物料占位符作者源码。

    参数说明：``fan_out`` 为真时让同一 ``sample`` 输入绑定到两个动作（Action），
    ``ordered`` 为真时再用首个动作的 ``ready`` 输出建立严格先后关系，否则两个
    动作是可并发兄弟。返回：可由公共静态编译器解析的确定性 Python 文本；文本
    只声明工作流创作（Workflow Authoring）事实，不访问物料权威。异常：无。
    """

    # ``second_consumer`` 是非法第二物理消费者的完整静态声明；关闭时不留下节点。
    ordering_argument = ", ready=primary.ready" if ordered else ""
    second_consumer = (
        (
            f"    # unilab:node_uuid={SECOND_CONSUMER_NODE_UUID}\n"
            f"    duplicate = reactor.prepare(sample=sample{ordering_argument})\n"
        )
        if fan_out
        else ""
    )
    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device()


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Workflow input material boundary",
)
def workflow_input_material_boundary(*, sample: ResourceSlot):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    primary = reactor.prepare(sample=sample)
{second_consumer}    return workflow_output(sample=sample)
'''


def _compile_workflow_input(
    engine: WorkflowAuthoringEngine,
    *,
    fan_out: bool,
    ordered: bool = False,
) -> CandidateCompilation:
    """通过公共编译接缝解析工作流输入（Workflow Input）边界源码。

    参数说明：``engine`` 是冻结目录的工作流创作编译器，``fan_out`` 选择单个
    或两个物理消费者，``ordered`` 决定两个消费者间是否存在严格依赖。返回：
    公共候选编译结果；编译不执行作者源码或查询库存。异常：公共编译器把失败
    转换为候选诊断，不从本辅助函数泄漏内部异常。
    """

    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_workflow_input_source(fan_out=fan_out, ordered=ordered),
        source_uri=_SOURCE_URI,
        applied_graph=_applied_graph(),
    )


def _workflow_input_fan_out_candidate(
    engine: WorkflowAuthoringEngine,
) -> dict[str, Any]:
    """从合法单消费者候选构造工作流输入双绑定反例。

    参数说明：``engine`` 通过公共编译接缝产生合法基线。局部 ``duplicate_node``
    保留同一输入绑定，只获得不同节点 UUID。返回：不修改基线结果的非法完整图；
    若合法基线本身不能编译，测试准备立即以断言失败。
    """

    compiled = _compile_workflow_input(engine, fan_out=False)
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = deepcopy(compiled.graph)
    original_node = next(
        node for node in graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    duplicate_node = deepcopy(original_node)
    duplicate_node["uuid"] = SECOND_CONSUMER_NODE_UUID
    duplicate_node["meta_data"]["unilab"]["authoring_result_name"] = "duplicate"
    graph["nodes"].append(duplicate_node)
    graph["nodes"].sort(key=lambda node: node["uuid"])
    return graph


def _list_resource_slot_schema(
    allowlist: tuple[str, ...],
) -> dict[str, Any]:
    """构造数组物料占位符（list[ResourceSlot]）值 Schema。

    参数说明：``allowlist`` 是每个数组成员允许的资源模板（ResourceTemplate）
    UUID 闭集。返回：规范数组 Schema；空集合不在本轮测试输入中。
    """

    return {
        "type": "array",
        "items": {
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": list(allowlist),
        },
    }


def _replace_handle_with_list_schema(
    graph: dict[str, Any],
    *,
    handle_uuid: str,
    allowlist: tuple[str, ...],
) -> None:
    """把候选连接点（Handle）改为数组物料占位符 Schema。

    参数说明：``graph`` 是本测试独占可变候选，``handle_uuid`` 是目标连接点稳定
    身份，``allowlist`` 是数组成员资源模板闭集。返回：无；只原地修改候选目录
    投影，不修改编译器目录或持久权威；目标缺失时抛出 ``StopIteration``。
    """

    # ``handle`` 是候选五集合中的目录投影，不是运行时物料实例或库存事实。
    handle = next(
        item for item in graph["handle_templates"] if item["uuid"] == handle_uuid
    )
    unilab = handle["meta_data"]["unilab"]
    unilab["value_schema"] = _list_resource_slot_schema(allowlist)
    unilab["allowed_resource_template_uuids"] = list(allowlist)
    handle["type"] = "array"
    # F06 起，动作物料输出的精确类型还会冻结到节点级输出 Schema 覆盖中；测试
    # 改写候选目录时必须同步该投影，否则会先被组合工作流输出合同拒绝，无法抵达
    # 本测试要验证的物料流线性（MaterialFlowLinearity）边界。
    for node in graph["nodes"]:
        node_unilab = node.get("meta_data", {}).get("unilab", {})
        overrides = node_unilab.get("output_schema_overrides", {})
        if handle_uuid in overrides:
            overrides[handle_uuid] = _list_resource_slot_schema(allowlist)


def _duplicate_edge_consumer(
    graph: dict[str, Any],
    *,
    source_node_uuid: str,
    source_handle_uuid: str,
    disabled: bool = False,
) -> None:
    """为一个物料输出加入第二物理消费者。

    参数说明：``graph`` 是本测试独占候选；两个来源 UUID 标识待分叉输出；
    ``disabled`` 决定第二消费者是否禁用。返回：无；原地加入一个新节点和一条
    确定性边，保持第一个消费者不变；来源边缺失时抛出 ``StopIteration``。
    """

    original_node = next(
        node for node in graph["nodes"] if node["uuid"] == PREPARE_NODE_UUID
    )
    duplicate_node = deepcopy(original_node)
    duplicate_node["uuid"] = SECOND_CONSUMER_NODE_UUID
    duplicate_node["disabled"] = disabled
    duplicate_node["meta_data"]["unilab"]["authoring_result_name"] = "duplicate"
    graph["nodes"].append(duplicate_node)
    graph["nodes"].sort(key=lambda node: node["uuid"])

    original_edge = next(
        edge
        for edge in graph["edges"]
        if edge["source_node_uuid"] == source_node_uuid
        and edge["source_handle_uuid"] == source_handle_uuid
        and edge["target_node_uuid"] == PREPARE_NODE_UUID
    )
    duplicate_edge = deepcopy(original_edge)
    duplicate_edge.update(
        {
            "uuid": authoring_edge_uuid(
                workflow_uuid=WORKFLOW_UUID,
                source_node_uuid=source_node_uuid,
                source_handle_uuid=source_handle_uuid,
                target_node_uuid=SECOND_CONSUMER_NODE_UUID,
                target_handle_uuid=str(original_edge["target_handle_uuid"]),
            ),
            "target_node_uuid": SECOND_CONSUMER_NODE_UUID,
        }
    )
    graph["edges"].append(duplicate_edge)
    graph["edges"].sort(key=lambda edge: edge["uuid"])


def _list_boundary_candidate(
    *,
    consumer_allowlist: tuple[str, ...],
    fan_out: bool,
) -> tuple[WorkflowAuthoringEngine, dict[str, Any]]:
    """构造显式动作输出的数组物料边界候选。

    参数说明：``consumer_allowlist`` 是最终消费者接受的资源模板闭集，
    ``fan_out`` 决定是否复制最终消费者。返回：公共编译器与其合法单链候选的
    独占数组 Schema 变体；基线编译失败时测试准备以断言失败。
    """

    engine = material_graph_engine(
        include_passthrough=True,
        prepare_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_input_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_output_allowlist=(PLATE_TEMPLATE_UUID,),
        passthrough_implicit=False,
    )
    compiled = compile_material_source_graph(engine, passthrough_chain_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = deepcopy(compiled.graph)
    _replace_handle_with_list_schema(
        graph,
        handle_uuid=PASSTHROUGH_SOURCE_UUID,
        allowlist=(PLATE_TEMPLATE_UUID,),
    )
    passthrough_node = next(
        node for node in graph["nodes"] if node["uuid"] == PASSTHROUGH_NODE_UUID
    )
    passthrough_target_uuid = passthrough_node["meta_data"]["unilab"][
        "material_passthrough_handles"
    ][PASSTHROUGH_SOURCE_UUID]
    _replace_handle_with_list_schema(
        graph,
        handle_uuid=passthrough_target_uuid,
        allowlist=(PLATE_TEMPLATE_UUID,),
    )
    _replace_handle_with_list_schema(
        graph,
        handle_uuid=PREPARE_SAMPLE_TARGET,
        allowlist=consumer_allowlist,
    )
    if fan_out:
        _duplicate_edge_consumer(
            graph,
            source_node_uuid=PASSTHROUGH_NODE_UUID,
            source_handle_uuid=PASSTHROUGH_SOURCE_UUID,
        )
    return engine, graph


def test_compile_accepts_ordered_workflow_input_material_reuse() -> None:
    """编译必须接受同一工作流输入物料被两个顺序动作复用。

    参数：无。返回：无；断言作者源码顺序产生严格全序并生成候选图和规范源码。
    异常：公共编译接缝不得泄漏内部异常；无序兄弟候选仍由后续测试关闭失败。
    """

    result = _compile_workflow_input(material_graph_engine(), fan_out=True)

    assert result.valid and result.graph is not None, result.diagnostics
    assert result.normalized_python_source is not None
    assert _diagnostic_codes(result) == []


def test_ordered_workflow_input_material_reuse_is_a_fixed_point() -> None:
    """同一工作流输入物料被严格排序的动作复用时必须保持合法固定点。

    参数：无。返回：无；断言有序复用生成候选图，两个消费者间存在依赖边，且
    Python→图→Python→图保持相同语义。异常：公共编译和源码生成接缝不得泄漏
    内部异常；无序兄弟分叉继续由相邻测试关闭失败。
    """

    engine = material_graph_engine()
    compiled = _compile_workflow_input(engine, fan_out=True, ordered=True)

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    assert any(
        edge["source_node_uuid"] == PREPARE_NODE_UUID
        and edge["target_node_uuid"] == SECOND_CONSUMER_NODE_UUID
        for edge in compiled.graph["edges"]
    )
    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        source_uri=_SOURCE_URI,
    )
    assert generated.valid and generated.normalized_python_source is not None, (
        generated.diagnostics
    )
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=generated.normalized_python_source,
        source_uri=_SOURCE_URI,
        applied_graph=_applied_graph(),
    )
    assert repeated.valid and repeated.graph is not None, repeated.diagnostics
    assert repeated.graph == compiled.graph
    assert (
        generated.normalized_python_source.count("reactor.prepare(sample=sample)") == 2
    )


@pytest.mark.parametrize("public_seam", ("generate_python", "validate"))
def test_graph_public_seams_reject_workflow_input_material_fan_out(
    public_seam: Literal["generate_python", "validate"],
) -> None:
    """图公共接缝必须统一拒绝工作流输入物料双绑定。

    参数说明：``public_seam`` 选择确定性源码生成或图/源码共同验证。返回：无；
    两种入口均只返回 ``material_flow_fan_out``，不得泄漏候选或规范源码。
    """

    engine = material_graph_engine()
    # ``graph`` 是从合法单消费者候选复制出的双绑定反例，不依赖未来 RED 实现。
    graph = _workflow_input_fan_out_candidate(engine)
    if public_seam == "generate_python":
        result = engine.generate_python(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            source_uri=_SOURCE_URI,
        )
    else:
        result = engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=graph,
            python_source=_workflow_input_source(fan_out=True),
            source_uri=_SOURCE_URI,
        )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]


def test_direct_save_rejects_workflow_input_material_fan_out_without_writes(
    tmp_path: Path,
) -> None:
    """直接保存工作流输入物料双绑定必须保持图和修订零写入。

    参数说明：``tmp_path`` 提供隔离 SQLite 文件。返回：无；``before`` 是保存前
    工作流权威投影，拒绝后必须逐字段相同。异常：公共保存接缝必须抛出错误码为
    ``material_flow_fan_out`` 的 ``StoreAuthoringConflict``。
    """

    engine = material_graph_engine()
    graph = _workflow_input_fan_out_candidate(engine)
    with opened_material_graph_store(
        tmp_path / "workflow.db",
        prepare_allowlist=None,
        workflow_meta_data=graph["workflow"]["meta_data"],
    ) as context:
        before = context.service.get_graph(WORKFLOW_UUID)
        with pytest.raises(StoreAuthoringConflict) as caught:
            context.store.save_graph(
                WORKFLOW_UUID,
                revision=1,
                nodes=[
                    WorkflowNodeWrite.model_validate(node) for node in graph["nodes"]
                ],
                edges=[
                    WorkflowEdgeWrite.model_validate(edge) for edge in graph["edges"]
                ],
                protect_reserved_metadata=False,
                validate_workflow_io_contract=True,
            )

        assert caught.value.code == "material_flow_fan_out"
        assert context.service.get_graph(WORKFLOW_UUID) == before
        assert before == context.applied_graph
        assert before["workflow"]["revision"] == 1


def test_list_resource_slot_output_fan_out_is_rejected() -> None:
    """数组物料占位符输出仍必须满足物料流线性。

    参数：无。返回：无；公共源码生成接缝必须以 ``material_flow_fan_out`` 拒绝
    同一 list[ResourceSlot] 输出的两个消费者，不得把数组当成普通数据边。
    """

    engine, graph = _list_boundary_candidate(
        consumer_allowlist=(PLATE_TEMPLATE_UUID,),
        fan_out=True,
    )
    result = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri=_SOURCE_URI,
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]


def test_list_resource_slot_producer_must_satisfy_consumer_template() -> None:
    """数组物料生产保证必须满足消费者资源模板约束。

    参数：无。返回：无；公共源码生成接缝必须以
    ``material_template_mismatch`` 拒绝成员模板集合互斥的 list[ResourceSlot]，
    不得跳过嵌套 Schema。异常：公共接口不得泄漏内部 Schema 异常。
    """

    engine, graph = _list_boundary_candidate(
        consumer_allowlist=(INCOMPATIBLE_TEMPLATE_UUID,),
        fan_out=False,
    )
    result = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri=_SOURCE_URI,
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_template_mismatch"]


def test_disabled_second_material_consumer_still_counts_as_fan_out() -> None:
    """禁用节点不得把非法物料分叉隐藏在持久候选图中。

    参数：无。返回：无；公共源码生成接缝必须以 ``material_flow_fan_out`` 拒绝
    含禁用第二消费者的完整图，禁用状态不改变物料占位符身份或安全不变量。
    """

    engine = material_graph_engine()
    compiled = compile_material_source_graph(engine, single_chain_source())
    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    graph = fan_out_candidate(compiled.graph)
    second_consumer = next(
        node for node in graph["nodes"] if node["uuid"] == SECOND_CONSUMER_NODE_UUID
    )
    second_consumer["disabled"] = True

    result = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri=_SOURCE_URI,
    )

    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert _diagnostic_codes(result) == ["material_flow_fan_out"]


def test_material_source_candidate_apply_does_not_need_inventory_authority(
    tmp_path: Path,
) -> None:
    """合法物料来源候选应用不得依赖库存权威（Inventory Authority）。

    参数说明：``tmp_path`` 提供 SQLite 与作者包隔离目录。返回：无；以不装配
    任何库存、物料实例、库位（Site）或预留（Reservation）端口的服务完成草稿、
    候选和应用，并断言修订推进。异常：公共创作接缝不得泄漏内部异常。
    """

    package_root = tmp_path / "package"
    (package_root / "workflows").mkdir(parents=True)
    with opened_material_graph_store(
        tmp_path / "workflow.db",
        prepare_allowlist=None,
    ) as context:
        context.service.replace_active_editable_source_authorization(
            workflow_uuid=WORKFLOW_UUID,
            package_id="lab",
            package_root=package_root,
            relative_path="workflows/material_boundary.py",
        )
        draft = context.service.save_draft(
            WORKFLOW_UUID,
            python_source=single_chain_source(),
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
        candidate = draft["candidate"]
        assert candidate is not None

        applied = context.service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=candidate["candidate_hash"],
        )

        assert applied["apply_result"]["workflow_revision"] == 2
        assert context.service.get_graph(WORKFLOW_UUID)["workflow"]["revision"] == 2
