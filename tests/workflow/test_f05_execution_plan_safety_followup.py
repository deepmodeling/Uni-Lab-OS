"""F05.3-A2 执行计划（ExecutionPlan）安全收口合同。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import node_from_dict
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.execution_plan import (
    ExecutionPlanBuilder,
    ExecutionPlanBuildError,
)
from unilabos.workflow.workflow_spec_compiler import (
    WorkflowSpecCompilationError,
    WorkflowSpecCompiler,
)

# 这些 UUID 分别代表工作流任务、节点、模板、具体物料与设备物料的稳定身份。
TASK_UUID = "21000000-0000-4000-8000-000000000001"
SOURCE_NODE_UUID = "22000000-0000-4000-8000-000000000001"
ACTION_NODE_UUID = "22000000-0000-4000-8000-000000000002"
SOURCE_TEMPLATE_UUID = "23000000-0000-4000-8000-000000000001"
ACTION_TEMPLATE_UUID = "23000000-0000-4000-8000-000000000002"
SOURCE_HANDLE_UUID = "24000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "24000000-0000-4000-8000-000000000002"
MATERIAL_UUID = "25000000-0000-4000-8000-000000000001"
SECOND_MATERIAL_UUID = "25000000-0000-4000-8000-000000000002"
DEVICE_MATERIAL_UUID = "26000000-0000-4000-8000-000000000001"
DEVICE_ID = "reactor-a"


def _action_schema() -> dict[str, Any]:
    """构造冻结的动作物料锁（Action Material Lock）合同。

    参数：无。返回：要求 ``plate`` 是物料引用并建立物料锁的
    完整动作 Schema。异常：无。
    """

    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "plate": {
                        "type": "object",
                        "x-unilabos-material-lock": True,
                        "properties": {
                            "uuid": {"type": "string", "format": "uuid"},
                        },
                        "required": ["uuid"],
                        "additionalProperties": False,
                    }
                },
                "required": ["plate"],
                "additionalProperties": False,
            }
        },
        "required": ["goal"],
    }


def _real_authoring_graph(*, explicit_executor: bool = True) -> dict[str, Any]:
    """构造未预填消费动作参数的真实工作流创作图。

    参数：``explicit_executor`` 决定动作节点是否声明固定执行器
    （Executor）。返回：固定的 `existing` 物料来源（MaterialSource）通过
    物料占位符（ResourceSlot）连到动作的应用图。异常：无。
    """

    # ``executor_metadata`` 是创作阶段冻结的显式执行器绑定。
    executor_metadata: dict[str, Any] = {"unilab": {}}
    if explicit_executor:
        executor_metadata["unilab"]["executor_binding"] = {
            "mode": "fixed",
            "device_id": DEVICE_ID,
        }
    # ``action_contract`` 是注册表保存并经模板保留元数据冻结的完整动作合同。
    action_contract = _action_schema()
    # ``source_selector`` 是已固定到具体物料的物料来源选择器。
    source_selector = {
        "mode": "existing",
        "resource_template_uuid": "27000000-0000-4000-8000-000000000001",
        "material_uuid": MATERIAL_UUID,
        "mount": None,
        "site": None,
        "slot_range": None,
        "flow_role": "primary_sample",
    }
    return {
        "nodes": [
            {
                "uuid": SOURCE_NODE_UUID,
                "workflow_node_template_uuid": SOURCE_TEMPLATE_UUID,
                "type": "material_source",
                "param": source_selector,
                "disabled": False,
            },
            {
                "uuid": ACTION_NODE_UUID,
                "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
                "type": "ILab",
                "material_uuid": DEVICE_MATERIAL_UUID,
                "action_name": "distribute",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "meta_data": executor_metadata,
                "disabled": False,
            },
        ],
        "edges": [
            {
                "uuid": "28000000-0000-4000-8000-000000000001",
                "source_node_uuid": SOURCE_NODE_UUID,
                "source_handle_uuid": SOURCE_HANDLE_UUID,
                "target_node_uuid": ACTION_NODE_UUID,
                "target_handle_uuid": TARGET_HANDLE_UUID,
            }
        ],
        "node_templates": [
            {"uuid": SOURCE_TEMPLATE_UUID, "node_type": "material_source"},
            {
                "uuid": ACTION_TEMPLATE_UUID,
                "node_type": "ILab",
                "schema": action_contract["properties"]["goal"],
                "meta_data": {
                    "unilab": {
                        "contract_kind": "typed",
                        "action_contract_schema": action_contract,
                    }
                },
            },
        ],
        "handle_templates": [
            {
                "uuid": SOURCE_HANDLE_UUID,
                "workflow_node_template_uuid": SOURCE_TEMPLATE_UUID,
                "handle_key": "material",
                "data_key": "material",
                "data_source": "executor",
                "io_type": "source",
                "type": "ResourceSlot",
            },
            {
                "uuid": TARGET_HANDLE_UUID,
                "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
                "handle_key": "plate",
                "data_key": "plate",
                "data_source": "executor",
                "io_type": "target",
                "type": "ResourceSlot",
                "required": True,
            },
        ],
    }


def _build_real_plan(
    *, explicit_executor: bool = True
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """从真实创作图构造执行计划（ExecutionPlan）和作业。

    参数：``explicit_executor`` 透传给创作图，用于覆盖安全失败
    边界。返回：计划与首次工作流节点作业（WorkflowNodeJob）。
    异常：不合法的执行器绑定（ExecutorBinding）或图会失败关闭。
    """

    return ExecutionPlanBuilder().build(
        _real_authoring_graph(explicit_executor=explicit_executor),
        run_mode="normal",
        target_node_uuid=None,
    )


def _compile_real_plan(plan: dict[str, Any], jobs: list[dict[str, Any]]) -> Any:
    """把真实执行计划（ExecutionPlan）编译为遗留调度规格。

    参数：``plan`` 是已冻结计划，``jobs`` 是已持久作业身份与
    最终参数。返回：遗留工作流规格（WorkflowSpec）。异常：计划或
    作业合同非法时保留编译错误。
    """

    task_snapshot = {
        "uuid": TASK_UUID,
        "workflow_snapshot": _real_authoring_graph(),
        "execution_plan": plan,
    }
    return WorkflowSpecCompiler().compile(task_snapshot, jobs)


def _action_plan_node(plan: dict[str, Any]) -> dict[str, Any]:
    """按稳定节点身份取得设备动作计划节点。

    参数：``plan`` 是同时包含协调器与设备责任的执行计划（ExecutionPlan）。
    返回：唯一设备动作节点。异常：夹具缺失或重复时由 ``next``/断言暴露。
    """

    return next(node for node in plan["nodes"] if node["uuid"] == ACTION_NODE_UUID)


def _action_job(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """按稳定节点身份取得设备动作工作流节点作业。

    参数：``jobs`` 是首次工作流节点作业（WorkflowNodeJob）集合。返回：唯一
    设备动作作业。异常：夹具缺失或重复时由 ``next``/断言暴露。
    """

    return next(job for job in jobs if job["workflow_node_uuid"] == ACTION_NODE_UUID)


class _StaleRegistryResolver:
    """模拟任务创建后已变更的实时注册表（Registry）。"""

    def __init__(self) -> None:
        """初始化解析调用计数。

        参数：无。返回：无。异常：无。
        """

        # ``calls`` 用来证明冻结动作合同存在时没有读取实时注册表。
        self.calls = 0

    def __call__(
        self,
        device_id: str,
        action_name: str,
        final_param: dict[str, Any],
    ) -> tuple[str, ...]:
        """返回与冻结合同不同的物料身份。

        参数：设备身份、动作名和最终参数是旧注册表解析器接口。
        返回：错误地指向第二个物料的 UUID。异常：无。
        """

        del device_id, action_name, final_param
        self.calls += 1
        return (SECOND_MATERIAL_UUID,)


def test_execution_plan_rejects_material_uuid_as_executor_fallback() -> None:
    """设备物料身份不得替代显式执行器绑定。

    参数：无。返回：无；断言缺少执行器绑定（ExecutorBinding）的
    真实创作图以稳定错误码失败关闭。异常：预期计划构建错误。
    """

    with pytest.raises(ExecutionPlanBuildError) as caught:
        _build_real_plan(explicit_executor=False)

    assert caught.value.code == "invalid_executor_binding"


def test_empty_job_list_cannot_erase_frozen_resource_slot_materials() -> None:
    """空作业数组不得擦除已冻结的物料占位符列表。

    参数：无。返回：无；断言工作流节点作业（WorkflowNodeJob）
    的空 ``tips`` 不会覆盖计划中两个稳定物料身份。异常：编译失败
    或身份丢失即测试失败。
    """

    plan, jobs = _build_real_plan()
    # ``frozen_tips`` 是创建任务时已确定的物料占位符实例列表。
    frozen_tips = [{"uuid": MATERIAL_UUID}, {"uuid": SECOND_MATERIAL_UUID}]
    action_node = _action_plan_node(plan)
    action_node["param"]["tips"] = frozen_tips
    # ``job_param`` 模拟从持久层独立读回的作业最终参数，不与计划容器共享。
    action_job = _action_job(jobs)
    job_param = dict(action_job["param"])
    job_param["tips"] = []
    action_job["param"] = job_param

    spec = _compile_real_plan(plan, jobs)

    assert spec.nodes[0].param["tips"] == frozen_tips


def test_fixed_material_source_populates_first_consumer_final_param() -> None:
    """固定的 `existing` 物料来源必须写入首个消费动作参数。

    参数：无。返回：无；断言未预填动作参数的真实创作图
    仍沿物料占位符（ResourceSlot）生成 ``plate`` 物料引用。异常：
    计划或编译失败即测试失败。
    """

    plan, jobs = _build_real_plan()

    assert _action_plan_node(plan)["param"] == {
        "plate": {"uuid": MATERIAL_UUID}
    }
    spec = _compile_real_plan(plan, jobs)
    assert spec.nodes[0].param == {"plate": {"uuid": MATERIAL_UUID}}


def test_frozen_param_schema_enters_legacy_scheduler_node() -> None:
    """执行计划冻结的参数 Schema 必须进入遗留调度节点。

    参数：无。返回：无；断言工作流规格编译器
    （WorkflowSpecCompiler）保留任务创建时的动作合同（Action Contract）。
    异常：合同丢失即测试失败。
    """

    plan, jobs = _build_real_plan()
    spec = _compile_real_plan(plan, jobs)

    assert spec.nodes[0].param_schema == _action_schema()


def test_frozen_action_contract_wins_over_changed_registry() -> None:
    """冻结动作合同必须覆盖任务创建后的实时注册表变化。

    参数：无。返回：无；断言本地调度器（Local Scheduler）
    仅按冻结 Schema 为原物料建立动作物料锁（Action Material Lock），
    不调用陈旧注册表解析器。异常：物料锁身份改变即测试失败。
    """

    plan, jobs = _build_real_plan()
    spec = _compile_real_plan(plan, jobs)
    stale_registry = _StaleRegistryResolver()
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        material_lock_resolver=stale_registry,
    )

    result = scheduler.submit_workflow(spec)
    inflight_jobs = scheduler.snapshot()["inflight_jobs"]
    # ``resource_locks`` 是当前作业执行占用（JobExecutionClaim）的内存前身。
    resource_locks = next(iter(inflight_jobs.values()))["resource_locks"]

    assert len(result["dispatched"]) == 1
    assert resource_locks == [f"material/{MATERIAL_UUID}/exclusive"]
    assert stale_registry.calls == 0


@pytest.mark.parametrize(
    ("schema_present", "schema_value"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(True, None, id="null"),
        pytest.param(True, "not-an-object", id="non-object"),
    ],
)
def test_standard_plan_requires_frozen_param_schema(
    schema_present: bool,
    schema_value: Any,
) -> None:
    """标准执行计划的设备动作必须带冻结参数 Schema。

    参数：``schema_present`` 表示字段是否存在，``schema_value`` 是待验证
    合同值。返回：无；断言缺失、空值和非对象合同都以稳定
    错误码失败关闭。异常：预期工作流规格编译错误。
    """

    plan, jobs = _build_real_plan()
    action_node = _action_plan_node(plan)
    if schema_present:
        action_node["param_schema"] = schema_value
    else:
        action_node.pop("param_schema")

    with pytest.raises(WorkflowSpecCompilationError) as caught:
        _compile_real_plan(plan, jobs)

    assert caught.value.code == "invalid_action_contract"


@pytest.mark.parametrize(
    ("contract_present", "invalid_contract"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(True, None, id="null"),
        pytest.param(True, "not-an-object", id="non-object"),
        pytest.param(
            True,
            {"properties": {"goal": "not-an-object"}},
            id="goal-non-object",
        ),
    ],
)
def test_execution_plan_builder_rejects_invalid_action_contract(
    contract_present: bool,
    invalid_contract: Any,
) -> None:
    """执行计划构建器必须在持久前拒绝不完整的动作合同。

    参数：``contract_present`` 区分字段缺失与显式空值，
    ``invalid_contract`` 是待验证的保留合同值。返回：无；断言设备动作模板
    没有合法冻结动作合同（Action Contract）时，不会生成可持久的标准
    执行计划（ExecutionPlan）。异常：预期稳定计划构建错误。
    """

    graph = _real_authoring_graph()
    # ``action_template`` 是设备动作的冻结模板，本测试只破坏完整合同边界。
    action_template = graph["node_templates"][1]
    action_unilab = action_template["meta_data"]["unilab"]
    if contract_present:
        action_unilab["action_contract_schema"] = invalid_contract
    else:
        action_unilab.pop("action_contract_schema")

    with pytest.raises(ExecutionPlanBuildError) as caught:
        ExecutionPlanBuilder().build(
            graph,
            run_mode="normal",
            target_node_uuid=None,
        )

    assert caught.value.code == "invalid_action_contract"


@pytest.mark.parametrize(
    "invalid_schema",
    [
        pytest.param([("type", "object")], id="key-value-array"),
        pytest.param("not-an-object", id="string"),
    ],
)
def test_legacy_node_parser_rejects_non_object_param_schema(
    invalid_schema: Any,
) -> None:
    """遗留节点解析器不得把其他容器猜测为动作合同。

    参数：``invalid_schema`` 是键值对数组或字符串。返回：无；断言
    ``node_from_dict`` 仅接受对象或 ``None``。异常：预期稳定 ``TypeError``
    与中文诊断。
    """

    with pytest.raises(TypeError, match="param_schema 必须是对象或 None"):
        node_from_dict({"id": "legacy-node", "param_schema": invalid_schema})
