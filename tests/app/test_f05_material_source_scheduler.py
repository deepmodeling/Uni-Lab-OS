"""F05.4-C14 物料来源准入与本地调度顺序合同。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import InsufficientStock
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import (
    TaskSchedulerBridge,
    TaskSchedulerBridgeError,
)

WORKFLOW_UUID = "12000000-0000-4000-8000-000000000001"
TASK_UUID = "22000000-0000-4000-8000-000000000001"
SOURCE_NODE_UUID = "32000000-0000-4000-8000-000000000001"
ACTION_NODE_UUID = "32000000-0000-4000-8000-000000000002"
SOURCE_JOB_UUID = "42000000-0000-4000-8000-000000000001"
ACTION_JOB_UUID = "42000000-0000-4000-8000-000000000002"
MATERIAL_UUID = "52000000-0000-4000-8000-000000000001"
TEMPLATE_UUID = "62000000-0000-4000-8000-000000000001"
_CREATED_AT = "2026-08-06T00:00:00Z"


class _ToggleInventory:
    """模拟可由补料改变结果的短期库存权威（Inventory Authority）。"""

    def __init__(self, *, available: bool) -> None:
        """设置固定物料是否可一次性预留。

        参数：``available`` 为假时准入受阻。返回无。异常：无；调用历史用于证明
        准入重试（AdmissionRetry）复用同一任务和来源身份。
        """

        self.available = available
        self.admission_calls: list[tuple[str, list[Any]]] = []
        self.release_calls: list[tuple[str, str]] = []

    def admit_task_materials(
        self,
        workflow_uuid: str,
        requests: list[Any],
        quantity_allocations: list[Any] | tuple[Any, ...],
    ) -> dict[str, Any]:
        """模拟整组物料来源的策略化单事务准入。

        参数：``workflow_uuid`` 是工作流任务（WorkflowTask）身份；
        ``requests`` 是按来源节点冻结的选择器与保管策略。返回无。异常：不可用时抛
        ``InsufficientStock``，且不形成部分预留。
        """

        assert not quantity_allocations
        self.admission_calls.append((workflow_uuid, requests))
        if not self.available:
            raise InsufficientStock("测试固定物料已被其他任务预留")
        return {
            "workflow_id": workflow_uuid,
            "reserved_nodes": [
                request.node_id
                for request in requests
                if request.custody_policy == "task_exclusive"
            ],
            "allocations": {
                request.node_id: [MATERIAL_UUID] for request in requests
            },
            "allocation_sites": {},
        }

    def describe_wait_resources(
        self,
        resources: list[dict[str, str]],
    ) -> tuple[dict[str, str], ...]:
        """模拟库存权威把稳定物料身份解析为前端可读名称。"""

        return tuple(
            {
                **resource,
                "material_name": "测试固定物料",
            }
            for resource in resources
        )

    def consume_reservation(self, workflow_uuid: str, node_uuid: str) -> None:
        """保留既有调度器调用面；参数是任务和节点身份，返回无。"""

    def quarantine_reservation(self, workflow_uuid: str, node_uuid: str) -> None:
        """保留既有失败隔离调用面；参数是任务和节点身份，返回无。"""

    def release_workflow(self, workflow_uuid: str, *, reason: str) -> None:
        """记录终态释放；参数是任务身份和原因，返回无；异常：无。"""

        self.release_calls.append((workflow_uuid, reason))


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离工作流存储（WorkflowStore）。

    参数：``tmp_path`` 是测试临时目录。产生：唯一任务/作业权威；结束时关闭。
    """

    opened_store = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield opened_store
    finally:
        opened_store.close()


def _source_plan_node(
    *,
    automatic: bool = False,
    custody_policy: str = "task_exclusive",
) -> dict[str, Any]:
    """构造 existing 物料来源的协调器计划节点。

    参数：``automatic`` 决定由库存选择或固定实例；``custody_policy`` 决定任务
    独占或共享来源。返回：包含冻结选择器和唯一实例需求的计划对象。异常：无。
    """

    return {
        "uuid": SOURCE_NODE_UUID,
        "topological_index": 0,
        "kind": "material_source",
        "param": {
            "mode": "existing",
            "resource_template_uuid": TEMPLATE_UUID,
            "material_uuid": None if automatic else MATERIAL_UUID,
            "mount": {"uuid": "72000000-0000-4000-8000-000000000001"},
            "site": None,
            "slot_range": None,
            "flow_role": "primary_sample",
            "custody_policy": custody_policy,
        },
        "execution_policy": {},
        "inputs": [],
        "source_handle_uuids": [],
        "material_requirements": (
            [
                {
                    "template_id": TEMPLATE_UUID,
                    "mount_uuid": "72000000-0000-4000-8000-000000000001",
                    "site_uuid": "",
                    "slot_uuids": [],
                }
            ]
            if automatic
            else [
                {
                    "template_id": TEMPLATE_UUID,
                    "instance_uuid": MATERIAL_UUID,
                }
            ]
        ),
        "material_binding_targets": (
            [
                {
                    "workflow_node_uuid": ACTION_NODE_UUID,
                    "param_key": "plate",
                }
            ]
            if automatic
            else []
        ),
    }


def _action_plan_node(*, automatic: bool = False) -> dict[str, Any]:
    """构造来源之后唯一普通设备动作计划节点。

    参数：无。返回：带固定执行器和完整动作合同的计划对象。异常：无。
    """

    return {
        "uuid": ACTION_NODE_UUID,
        "topological_index": 1,
        "kind": "device_action",
        "device_id": "reactor-a",
        "action_name": "distribute",
        "action_type": "UniLabJsonCommand",
        "param": {} if automatic else {"plate": {"uuid": MATERIAL_UUID}},
        "param_schema": {
            "type": "object",
            "properties": {"goal": {"type": "object", "additionalProperties": True}},
            "required": ["goal"],
        },
        "execution_policy": {},
        "inputs": [],
        "source_handle_uuids": [],
    }


def _seed_task(
    store: WorkflowStore,
    *,
    with_action: bool,
    automatic: bool = False,
    custody_policy: str = "task_exclusive",
) -> dict[str, Any]:
    """持久化含来源协调责任的待处理工作流任务。

    参数：``store`` 是唯一写权威；``with_action`` 决定准入后是否需要物理派发；
    ``automatic`` 决定库存自动选择；``custody_policy`` 决定独占或共享。返回：
    标准任务投影。异常：数据库约束原样传播。
    """

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="F05.4-C14 来源准入",
        tags=[],
        description=None,
        meta_data={},
    )
    nodes = [
        _source_plan_node(
            automatic=automatic,
            custody_policy=custody_policy,
        )
    ]
    if with_action:
        nodes.append(_action_plan_node(automatic=automatic))
    execution_plan = {
        "version": 1,
        "run_mode": "normal",
        "nodes": nodes,
        "handles": [],
        "edges": [],
    }
    jobs = [
        (SOURCE_JOB_UUID, SOURCE_NODE_UUID, 0, "material_source", nodes[0]["param"])
    ]
    if with_action:
        jobs.append(
            (ACTION_JOB_UUID, ACTION_NODE_UUID, 1, "device_action", nodes[1]["param"])
        )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', ?,
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (
                TASK_UUID,
                _CREATED_AT,
                _CREATED_AT,
                WORKFLOW_UUID,
                json.dumps(execution_plan),
            ),
        )
        for job_uuid, node_uuid, index, kind, param in jobs:
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status, attempt,
                    param, feedback_data, return_info, control_data, error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?, ?, '{}', 0,
                          'pending', 1, ?, '{}', '{}', '{}', '[]')
                """,
                (
                    job_uuid,
                    _CREATED_AT,
                    _CREATED_AT,
                    TASK_UUID,
                    node_uuid,
                    index,
                    kind,
                    json.dumps(param),
                ),
            )
    return store.get_task(TASK_UUID)


def test_source_only_admission_never_calls_dispatcher(store: WorkflowStore) -> None:
    """只含来源的成功准入不得进入设备派发器。

    参数：``store`` 是隔离任务权威。返回无；断言来源作业与父任务直接成功，
    调度器（Scheduler）和派发器（Dispatcher）没有伪造设备动作。
    """

    task = _seed_task(store, with_action=False)
    inventory = _ToggleInventory(available=True)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        aggregate = bridge.submit(task)
    finally:
        bridge.close()

    assert dispatcher.dispatched == []
    assert aggregate["task"]["status"] == "succeeded"
    assert aggregate["jobs"][0]["status"] == "succeeded"
    assert aggregate["jobs"][0]["return_info"] == {
        "material": {
            "uuid": MATERIAL_UUID,
            "resource_template_uuid": TEMPLATE_UUID,
            "custody_policy": "task_exclusive",
        }
    }
    assert inventory.release_calls == [(TASK_UUID, "workflow_succeeded")]


def test_blocked_admission_retry_reuses_task_and_job_identities(
    store: WorkflowStore,
) -> None:
    """受阻后补料必须以同一任务和作业身份完成准入重试。

    参数：``store`` 是隔离任务权威。返回无；断言第一次零派发且全部待处理，
    重启后的第二次准入重试（AdmissionRetry）只推进原身份并派发原动作作业。
    """

    task = _seed_task(store, with_action=True)
    inventory = _ToggleInventory(available=False)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        blocked = bridge.submit(task)
    finally:
        bridge.close()

    inventory.available = True
    restarted_scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        inventory=inventory,
    )
    restarted_bridge = TaskSchedulerBridge(store, scheduler=restarted_scheduler)
    try:
        admitted = restarted_bridge.retry_admission(TASK_UUID)
    finally:
        restarted_bridge.close()

    assert blocked["task"]["status"] == "pending"
    assert [job["uuid"] for job in blocked["jobs"]] == [
        SOURCE_JOB_UUID,
        ACTION_JOB_UUID,
    ]
    assert [job["status"] for job in blocked["jobs"]] == ["pending", "pending"]
    assert blocked["task"]["wait_reason"]["resources"] == [
        {
            "scope": "material",
            "material_uuid": MATERIAL_UUID,
            "material_name": "测试固定物料",
        },
    ]
    assert [call[0] for call in inventory.admission_calls] == [TASK_UUID, TASK_UUID]
    assert admitted["jobs"][0]["uuid"] == SOURCE_JOB_UUID
    assert admitted["jobs"][0]["status"] == "succeeded"
    assert admitted["jobs"][0]["wait_reason"] == {}
    assert dispatcher.dispatched[0]["job_id"] == ACTION_JOB_UUID
    with store.transaction() as connection:
        admission = connection.execute(
            "SELECT status, attempt, revision "
            "FROM workflow_task_material_admission WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()
    assert tuple(admission) == ("admitted", 2, 2)


def test_blocked_material_admission_can_be_canceled_before_scheduler_submit(
    store: WorkflowStore,
) -> None:
    """证明等料任务未注册 Scheduler 时仍可通过公开取消语义安全收尾。"""

    task = _seed_task(store, with_action=True)
    inventory = _ToggleInventory(available=False)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        blocked = bridge.submit(task)
        canceled = bridge.cancel(
            TASK_UUID,
            command_uuid="82000000-0000-4000-8000-000000000001",
            reason="operator_canceled_waiting_material",
        )
    finally:
        bridge.close()

    assert blocked["task"]["status"] == "pending"
    assert scheduler.workflow_snapshot(TASK_UUID) is None
    assert canceled["task"]["status"] == "canceled"
    assert canceled["task"]["cleanup_status"] == "settled"
    assert [job["status"] for job in canceled["jobs"]] == [
        "canceled",
        "canceled",
    ]
    assert dispatcher.dispatched == []
    assert inventory.release_calls == [(TASK_UUID, "workflow_canceled")]


def test_source_admission_commits_before_ordinary_action_dispatch(
    store: WorkflowStore,
) -> None:
    """全部来源准入必须先于普通动作越过物理派发边界。

    参数：``store`` 是隔离任务权威。返回无；断言派发器观察到来源作业已经成功
    且写有物料占位符（ResourceSlot）结果，普通动作仍复用既有身份。
    """

    task = _seed_task(store, with_action=True)
    inventory = _ToggleInventory(available=True)
    observed_source_states: list[tuple[str, dict[str, Any]]] = []

    class _ObservingDispatcher(RecordingDispatcher):
        """在设备派发边界观察来源协调事实。"""

        def dispatch(self, payload: Any) -> None:
            """记录来源作业状态后转交记录派发器。

            参数：``payload`` 是普通动作命令。返回无。异常：存储读取异常传播。
            """

            source_job = store.get_job(SOURCE_JOB_UUID)
            observed_source_states.append(
                (source_job["status"], source_job["return_info"])
            )
            super().dispatch(payload)

    dispatcher = _ObservingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(task)
    finally:
        bridge.close()

    assert observed_source_states == [
        (
            "succeeded",
            {
                "material": {
                    "uuid": MATERIAL_UUID,
                    "resource_template_uuid": TEMPLATE_UUID,
                    "custody_policy": "task_exclusive",
                }
            },
        )
    ]
    assert dispatcher.dispatched[0]["job_id"] == ACTION_JOB_UUID


def test_pre_dispatch_submission_failure_releases_source_reservations(
    store: WorkflowStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """物理派发前提交失败必须终止 Task，并在声明 settled 前释放来源预留。"""

    task = _seed_task(store, with_action=True, automatic=True)

    class _ObservingInventory(_ToggleInventory):
        """在释放边界观察 Task 尚未提前声明清理完成。"""

        def release_workflow(self, workflow_uuid: str, *, reason: str) -> None:
            assert store.get_task(TASK_UUID)["cleanup_status"] == "required"
            super().release_workflow(workflow_uuid, reason=reason)

    inventory = _ObservingInventory(available=True)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
    )

    def fail_before_dispatch(_spec: WorkflowSpec) -> dict[str, Any]:
        raise RuntimeError("调度提交失败")

    monkeypatch.setattr(scheduler, "submit_workflow", fail_before_dispatch)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        with pytest.raises(TaskSchedulerBridgeError):
            bridge.submit(task)
    finally:
        bridge.close()

    assert store.get_task(TASK_UUID)["status"] == "canceled"
    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
    assert inventory.release_calls == [
        (TASK_UUID, "workflow_submission_failed")
    ]


def test_post_admission_compile_failure_releases_source_reservations(
    store: WorkflowStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """物料准入后、调度注册前失败也必须终止 Task 并释放来源预留。"""

    task = _seed_task(store, with_action=True, automatic=True)
    inventory = _ToggleInventory(available=True)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    original_compile = bridge._compiler.compile
    compile_calls = 0

    def fail_second_compile(
        persisted_task: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> WorkflowSpec:
        """首次编译成功，模拟准入结果写回后的二次编译失败。"""

        nonlocal compile_calls
        compile_calls += 1
        if compile_calls == 2:
            raise RuntimeError("准入后二次编译失败")
        return original_compile(persisted_task, jobs)

    monkeypatch.setattr(bridge._compiler, "compile", fail_second_compile)
    try:
        with pytest.raises(TaskSchedulerBridgeError):
            bridge.submit(task)
    finally:
        bridge.close()

    assert compile_calls == 2
    assert store.get_task(TASK_UUID)["status"] == "canceled"
    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
    assert inventory.release_calls == [
        (TASK_UUID, "workflow_submission_failed")
    ]


def test_malformed_admission_result_releases_committed_reservations(
    store: WorkflowStore,
) -> None:
    """库存提交后返回结构校验失败也必须补偿已经形成的来源预留。"""

    class _MalformedResultInventory(_ToggleInventory):
        """先记录成功准入，再返回缺失 allocations 的非法结果。"""

        def admit_task_materials(
            self,
            workflow_uuid: str,
            requests: list[Any],
            quantity_allocations: list[Any] | tuple[Any, ...],
        ) -> dict[str, Any]:
            super().admit_task_materials(
                workflow_uuid,
                requests,
                quantity_allocations,
            )
            return {"allocation_sites": {}}

    task = _seed_task(store, with_action=True, automatic=True)
    inventory = _MalformedResultInventory(available=True)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        with pytest.raises(TaskSchedulerBridgeError):
            bridge.submit(task)
    finally:
        bridge.close()

    assert store.get_task(TASK_UUID)["status"] == "canceled"
    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
    assert inventory.release_calls == [
        (TASK_UUID, "workflow_submission_failed")
    ]


def _pause_task_before_dispatch(store: WorkflowStore) -> None:
    """把已播种任务改为尚未派发的单步暂停状态。"""

    with store.transaction() as connection:
        row = connection.execute(
            "SELECT execution_plan FROM workflow_task WHERE uuid = ?",
            (TASK_UUID,),
        ).fetchone()
        plan = json.loads(str(row["execution_plan"]))
        plan["run_mode"] = "step"
        connection.execute(
            "UPDATE workflow_task SET execution_plan = ?, run_mode = 'step', "
            "control_status = 'paused' WHERE uuid = ?",
            (json.dumps(plan), TASK_UUID),
        )


def test_cancel_before_dispatch_releases_source_reservations(
    store: WorkflowStore,
) -> None:
    """尚未派发的 Task 取消时必须先释放来源预留，再声明清理完成。"""

    _seed_task(store, with_action=True, automatic=True)
    _pause_task_before_dispatch(store)

    class _ObservingInventory(_ToggleInventory):
        """在取消释放边界观察 cleanup 尚未提前完成。"""

        def release_workflow(self, workflow_uuid: str, *, reason: str) -> None:
            release_cleanup_states.append(
                store.get_task(TASK_UUID)["cleanup_status"]
            )
            super().release_workflow(workflow_uuid, reason=reason)

    class _ObservingQuantityInventory:
        """记录数量预留同样遵循 required → release → settled。"""

        def release_task(self, task_uuid: str, *, reason: str) -> None:
            quantity_release_calls.append((task_uuid, reason))
            quantity_cleanup_states.append(
                store.get_task(TASK_UUID)["cleanup_status"]
            )

    release_cleanup_states: list[str] = []
    quantity_release_calls: list[tuple[str, str]] = []
    quantity_cleanup_states: list[str] = []
    inventory = _ObservingInventory(available=True)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    bridge._quantity_inventory = _ObservingQuantityInventory()
    try:
        submitted = bridge.submit(store.get_task(TASK_UUID))
        assert submitted["task"]["control_status"] == "paused"
        canceled = bridge.cancel(
            TASK_UUID,
            command_uuid="82000000-0000-4000-8000-000000000001",
        )
        replayed = bridge.cancel(
            TASK_UUID,
            command_uuid="82000000-0000-4000-8000-000000000001",
        )
    finally:
        bridge.close()

    assert canceled["task"]["status"] == "canceled"
    assert canceled["task"]["cleanup_status"] == "settled"
    assert replayed["task"]["cleanup_status"] == "settled"
    assert inventory.release_calls == [
        (TASK_UUID, "workflow_canceled"),
        (TASK_UUID, "workflow_canceled"),
    ]
    assert release_cleanup_states == ["required", "settled"]
    assert quantity_release_calls == [
        (TASK_UUID, "workflow_canceled"),
        (TASK_UUID, "workflow_canceled"),
    ]
    assert quantity_cleanup_states == ["required", "settled"]


def test_cancel_replay_preserves_reservations_requiring_attention(
    store: WorkflowStore,
) -> None:
    """人工对账中的取消重放不得把状态未知的来源预留释放为可用。"""

    _seed_task(store, with_action=True, automatic=True)
    _pause_task_before_dispatch(store)

    inventory = _ToggleInventory(available=True)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_task SET status = 'canceled', "
                "cleanup_status = 'requires_attention', "
                "control_status = 'waiting_reconciliation' WHERE uuid = ?",
                (TASK_UUID,),
            )
            connection.execute(
                "UPDATE workflow_node_job SET status = 'canceled' "
                "WHERE workflow_task_uuid = ?",
                (TASK_UUID,),
            )

        replayed = bridge.cancel(
            TASK_UUID,
            command_uuid="82000000-0000-4000-8000-000000000002",
        )
    finally:
        bridge.close()

    assert replayed["task"]["status"] == "canceled"
    assert replayed["task"]["cleanup_status"] == "requires_attention"
    assert inventory.release_calls == []


def test_cancel_release_failure_keeps_cleanup_required(
    store: WorkflowStore,
) -> None:
    """释放来源失败时不得把取消任务提前标记为已经清理。"""

    _seed_task(store, with_action=True, automatic=True)
    _pause_task_before_dispatch(store)

    class _FailingReleaseInventory(_ToggleInventory):
        """模拟库存释放事务失败。"""

        def release_workflow(self, workflow_uuid: str, *, reason: str) -> None:
            super().release_workflow(workflow_uuid, reason=reason)
            raise RuntimeError("库存释放失败")

    inventory = _FailingReleaseInventory(available=True)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        with pytest.raises(RuntimeError, match="库存释放失败"):
            bridge.cancel(
                TASK_UUID,
                command_uuid="82000000-0000-4000-8000-000000000003",
            )
    finally:
        bridge.close()

    assert store.get_task(TASK_UUID)["status"] == "canceled"
    assert store.get_task(TASK_UUID)["cleanup_status"] == "required"
    assert inventory.release_calls == [(TASK_UUID, "workflow_canceled")]


def test_automatic_source_projects_selected_material_before_dispatch(
    store: WorkflowStore,
) -> None:
    """自动来源应先把库存选择结果写入原动作作业，再越过派发边界。

    参数：``store`` 是隔离任务权威。返回无；断言物料来源解析作业
    （MaterialSourceResolutionJob）和工作流节点作业（WorkflowNodeJob）使用同一
    次准入结果，派发参数包含具体物料（Material）UUID。异常：任何临时作业、
    空参数派发或计划改写都会使断言失败。
    """

    task = _seed_task(store, with_action=True, automatic=True)
    inventory = _ToggleInventory(available=True)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        aggregate = bridge.submit(task)
    finally:
        bridge.close()

    source_job, action_job = aggregate["jobs"]
    assert source_job["return_info"] == {
        "material": {
            "uuid": MATERIAL_UUID,
            "resource_template_uuid": TEMPLATE_UUID,
            "custody_policy": "task_exclusive",
        }
    }
    assert action_job["param"] == {"plate": {"uuid": MATERIAL_UUID}}
    assert dispatcher.dispatched[0]["action_args"] == {
        "plate": {"uuid": MATERIAL_UUID}
    }


def test_shared_source_uses_atomic_admission_without_exclusive_reservation(
    store: WorkflowStore,
) -> None:
    """共享来源必须进入整组准入，但不得形成任务独占预留。

    参数：``store`` 是隔离任务权威。返回无；断言协调器把共享保管策略交给库存
    权威原子准入，仍持久化任务物料绑定，但不会创建任务物料预留事实。
    """

    task = _seed_task(
        store,
        with_action=True,
        automatic=True,
        custody_policy="shared_source",
    )
    inventory = _ToggleInventory(available=True)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(task)
    finally:
        bridge.close()

    assert [call[0] for call in inventory.admission_calls] == [TASK_UUID]
    assert [
        request.custody_policy
        for request in inventory.admission_calls[0][1]
    ] == ["shared_source"]
    with store.transaction() as connection:
        binding = connection.execute(
            "SELECT custody_policy FROM workflow_task_material_binding "
            "WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()
        claim_count = connection.execute(
            "SELECT COUNT(*) FROM workflow_task_material_claim "
            "WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        ).fetchone()[0]
    assert binding["custody_policy"] == "shared_source"
    assert claim_count == 0


def test_successful_material_task_releases_source_reservations(
    store: WorkflowStore,
) -> None:
    """带来源任务成功后必须释放协调器持有的短期预留。

    参数：``store`` 是隔离任务权威。返回无；断言最后一个普通动作成功投影后，
    调度桥以同一工作流任务（WorkflowTask）身份幂等释放物料来源
    （MaterialSource）预留，避免阻塞后续自动分配。
    """

    task = _seed_task(store, with_action=True, automatic=True)
    inventory = _ToggleInventory(available=True)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(task)
        scheduler.on_job_finished(ACTION_JOB_UUID, True, {"success": True})
    finally:
        bridge.close()

    assert store.get_task(TASK_UUID)["status"] == "succeeded"
    assert inventory.release_calls == [(TASK_UUID, "workflow_succeeded")]


def test_failed_material_task_retains_reservations_until_user_cancels(
    store: WorkflowStore,
) -> None:
    """节点失败暂停并保留预留；用户取消当前任务后才结算来源预留。"""

    task = _seed_task(store, with_action=True, automatic=True)
    inventory = _ToggleInventory(available=True)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher(), inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(task)
        scheduler.on_job_finished(ACTION_JOB_UUID, False, {"success": False})
        assert store.get_task(TASK_UUID)["status"] == "running"
        assert store.get_task(TASK_UUID)["control_status"] == "error_waiting"
        assert inventory.release_calls == []
        snapshot = bridge.error_control.snapshot()
        assert snapshot["mode"] == "PAUSED"
        decision = snapshot["errors"][0]
        bridge.decide_error(
            decision["decision_id"],
            {
                "action": "cancel_task",
                "expected_decision_version": decision["decision_version"],
                "expected_station_version": snapshot["station_version"],
                "observed_error_epoch": snapshot["error_epoch"],
                "resume_requested": False,
            },
            idempotency_key="cancel-source-task",
            operator_id="tester",
        )
        assert store.get_task(TASK_UUID)["status"] == "canceled"
        assert inventory.release_calls == [(TASK_UUID, "workflow_canceled")]
        assert bridge.error_control.snapshot()["mode"] == "PAUSED"
    finally:
        bridge.close()


def test_shared_source_action_lock_serializes_across_workflow_tasks() -> None:
    """两个工作流任务共享同一试剂时动作执行必须跨设备串行。

    参数：无。返回：无；通过冻结动作合同（Action Contract）的物料锁标记
    断言两个独立工作流任务可同时进入调度器，但只有一个动作先越过派发边界；
    首个作业完成释放锁后，另一个任务继续派发。异常：Schema 或调度不变量漂移
    使断言失败。
    """

    action_schema = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "reagent": {
                        "type": "object",
                        "x-unilabos-material-lock": True,
                        "properties": {
                            "uuid": {"type": "string", "format": "uuid"},
                        },
                        "required": ["uuid"],
                        "additionalProperties": False,
                    }
                },
                "required": ["reagent"],
                "additionalProperties": False,
            }
        },
        "required": ["goal"],
    }
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    def spec(workflow_uuid: str, node_uuid: str, device_uuid: str) -> WorkflowSpec:
        """构造绑定同一共享试剂、使用不同设备的单动作任务规格。

        参数：三个 UUID 分别标识任务、动作节点和设备。返回：冻结同一试剂参数
        与动作合同的 ``WorkflowSpec``。异常：构造阶段不访问外部状态。
        """

        return WorkflowSpec(
            workflow_id=workflow_uuid,
            nodes=[
                WorkflowNode(
                    id=node_uuid,
                    job_id=f"job-{node_uuid}",
                    device_id=device_uuid,
                    action_name="dose_reagent",
                    action_type="UniLabJsonCommand",
                    param={"reagent": {"uuid": MATERIAL_UUID}},
                    param_schema=action_schema,
                )
            ],
        )

    first = scheduler.submit_workflow(spec("workflow-shared-a", "action-a", "reactor-a"))
    second = scheduler.submit_workflow(spec("workflow-shared-b", "action-b", "reactor-b"))

    assert [item["node_id"] for item in first["dispatched"]] == ["action-a"]
    assert second["dispatched"] == []
    scheduler.on_job_finished("job-action-a", True, {"success": True})
    assert [item["node_id"] for item in dispatcher.dispatched] == [
        "action-a",
        "action-b",
    ]
