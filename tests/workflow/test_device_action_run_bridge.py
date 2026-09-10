"""设备单动作运行（DeviceActionRun）复用公共任务调度桥的行为测试。"""

from __future__ import annotations

from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import CallbackDispatcher, RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.device_action_run_store import DeviceActionRunStore
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
from unilabos.workflow.task_scheduler_bridge import (
    TaskSchedulerBridge,
    TaskSchedulerBridgeError,
)

DEVICE_A_UUID = "10000000-0000-4000-8000-000000000001"
DEVICE_B_UUID = "10000000-0000-4000-8000-000000000002"
PLATE_UUID = "10000000-0000-4000-8000-000000000003"
TASK_A_UUID = "20000000-0000-4000-8000-000000000001"
TASK_B_UUID = "20000000-0000-4000-8000-000000000002"
JOB_A_UUID = "30000000-0000-4000-8000-000000000001"
JOB_B_UUID = "30000000-0000-4000-8000-000000000002"
NODE_A_UUID = "40000000-0000-4000-8000-000000000001"
NODE_B_UUID = "40000000-0000-4000-8000-000000000002"


def test_bridge_reuses_standard_job_identity_and_writes_terminal_state(
    tmp_path: Any,
) -> None:
    """公共桥必须沿用标准作业身份并投影未结算的业务终态。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言派发载荷复用既有任务/作业
    UUID，完成后任务和作业成功，且没有物理结算（PhysicalSettlement）证据时
    ``cleanup_status`` 保持 ``none``。
    """

    store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        # ``aggregate`` 是设备单动作创建事务已经提交的标准 Task/Job 聚合。
        aggregate = _insert_run(
            store,
            task_uuid=TASK_A_UUID,
            job_uuid=JOB_A_UUID,
            node_uuid=NODE_A_UUID,
            device_material_uuid=DEVICE_A_UUID,
        )

        bridge.submit(aggregate["task"])

        assert len(dispatcher.dispatched) == 1
        assert dispatcher.dispatched[0]["job_id"] == JOB_A_UUID
        assert dispatcher.dispatched[0]["task_id"] == TASK_A_UUID
        assert dispatcher.dispatched[0]["device_id"] == "device-a"
        assert store.get_task(TASK_A_UUID)["status"] == "running"
        assert store.get_job(JOB_A_UUID)["status"] == "dispatched"

        scheduler.on_job_finished(
            JOB_A_UUID,
            True,
            {"completed": True},
        )

        task = store.get_task(TASK_A_UUID)
        job = store.get_job(JOB_A_UUID)
        assert task["status"] == "succeeded"
        assert task["cleanup_status"] == "none"
        assert job["status"] == "succeeded"
        assert job["return_info"] == {"completed": True}
    finally:
        bridge.close()
        store.close()


def test_bridge_commits_standard_job_before_physical_dispatch(tmp_path: Any) -> None:
    """公共桥必须先提交标准作业派发状态再调用物理执行适配器。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言执行适配器观察到父任务为
    ``running`` 且作业为 ``dispatched``，守住持久事实先于物理效果的顺序。
    """

    store = WorkflowStore(tmp_path / "workflow_history.db")
    # ``observed_states`` 记录执行适配器被调用当刻的持久事实，用于守住
    # “先提交派发意图、后产生物理效果”的最小崩溃窗口不变量。
    observed_states: list[tuple[str, str]] = []

    def observe_dispatch(_payload: dict[str, Any]) -> None:
        """在物理派发边界读取状态；参数是旧 job_start 载荷，返回无。"""

        observed_states.append(
            (
                store.get_task(TASK_A_UUID)["status"],
                store.get_job(JOB_A_UUID)["status"],
            )
        )

    scheduler = EdgeScheduler(dispatcher=CallbackDispatcher(observe_dispatch))
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        aggregate = _insert_run(
            store,
            task_uuid=TASK_A_UUID,
            job_uuid=JOB_A_UUID,
            node_uuid=NODE_A_UUID,
            device_material_uuid=DEVICE_A_UUID,
        )

        bridge.submit(aggregate["task"])

        assert observed_states == [("running", "dispatched")]
    finally:
        bridge.close()
        store.close()


def test_bridge_commit_failure_cannot_leave_a_dispatchable_scheduler_run(
    tmp_path: Any,
) -> None:
    """派发前投影失败后本地运行不得在后续重排中偷偷执行。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言公共任务运行投影失败会
    阻止设备命令、取消尚未越过派发边界的内存运行，并保留作业 ``pending``。
    """

    store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    class _FailingPreDispatchProjection(TaskRuntimeProjection):
        """模拟派发前标准工作流写事务不可用。"""

        def project_pre_dispatch(
            self,
            *,
            task_uuid: str,
            job_uuid: str,
            resolved_param: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """拒绝派发意图投影。

            参数：``task_uuid`` 与 ``job_uuid`` 是待推进的稳定任务/作业身份。
            返回：永不返回。异常：始终抛运行时错误以模拟数据库不可用。
            """

            del task_uuid, job_uuid, resolved_param
            raise RuntimeError("workflow database unavailable")

    bridge = TaskSchedulerBridge(
        store,
        scheduler=scheduler,
        projection=_FailingPreDispatchProjection(store),
    )
    try:
        aggregate = _insert_run(
            store,
            task_uuid=TASK_A_UUID,
            job_uuid=JOB_A_UUID,
            node_uuid=NODE_A_UUID,
            device_material_uuid=DEVICE_A_UUID,
        )

        with pytest.raises(TaskSchedulerBridgeError) as captured_error:
            bridge.submit(aggregate["task"])

        assert isinstance(captured_error.value.__cause__, RuntimeError)
        assert str(captured_error.value.__cause__) == "workflow database unavailable"
        assert dispatcher.dispatched == []
        assert scheduler.reschedule() == []
        assert scheduler.workflow_snapshot(TASK_A_UUID)["state"] == "canceled"
        assert store.get_job(JOB_A_UUID)["status"] == "pending"
    finally:
        bridge.close()
        store.close()


def test_bridge_keeps_second_job_pending_until_shared_material_lock_releases(
    tmp_path: Any,
) -> None:
    """不同设备引用同一物料时第二个作业必须等待共享动作物料锁。

    参数：``tmp_path`` 隔离工作流数据库。返回无；断言冻结动作合同（Action
    Contract）让公共桥只派发首个作业，明确完成并释放内存锁后才派发第二个。
    """

    store = WorkflowStore(tmp_path / "workflow_history.db")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        first = _insert_run(
            store,
            task_uuid=TASK_A_UUID,
            job_uuid=JOB_A_UUID,
            node_uuid=NODE_A_UUID,
            device_material_uuid=DEVICE_A_UUID,
        )
        second = _insert_run(
            store,
            task_uuid=TASK_B_UUID,
            job_uuid=JOB_B_UUID,
            node_uuid=NODE_B_UUID,
            device_material_uuid=DEVICE_B_UUID,
        )

        bridge.submit(first["task"])
        bridge.submit(second["task"])

        assert [item["job_id"] for item in dispatcher.dispatched] == [JOB_A_UUID]
        assert store.get_job(JOB_B_UUID)["status"] == "pending"

        scheduler.on_job_finished(JOB_A_UUID, True, {"completed": True})

        assert [item["job_id"] for item in dispatcher.dispatched] == [
            JOB_A_UUID,
            JOB_B_UUID,
        ]
        assert store.get_job(JOB_B_UUID)["status"] == "dispatched"
    finally:
        bridge.close()
        store.close()


def _insert_run(
    store: WorkflowStore,
    *,
    task_uuid: str,
    job_uuid: str,
    node_uuid: str,
    device_material_uuid: str,
) -> dict[str, Any]:
    """写入一个标准单节点 Task/Job 聚合并返回公共持久投影。

    参数：``store`` 是隔离工作流写模型；三个 UUID 固定执行身份；
    ``device_material_uuid`` 决定已冻结的实际设备绑定。返回：供公共任务调度桥
    消费的创建结果。异常：未知设备物料身份抛 ``KeyError``；持久化错误原样传播。
    """

    # ``device_id`` 是创建设备单动作时已经冻结的具体执行器身份，调度阶段不得
    # 再访问物料解析器或设备注册表（Registry）。
    device_id = {
        DEVICE_A_UUID: "device-a",
        DEVICE_B_UUID: "device-b",
    }[device_material_uuid]
    # ``param_schema`` 是声明孔板需取得动作物料锁（Action Material Lock）的完整
    # 冻结动作合同，公共调度器只能从该计划事实解析锁身份。
    param_schema = {
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
    # ``node_snapshot`` 是 Backend-shaped 审计快照；唯一运行静态输入位于下方
    # 执行计划（ExecutionPlan）。
    node_snapshot = {
        "uuid": node_uuid,
        "workflow_node_template_uuid": "50000000-0000-4000-8000-000000000001",
        "material_uuid": device_material_uuid,
        "name": "转移物料",
        "type": "ILab",
        "param": {"plate": {"uuid": PLATE_UUID}},
        "action_name": "transfer",
        "action_type": "UniLabJsonCommand",
        "execution_policy": {},
    }
    task = {
        "uuid": task_uuid,
        "description": "桥接测试",
        "meta_data": {},
        "idempotency_key": f"bridge-{task_uuid}",
        "request_fingerprint": f"fingerprint-{task_uuid}",
        "workflow_snapshot": {
            "execution_kind": "ad_hoc_device_action",
            "material_uuid": device_material_uuid,
            "nodes": [node_snapshot],
            "node_templates": [],
        },
        "execution_plan": {
            "version": 1,
            "run_mode": "single_node",
            "target_node_uuid": node_uuid,
            "nodes": [
                {
                    "uuid": node_uuid,
                    "topological_index": 0,
                    "kind": "device_action",
                    "device_id": device_id,
                    "action_name": "transfer",
                    "action_type": "UniLabJsonCommand",
                    "material_uuid": device_material_uuid,
                    "param_schema": param_schema,
                    "param": node_snapshot["param"],
                    "execution_policy": {},
                    "inputs": [],
                    "source_handle_uuids": [],
                    "material_requirements": [],
                }
            ],
            "edges": [],
            "handles": [],
        },
        "target_node_uuid": node_uuid,
    }
    job = {
        "uuid": job_uuid,
        "workflow_node_uuid": node_uuid,
        "material_uuid": device_material_uuid,
        "execution_policy": {},
        "param": node_snapshot["param"],
    }
    return DeviceActionRunStore(store).create_or_reuse(
        task=task,
        job=job,
        idempotency_key=task["idempotency_key"],
        request_fingerprint=task["request_fingerprint"],
    )
