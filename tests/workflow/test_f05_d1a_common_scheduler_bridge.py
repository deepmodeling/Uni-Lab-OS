"""F05.3-D 设备单动作运行（DeviceActionRun）共享调度接缝测试。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.registry.test_template_projection import (
    DEVICE_MATERIAL_UUID,
    FakeInventoryStore,
    FakeRegistry,
)
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.composition import (
    compose_local_workflow_template_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.service import WorkflowError
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler


class _MissingEdgeLocalIdInventoryStore(FakeInventoryStore):
    """提供缺少 Edge 本地执行器身份的设备物料摘要。"""

    def query_one(
        self,
        sql: str,
        params: tuple[Any, ...],
    ) -> dict[str, Any] | None:
        """读取测试库存身份并移除设备 ``edge_local_id``。

        参数：``sql`` 是组合根发出的规范查询，``params`` 是资源模板业务名或
        设备物料 UUID。返回：资源模板查询沿用父实现；设备物料查询返回不含
        Edge 本地执行器身份的摘要，用于证明创建阶段关闭失败。
        """

        # ``resolved_row`` 是库存权威返回的活动身份；设备物料行只保留匹配模板
        # 所需事实，刻意不提供可冻结进执行计划（ExecutionPlan）的执行器身份。
        resolved_row = super().query_one(sql, params)
        if resolved_row is None or "FROM material" not in sql:
            return resolved_row
        return {
            "uuid": resolved_row["uuid"],
            "resource_template_uuid": resolved_row["resource_template_uuid"],
            "meta_data": {},
        }


def _thaw_json(value: Any) -> Any:
    """把只读目录 JSON 容器恢复为持久化可比较形状。

    参数：``value`` 是由映射代理、元组和 JSON 标量组成的动作合同值。返回：映射
    递归转换为字典、元组递归转换为列表后的等价 JSON 值。异常：无；目录已经
    保证输入只含受支持的 JSON 值。
    """

    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def test_device_action_run_freezes_compiler_valid_execution_plan(
    tmp_path: Path,
) -> None:
    """设备单动作运行必须先冻结可由公共编译器读取的完整执行计划。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言持久执行计划
    （ExecutionPlan）携带版本、连接点、固定执行器和冻结动作合同，且公共编译器
    无需再读取注册表或物料解析器即可保留标准任务/作业身份。
    """

    reset_workflow_service_for_test()
    try:
        # ``workflow_service`` 是本地工作流任务（WorkflowTask）写权威；
        # ``projection`` 提供创建命令使用的已发布动作模板目录代际。
        workflow_service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=FakeInventoryStore(),
            registry=FakeRegistry(),
        )
        # ``action_template`` 是设备单动作运行冻结进执行计划（ExecutionPlan）的
        # 已发布动作合同来源，测试据此比较完整 Schema，而非手写局部字段。
        action_template = (
            projection.snapshot()
            .require_action(
                "lab.devices:Pump",
                "transfer",
            )
            .template
        )
        # ``created`` 是已经持久化的设备单动作任务及唯一作业聚合。
        created = workflow_service.create_device_action_run(
            material_uuid=DEVICE_MATERIAL_UUID,
            workflow_node_template_uuid=action_template["uuid"],
            param={"volume": 2.0},
            execution_policy={},
            idempotency_key="d1a-frozen-plan",
            description=None,
            meta_data={},
        )

        # ``plan`` 是创建事务返回的冻结执行计划（ExecutionPlan），不得依赖后续
        # 注册表或物料解析器补齐静态执行事实。
        plan = created["task"]["execution_plan"]
        assert plan["version"] == 1
        assert plan["handles"] == []
        assert plan["nodes"][0]["device_id"] == "pump-01"
        assert plan["nodes"][0]["action_name"] == "transfer"
        assert plan["nodes"][0]["action_type"] == "UniLabJsonCommand"
        # ``expected_param_schema`` 把只读目录容器规范化为持久 JSON 形状；字段和值
        # 仍完整来自发布时的动作合同（Action Contract），不手写任何子集。
        expected_param_schema = _thaw_json(
            action_template["meta_data"]["unilab"]["action_contract_schema"]
        )
        assert plan["nodes"][0]["param_schema"] == expected_param_schema

        # ``compiled_spec`` 证明执行只依赖冻结计划和既有作业，不再调度期补事实。
        compiled_spec = WorkflowSpecCompiler().compile(
            created["task"],
            [created["job"]],
        )
        assert compiled_spec.task_id == created["task"]["uuid"]
        assert compiled_spec.nodes[0].job_id == created["job"]["uuid"]
    finally:
        reset_workflow_service_for_test()


def test_device_action_run_requires_edge_executor_before_persistence(
    tmp_path: Path,
) -> None:
    """缺少 Edge 执行器身份时必须在设备单动作创建事务前关闭失败。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言创建抛稳定目录不可用
    错误，且工作流任务（WorkflowTask）列表总数仍为零，不遗留待处理聚合。
    """

    reset_workflow_service_for_test()
    try:
        # ``workflow_service`` 是公开设备单动作创建接缝；``projection`` 提供本次
        # 请求引用的已发布动作模板稳定身份。
        workflow_service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=_MissingEdgeLocalIdInventoryStore(),
            registry=FakeRegistry(),
        )
        action_template = (
            projection.snapshot()
            .require_action(
                "lab.devices:Pump",
                "transfer",
            )
            .template
        )

        with pytest.raises(WorkflowError) as captured_error:
            workflow_service.create_device_action_run(
                material_uuid=DEVICE_MATERIAL_UUID,
                workflow_node_template_uuid=action_template["uuid"],
                param={"volume": 2.0},
                execution_policy={},
                idempotency_key="d1a-missing-edge-executor",
                description=None,
                meta_data={},
            )

        assert captured_error.value.code == "template_catalog_unavailable"
        # ``task_page`` 是创建失败后的公开任务读模型，证明失败发生在持久化之前。
        task_page = workflow_service.list_workflow_tasks(page=1, page_size=20)
        assert task_page["total"] == 0
    finally:
        reset_workflow_service_for_test()


def test_d1a_terminal_projection_does_not_claim_physical_settlement(
    tmp_path: Path,
) -> None:
    """公共桥完成设备单动作后不得把业务终态冒充物理结算。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言同一工作流任务
    （WorkflowTask）与工作流节点作业（WorkflowNodeJob）进入 ``succeeded``，
    但共享投影保留 ``cleanup_status=none``，因为没有物理结算证据。
    """

    reset_workflow_service_for_test()
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    try:
        # ``workflow_service`` 通过生产组合根接入公共任务调度桥；``projection``
        # 提供设备单动作请求所引用的冻结动作模板。
        workflow_service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=FakeInventoryStore(),
            registry=FakeRegistry(),
            scheduler=scheduler,
        )
        action_template = (
            projection.snapshot()
            .require_action(
                "lab.devices:Pump",
                "transfer",
            )
            .template
        )
        created = workflow_service.create_device_action_run(
            material_uuid=DEVICE_MATERIAL_UUID,
            workflow_node_template_uuid=action_template["uuid"],
            param={"volume": 2.0},
            execution_policy={},
            idempotency_key="d1a-unsettled-terminal",
            description=None,
            meta_data={},
        )

        scheduler.on_job_finished(
            created["job"]["uuid"],
            True,
            {"accepted": True},
        )

        # ``terminal_task`` 与 ``terminal_job`` 是完成回调后的标准持久投影。
        terminal_task = workflow_service.get_workflow_task(created["task"]["uuid"])
        terminal_job = workflow_service.get_workflow_node_job(created["job"]["uuid"])
        assert terminal_task["status"] == "succeeded"
        assert terminal_job["status"] == "succeeded"
        assert terminal_task["cleanup_status"] == "none"
    finally:
        reset_workflow_service_for_test()


def test_local_composition_uses_one_scheduler_listener_pair_for_d1a(
    tmp_path: Path,
) -> None:
    """普通任务与设备单动作运行必须共享唯一任务调度桥及监听器。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言生产组合根只注册一对
    调度器（Scheduler）生命周期监听器，避免第二套状态机重复推进相同作业。
    """

    reset_workflow_service_for_test()
    # ``scheduler`` 是普通工作流任务与设备单动作运行共同装配的唯一调度器
    # （Scheduler）实例，监听器注册次数反映是否误建了第二座桥。
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    try:
        # 两个观察器只包裹调度器（Scheduler）的公共监听器注册接缝，不读取其
        # 内部监听器容器；调用次数直接刻画生产组合根装配出的桥数量。
        with (
            patch.object(
                scheduler,
                "add_job_pre_dispatch_listener",
                wraps=scheduler.add_job_pre_dispatch_listener,
            ) as add_pre_dispatch_listener,
            patch.object(
                scheduler,
                "add_job_finished_listener",
                wraps=scheduler.add_job_finished_listener,
            ) as add_finished_listener,
        ):
            compose_local_workflow_template_runtime(
                tmp_path,
                inventory_store=FakeInventoryStore(),
                registry=FakeRegistry(),
                scheduler=scheduler,
            )

        assert add_pre_dispatch_listener.call_count == 1
        assert add_finished_listener.call_count == 1
    finally:
        reset_workflow_service_for_test()
