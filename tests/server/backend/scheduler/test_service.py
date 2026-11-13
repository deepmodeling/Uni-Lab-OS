from __future__ import annotations

from typing import Any
from unittest.mock import Mock
import pytest

from unilabos.protocol.materials import InventoryAllocation
from unilabos.server.backend.scheduler.dag.models import DagNode, NodeState
from unilabos.server.backend.scheduler.service import BackendScheduler, allocation_arguments
from unilabos.server.services.runtime.workflow.store import WorkflowStore

# 这些模块在顶层 ``from ...execution_claim import ExecutionClaims``，而 BackendScheduler 只在
# 运行时才惰性导入它们。若首次导入发生在下方 autouse 夹具的 monkeypatch 生效期间，模块会把
# Mock 工厂永久绑定为 ExecutionClaims，污染之后所有用真实调度器的测试（CI 按
# tests/server 顺序运行时稳定复现）。在打补丁前先导入，确保它们绑定真实类。
import unilabos.server.backend.scheduler.attempt_inventory  # noqa: E402,F401
import unilabos.server.backend.scheduler.inventory_recovery  # noqa: E402,F401
import unilabos.server.backend.scheduler.physical_recovery  # noqa: E402,F401


@pytest.fixture(autouse=True)
def isolated_claim_storage(monkeypatch):
    # 本文件只测调度策略，Workflow 本身是桩；持久资源事务由集成测试覆盖。
    monkeypatch.setattr(BackendScheduler, "_release_confirmed_custody_inventory", lambda self: None)
    intervals = Mock()
    intervals.admit.return_value = True
    intervals.unadmitted_active_claims.return_value = []
    intervals.snapshot.return_value = []
    monkeypatch.setattr("unilabos.server.services.runtime.workflow.resource_intervals.ResourceIntervals", lambda _store: intervals)
    claims = Mock()
    claims.restore_requests.return_value = []
    claims.unclaimed_active_jobs.return_value = []
    # 与 ExecutionClaims 一致：reserve 之后同一 attempt 的 get 读回该 Claim，之前为 None。
    reserved: dict[str, dict[str, Any]] = {}
    claims.reserve.side_effect = lambda request, **_: reserved.setdefault(
        request.owner_uuid,
        {"claim_uuid": "unit-claim", "attempt_no": 1, "fences": {}, "request": request.model_dump(mode="json")},
    )
    claims.release_if_terminal.return_value = True
    claims.get.side_effect = reserved.get
    claims.activate_authority.return_value = "unit-epoch"
    claims.seal_dispatch.return_value = {}
    monkeypatch.setattr("unilabos.server.services.runtime.workflow.execution_claim.ExecutionClaims", lambda _store: claims)


class _Workflow:
    """WorkflowService 桩：节点运行/attempt 事实的最小实现（store 契约见 test_workflow_node_run_attempts）。

    生产 WorkflowService 继承 WorkflowStore：库存/物理恢复经 ``write_lock``、
    ``connection``、``transaction()`` 直接读 runtime.db。桩持有一个空的
    内存 runtime.db（真实表结构），表示"没有遗留派发与待结算事实"。
    """

    def __init__(self) -> None:
        self._store = WorkflowStore(":memory:")
        self.running: list[str] = []
        self.terminal: list[tuple[str, str, dict[str, Any]]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, str] = {}  # job uuid -> run uuid

    @property
    def connection(self):
        return self._store.connection

    @property
    def write_lock(self):
        return self._store.write_lock

    def transaction(self):
        return self._store.transaction()

    def add_run(self, run_uuid: str, job_uuid: str) -> None:
        self.runs[run_uuid] = {
            "uuid": run_uuid,
            "status": "pending",
            "current_job_uuid": job_uuid,
            "attempt_count": 1,
            "return_info": {},
        }
        self.jobs[job_uuid] = run_uuid

    def mark_workflow_node_job_running(self, job_uuid: str) -> None:
        self.running.append(job_uuid)
        self.runs[self.jobs[job_uuid]]["status"] = "running"

    def record_workflow_node_job_terminal(
        self, job_uuid: str, *, status: str, return_info=None, error_info=None, error_resolution=None
    ) -> dict[str, Any]:
        self.terminal.append((job_uuid, status, dict(error_resolution or {})))
        run = self.runs[self.jobs[job_uuid]]
        next_job = None
        if status == "failed" and (error_resolution or {}).get("selected_action") == "retry":
            next_uuid = f"{job_uuid}-retry"
            run["attempt_count"] += 1
            run["current_job_uuid"] = next_uuid
            run["status"] = "pending"
            self.jobs[next_uuid] = run["uuid"]
            next_job = {"uuid": next_uuid, "attempt_no": run["attempt_count"]}
        else:
            run["status"] = status
            run["return_info"] = dict(return_info or {})
        return {"job": {"uuid": job_uuid, "status": status}, "run": dict(run), "next_job": next_job}

    def close_workflow_node_run(self, run_uuid: str, *, status: str) -> dict[str, Any]:
        run = self.runs[run_uuid]
        if run["status"] not in {"succeeded", "failed", "skipped", "canceled", "timeout"}:
            run["status"] = status
        return dict(run)

    def get_workflow_node_run(self, run_uuid: str) -> dict[str, Any]:
        return dict(self.runs[run_uuid])

    def mark_workflow_node_job_decision_pending(self, job_uuid: str, report: dict[str, Any]) -> dict[str, Any]:
        run = self.runs[self.jobs[job_uuid]]
        run["status"] = "intervention_required"
        run["pending_decision"] = report.get("decision_id")
        return dict(run)


class _Materials:
    """库存权威桩：库存预留只能来自权威，带预留的派发须先在权威冻结物理执行记录。"""

    def __init__(self) -> None:
        self.prepared: list[Any] = []

    def list_inventory_reservations(self, **_filters: Any) -> list[Any]:
        return []

    def prepare_material_execution(self, value: Any) -> dict[str, Any]:
        self.prepared.append(value)
        return {"job_uuid": value.job_uuid, "claim_uuid": value.claim_uuid, "plan_hash": "plan-of-" + value.job_uuid}


class _Executor:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    def add_job_finished_listener(self, listener) -> None:
        self.listener = listener

    def dispatch(self, payload: dict[str, Any]) -> None:
        self.dispatched.append(payload)

    def cancel_task(self, _task_uuid: str) -> list[str]:
        return []


class _Runner:
    def __init__(self) -> None:
        self.terminal: list[tuple[str, NodeState]] = []

    def notify_terminal(self, job_id: str, state: NodeState) -> None:
        self.terminal.append((job_id, state))


def _spec(run_uuid: str, material_uuid: str) -> dict[str, Any]:
    return {
        "workflow_node_uuid": f"node-of-{run_uuid}",
        "base_param": {"plate": {"uuid": material_uuid}},
        "edges": [],
        "runs_by_node": {},
        "inventory_requirements": [],
        "reserved_material_uuids": [],
        "scheduler_revision": 1,
        "current_job_uuid": f"{run_uuid}-a1",
        "attempt_no": 1,
        "job_uuids": [f"{run_uuid}-a1"],
    }


def _attach(scheduler: BackendScheduler, workflow: _Workflow, task: dict[str, Any], node: DagNode, material: str = "material-x") -> _Runner:
    """把一个节点运行接入调度器簿记（等价于 run_task 里 _build_dag 之后的状态）。"""

    spec = _spec(node.node_id, material)
    workflow.add_run(node.node_id, spec["current_job_uuid"])
    scheduler._run_specs[node.node_id] = spec  # noqa: SLF001
    scheduler._run_to_task[node.node_id] = task["uuid"]  # noqa: SLF001
    runner = scheduler._runners.get(task["uuid"])  # noqa: SLF001
    if runner is None:
        runner = _Runner()
        scheduler._runners[task["uuid"]] = runner  # type: ignore[assignment]  # noqa: SLF001
    return runner  # type: ignore[return-value]


def test_one_scheduler_serializes_the_complete_action_and_material_set() -> None:
    workflow = _Workflow()
    executor = _Executor()
    scheduler = BackendScheduler(
        workflow,  # type: ignore[arg-type]
        executor,
        materials_need_lock_resolver=lambda _device, _action: ["plate"],
    )
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    first = DagNode(node_id="run-1", device_id="device-a", action="use")
    second = DagNode(node_id="run-2", device_id="device-b", action="use")
    _attach(scheduler, workflow, task, first, "material-shared")
    _attach(scheduler, workflow, task, second, "material-shared")

    scheduler._start_node(task, first)  # noqa: SLF001
    scheduler._start_node(task, second)  # noqa: SLF001

    # 资源 owner / 执行器 job_id 都是 attempt uuid，DAG 键是节点运行 uuid
    assert [item["job_id"] for item in executor.dispatched] == ["run-1-a1"]
    assert executor.dispatched[0]["node_run_uuid"] == "run-1"
    assert executor.dispatched[0]["attempt_no"] == 1
    assert executor.dispatched[0]["retry_count"] == 0
    assert scheduler.resources.request_for_owner("run-1-a1").status == "held"
    assert scheduler.resources.request_for_owner("run-2-a1").status == "waiting"
    assert scheduler.resources.request_for_owner("run-2-a1").blockers == ["run-1-a1"]

    scheduler._release_job_resources("run-1-a1", canceled=False)  # noqa: SLF001

    assert [item["job_id"] for item in executor.dispatched] == ["run-1-a1", "run-2-a1"]
    assert workflow.running == ["run-1-a1", "run-2-a1"]
    assert scheduler.resources.request_for_owner("run-2-a1").status == "held"


def test_reserved_warehouse_material_is_part_of_the_same_resource_request() -> None:
    workflow = _Workflow()
    executor = _Executor()
    materials = _Materials()
    scheduler = BackendScheduler(workflow, executor, materials_gateway=materials)  # type: ignore[arg-type]
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    node = DagNode(node_id="run-warehouse", device_id="device-a", action="use")
    _attach(scheduler, workflow, task, node)
    scheduler._run_specs["run-warehouse"].update(  # noqa: SLF001
        {
            "base_param": {},
            "reserved_material_uuids": ["allocated-material"],
            "inventory_reservation_uuid": "reservation-1",
        }
    )

    scheduler._start_node(task, node)  # noqa: SLF001

    identifiers = scheduler.resources.request_for_owner("run-warehouse-a1").identifiers
    assert [item.kind for item in identifiers] == ["action", "material"]
    assert identifiers[1].material_uuid == "allocated-material"
    assert executor.dispatched[0]["inventory_reservation_uuid"] == "reservation-1"
    # 库存在开始时按预留扣减：派发前权威按 attempt/Claim 冻结执行记录，载荷携带其身份。
    (prepared,) = materials.prepared
    assert (prepared.job_uuid, prepared.task_uuid, prepared.inventory_reservation_uuid) == (
        "run-warehouse-a1", "task-1", "reservation-1",
    )
    assert executor.dispatched[0]["material_execution"]["job_uuid"] == "run-warehouse-a1"


def test_cancelled_attempt_settles_as_canceled_not_failed() -> None:
    """执行面撤单（suc_type=cancellation）不是设备失败：attempt 落 canceled、不带 action_failed，
    节点以 CANCELLED 收敛（不 fail-fast 成失败、不进失败决策）。"""

    workflow = _Workflow()
    executor = _Executor()
    scheduler = BackendScheduler(workflow, executor)  # type: ignore[arg-type]
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    node = DagNode(node_id="run-1", device_id="device-a", action="use")
    runner = _attach(scheduler, workflow, task, node)
    scheduler._start_node(task, node)  # noqa: SLF001

    scheduler._on_executor_finished(  # noqa: SLF001
        "run-1-a1", False, None, "cancellation", {"suc": False, "suc_type": "cancellation"}
    )

    assert workflow.terminal == [("run-1-a1", "canceled", {})]
    assert workflow.runs["run-1"]["status"] == "canceled"
    assert runner.terminal == [("run-1", NodeState.CANCELLED)]

    # 普通失败仍然是 failed + FAILED
    other = DagNode(node_id="run-2", device_id="device-b", action="use")
    _attach(scheduler, workflow, task, other)
    scheduler._start_node(task, other)  # noqa: SLF001
    scheduler._on_executor_finished("run-2-a1", False, None, "normal", {})  # noqa: SLF001
    assert workflow.terminal[-1][:2] == ("run-2-a1", "failed")
    assert runner.terminal[-1] == ("run-2", NodeState.FAILED)


def test_retry_decision_redispatches_next_attempt_without_ending_the_node_run() -> None:
    """retry：store 返回 next_job → 新 attempt 重新申请资源并下发，DAG 节点不终结。"""

    workflow = _Workflow()
    executor = _Executor()
    scheduler = BackendScheduler(workflow, executor)  # type: ignore[arg-type]
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    node = DagNode(node_id="run-1", device_id="device-a", action="use")
    runner = _attach(scheduler, workflow, task, node)
    scheduler._start_node(task, node)  # noqa: SLF001
    assert [item["job_id"] for item in executor.dispatched] == ["run-1-a1"]

    scheduler._on_executor_finished(  # noqa: SLF001
        "run-1-a1",
        False,
        None,
        "normal",
        {"error_resolution": {"selected_action": "retry", "decision_id": "d-1"}},
    )

    failed_uuid, failed_status, resolution = workflow.terminal[0]
    assert (failed_uuid, failed_status) == ("run-1-a1", "failed")
    assert resolution["selected_action"] == "retry"
    # 节点运行未终结：runner 没收到终态；新 attempt 已派发，序号/retry_count 递增
    assert runner.terminal == []
    assert [item["job_id"] for item in executor.dispatched] == ["run-1-a1", "run-1-a1-retry"]
    assert executor.dispatched[1]["node_run_uuid"] == "run-1"
    assert executor.dispatched[1]["attempt_no"] == 2
    assert executor.dispatched[1]["retry_count"] == 1
    assert scheduler.resources.request_for_owner("run-1-a1").status == "released"
    assert scheduler.resources.request_for_owner("run-1-a1-retry").status == "held"
    assert scheduler._run_specs["run-1"]["job_uuids"] == ["run-1-a1", "run-1-a1-retry"]  # noqa: SLF001

    # 新 attempt 成功：按节点运行键终结 SUCCESS
    scheduler._on_executor_finished("run-1-a1-retry", True, {"ok": True}, "normal", {})  # noqa: SLF001
    assert workflow.terminal[-1][:2] == ("run-1-a1-retry", "succeeded")
    assert runner.terminal == [("run-1", NodeState.SUCCESS)]
    assert scheduler.resources.request_for_owner("run-1-a1-retry").status == "released"


def test_abort_decision_fails_the_node_run_without_retry() -> None:
    workflow = _Workflow()
    executor = _Executor()
    scheduler = BackendScheduler(workflow, executor)  # type: ignore[arg-type]
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    node = DagNode(node_id="run-1", device_id="device-a", action="use")
    runner = _attach(scheduler, workflow, task, node)
    scheduler._start_node(task, node)  # noqa: SLF001

    scheduler._on_executor_finished(  # noqa: SLF001
        "run-1-a1", False, None, "normal", {"error_resolution": {"selected_action": "abort"}}
    )

    assert workflow.terminal[0][:2] == ("run-1-a1", "failed")
    assert runner.terminal == [("run-1", NodeState.FAILED)]
    assert [item["job_id"] for item in executor.dispatched] == ["run-1-a1"]


def test_foreign_job_finish_is_ignored() -> None:
    workflow = _Workflow()
    executor = _Executor()
    scheduler = BackendScheduler(workflow, executor)  # type: ignore[arg-type]

    scheduler._on_executor_finished("execution-job-from-backend", True, None, "normal", {})  # noqa: SLF001

    assert workflow.terminal == []


def test_always_free_is_resolved_from_the_action_registry_unless_the_node_declares_it() -> None:
    """注册表 @action(always_free) 让同设备同动作并行；节点 execution_policy 显式声明优先。"""

    workflow = _Workflow()
    executor = _Executor()
    executor.resolve_action_always_free = (  # type: ignore[attr-defined]
        lambda device_id, action: (device_id, action) == ("device-a", "peek")
    )
    scheduler = BackendScheduler(workflow, executor)  # type: ignore[arg-type]
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    first = DagNode(node_id="run-1", device_id="device-a", action="peek")
    second = DagNode(node_id="run-2", device_id="device-a", action="peek")
    pinned = DagNode(node_id="run-3", device_id="device-a", action="peek")
    for node in (first, second, pinned):
        _attach(scheduler, workflow, task, node)
        scheduler._run_specs[node.node_id]["base_param"] = {}  # noqa: SLF001
    scheduler._run_specs["run-3"]["always_free_policy"] = False  # noqa: SLF001

    scheduler._start_node(task, first)  # noqa: SLF001
    scheduler._start_node(task, second)  # noqa: SLF001
    scheduler._start_node(task, pinned)  # noqa: SLF001

    # 两个注册表 always_free 的 peek 同时持有（无动作锁身份）；显式关闭的第三个申请动作锁并立即获得
    assert [item["job_id"] for item in executor.dispatched] == ["run-1-a1", "run-2-a1", "run-3-a1"]
    assert all(item["always_free"] for item in executor.dispatched[:2])
    assert executor.dispatched[2]["always_free"] is False
    assert scheduler.resources.request_for_owner("run-1-a1").identifiers == []
    assert [i.kind for i in scheduler.resources.request_for_owner("run-3-a1").identifiers] == ["action"]


def test_allocation_arguments_merge_lot_allocations_and_reference_materials() -> None:
    """权威分配 → 动作参数：material 给 ResourceSlot 引用，lot 按 key 合计并列出 lot 明细。"""

    items = [
        InventoryAllocation(key="water", kind="lot", template_uuid="t-water", lot_uuid="lot-1", quantity=30, unit="ml"),
        InventoryAllocation(key="water", kind="lot", template_uuid="t-water", lot_uuid="lot-2", quantity=10, unit="ml"),
        InventoryAllocation(key="plate", kind="material", template_uuid="t-plate", material_uuid="m-1"),
    ]

    arguments = allocation_arguments(items)

    assert arguments["water"] == {
        "key": "water",
        "kind": "lot",
        "template_uuid": "t-water",
        "unit": "ml",
        "quantity": 40.0,
        "lots": [{"lot_uuid": "lot-1", "quantity": 30.0}, {"lot_uuid": "lot-2", "quantity": 10.0}],
    }
    assert arguments["plate"] == {"key": "plate", "kind": "material", "uuid": "m-1", "template_uuid": "t-plate"}


def test_start_node_injects_resolved_inventory_into_action_args() -> None:
    """InventoryRequirement 是节点声明；派发给设备的是权威解析出的出库内容（按 key 注入同名参数）。"""

    workflow = _Workflow()
    executor = _Executor()
    scheduler = BackendScheduler(workflow, executor)  # type: ignore[arg-type]
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    node = DagNode(node_id="run-1", device_id="dispenser", action="dispense")
    _attach(scheduler, workflow, task, node)
    scheduler._run_specs["run-1"].update(  # noqa: SLF001
        {
            "base_param": {"target": "beaker"},
            "inventory_allocations": {
                "water": {"key": "water", "kind": "lot", "template_uuid": "t", "unit": "ml", "quantity": 40.0, "lots": [{"lot_uuid": "lot-1", "quantity": 40.0}]}
            },
        }
    )

    scheduler._start_node(task, node)  # noqa: SLF001

    (payload,) = executor.dispatched
    assert payload["action_args"] == {
        "target": "beaker",
        "water": {"key": "water", "kind": "lot", "template_uuid": "t", "unit": "ml", "quantity": 40.0, "lots": [{"lot_uuid": "lot-1", "quantity": 40.0}]},
    }


def test_mixed_unit_lot_arguments_share_one_unit_without_changing_reservations() -> None:
    items = [
        InventoryAllocation(key="water", kind="lot", template_uuid="water", lot_uuid="a", quantity=500, unit="uL"),
        InventoryAllocation(key="water", kind="lot", template_uuid="water", lot_uuid="b", quantity=0.5, unit="mL"),
    ]
    arguments = allocation_arguments(items)["water"]
    assert arguments["unit"] == "uL"
    assert arguments["quantity"] == 1000
    assert arguments["lots"] == [{"lot_uuid": "a", "quantity": 500}, {"lot_uuid": "b", "quantity": 500}]
    assert items[1].quantity == 0.5 and items[1].unit == "mL"


def test_dispatch_waits_for_the_execution_adapter_and_resumes_on_host_ready() -> None:
    """执行适配器未注册（ROS2 host node 晚于管理 API 就绪）时不派发，host ready 后原样派发。"""

    workflow = _Workflow()
    executor = _Executor()
    executor.ready = False
    executor.host_ready = lambda: executor.ready  # type: ignore[attr-defined]
    scheduler = BackendScheduler(workflow, executor)  # type: ignore[arg-type]
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    node = DagNode(node_id="run-1", device_id="device-a", action="use")
    _attach(scheduler, workflow, task, node)

    scheduler._start_node(task, node)  # noqa: SLF001

    # 资源已持有，但没有派发、没有标 running、没有失败
    assert scheduler.resources.request_for_owner("run-1-a1").status == "held"
    assert executor.dispatched == []
    assert workflow.running == []
    assert "run-1-a1" in scheduler._waiting_resource_jobs  # noqa: SLF001

    executor.ready = True
    scheduler.resume_pending_dispatches()

    assert [item["job_id"] for item in executor.dispatched] == ["run-1-a1"]
    assert workflow.running == ["run-1-a1"]


def test_scheduler_owns_local_jobs_and_marks_pending_decisions() -> None:
    """调度器是本机 job 的生命周期 owner：派发载荷带 origin，挂起决策时节点运行进入 intervention_required。"""

    workflow = _Workflow()
    executor = _Executor()
    scheduler = BackendScheduler(workflow, executor)  # type: ignore[arg-type]
    task = {"uuid": "task-1", "workflow_uuid": "workflow-1"}
    node = DagNode(node_id="run-1", device_id="device-a", action="use")
    _attach(scheduler, workflow, task, node)
    scheduler._start_node(task, node)  # noqa: SLF001

    assert "local_scheduler" in scheduler.job_origins
    assert executor.dispatched[0]["origin"] == "local_scheduler"

    assert scheduler.publish_job_error_decision_required(
        {"job_id": "run-1-a1", "decision_id": "d-1", "options": [{"action": "retry"}]}
    ) is True
    assert workflow.runs["run-1"]["status"] == "intervention_required"
    assert workflow.runs["run-1"]["pending_decision"] == "d-1"
    # 不是本调度器派发的 job：不认领
    assert scheduler.publish_job_error_decision_required({"job_id": "someone-else"}) is False
