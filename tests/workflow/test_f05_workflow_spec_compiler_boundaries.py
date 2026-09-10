"""F05.3-A 工作流规格编译器（WorkflowSpecCompiler）的真实计划边界。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.workflow.service import WorkflowService
from unilabos.workflow.workflow_spec_compiler import (
    WorkflowSpecCompilationError,
    WorkflowSpecCompiler,
)

WORKFLOW_UUID = "11000000-0000-4000-8000-000000000001"
TASK_UUID = "12000000-0000-4000-8000-000000000001"
NODE_A_UUID = "13000000-0000-4000-8000-000000000001"
NODE_B_UUID = "13000000-0000-4000-8000-000000000002"
NODE_C_UUID = "13000000-0000-4000-8000-000000000003"
JOB_A_UUID = "14000000-0000-4000-8000-000000000001"
JOB_B_UUID = "14000000-0000-4000-8000-000000000002"
JOB_C_UUID = "14000000-0000-4000-8000-000000000003"
TEMPLATE_A_UUID = "15000000-0000-4000-8000-000000000001"
TEMPLATE_B_UUID = "15000000-0000-4000-8000-000000000002"
TEMPLATE_C_UUID = "15000000-0000-4000-8000-000000000003"
TARGET_HANDLE_UUID = "16000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "16000000-0000-4000-8000-000000000002"
MATERIAL_UUID = "17000000-0000-4000-8000-000000000001"


def _node(
    node_uuid: str,
    template_uuid: str,
    *,
    disabled: bool = False,
    node_type: str = "ILab",
    action_name: str | None = "distribute",
) -> dict[str, Any]:
    """构造后端（Backend）形状的应用工作流节点（WorkflowNode）。

    参数：两个 UUID 是节点/模板稳定身份；``disabled``、``node_type`` 与
    ``action_name`` 冻结执行边界。返回：与当前 ``authoring_graph`` 一致、动作
    类型为空且执行器绑定位于 ``meta_data.unilab`` 的节点。异常：无。
    """

    # ``executor_binding`` 是创作编译器冻结的固定设备选择，不是实时分配结果。
    executor_binding = {"mode": "fixed", "device_id": "reactor-a"}
    return {
        "uuid": node_uuid,
        "workflow_node_template_uuid": template_uuid,
        "parent_uuid": None,
        "material_uuid": None,
        "name": f"节点-{node_uuid[-1]}",
        "type": node_type,
        "icon": None,
        "pose": {},
        "param": {"plate": {"uuid": MATERIAL_UUID}},
        "footer": None,
        "action_name": action_name,
        "action_type": None,
        "execution_policy": {},
        "disabled": disabled,
        "minimized": False,
        "script": None,
        "description": None,
        "meta_data": {"unilab": {"executor_binding": executor_binding}},
    }


def _template(
    template_uuid: str,
    *,
    node_type: str = "ILab",
) -> dict[str, Any]:
    """构造注册表投影形状的节点模板。

    参数：``template_uuid`` 是稳定模板身份，``node_type`` 决定执行器种类。
    返回：公开 ``schema`` 仅含 Goal 子模式、保留元数据冻结完整动作合同
    （Action Contract）的真实模板。异常：无。
    """

    # ``action_contract_schema`` 是创建任务时必须冻结的完整动作合同。
    action_contract_schema = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "plate": {
                        "type": "object",
                        "x-unilabos-material-lock": True,
                        "properties": {"uuid": {"type": "string", "format": "uuid"}},
                        "required": ["uuid"],
                    }
                },
                "required": ["plate"],
            }
        },
        "required": ["goal"],
    }
    return {
        "uuid": template_uuid,
        "node_type": node_type,
        "type": node_type,
        "name": "distribute",
        "schema": deepcopy(action_contract_schema["properties"]["goal"]),
        "meta_data": {
            "unilab": {
                "contract_kind": "typed",
                "action_contract_schema": action_contract_schema,
            }
        },
    }


def _handle(
    handle_uuid: str,
    template_uuid: str,
    *,
    io_type: str,
    key: str,
) -> dict[str, Any]:
    """构造真实工作流连接点（Handle）模板。

    参数：两个 UUID 是连接点/节点模板身份；``io_type`` 与 ``key`` 冻结方向和
    参数业务键。返回：传递物料占位符（ResourceSlot）的模板。异常：无。
    """

    return {
        "uuid": handle_uuid,
        "workflow_node_template_uuid": template_uuid,
        "handle_key": key,
        "data_key": key,
        "data_source": "executor",
        "io_type": io_type,
        "type": "ResourceSlot",
        "required": io_type == "target",
    }


def _edge(
    edge_uuid: str,
    source_node_uuid: str,
    target_node_uuid: str,
    *,
    source_handle_uuid: str = SOURCE_HANDLE_UUID,
    target_handle_uuid: str = TARGET_HANDLE_UUID,
) -> dict[str, Any]:
    """构造后端（Backend）形状的工作流边（WorkflowEdge）。

    参数：前三个 UUID 是边和节点端点身份；两个连接点 UUID 冻结数据端点。
    返回：当前 ``_build_execution_plan`` 接受的边。异常：无。
    """

    return {
        "uuid": edge_uuid,
        "source_node_uuid": source_node_uuid,
        "target_node_uuid": target_node_uuid,
        "source_handle_uuid": source_handle_uuid,
        "target_handle_uuid": target_handle_uuid,
    }


def _single_action_graph() -> dict[str, Any]:
    """构造一个固定执行器动作的真实应用工作流图。

    参数：无。返回：包含节点、模板和连接点模板的完整图；节点没有伪造顶层
    ``device_id``，动作类型保持当前创作输出的 ``None``。异常：无。
    """

    return {
        "nodes": [_node(NODE_A_UUID, TEMPLATE_A_UUID)],
        "edges": [],
        "node_templates": [_template(TEMPLATE_A_UUID)],
        "handle_templates": [
            _handle(
                TARGET_HANDLE_UUID,
                TEMPLATE_A_UUID,
                io_type="target",
                key="plate",
            ),
            _handle(
                SOURCE_HANDLE_UUID,
                TEMPLATE_A_UUID,
                io_type="source",
                key="plate",
            ),
        ],
    }


def _build_plan(
    graph: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """通过当前产品实现构造执行计划（ExecutionPlan）与作业。

    参数：``graph`` 是真实应用图。返回：``WorkflowService._build_execution_plan``
    的原始计划与作业列表。异常：图不合法时保留 ``StoreConflict``，使测试暴露
    产品计划合同而非测试自造结构。
    """

    service = object.__new__(WorkflowService)
    return service._build_execution_plan(
        graph,
        run_mode="normal",
        target_node_uuid=None,
    )


def _task_from_plan(
    execution_plan: dict[str, Any],
    *,
    workflow_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把真实计划放进持久工作流任务（WorkflowTask）读取形状。

    参数：``execution_plan`` 是唯一运行静态输入；``workflow_snapshot`` 只用于
    完整性/审计验证，省略时为空图。返回：编译器公共接口接受的任务对象。
    异常：无；测试故意允许审计快照缺少运行字段以禁止运行时回退。
    """

    # ``frozen_plan`` 补齐本轮正式版本和运行连接点集合；各测试仍只改变其关注的
    # 计划节点/边，不再手工重复公共 envelope。
    frozen_plan = deepcopy(execution_plan)
    frozen_plan.setdefault("version", 1)
    frozen_plan.setdefault("handles", [])
    return {
        "uuid": TASK_UUID,
        "workflow_uuid": WORKFLOW_UUID,
        "workflow_snapshot": workflow_snapshot
        or {
            "nodes": [],
            "edges": [],
            "node_templates": [],
            "handle_templates": [],
        },
        "execution_plan": frozen_plan,
    }


def _planned_action(
    *,
    node_uuid: str = NODE_A_UUID,
    kind: str = "device_action",
    device_id: str | None = "reactor-a",
    action_name: str | None = "distribute",
) -> dict[str, Any]:
    """构造与目标执行计划节点字段一字一致的动作计划。

    参数：节点 UUID、执行器种类、固定设备和动作名决定可执行合同。返回：包含
    最终参数、输入和动作类型的计划节点。异常：无；缺失值供失败关闭测试使用。
    """

    return {
        "uuid": node_uuid,
        "topological_index": 0,
        "kind": kind,
        "device_id": device_id,
        "action_name": action_name,
        "action_type": "UniLabJsonCommand",
        "param_schema": {"type": "object"},
        "param": {"plate": {"uuid": MATERIAL_UUID}},
        "execution_policy": {},
        "inputs": [],
        "source_handle_uuids": [],
    }


def _job(job_uuid: str, node_uuid: str) -> dict[str, Any]:
    """构造已有工作流节点作业（WorkflowNodeJob）身份。

    参数：``job_uuid`` 与 ``node_uuid`` 分别是作业和所属计划节点身份。返回：
    保留最终参数的作业对象。异常：无。
    """

    return {
        "uuid": job_uuid,
        "workflow_node_uuid": node_uuid,
        "param": {"plate": {"uuid": MATERIAL_UUID}},
    }


def test_execution_plan_is_the_only_runtime_static_input() -> None:
    """编译器必须只从执行计划读取动作，不从审计快照补齐运行事实。

    参数：无。返回：无；断言没有顶层设备/动作字段的空审计快照仍能编译计划。
    异常：编译失败即测试失败。
    """

    # ``execution_plan`` 是任务提交后唯一可重放的运行静态输入。
    execution_plan = {
        "run_mode": "normal",
        "nodes": [_planned_action()],
        "edges": [],
    }

    spec = WorkflowSpecCompiler().compile(
        _task_from_plan(execution_plan),
        [_job(JOB_A_UUID, NODE_A_UUID)],
    )

    assert [(node.id, node.device_id, node.action_name) for node in spec.nodes] == [
        (NODE_A_UUID, "reactor-a", "distribute")
    ]


def test_real_plan_freezes_executor_binding_and_action_contract() -> None:
    """真实计划必须冻结固定执行器与完整动作合同。

    参数：无。返回：无；断言 ``executor_binding`` 投影为设备身份，动作名被冻结，
    创作节点的空动作类型规范为 ``UniLabJsonCommand``。异常：计划构造失败即失败。
    """

    plan, _jobs = _build_plan(_single_action_graph())

    assert plan["nodes"][0]["device_id"] == "reactor-a"
    assert plan["nodes"][0]["action_name"] == "distribute"
    assert plan["nodes"][0]["action_type"] == "UniLabJsonCommand"


def test_compiler_preserves_final_material_reference_param() -> None:
    """编译必须保留最终参数中的稳定物料引用供动作物料锁解析。

    参数：无。返回：无；断言 ``{"uuid": ...}`` 不被快照回退或计划转换删除。
    异常：编译失败即测试失败。
    """

    execution_plan = {
        "run_mode": "normal",
        "nodes": [_planned_action()],
        "edges": [],
    }

    spec = WorkflowSpecCompiler().compile(
        _task_from_plan(execution_plan),
        [_job(JOB_A_UUID, NODE_A_UUID)],
    )

    assert spec.nodes[0].param == {"plate": {"uuid": MATERIAL_UUID}}


def test_group_node_without_job_is_not_dispatchable() -> None:
    """虚拟分组节点（Group）没有作业时也必须被编译器跳过。

    参数：无。返回：无；断言执行计划中只有动作节点，审计快照中的 Group 不会
    触发缺作业错误。异常：编译失败即测试失败。
    """

    execution_plan = {
        "run_mode": "normal",
        "nodes": [_planned_action()],
        "edges": [],
    }
    group_node = _node(
        NODE_B_UUID,
        TEMPLATE_B_UUID,
        node_type="Group",
        action_name=None,
    )
    workflow_snapshot = _single_action_graph()
    workflow_snapshot["nodes"].insert(0, group_node)

    spec = WorkflowSpecCompiler().compile(
        _task_from_plan(execution_plan, workflow_snapshot=workflow_snapshot),
        [_job(JOB_A_UUID, NODE_A_UUID)],
    )

    assert [node.id for node in spec.nodes] == [NODE_A_UUID]


def test_unsupported_executor_contracts_fail_closed() -> None:
    """不支持的执行器种类或缺失动作绑定必须在派发前稳定失败。

    参数：无。返回：无；``cases`` 中每项冻结计划变体与预期错误码。异常：每项
    必须抛 ``WorkflowSpecCompilationError``，不得生成空设备或空动作派发。
    """

    # 每个计划变体代表当前旧调度器无法安全承担的一类执行责任。
    cases = [
        (_planned_action(kind="compute"), "unsupported_executor_kind"),
        (_planned_action(kind="tool_call"), "unsupported_executor_kind"),
        (_planned_action(kind="condition"), "unsupported_executor_kind"),
        (_planned_action(kind="Transfer"), "unsupported_executor_kind"),
        (_planned_action(kind="future_executor"), "unsupported_executor_kind"),
        (_planned_action(device_id=None), "invalid_executor_binding"),
        (_planned_action(action_name=None), "invalid_action_contract"),
    ]
    for planned_node, expected_code in cases:
        execution_plan = {"run_mode": "normal", "nodes": [planned_node], "edges": []}
        with pytest.raises(WorkflowSpecCompilationError) as caught:
            WorkflowSpecCompiler().compile(
                _task_from_plan(execution_plan),
                [_job(JOB_A_UUID, NODE_A_UUID)],
            )
        assert caught.value.code == expected_code


def test_disabled_middle_node_is_contractually_bypassed() -> None:
    """活动 A→禁用 B→活动 C 必须收敛为 A→C 的持久依赖。

    参数：无。返回：无；断言禁用节点不会让 C 变成新的根节点，旁路边只表达
    顺序依赖而不伪造 B 的动作返回值。异常：真实计划构造失败即测试失败。
    """

    graph = {
        "nodes": [
            _node(NODE_A_UUID, TEMPLATE_A_UUID),
            _node(NODE_B_UUID, TEMPLATE_B_UUID, disabled=True),
            _node(NODE_C_UUID, TEMPLATE_C_UUID),
        ],
        "edges": [
            _edge(
                "18000000-0000-4000-8000-000000000001",
                NODE_A_UUID,
                NODE_B_UUID,
            ),
            _edge(
                "18000000-0000-4000-8000-000000000002",
                NODE_B_UUID,
                NODE_C_UUID,
            ),
        ],
        "node_templates": [
            _template(TEMPLATE_A_UUID),
            _template(TEMPLATE_B_UUID),
            _template(TEMPLATE_C_UUID),
        ],
        "handle_templates": [
            _handle(TARGET_HANDLE_UUID, TEMPLATE_A_UUID, io_type="target", key="plate"),
            _handle(SOURCE_HANDLE_UUID, TEMPLATE_A_UUID, io_type="source", key="plate"),
        ],
    }
    # 为每个模板使用独立端点，避免本测试混入运行连接点身份规则。
    for template_uuid, suffix in ((TEMPLATE_B_UUID, "3"), (TEMPLATE_C_UUID, "5")):
        graph["handle_templates"].extend(
            [
                _handle(
                    f"16000000-0000-4000-8000-00000000000{suffix}",
                    template_uuid,
                    io_type="target",
                    key="plate",
                ),
                _handle(
                    f"16000000-0000-4000-8000-00000000000{int(suffix) + 1}",
                    template_uuid,
                    io_type="source",
                    key="plate",
                ),
            ]
        )
    graph["edges"][0].update(target_handle_uuid="16000000-0000-4000-8000-000000000003")
    graph["edges"][1].update(
        source_handle_uuid="16000000-0000-4000-8000-000000000004",
        target_handle_uuid="16000000-0000-4000-8000-000000000005",
    )

    plan, _jobs = _build_plan(graph)

    assert [
        (edge["source_node_uuid"], edge["target_node_uuid"]) for edge in plan["edges"]
    ] == [(NODE_A_UUID, NODE_C_UUID)]
    assert plan["edges"][0]["dependency_only"] is True


def test_reused_template_gets_node_scoped_runtime_handle_identities() -> None:
    """复用同一模板的两个节点必须获得各自运行连接点身份。

    参数：无。返回：无；断言重复构建稳定、节点端点互异且边精确引用 A 来源与
    C 目标，避免旧运行时 ``by_uuid`` 覆盖。异常：计划构造失败即测试失败。
    """

    graph = _single_action_graph()
    graph["nodes"].append(_node(NODE_C_UUID, TEMPLATE_A_UUID))
    graph["edges"].append(
        _edge(
            "18000000-0000-4000-8000-000000000003",
            NODE_A_UUID,
            NODE_C_UUID,
        )
    )

    first_plan, _first_jobs = _build_plan(graph)
    second_plan, _second_jobs = _build_plan(graph)
    planned = {node["uuid"]: node for node in first_plan["nodes"]}
    # 三个身份分别是 A 输出、C 输入与 C 输出的节点作用域连接点 UUID。
    a_source_uuid = planned[NODE_A_UUID]["source_handle_uuids"][0]
    c_target_uuid = planned[NODE_C_UUID]["inputs"][0]["handle_uuid"]
    c_source_uuid = planned[NODE_C_UUID]["source_handle_uuids"][0]

    assert len({a_source_uuid, c_target_uuid, c_source_uuid}) == 3
    assert first_plan["edges"][0]["source_handle_uuid"] == a_source_uuid
    assert first_plan["edges"][0]["target_handle_uuid"] == c_target_uuid
    assert second_plan["nodes"] == first_plan["nodes"]
    assert second_plan["edges"] == first_plan["edges"]


def test_compilation_error_rejects_unknown_machine_code() -> None:
    """编译错误码必须属于闭集，未知码不得构造或泄漏。

    参数：无。返回：无；断言随意错误码在边界被 ``ValueError`` 拒绝。异常：若
    未抛异常则测试失败，防止上层收到无法稳定映射的错误。
    """

    with pytest.raises(ValueError, match="unknown workflow spec compilation code"):
        WorkflowSpecCompilationError("invented_runtime_error", "不得泄漏")
