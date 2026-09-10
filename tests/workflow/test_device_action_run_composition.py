"""设备单动作运行（DeviceActionRun）的生产组合根测试。"""

from __future__ import annotations

from pathlib import Path

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


def test_local_runtime_submits_device_action_run_to_existing_scheduler(
    tmp_path: Path,
) -> None:
    """本地组合根必须把设备单动作运行接入既有调度器且复用标准 Job 身份。

    参数说明：``tmp_path`` 隔离工作流数据库；测试用记录派发器证明装配层完成了
    工作流规格（WorkflowSpec）转换，而不是只在手工构造的服务测试中生效。
    """

    reset_workflow_service_for_test()
    # ``dispatcher`` 记录真正越过执行适配器边界的命令；``scheduler`` 是产品现有
    # 本地调度器（EdgeScheduler），物料锁解析器在此动作无物料参数时返回空集合。
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        material_lock_resolver=(
            lambda _device_id, _action_name, _param: tuple()
        ),
    )
    try:
        workflow_service, projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=FakeInventoryStore(),
            registry=FakeRegistry(),
            scheduler=scheduler,
        )
        action_template = projection.snapshot().require_action(
            "lab.devices:Pump",
            "transfer",
        ).template

        created = workflow_service.create_device_action_run(
            material_uuid=DEVICE_MATERIAL_UUID,
            workflow_node_template_uuid=action_template["uuid"],
            param={"volume": 2.0},
            execution_policy={},
            idempotency_key="local-composition-dispatch",
            description=None,
            meta_data={},
        )

        assert created["task"]["status"] == "running"
        assert created["job"]["status"] == "dispatched"
        assert dispatcher.dispatched[0]["job_id"] == created["job"]["uuid"]
        assert dispatcher.dispatched[0]["device_id"] == "pump-01"
    finally:
        reset_workflow_service_for_test()
