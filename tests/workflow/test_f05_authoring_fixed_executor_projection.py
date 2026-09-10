"""F05.4-C0b 固定执行器（Fixed Executor）双投影合同测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.workflow.authoring_ast import parse_authoring_source
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_graph import build_candidate_graph
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.models import CandidateCompilation

from .test_authoring_engine import WORKFLOW_UUID, _applied_graph, _template

# ``DEVICE_MATERIAL_UUID`` 是可信工作流（Workflow）源码固定选择的实际设备
# 物料（Material）稳定身份。
DEVICE_MATERIAL_UUID = "51000000-0000-4000-8000-000000000001"
# ``ACTION_NODE_UUID`` 是固定执行器（Fixed Executor）执行设备动作的
# 工作流节点（WorkflowNode）稳定身份。
ACTION_NODE_UUID = "52000000-0000-4000-8000-000000000001"
# ``ACTION_TEMPLATE_UUID`` 只标识动作模板（Action Template）。
# 它绝不能冒充实际设备物料（Material）身份。
ACTION_TEMPLATE_UUID = "53000000-0000-4000-8000-000000000001"
# ``DEVICE_RESOURCE_TEMPLATE_UUID`` 是测试动作所属实际设备类型的资源模板身份。
DEVICE_RESOURCE_TEMPLATE_UUID = "31000000-0000-4000-8000-000000000001"


def _catalog(
    *, action_type: str = "UniLabJsonCommand",
) -> AuthoringCatalogSnapshot:
    """构造带完整动作合同（Action Contract）的 ``ILab`` 目录快照（Catalog Snapshot）。

    参数说明：无。返回：仅含一个设备动作模板（Action Template）、不含
    连接点（Handle）定义的不可变目录快照（Catalog Snapshot）。
    异常：夹具实体不满足目录约束时让构造异常直接暴露。
    """

    # ``action_template`` 是唯一设备动作模板（Action Template）。
    # 可信工作流（Workflow）编译会冻结该模板。
    # ``handles`` 是该无参数动作（Action）的空连接点（Handle）集合。
    action_template, handles = _template(
        ACTION_TEMPLATE_UUID,
        name="prepare",
        handles=[],
    )
    action_template["node_type"] = "ILab"
    action_template["type"] = action_type
    action_template["schema"] = {"type": "object", "properties": {}}
    action_template["meta_data"] = {
        "unilab": {
            "action_contract_schema": {
                "type": "object",
                "properties": {
                    "goal": {"type": "object", "properties": {}},
                },
                "required": ["goal"],
            }
        }
    }
    return AuthoringCatalogSnapshot.from_entities([action_template], handles)


def _source(device_identity: str | None) -> str:
    """构造只包含固定或动态 ``device()`` 声明的可信工作流（Workflow）源码。

    参数说明：``device_identity`` 为 ``None`` 时生成动态 ``device()``，否则把
    该字面量写入固定 ``device()`` 声明。返回：Python 源码可由
    可信工作流编译器（Authoring Compiler）解析。异常：无；非法身份由公共
    编译接缝给出结构化诊断。
    """

    # ``device_argument`` 是可信工作流（Workflow）源码中的静态设备选择表达式，
    # 不代表模板身份。
    device_argument = "" if device_identity is None else repr(device_identity)
    return f'''from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output


reactor: Reactor = device({device_argument})


@workflow(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Fixed executor projection",
)
def fixed_executor_projection():
    # unilab:node_uuid={ACTION_NODE_UUID}
    prepared = reactor.prepare()
    return workflow_output()
'''


def _build_graph(
    device_identity: str | None,
    *,
    action_type: str = "UniLabJsonCommand",
) -> dict[str, Any]:
    """经公共接缝生成尚未进入候选包（Candidate Bundle）校验的候选图（Candidate Graph）。

    参数说明：``device_identity`` 控制固定或动态执行器绑定（ExecutorBinding）。
    返回：``build_candidate_graph`` 生成的完整五集合候选图（Candidate Graph）。
    异常：可信工作流（Workflow）源码或 ``catalog`` 映射非法时保留公共解析或
    构建候选图（Candidate Graph）时的异常。
    """

    # ``program`` 是不执行源码得到的可信工作流（Workflow）静态中间表示。
    program = parse_authoring_source(
        python_source=_source(device_identity),
        expected_workflow_uuid=WORKFLOW_UUID,
    )
    # ``graph`` 是待公共校验的完整候选图（Candidate Graph）；``_changeset`` 是
    # 同次构建产生、但本接缝测试不消费的候选变更集（CandidateChangeset）。
    graph, _changeset = build_candidate_graph(
        program=program,
        catalog=_catalog(action_type=action_type),
        applied_graph=_applied_graph(),
    )
    return graph


def _compile(device_identity: str | None) -> CandidateCompilation:
    """经可信工作流编译器（Authoring Compiler）编译设备选择源码。

    参数说明：``device_identity`` 控制固定或动态执行器绑定（ExecutorBinding）。
    返回：公共编译接口产生的候选编译结果（CandidateCompilation）。异常：
    可信工作流编译器（Authoring Compiler）把领域失败收敛为诊断，不应向测试
    泄漏预期异常。
    """

    return WorkflowAuthoringEngine(catalog=_catalog()).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source(device_identity),
        source_uri="package://lab/workflows/fixed_executor_projection.py",
        applied_graph=_applied_graph(),
    )


def test_fixed_device_material_identity_projects_to_both_candidate_fields() -> None:
    """固定实际设备物料（Material）身份必须完成双投影。

    参数说明：无。返回：无。异常/断言：公共候选图（Candidate Graph）构建结果
    的 ``material_uuid`` 或执行器绑定（ExecutorBinding）元数据
    ``executor_binding.device_id`` 不等于同一个规范 UUID 时测试失败。
    """

    # ``graph`` 是固定执行器（Fixed Executor）的可信工作流（Workflow）源码
    # 产生的候选图（Candidate Graph）。
    graph = _build_graph(DEVICE_MATERIAL_UUID)
    # ``action_node`` 是可信工作流（Workflow）源码生成的唯一设备动作工作流节点
    # （WorkflowNode）。
    action_node = graph["nodes"][0]

    assert action_node["material_uuid"] == DEVICE_MATERIAL_UUID
    assert action_node["meta_data"]["unilab"]["executor_binding"] == {
        "mode": "fixed",
        "device_id": DEVICE_MATERIAL_UUID,
    }


def test_fixed_device_business_id_resolves_to_material_uuid() -> None:
    """部署设备业务 ID 必须解析为实际设备物料 UUID 并保留派发身份。

    参数：无。返回：无。断言：库存权威（Inventory Authority）把
    ``reactor-a`` 解析为实际设备物料（Material）后，节点顶层写规范 UUID，
    执行器绑定（ExecutorBinding）仍保留运行时设备 ID；解析出的资源模板必须
    与动作模板设备类型一致。本测试不创建工作流任务（WorkflowTask）或执行动作。
    """

    def resolve_device_identity(resource_id: str) -> dict[str, str] | None:
        """返回测试设备业务 ID 对应的最小库存身份摘要。

        参数：``resource_id`` 是作者源码声明的设备业务 ID。返回：命中
        ``reactor-a`` 时返回实际物料与资源模板 UUID，否则返回 ``None``。
        异常：无。
        """

        if resource_id != "reactor-a":
            return None
        return {
            "uuid": DEVICE_MATERIAL_UUID,
            "resource_template_uuid": DEVICE_RESOURCE_TEMPLATE_UUID,
        }

    # ``compilation`` 只读取注入的库存身份端口，不查询名称或设备模板猜测 UUID。
    compilation = WorkflowAuthoringEngine(
        catalog=_catalog(),
        resource_reference_resolver=resolve_device_identity,
    ).compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source("reactor-a"),
        source_uri="package://lab/workflows/fixed_executor_business_id.py",
        applied_graph=_applied_graph(),
    )

    assert compilation.valid and compilation.graph is not None, compilation.diagnostics
    action_node = compilation.graph["nodes"][0]
    assert action_node["material_uuid"] == DEVICE_MATERIAL_UUID
    assert action_node["meta_data"]["unilab"]["executor_binding"] == {
        "mode": "fixed",
        "device_id": "reactor-a",
    }


def test_trusted_compile_freezes_same_device_identity_into_execution_plan() -> None:
    """可信编译与执行计划（ExecutionPlan）必须保留同一设备物料（Material）身份。

    参数说明：无。返回：无。异常/断言：候选编译结果（CandidateCompilation）
    无效，或执行计划（ExecutionPlan）中的顶层 ``material_uuid`` 与固定
    ``device_id`` 分叉时测试失败。
    """

    # ``compilation`` 是已穿越公共候选图（Candidate Graph）校验的
    # 候选编译结果（CandidateCompilation）。
    compilation = _compile(DEVICE_MATERIAL_UUID)

    assert compilation.valid and compilation.graph is not None, compilation.diagnostics
    # ``execution_plan`` 是从已通过公共候选图（Candidate Graph）校验的冻结图
    # 派生的执行计划（ExecutionPlan）。
    # ``_jobs`` 是与执行计划（ExecutionPlan）同步生成、但本合同不检查的首次
    # 工作流节点作业（WorkflowNodeJob）集合。
    execution_plan, _jobs = ExecutionPlanBuilder().build(
        compilation.graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    # ``planned_action`` 是执行计划（ExecutionPlan）中冻结的动作（Action）。
    # 它携带同一实际设备物料（Material）身份。
    planned_action = execution_plan["nodes"][0]
    assert planned_action["material_uuid"] == DEVICE_MATERIAL_UUID
    assert planned_action["device_id"] == DEVICE_MATERIAL_UUID


def test_async_action_type_is_frozen_into_candidate_and_execution_plan() -> None:
    """异步目录动作必须穿过候选图并冻结进执行计划（ExecutionPlan）。"""

    graph = _build_graph(
        DEVICE_MATERIAL_UUID,
        action_type="UniLabJsonCommandAsync",
    )

    assert graph["nodes"][0]["action_type"] == "UniLabJsonCommandAsync"

    # 历史应用图可能尚未冻结动作类型；执行计划仍须只从同一冻结模板恢复，
    # 不能把异步动作静默降级为同步 JSON 动作。
    graph["nodes"][0]["action_type"] = None
    execution_plan, _jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="normal",
        target_node_uuid=None,
    )

    assert execution_plan["nodes"][0]["action_type"] == "UniLabJsonCommandAsync"


def test_fixed_device_projection_survives_authoring_round_trip() -> None:
    """固定执行器（Fixed Executor）双投影必须在重复编译后结果不变。

    参数说明：无。返回：无。异常/断言：候选图（Candidate Graph）与
    可信工作流（Workflow）源码往返失败，或重复候选图（Candidate Graph）改变
    实际设备物料（Material）身份与执行器绑定（ExecutorBinding）时测试失败。
    """

    # ``engine`` 持有同一不可变目录快照（Catalog Snapshot），是
    # 可信工作流编译器（Authoring Compiler）。
    engine = WorkflowAuthoringEngine(catalog=_catalog())
    # ``compilation`` 是首次固定设备物料（Material）双投影的
    # 候选编译结果（CandidateCompilation）。
    compilation = _compile(DEVICE_MATERIAL_UUID)
    assert compilation.valid and compilation.graph is not None, compilation.diagnostics
    # ``generated`` 是从候选图（Candidate Graph）确定性生成的规范工作流
    # （Workflow）源码结果。
    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compilation.graph,
        source_uri="package://lab/workflows/generated_fixed_executor.py",
    )
    assert generated.valid and generated.normalized_python_source is not None
    # ``repeated`` 是规范源码重新编译后的候选编译结果（CandidateCompilation），
    # 用于证明重复编译后结果不变。
    repeated = engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=generated.normalized_python_source,
        source_uri="package://lab/workflows/generated_fixed_executor.py",
        applied_graph=compilation.graph,
    )

    assert repeated.valid, repeated.diagnostics
    assert repeated.graph == compilation.graph


@pytest.mark.parametrize(
    ("field_name", "field_present"),
    [
        pytest.param("action_name", True, id="null-action-name"),
        pytest.param("action_name", False, id="omitted-action-name"),
        pytest.param("action_type", True, id="null-action-type"),
        pytest.param("action_type", False, id="omitted-action-type"),
        pytest.param("description", True, id="null-default-description"),
        pytest.param("description", False, id="omitted-default-description"),
    ],
)
def test_persisted_nullable_node_defaults_remain_a_canvas_fixed_point(
    field_name: str,
    field_present: bool,
) -> None:
    """Backend 可空读形状不得让画布生成源码后误报往返不一致。

    参数：``field_name`` 是由动作目录确定的可空节点字段；``field_present``
    区分显式 ``None`` 与 ``omitempty`` 省略。返回：无；通过画布实际调用的
    generate-python → validate 链证明目录默认值与旧读形状语义相同。
    """

    engine = WorkflowAuthoringEngine(catalog=_catalog())
    compilation = _compile(DEVICE_MATERIAL_UUID)
    assert compilation.valid and compilation.graph is not None, compilation.diagnostics
    persisted = deepcopy(compilation.graph)
    if field_present:
        persisted["nodes"][0][field_name] = None
    else:
        persisted["nodes"][0].pop(field_name, None)

    generated = engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=persisted,
        source_uri="package://lab/workflows/persisted_nullable_node.py",
    )
    assert generated.valid and generated.graph is not None, generated.diagnostics
    assert generated.normalized_python_source is not None

    validated = engine.validate(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=generated.graph,
        python_source=generated.normalized_python_source,
        source_uri="package://lab/workflows/persisted_nullable_node.py",
    )

    assert validated.valid, validated.diagnostics


def test_dynamic_device_does_not_fabricate_concrete_material_identity() -> None:
    """动态 ``device()`` 声明不得伪造物料（Material）身份并须关闭失败（Fail-closed）。

    参数说明：无。返回：无。异常/断言：
    候选图构建器（Candidate Graph Builder）写入具体 ``material_uuid`` 或
    ``executor_binding``，或可信工作流编译器（Authoring Compiler）放行当前
    不可执行的动态 ``ILab`` 节点时测试失败。
    """

    # ``graph`` 是动态 ``device()`` 声明产生的候选图（Candidate Graph），尚未
    # 进入候选包（Candidate Bundle）校验。
    graph = _build_graph(None)
    # ``action_node`` 是必须保持空实际设备物料（Material）身份的设备动作节点。
    action_node = graph["nodes"][0]
    # ``compilation`` 是动态声明的候选编译结果（CandidateCompilation）。
    # 公共图校验（Graph Validation）必须对其动态执行器绑定（ExecutorBinding）
    # 关闭失败（Fail-closed）。
    compilation = _compile(None)

    assert action_node["material_uuid"] is None
    assert "executor_binding" not in action_node["meta_data"]["unilab"]
    assert not compilation.valid
    assert compilation.graph is None
    assert [item["code"] for item in compilation.diagnostics] == ["candidate_invalid"]


@pytest.mark.parametrize(
    ("device_identity", "expected_code"),
    [
        pytest.param("", "invalid_device_selector", id="empty"),
        pytest.param("reactor-a", "invalid_executor_binding", id="unresolved-business-id"),
    ],
)
def test_invalid_fixed_device_identity_fails_closed(
    device_identity: str,
    expected_code: str,
) -> None:
    """空值或非 UUID 固定身份不得冒充实际设备物料（Material）身份。

    参数说明：``device_identity`` 是不可信固定身份字面量，``expected_code`` 是
    对应公共诊断码。返回：无。异常/断言：
    可信工作流编译器（Authoring Compiler）返回候选图（Candidate Graph）或
    诊断码漂移时测试失败。
    """

    # ``compilation`` 是不可信固定身份经可信工作流编译器（Authoring Compiler）
    # 公共边界得到的候选编译结果（CandidateCompilation）。
    compilation = _compile(device_identity)

    assert not compilation.valid
    assert compilation.graph is None
    assert [item["code"] for item in compilation.diagnostics] == [expected_code]
