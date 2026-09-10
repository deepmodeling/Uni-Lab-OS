"""F05.3-A 工作流规格编译器（WorkflowSpecCompiler）的失败关闭合同。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from unilabos.workflow.execution_plan import (
    ExecutionPlanBuilder,
    ExecutionPlanBuildError,
)

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
TASK_UUID = "20000000-0000-4000-8000-000000000001"
SOURCE_NODE_UUID = "30000000-0000-4000-8000-000000000001"
FIRST_NODE_UUID = "30000000-0000-4000-8000-000000000002"
SECOND_NODE_UUID = "30000000-0000-4000-8000-000000000003"
SOURCE_JOB_UUID = "40000000-0000-4000-8000-000000000001"
FIRST_JOB_UUID = "40000000-0000-4000-8000-000000000002"
SECOND_JOB_UUID = "40000000-0000-4000-8000-000000000003"
MATERIAL_UUID = "50000000-0000-4000-8000-000000000001"
DEVICE_UUID = "60000000-0000-4000-8000-000000000001"


def _action_contract_schema() -> dict[str, Any]:
    """构造工作流规格编译测试使用的完整动作合同（Action Contract）。

    参数：无。返回：允许任意 Goal 参数的动作 Schema envelope；本夹具不声明
    额外物料锁标记，短期物料需求仍由物料来源链产生。异常：无。
    """

    return {
        "type": "object",
        "properties": {
            "goal": {"type": "object", "additionalProperties": True},
        },
        "required": ["goal"],
    }


def _compiler_contract() -> tuple[type[Any], type[BaseException]]:
    """取得待实现编译器及其稳定错误类型。

    参数：无。返回：工作流规格编译器（WorkflowSpecCompiler）类型与编译错误
    类型。异常：生产模块尚未实现时保留 ``ModuleNotFoundError``，使每个行为
    测试独立呈现 RED，而不是在测试收集阶段中止。
    """

    module = import_module("unilabos.workflow.workflow_spec_compiler")
    return module.WorkflowSpecCompiler, module.WorkflowSpecCompilationError


def _task_snapshot(
    *,
    mode: str = "existing",
    material_uuid: str | None = MATERIAL_UUID,
    disable_first_consumer: bool = False,
    implicit_passthrough: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """构造冻结任务快照与工作流节点作业（WorkflowNodeJob）列表。

    参数：``mode`` 是物料来源模式（MaterialSourceMode）；``material_uuid`` 是
    任务提交前已固定的具体物料身份；``disable_first_consumer`` 控制物料链首个
    物理消费者是否禁用。返回：纯字典任务快照与按节点稳定绑定的作业列表。
    异常：无；非法组合由待测编译边界失败关闭。
    """

    # ``source_selector`` 是冻结的物料来源（MaterialSource）选择器，不允许
    # 编译器在运行时查询库存（Inventory）或补写具体物料身份。
    source_selector = {
        "mode": mode,
        "resource_template_uuid": "70000000-0000-4000-8000-000000000001",
        "mount": {"uuid": "70000000-0000-4000-8000-000000000002"},
        "material_uuid": material_uuid,
        "site": None,
        "slot_range": None,
        "flow_role": "primary_sample",
    }
    # ``snapshot_nodes`` 是已应用工作流图的不可变节点顺序；物料来源节点只提供
    # 任务物料绑定（TaskMaterialBinding），两个 ILab 节点才是可派发动作。
    snapshot_nodes = [
        {
            "uuid": SOURCE_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000001",
            "name": "固定孔板来源",
            "type": "material_source",
            "param": source_selector,
            "disabled": False,
        },
        {
            "uuid": FIRST_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "material_uuid": DEVICE_UUID,
            "name": "加入预混液",
            "type": "ILab",
            "action_name": "distribute",
            "action_type": "UniLabJsonCommand",
            "param": (
                {"plate": {"uuid": material_uuid}} if material_uuid is not None else {}
            ),
            "disabled": disable_first_consumer,
            "meta_data": {
                "unilab": {
                    "executor_binding": {"mode": "fixed", "device_id": DEVICE_UUID}
                }
            },
        },
        {
            "uuid": SECOND_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000003",
            "material_uuid": DEVICE_UUID,
            "name": "读取孔板",
            "type": "ILab",
            "action_name": "read_plate",
            "action_type": "UniLabJsonCommand",
            "param": (
                {"plate": {"uuid": material_uuid}} if material_uuid is not None else {}
            ),
            "disabled": False,
            "meta_data": {
                "unilab": {
                    "executor_binding": {"mode": "fixed", "device_id": DEVICE_UUID}
                }
            },
        },
    ]
    # ``snapshot_edges`` 冻结同一物料占位符（ResourceSlot）的线性消费顺序。
    snapshot_edges = [
        {
            "uuid": "72000000-0000-4000-8000-000000000001",
            "source_node_uuid": SOURCE_NODE_UUID,
            "source_handle_uuid": "73000000-0000-4000-8000-000000000001",
            "target_node_uuid": FIRST_NODE_UUID,
            "target_handle_uuid": "73000000-0000-4000-8000-000000000002",
        },
        {
            "uuid": "72000000-0000-4000-8000-000000000002",
            "source_node_uuid": (
                SOURCE_NODE_UUID if implicit_passthrough else FIRST_NODE_UUID
            ),
            "source_handle_uuid": (
                "73000000-0000-4000-8000-000000000001"
                if implicit_passthrough
                else "73000000-0000-4000-8000-000000000003"
            ),
            "target_node_uuid": SECOND_NODE_UUID,
            "target_handle_uuid": "73000000-0000-4000-8000-000000000004",
        },
    ]
    # ``handle_templates`` 冻结每条边是否传递物料占位符，而不让编译器从参数
    # 名称猜测物料语义。
    handle_templates = [
        {
            "uuid": "73000000-0000-4000-8000-000000000001",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000001",
            "handle_key": "material",
            "io_type": "source",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000002",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "handle_key": "plate",
            "io_type": "target",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000003",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "handle_key": "plate",
            "io_type": "source",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000004",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000003",
            "handle_key": "plate",
            "io_type": "target",
            "type": "ResourceSlot",
        },
    ]
    # ``first_action_contract``/``second_action_contract`` 分别模拟真实模板投影中
    # Goal 子模式和保留完整合同的双层表示。
    first_action_contract = _action_contract_schema()
    second_action_contract = _action_contract_schema()
    graph = {
        "nodes": snapshot_nodes,
        "edges": snapshot_edges,
        "node_templates": [
            {
                "uuid": "71000000-0000-4000-8000-000000000001",
                "node_type": "material_source",
            },
            {
                "uuid": "71000000-0000-4000-8000-000000000002",
                "node_type": "ILab",
                "schema": first_action_contract["properties"]["goal"],
                "meta_data": {
                    "unilab": {
                        "contract_kind": "typed",
                        "action_contract_schema": first_action_contract,
                    }
                },
            },
            {
                "uuid": "71000000-0000-4000-8000-000000000003",
                "node_type": "ILab",
                "schema": second_action_contract["properties"]["goal"],
                "meta_data": {
                    "unilab": {
                        "contract_kind": "typed",
                        "action_contract_schema": second_action_contract,
                    }
                },
            },
        ],
        "handle_templates": handle_templates,
    }
    # ``execution_plan`` 是从冻结应用图一次性产生的唯一运行静态输入；测试不再
    # 允许编译器回退读取 ``workflow_snapshot``。
    execution_plan, jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    job_identities = {
        SOURCE_NODE_UUID: SOURCE_JOB_UUID,
        FIRST_NODE_UUID: FIRST_JOB_UUID,
        SECOND_NODE_UUID: SECOND_JOB_UUID,
    }
    for job in jobs:
        # ``job`` 是构建器首次作业；替换随机 UUID 只为断言持久身份保持不变。
        job["uuid"] = job_identities[job["workflow_node_uuid"]]
    task_snapshot = {
        "uuid": TASK_UUID,
        "workflow_uuid": WORKFLOW_UUID,
        "workflow_snapshot": graph,
        "execution_plan": execution_plan,
    }
    return task_snapshot, jobs


def test_fixed_existing_source_compiles_only_physical_consumers() -> None:
    """固定既有物料来源只把普通动作交给本地调度器。

    参数：无。返回：无；断言物料来源解析作业
    （MaterialSourceResolutionJob）不派发，且两个普通动作都不重复承担任务物料
    准入（TaskMaterialAdmission）。异常：编译失败即测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    assert [node.id for node in spec.nodes] == [FIRST_NODE_UUID, SECOND_NODE_UUID]
    assert SOURCE_NODE_UUID not in {node.id for node in spec.nodes}
    assert spec.material_requirements_by_node() == {}


def test_material_requirement_is_not_duplicated_on_downstream_consumer() -> None:
    """同一线性物料链的普通消费者不得重复申请任务物料预留。

    参数：无。返回：无；断言短期整图物料预留输入只由物料来源解析作业
    （MaterialSourceResolutionJob）承担，两个普通动作都不重复；该兼容输入将在
    K11 被正式任务物料预留（TaskMaterialReservation）替换。异常：编译失败即
    测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    requirements_by_node = spec.material_requirements_by_node()
    assert requirements_by_node == {}
    assert spec.nodes[1].material_requirements == []


def test_disabled_consumer_moves_requirement_to_first_enabled_node() -> None:
    """禁用消费者不派发，物料需求应归属首个启用物理消费者。

    参数：无。返回：无；断言禁用节点及其作业身份都不会进入调度规格，后续
    启用动作也不获得新的物料需求。异常：编译失败即测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot(disable_first_consumer=True)

    spec = compiler_type().compile(task_snapshot, jobs)

    assert [node.id for node in spec.nodes] == [SECOND_NODE_UUID]
    assert spec.nodes[0].job_id == SECOND_JOB_UUID
    assert spec.nodes[0].material_requirements == []


def test_compiler_preserves_task_node_and_job_stable_identities() -> None:
    """编译必须保留任务、节点与作业的稳定 UUID，禁止创建第二套身份。

    参数：无。返回：无；断言工作流规格（WorkflowSpec）的任务身份以及每个
    工作流节点（WorkflowNode）/作业身份与持久快照完全一致。异常：编译失败即
    测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    assert spec.workflow_id == TASK_UUID
    assert spec.task_id == TASK_UUID
    assert [(node.id, node.job_id) for node in spec.nodes] == [
        (FIRST_NODE_UUID, FIRST_JOB_UUID),
        (SECOND_NODE_UUID, SECOND_JOB_UUID),
    ]


def test_create_new_material_source_fails_closed_before_scheduling() -> None:
    """短期桥不支持新建物料来源时必须在调度前失败关闭。

    参数：无。返回：无；断言 ``create_new`` 不会绕过库存权威（Inventory
    Authority）或生成可派发节点。异常：预期稳定编译错误码。
    """

    with pytest.raises(ExecutionPlanBuildError) as caught:
        _task_snapshot(mode="create_new", material_uuid=None)

    assert caught.value.code == "unsupported_material_source_mode"


def test_automatic_existing_source_freezes_selector_without_inventory_lookup() -> None:
    """自动既有来源只冻结选择条件和运行时写入目标。

    参数：无。返回：无；断言执行计划（ExecutionPlan）不查询库存权威
    （Inventory Authority）或伪造具体物料（Material）身份，但保留挂载点、资源
    模板和首个物理消费者参数目标，供边缘调度器（EdgeScheduler）准入时解析。
    异常：计划构建或规格编译失败即测试失败。
    """

    task_snapshot, jobs = _task_snapshot(material_uuid=None)

    source = task_snapshot["execution_plan"]["nodes"][0]
    assert source["material_requirements"] == [
        {
            "template_id": "70000000-0000-4000-8000-000000000001",
            "mount_uuid": "70000000-0000-4000-8000-000000000002",
            "site_uuid": "",
            "slot_uuids": [],
        }
    ]
    assert source["material_binding_targets"] == [
        {"workflow_node_uuid": FIRST_NODE_UUID, "param_key": "plate"}
    ]
    assert "plate" not in task_snapshot["execution_plan"]["nodes"][1]["param"]
    assert "plate" not in jobs[1]["param"]


def test_automatic_source_binds_every_ordered_implicit_passthrough_consumer() -> None:
    """自动来源必须冻结所有隐式透传消费者的运行绑定目标。

    参数：无。返回无；断言复合工作流（CompositeWorkflow）展开后同一
    物料占位符（ResourceSlot）直接连接多个严格有序动作时，每个动作都能从
    调度边缘（EdgeScheduler）的单次预分配取得同一个物料（Material）身份。
    """

    task_snapshot, jobs = _task_snapshot(
        material_uuid=None,
        implicit_passthrough=True,
    )

    source = task_snapshot["execution_plan"]["nodes"][0]
    assert source["material_binding_targets"] == [
        {"workflow_node_uuid": FIRST_NODE_UUID, "param_key": "plate"},
        {"workflow_node_uuid": SECOND_NODE_UUID, "param_key": "plate"},
    ]
    assert "plate" not in jobs[1]["param"]
    assert "plate" not in jobs[2]["param"]
