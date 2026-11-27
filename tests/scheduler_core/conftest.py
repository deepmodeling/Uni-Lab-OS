"""调度核心独立套件的系统边界替身与持久任务工厂。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from unilabos.app.scheduler.device_target import (
    DeviceTargetUnavailable,
    ResolvedDeviceTarget,
)
from unilabos.app.scheduler.dispatch import CancelDispatchState, DispatchPayload
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import PreparedTaskInput
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


class ManualTimer:
    """由 :class:`ManualClock` 驱动、不会创建线程的计时器。"""

    def __init__(
        self,
        clock: "ManualClock",
        delay: float,
        callback: Callable[..., None],
        *,
        kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self._clock = clock
        self._deadline = clock.time() + delay
        self._callback = callback
        self._kwargs = dict(kwargs or {})
        self._canceled = False
        self.daemon = False

    def start(self) -> None:
        """把计时器登记到手动时钟。"""

        self._clock.register(self)

    def cancel(self) -> None:
        """阻止尚未触发的回调。"""

        self._canceled = True

    @property
    def due(self) -> bool:
        """返回计时器是否已到期且仍有效。"""

        return not self._canceled and self._deadline <= self._clock.time()

    def fire(self) -> None:
        """同步执行一次到期回调。"""

        if self._canceled:
            return
        self._canceled = True
        self._callback(**self._kwargs)


class ManualClock:
    """同时提供 epoch 秒与 UTC datetime 的确定性测试时钟。"""

    def __init__(self, initial: float = 1_800_000_000.0) -> None:
        self._value = initial
        self._timers: list[ManualTimer] = []

    def time(self) -> float:
        """返回当前测试时间的 epoch 秒。"""

        return self._value

    def now(self) -> datetime:
        """返回当前测试时间的 UTC datetime。"""

        return datetime.fromtimestamp(self._value, tz=timezone.utc)

    def timer(
        self,
        delay: float,
        callback: Callable[..., None],
        *,
        kwargs: Mapping[str, Any] | None = None,
    ) -> ManualTimer:
        """创建一个必须由本时钟推进的计时器。"""

        return ManualTimer(self, delay, callback, kwargs=kwargs)

    def register(self, timer: ManualTimer) -> None:
        """登记已启动计时器。"""

        self._timers.append(timer)

    def advance(self, seconds: float) -> None:
        """推进时间并同步触发所有到期回调。"""

        self._value += seconds
        while True:
            due = [timer for timer in self._timers if timer.due]
            if not due:
                return
            for timer in due:
                self._timers.remove(timer)
                timer.fire()


def stable_uuid(value: str) -> str:
    """把易读测试身份转换为稳定 UUID。"""

    return str(uuid5(NAMESPACE_URL, f"scheduler-core:{value}"))


def persist_task(
    store: WorkflowStore,
    *,
    task_name: str,
    devices: Sequence[str],
    device_material_uuids: Sequence[str] | None = None,
    material_requirements_by_node: Sequence[Sequence[Mapping[str, Any]]] | None = None,
    edges: Sequence[tuple[int, int]] = (),
    run_mode: str = "normal",
    priority: str = "normal",
) -> dict[str, Any]:
    """通过 WorkflowStore 公共入口持久化冻结 Task、Plan 和 Jobs。"""

    workflow_uuid = stable_uuid(f"workflow:{task_name}")
    task_uuid = stable_uuid(f"task:{task_name}")
    node_uuids = [
        stable_uuid(f"node:{task_name}:{index}") for index in range(len(devices))
    ]
    jobs = [
        {
            "uuid": stable_uuid(f"job:{task_name}:{index}"),
            "workflow_node_uuid": node_uuid,
            "topological_index": index,
            "executor_kind": "device_action",
            "execution_policy": {},
            "execution_timeout_seconds": 0,
            "param": {},
        }
        for index, node_uuid in enumerate(node_uuids)
    ]
    material_uuids = list(device_material_uuids or ("" for _ in devices))
    if len(material_uuids) != len(devices):
        raise ValueError("device_material_uuids 必须与 devices 等长")
    requirements = list(material_requirements_by_node or (() for _ in devices))
    if len(requirements) != len(devices):
        raise ValueError("material_requirements_by_node 必须与 devices 等长")
    plan = {
        "version": 1,
        "run_mode": run_mode,
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": node_uuid,
                "kind": "device_action",
                "device_id": device_id,
                "action_name": "run",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        }
                    },
                    "additionalProperties": False,
                },
                "material_requirements": [dict(item) for item in node_requirements],
            }
            | {"material_uuid": material_uuid}
            for node_uuid, device_id, material_uuid, node_requirements in zip(
                node_uuids,
                devices,
                material_uuids,
                requirements,
            )
        ],
        "handles": [],
        "edges": [
            {
                "uuid": stable_uuid(f"edge:{task_name}:{source}:{target}"),
                "source_node_uuid": node_uuids[source],
                "target_node_uuid": node_uuids[target],
                "source_handle_uuid": "",
                "target_handle_uuid": "",
                "dependency_only": True,
                "source_data_key": "",
                "target_data_key": "",
                "source_type": "",
                "target_type": "",
            }
            for source, target in edges
        ],
    }
    prepared = PreparedTaskInput(
        workflow_snapshot={},
        resolved_input={},
        execution_plan=plan,
        jobs=jobs,
    )
    return store.create_task_with_jobs(
        workflow_uuid=workflow_uuid,
        task_uuid=task_uuid,
        run_mode=run_mode,
        target_node_uuid=None,
        description=None,
        meta_data={},
        priority=priority,
        plan_builder=lambda _graph: prepared,
        applied_graph={},
    )


def persist_frozen_task(
    store: WorkflowStore,
    *,
    task_name: str,
    execution_plan: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    resolved_input: Mapping[str, Any] | None = None,
    priority: str = "normal",
) -> dict[str, Any]:
    """通过公共入口持久化调用方提供的静态冻结执行计划。"""

    task_uuid = stable_uuid(f"task:{task_name}")
    prepared = PreparedTaskInput(
        workflow_snapshot={},
        resolved_input=dict(resolved_input or {}),
        execution_plan=dict(execution_plan),
        jobs=[dict(job) for job in jobs],
    )
    return store.create_task_with_jobs(
        workflow_uuid=stable_uuid(f"workflow:{task_name}"),
        task_uuid=task_uuid,
        run_mode=str(execution_plan.get("run_mode") or "normal"),
        target_node_uuid=None,
        description=None,
        meta_data={},
        priority=priority,
        plan_builder=lambda _graph: prepared,
        applied_graph={},
    )


def seconds(value: float) -> timedelta:
    """构造测试可读的秒级时间差。"""

    return timedelta(seconds=value)


class FakeDispatcher:
    """只记录越过物理边界的命令，并可配置取消即时结果。"""

    def __init__(self) -> None:
        self.dispatched: list[DispatchPayload] = []
        self.cancel_requests: list[str] = []
        self.cancel_state = CancelDispatchState.REQUESTED
        self.cancel_callback: Callable[[bool], None] | None = None
        self.dispatch_error: BaseException | None = None

    def dispatch(self, payload: DispatchPayload) -> None:
        """记录一份与调用方隔离的派发载荷。"""

        self.dispatched.append(DispatchPayload(payload))
        if self.dispatch_error is not None:
            raise self.dispatch_error

    def cancel(
        self,
        job_id: str,
        on_accepted: Callable[[bool], None],
    ) -> CancelDispatchState:
        """记录取消请求，并按测试配置返回即时状态。"""

        self.cancel_requests.append(job_id)
        self.cancel_callback = on_accepted
        return self.cancel_state

    def acknowledge_cancel(self, accepted: bool) -> None:
        """同步回放一次执行器取消 ACK。"""

        if self.cancel_callback is None:
            raise RuntimeError("没有待确认的取消请求")
        callback = self.cancel_callback
        self.cancel_callback = None
        callback(accepted)


@dataclass
class CoreRuntime:
    """单 Runtime 生命周期内的调度核心组合根。"""

    clock: ManualClock
    workflow_store: WorkflowStore
    inventory_store: InventoryStore
    inventory: InventoryService
    dispatcher: Any
    scheduler: EdgeScheduler
    bridge: TaskSchedulerBridge
    device_materials: dict[str, str]

    def restart_scheduler(self) -> None:
        """重建控制器和 DAG，保留两个数据库与仍在运行的测试设备。"""
        serial_queue = self.scheduler.serial_queue_enabled
        self.bridge.close()
        self.scheduler = EdgeScheduler(
            dispatcher=self.dispatcher, inventory=self.inventory,
            station_resources=self.inventory.station_resources,
            device_target_resolver=lambda selector, _action, _busy: ResolvedDeviceTarget(
                selector["local_device_id"], self.device_materials[selector["local_device_id"]]),
            clock=self.clock.time,
            serial_queue=serial_queue,
        )
        self.bridge = TaskSchedulerBridge(self.workflow_store, scheduler=self.scheduler,
            clock=self.clock.now, timer_factory=self.clock.timer)
        self.bridge.recover_active_tasks()

    def release_error_resources_and_resume(self, task_uuid: str, *, command_uuid: str, reason: str) -> dict[str, Any]:
        """从公开占用快照精确释放选中任务，再由测试操作者明确恢复工站。"""
        control = self.bridge.error_control
        snapshot = control.snapshot()
        errors = [item for item in snapshot["errors"] if item["source_task_uuid"] == task_uuid]
        if not errors:
            return self.bridge.unlock_resources(task_uuid, command_uuid=command_uuid, reason=reason)
        occupancies = [item for item in control.occupancies() if item["workflow_task_uuid"] == task_uuid]
        if occupancies:
            result = self.bridge.apply_auxiliary(f"resource:{errors[0]['decision_id']}", {
                "decision_id": errors[0]["decision_id"], "expected_station_version": snapshot["station_version"],
                "occupancy_ids": [item["uuid"] for item in occupancies],
                "expected_versions": {item["uuid"]: item["version"] for item in occupancies},
                "operation": "release", "reason": reason,
            }, idempotency_key=command_uuid, operator_id="test-operator")
            assert result["status"] == "APPLIED", result
        snapshot = control.snapshot()
        result = control.resume({"expected_station_version": snapshot["station_version"],
            "observed_error_epoch": snapshot["error_epoch"], "operator_confirmed": True,
            "confirm_snapshot_id": snapshot["snapshot_id"]}, idempotency_key=command_uuid + ":resume", operator_id="test-operator")
        self.bridge.reschedule()
        return result

    def close(self) -> None:
        """按组合根逆序关闭桥与两个 SQLite Store。"""

        self.bridge.close()
        self.workflow_store.close()
        self.inventory_store.close()

    def persist(
        self,
        *,
        task_name: str,
        devices: Sequence[str],
        edges: Sequence[tuple[int, int]] = (),
        run_mode: str = "normal",
        priority: str = "normal",
    ) -> dict[str, Any]:
        """持久化一个绑定虚拟设备的冻结任务。"""

        return persist_task(
            self.workflow_store,
            task_name=task_name,
            devices=devices,
            device_material_uuids=[self.device_materials[item] for item in devices],
            edges=edges,
            run_mode=run_mode,
            priority=priority,
        )

    def submit(self, **values: Any) -> dict[str, Any]:
        """持久化并通过公共调度桥提交一个任务。"""

        task = self.persist(**values)
        return self.bridge.submit(task)

    def submit_manual(
        self,
        *,
        task_name: str,
        device_id: str = "reactor-a",
        timeout_seconds: int = 3600,
    ) -> tuple[dict[str, Any], str]:
        """提交一个包装真实设备动作的人工确认节点。"""

        node_uuid = stable_uuid(f"manual-node:{task_name}")
        job_uuid = stable_uuid(f"manual-job:{task_name}")
        node = {
            "uuid": node_uuid,
            "parent_uuid": None,
            "kind": "manual_confirm",
            "device_id": device_id,
            "material_uuid": self.device_materials[device_id],
            "action_name": "run",
            "action_type": "UniLabJsonCommand",
            "param": {"temperature": 25},
            "param_schema": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "object",
                        "properties": {"temperature": {"type": "number"}},
                        "required": ["temperature"],
                        "additionalProperties": False,
                    }
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
            "manual_confirmation": {"timeout_seconds": timeout_seconds},
            "execution_policy": {},
            "action_resource_contract": {},
            "material_requirements": [],
        }
        job = {
            "uuid": job_uuid,
            "workflow_node_uuid": node_uuid,
            "topological_index": 0,
            "executor_kind": "manual_confirm",
            "execution_policy": {},
            "execution_timeout_seconds": 0,
            "param": {},
        }
        aggregate = self.submit_frozen(
            task_name=task_name,
            execution_plan={
                "version": 1,
                "run_mode": "normal",
                "target_node_uuid": None,
                "nodes": [node],
                "handles": [],
                "edges": [],
            },
            jobs=[job],
        )
        return aggregate, job_uuid

    def submit_frozen(
        self,
        *,
        task_name: str,
        execution_plan: Mapping[str, Any],
        jobs: Sequence[Mapping[str, Any]],
        resolved_input: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """持久化并提交一份静态控制流执行计划。"""

        task = persist_frozen_task(
            self.workflow_store,
            task_name=task_name,
            execution_plan=execution_plan,
            jobs=jobs,
            resolved_input=resolved_input,
        )
        return self.bridge.submit(task)


def build_core_runtime(
    root: Path,
    *,
    device_ids: Sequence[str] = (
        "reactor-a",
        "reactor-b",
        "robot-a",
        "warehouse-a",
    ),
    max_in_flight_jobs: int = 100,
    max_active_tasks: int = 500,
    max_tasks_per_workflow: int = 100,
    device_available: Callable[[str], bool] | None = None,
    execution_backend: Any = None,
    serial_queue: bool = False,
) -> CoreRuntime:
    """装配可配置容量的双 SQLite 调度核心组合根。"""

    root.mkdir(parents=True, exist_ok=True)
    clock = ManualClock()
    workflow_store = WorkflowStore(
        root / "workflow.db",
        persist_workflow_definitions=False,
    )
    inventory_store = InventoryStore(str(root / "inventory.db"))
    inventory_backend = BackendResourceService(inventory_store)
    device_template = inventory_backend.sync_resource_templates(
        [
            {
                "id": "scheduler-core.device",
                "display_name": "调度核心虚拟设备",
                "registry_type": "resource",
                "class": {},
            }
        ]
    )["templates"][0]
    device_materials: dict[str, str] = {}
    for device_id in device_ids:
        material = inventory_backend.create_material(
            {
                "resource_template_uuid": device_template["uuid"],
                "barcode": f"CORE-{device_id}",
                "name": device_id,
            }
        )
        device_materials[device_id] = material["uuid"]
    with inventory_store.transaction() as connection:
        connection.execute(
            "UPDATE material SET type='device' WHERE uuid IN "
            f"({','.join('?' for _ in device_materials)})",
            tuple(device_materials.values()),
        )

    inventory = InventoryService(inventory_store, time_fn=clock.time)
    dispatcher = execution_backend if execution_backend is not None else FakeDispatcher()

    def resolve_device(
        selector: Mapping[str, Any],
        _action_name: str,
        _busy_keys: set[str],
    ) -> ResolvedDeviceTarget:
        """把冻结本地设备身份解析为同一库存中的 Device Material。"""

        local_id = str(selector.get("local_device_id") or "")
        if device_available is not None and not device_available(local_id):
            raise DeviceTargetUnavailable("device_offline", "虚拟设备当前离线")
        material_uuid = device_materials[local_id]
        frozen_material = str(selector.get("material_uuid") or "")
        if frozen_material and frozen_material != material_uuid:
            raise ValueError("冻结设备物料身份与虚拟注册不一致")
        return ResolvedDeviceTarget(local_id, material_uuid)

    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        inventory=inventory,
        station_resources=inventory.station_resources,
        device_target_resolver=resolve_device,
        max_in_flight_jobs=max_in_flight_jobs,
        max_active_tasks=max_active_tasks,
        max_tasks_per_workflow=max_tasks_per_workflow,
        clock=clock.time,
        serial_queue=serial_queue,
    )
    bridge = TaskSchedulerBridge(
        workflow_store,
        scheduler=scheduler,
        clock=clock.now,
        timer_factory=clock.timer,
    )
    return CoreRuntime(
        clock=clock,
        workflow_store=workflow_store,
        inventory_store=inventory_store,
        inventory=inventory,
        dispatcher=dispatcher,
        scheduler=scheduler,
        bridge=bridge,
        device_materials=device_materials,
    )


@pytest.fixture()
def core_runtime(tmp_path: Path) -> CoreRuntime:
    """提供默认容量的独立核心并在用例后关闭全部本地资源。"""

    runtime = build_core_runtime(tmp_path)
    try:
        yield runtime
    finally:
        runtime.close()
