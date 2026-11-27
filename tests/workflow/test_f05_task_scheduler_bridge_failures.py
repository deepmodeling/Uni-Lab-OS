"""F05.3-C 工作流任务调度桥（TaskSchedulerBridge）的保守失败合同。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.workflow.test_f05_task_scheduler_bridge import (
    JOB_UUID,
    MATERIAL_UUID,
    TASK_UUID,
    _seed_task,
)
from tests.workflow.test_f05_task_scheduler_composition import (
    WORKFLOW_UUID,
    _RecordingStore,
)
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionDecision,
    DispatchFence,
    DispatchPermit,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow import composition
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
from unilabos.workflow.task_scheduler_bridge import (
    TaskSchedulerBridge,
    TaskSchedulerBridgeError,
)


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离工作流存储（WorkflowStore）。

    参数：``tmp_path`` 是 pytest 临时目录。产生：本测试唯一工作流任务
    （WorkflowTask）写权威；测试结束后关闭连接。
    """

    # ``opened_store`` 是标准任务和作业事实的唯一 SQLite 所有者。
    opened_store = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield opened_store
    finally:
        opened_store.close()


def _assert_store_closed(store: WorkflowStore) -> None:
    """证明组合失败后工作流存储已经关闭。

    参数：``store`` 是组合根曾创建的存储实例。返回无；底层连接仍可查询时断言
    失败，关闭后的标准 ``sqlite3.ProgrammingError`` 被视为通过证据。
    """

    with pytest.raises(sqlite3.ProgrammingError):
        store.get_workflow("10000000-0000-4000-8000-000000000001")


def test_composition_closes_store_when_shared_bridge_construction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """唯一公共任务调度桥构造失败必须清理工作流存储。

    参数：``tmp_path`` 隔离 SQLite；``monkeypatch`` 注入公共桥构造故障。返回
    无；断言调度器（Scheduler）没有残留监听器，且工作流存储不泄漏。
    """

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    captured_stores: list[WorkflowStore] = []
    real_store_type = composition.WorkflowStore

    def open_store(database_path: Path | str, **options: Any) -> WorkflowStore:
        """记录组合根创建的工作流存储（WorkflowStore）。

        参数：``database_path`` 是待打开的 SQLite 数据库路径。返回：真实存储并
        保留其身份供关闭断言使用；异常：真实存储构造错误原样传播。
        """

        opened_store = real_store_type(database_path, **options)
        captured_stores.append(opened_store)
        return opened_store

    class _FailingTaskBridge:
        """模拟共享工作流任务调度桥在构造阶段失败。"""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """注入公共任务调度桥构造故障。

            参数：``_args`` 接收位置构造参数，``_kwargs`` 接收关键字构造参数，
            二者仅匹配生产签名。返回：无；异常：始终抛出 ``RuntimeError``，并由
            组合根原样传播以验证反向清理。
            """

            raise RuntimeError("公共任务调度桥构造失败")

    monkeypatch.setattr(composition, "WorkflowStore", open_store)
    monkeypatch.setattr(
        composition,
        "TaskSchedulerBridge",
        _FailingTaskBridge,
    )
    with (
        patch.object(
            scheduler,
            "bind_dispatch_admission_authority",
            wraps=scheduler.bind_dispatch_admission_authority,
        ) as bind_dispatch_admission_authority,
        patch.object(
            scheduler,
            "unbind_dispatch_admission_authority",
            wraps=scheduler.unbind_dispatch_admission_authority,
        ) as unbind_dispatch_admission_authority,
        patch.object(
            scheduler,
            "add_job_finished_listener",
            wraps=scheduler.add_job_finished_listener,
        ) as add_finished_listener,
        patch.object(
            scheduler,
            "remove_job_finished_listener",
            wraps=scheduler.remove_job_finished_listener,
        ) as remove_finished_listener,
        pytest.raises(RuntimeError, match="公共任务调度桥构造失败"),
    ):
        composition.compose_workflow_runtime(
            tmp_path,
            scheduler=scheduler,
            material_resolver=lambda _uuid: None,
        )

    assert bind_dispatch_admission_authority.call_count == 0
    assert unbind_dispatch_admission_authority.call_count == 0
    assert add_finished_listener.call_count == 0
    assert remove_finished_listener.call_count == 0
    assert len(captured_stores) == 2
    for captured_store in captured_stores:
        _assert_store_closed(captured_store)


def test_composition_closes_shared_bridge_when_service_construction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作流服务构造失败必须按反向所有权清理唯一公共桥。

    参数：``tmp_path`` 隔离 SQLite；``monkeypatch`` 注入服务构造故障。返回无；
    断言普通任务与设备单动作共享的监听器均被注销，存储被关闭。
    """

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    captured_stores: list[WorkflowStore] = []
    real_store_type = composition.WorkflowStore

    def open_store(database_path: Path | str, **options: Any) -> WorkflowStore:
        """记录组合根创建的工作流存储（WorkflowStore）。

        参数：``database_path`` 是待打开的 SQLite 数据库路径。返回：真实存储并
        保留其身份供关闭断言使用；异常：真实存储构造错误原样传播。
        """

        opened_store = real_store_type(database_path, **options)
        captured_stores.append(opened_store)
        return opened_store

    class _FailingWorkflowService:
        """模拟唯一公共桥创建后工作流服务构造失败。"""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """注入工作流服务构造故障。

            参数：``_args`` 接收位置构造参数，``_kwargs`` 接收关键字构造参数，
            二者仅匹配生产签名。返回：无；异常：始终抛出 ``RuntimeError``，并由
            组合根原样传播以验证所有权回滚。
            """

            raise RuntimeError("工作流服务构造失败")

    monkeypatch.setattr(composition, "WorkflowStore", open_store)
    monkeypatch.setattr(composition, "WorkflowService", _FailingWorkflowService)
    with (
        patch.object(
            scheduler,
            "bind_dispatch_admission_authority",
            wraps=scheduler.bind_dispatch_admission_authority,
        ) as bind_dispatch_admission_authority,
        patch.object(
            scheduler,
            "unbind_dispatch_admission_authority",
            wraps=scheduler.unbind_dispatch_admission_authority,
        ) as unbind_dispatch_admission_authority,
        patch.object(
            scheduler,
            "add_job_finished_listener",
            wraps=scheduler.add_job_finished_listener,
        ) as add_finished_listener,
        patch.object(
            scheduler,
            "remove_job_finished_listener",
            wraps=scheduler.remove_job_finished_listener,
        ) as remove_finished_listener,
        pytest.raises(RuntimeError, match="工作流服务构造失败"),
    ):
        composition.compose_workflow_runtime(
            tmp_path,
            scheduler=scheduler,
            material_resolver=lambda _uuid: None,
        )

    bind_dispatch_admission_authority.assert_called_once()
    unbind_dispatch_admission_authority.assert_called_once_with(
        bind_dispatch_admission_authority.call_args.args[0]
    )
    add_finished_listener.assert_called_once()
    remove_finished_listener.assert_called_once_with(
        add_finished_listener.call_args.args[0]
    )
    assert len(captured_stores) == 2
    for captured_store in captured_stores:
        _assert_store_closed(captured_store)


def _locked_task(store: WorkflowStore) -> dict[str, Any]:
    """持久化带动作物料锁（Action Material Lock）的待处理任务。

    参数：``store`` 是任务写权威。返回：更新后的标准任务投影。异常：数据库错误
    原样传播；物料 UUID 同时写入冻结计划和作业最终参数。
    """

    task = _seed_task(store, with_material=False)
    # ``execution_plan`` 是任务提交后不可变的运行静态输入；本测试只在首次提交前
    # 构造一个真实物料锁合同。
    execution_plan = dict(task["execution_plan"])
    planned_node = dict(execution_plan["nodes"][0])
    planned_node["param"] = {"plate": {"uuid": MATERIAL_UUID}}
    planned_node["param_schema"] = {
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
    execution_plan["nodes"] = [planned_node]
    # ``final_param`` 是派发时解析动作物料锁所使用的实际物料引用。
    final_param = {"plate": {"uuid": MATERIAL_UUID}}
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET execution_plan = ? WHERE uuid = ?",
            (json.dumps(execution_plan), TASK_UUID),
        )
        connection.execute(
            "UPDATE workflow_node_job SET param = ? WHERE uuid = ?",
            (json.dumps(final_param), JOB_UUID),
        )
    return store.get_task(TASK_UUID)


class _FailingDispatcher(RecordingDispatcher):
    """模拟派发意图持久化后执行适配器失去确认。"""

    def dispatch(self, payload: Any) -> None:
        """记录命令后抛出传输异常；参数是物理派发载荷，返回无。"""

        super().dispatch(payload)
        raise RuntimeError("执行适配器确认丢失")


class _PermitCommitFailureInventory:
    """模拟工作流已投影、库存 Permit 确认失败的跨库窗口。"""

    claim_uuid = "75000000-0000-4000-8000-000000000099"

    def __init__(self) -> None:
        """初始化生命周期记录；参数无，返回无，不访问外部状态。"""

        self.transitions: list[str] = []

    def acquire_dispatch_permit(self, request: Any) -> DispatchAdmissionDecision:
        """签发与全部锁资源一一对应的 prepared Permit。"""

        return DispatchAdmissionDecision(
            permit=DispatchPermit(
                effect_uuid=request.effect_uuid,
                claim_uuid=self.claim_uuid,
                task_uuid=request.task_uuid,
                job_uuid=request.job_uuid,
                attempt=request.attempt,
                parameter_hash=request.parameter_hash,
                expected_change_set=request.expected_change_set,
                fences=tuple(
                    DispatchFence(resource.lock_key, index)
                    for index, resource in enumerate(request.resources, start=1)
                ),
            )
        )

    def transition_dispatch_permit(
        self,
        claim_uuid: str,
        *,
        target_state: str,
    ) -> None:
        """记录转换，并仅在 reserved 确认点注入存储故障。"""

        assert claim_uuid == self.claim_uuid
        self.transitions.append(target_state)
        if target_state == "reserved":
            raise RuntimeError("库存 Permit 提交确认丢失")

    def release_unprojected_dispatch_permits(
        self,
        *,
        known_claim_uuids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """满足恢复窄接口；本用例不执行启动恢复。"""

        del known_claim_uuids
        return ()


def test_inventory_permit_commit_failure_freezes_projected_claim(
    store: WorkflowStore,
) -> None:
    """Permit 确认失败时必须冻结双库 Claim，且不得派发设备。"""

    task = _seed_task(store, with_material=False)
    inventory = _PermitCommitFailureInventory()
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        station_resources=inventory,  # type: ignore[arg-type]
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        with pytest.raises(TaskSchedulerBridgeError) as captured_error:
            bridge.submit(task)

        assert isinstance(captured_error.value.__cause__, RuntimeError)
        assert inventory.transitions == ["reserved", "uncertain"]
        assert "released" not in inventory.transitions
        assert dispatcher.dispatched == []
        assert store.get_job(JOB_UUID)["status"] == "running"
        assert store.get_job(JOB_UUID)["uncertainty_reason"] == (
            "inventory_permit_commit_unknown"
        )
        assert store.get_task(TASK_UUID)["control_status"] == (
            "waiting_reconciliation"
        )
        assert store.get_task(TASK_UUID)["cleanup_status"] == "requires_attention"
        assert {
            lease["state"]
            for lease in TaskRuntimeProjection(store).list_execution_locks(JOB_UUID)
        } == {"uncertain"}
    finally:
        bridge.close()


@pytest.mark.parametrize("serial_queue", [False, True])
def test_dispatcher_failure_preserves_inflight_lock_and_late_result(
    store: WorkflowStore, serial_queue: bool,
) -> None:
    """派发器异常后必须保守保留在途作业、物料锁和结果路由。

    参数：``store`` 是隔离任务权威。返回无；断言不调用取消，迟到明确结果仍可
    记录成功并在完成后释放内存锁，但不会替代用户的错误决定。
    """

    task = _locked_task(store)
    dispatcher = _FailingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, serial_queue=serial_queue)
    cancel_calls: list[str] = []
    original_cancel = scheduler.cancel_workflow

    def record_cancel(task_uuid: str) -> bool:
        """记录意外取消；参数是任务身份，返回真实取消结果。"""

        cancel_calls.append(task_uuid)
        return original_cancel(task_uuid)

    scheduler.cancel_workflow = record_cancel  # type: ignore[method-assign]
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        with pytest.raises(TaskSchedulerBridgeError) as captured_error:
            bridge.submit(task)

        assert isinstance(captured_error.value.__cause__, RuntimeError)
        assert cancel_calls == []
        assert store.get_task(TASK_UUID)["status"] == "running"
        assert store.get_job(JOB_UUID)["status"] == "running"
        assert store.get_job(JOB_UUID)["uncertainty_reason"] == (
            "local_dispatch_acceptance_unknown"
        )
        assert store.get_task(TASK_UUID)["control_status"] == "error_waiting"
        assert store.get_task(TASK_UUID)["cleanup_status"] == "requires_attention"
        assert {
            lease["state"]
            for lease in TaskRuntimeProjection(store).list_execution_locks(JOB_UUID)
        } == {"uncertain"}
        assert scheduler.snapshot()["inflight_jobs"][JOB_UUID]["resource_locks"] == [
            "/devices/reactor-a",
            f"material/{MATERIAL_UUID}/exclusive",
        ]

        scheduler.on_job_finished(JOB_UUID, True, {"late": True})

        assert store.get_task(TASK_UUID)["status"] == "running"
        assert store.get_task(TASK_UUID)["control_status"] == "error_waiting"
        assert bridge.error_control.awaits_decision(JOB_UUID)
        assert store.get_job(JOB_UUID)["status"] == "succeeded"
        assert store.get_job(JOB_UUID)["return_info"] == {"late": True}
        assert scheduler.snapshot()["inflight_jobs"] == {}
    finally:
        bridge.close()


class _FailOnceProjection:
    """在首次完成回写时模拟 SQLite 瞬态故障。"""

    def __init__(self, store: WorkflowStore) -> None:
        """绑定真实任务运行投影；参数是标准任务写权威，返回无。"""

        self._delegate = TaskRuntimeProjection(store)
        self._remaining_failures = 1

    def project_submission(
        self, task_uuid: str, scheduler_state: str
    ) -> dict[str, Any]:
        """委托首次提交投影；参数是任务身份和内部状态，返回标准聚合。"""

        return self._delegate.project_submission(task_uuid, scheduler_state)

    def project_pre_dispatch(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        resolved_param: Mapping[str, Any],
        execution_locks: list[Mapping[str, Any]],
        device_tenancy: Mapping[str, Any] | None = None,
        actual_executor: Mapping[str, Any] | None = None,
        dispatch_permit: Mapping[str, Any] | None = None,
        dispatch_payload: Mapping[str, Any] | None = None,
        max_active_tasks: int = 500,
        max_tasks_per_workflow: int = 100,
        max_in_flight_jobs: int = 100,
        aging_interval_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """委托派发前投影；参数含最终实参、资源与实际执行器，返回标准聚合。"""

        return self._delegate.project_pre_dispatch(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            resolved_param=resolved_param,
            execution_locks=execution_locks,
            device_tenancy=device_tenancy,
            actual_executor=actual_executor,
            dispatch_permit=dispatch_permit,
            dispatch_payload=dispatch_payload,
            max_active_tasks=max_active_tasks,
            max_tasks_per_workflow=max_tasks_per_workflow,
            max_in_flight_jobs=max_in_flight_jobs,
            aging_interval_seconds=aging_interval_seconds,
        )

    def project_dispatch_accepted(self, job_uuid: str) -> dict[str, Any]:
        """委托执行接受投影；参数是稳定作业身份。"""

        return self._delegate.project_dispatch_accepted(job_uuid)

    def get_execution_claim(self, job_uuid: str) -> dict[str, Any] | None:
        """委托读取持久 Claim/Fence；参数是稳定作业身份。"""

        return self._delegate.get_execution_claim(job_uuid)

    def project_execution_attention(
        self,
        job_uuid: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """委托物理对账等待投影；参数是作业身份和稳定原因。"""

        return self._delegate.project_execution_attention(job_uuid, reason=reason)

    def project_job_finished(self, **values: Any) -> dict[str, Any]:
        """首次抛 SQLite 瞬态错误，随后委托同一完成事实。

        参数：``values`` 是完成投影原始字段。返回：标准终态聚合。异常：第一次
        调用抛 ``sqlite3.OperationalError``。
        """

        if self._remaining_failures:
            self._remaining_failures -= 1
            raise sqlite3.OperationalError("database is temporarily busy")
        return self._delegate.project_job_finished(**values)


def test_completion_projection_failure_keeps_inflight_for_delivery_replay(
    store: WorkflowStore,
) -> None:
    """完成投影瞬态失败必须允许同一结果进行投递重放（DeliveryReplay）。

    参数：``store`` 是隔离任务权威。返回无；断言首次失败向上抛且在途作业与物料
    锁不释放，第二次相同结果成功后才完成本地与标准状态清理。
    """

    task = _locked_task(store)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    projection = _FailOnceProjection(store)
    bridge = TaskSchedulerBridge(
        store,
        scheduler=scheduler,
        projection=projection,  # type: ignore[arg-type]
    )
    try:
        bridge.submit(task)
        with pytest.raises(sqlite3.OperationalError):
            scheduler.on_job_finished(JOB_UUID, True, {"receipt": "same"})

        assert store.get_job(JOB_UUID)["status"] == "running"
        assert JOB_UUID in scheduler.snapshot()["inflight_jobs"]
        assert scheduler.snapshot()["inflight_jobs"][JOB_UUID]["resource_locks"]

        scheduler.on_job_finished(JOB_UUID, True, {"receipt": "same"})

        assert store.get_job(JOB_UUID)["status"] == "succeeded"
        assert scheduler.snapshot()["inflight_jobs"] == {}
    finally:
        bridge.close()


def test_workflow_service_maps_bridge_failure_to_stable_internal_error(
    store: WorkflowStore,
) -> None:
    """工作流服务必须把桥接实现错误映射为稳定错误封装。

    参数：``store`` 提供支持发布查询的真实定义存储。返回无；断言桥接编译或
    派发故障不会把内部异常类型泄漏到 HTTP 边界，而是使用既有 ``internal_error`` 合同。
    """

    events: list[str] = []
    recording_store = _RecordingStore(events)
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="recording workflow",
        tags=[],
        description=None,
        meta_data={},
    )

    class _FailingBridge:
        """模拟任务编译或派发失败的公共任务桥。"""

        def submit(self, _task: Mapping[str, Any]) -> dict[str, Any]:
            """拒绝任务；参数是已持久任务，异常模拟内部桥接故障。"""

            events.append("submit")
            raise TaskSchedulerBridgeError("内部调度失败")

        def close(self) -> None:
            """满足幂等关闭合同；参数无，返回无。"""

    service = WorkflowService(  # type: ignore[arg-type]
        recording_store,
        definition_store=store,
        task_scheduler_bridge=_FailingBridge(),
    )
    try:
        with pytest.raises(WorkflowError) as captured_error:
            service.create_workflow_task(
                workflow_uuid=WORKFLOW_UUID,
                run_mode="normal",
                target_node_uuid=None,
                input_value={},
                description=None,
                meta_data={},
            )
    finally:
        service.close()

    assert events == ["persist", "submit"]
    assert recording_store.closed is True
    assert captured_error.value.code == "internal_error"
    assert captured_error.value.status == 500
