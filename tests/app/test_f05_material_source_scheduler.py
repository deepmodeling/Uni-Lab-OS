"""F05.4-C14 物料来源准入与本地调度顺序合同。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import InsufficientStock
from unilabos.app.scheduler.material_source_resolution import (
    MaterialSourceResolutionCoordinator,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

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
        self.reserve_calls: list[tuple[str, dict[str, Any]]] = []
        self.release_calls: list[tuple[str, str]] = []
        self.consume_calls: list[tuple[str, str]] = []

    def reserve_workflow(
        self,
        workflow_uuid: str,
        requirements: dict[str, Any],
    ) -> dict[str, Any]:
        """模拟 gaojing 整图单事务短期预留。

        参数：``workflow_uuid`` 是工作流任务（WorkflowTask）身份；
        ``requirements`` 按来源节点汇总全部需求。返回无。异常：不可用时抛
        ``InsufficientStock``，且不形成部分预留。
        """

        self.reserve_calls.append((workflow_uuid, requirements))
        if not self.available:
            raise InsufficientStock("测试固定物料已被其他任务预留")
        return {
            "workflow_id": workflow_uuid,
            "reserved_nodes": list(requirements),
            "allocations": {
                node_uuid: [MATERIAL_UUID] for node_uuid in requirements
            },
        }

    def consume_reservation(self, workflow_uuid: str, node_uuid: str) -> None:
        """记录来源预留消费；参数是任务和节点身份，返回无。"""

        self.consume_calls.append((workflow_uuid, node_uuid))

    def quarantine_reservation(self, workflow_uuid: str, node_uuid: str) -> None:
        """保留既有失败隔离调用面；参数是任务和节点身份，返回无。"""

    def release_workflow(self, workflow_uuid: str, *, reason: str) -> None:
        """记录终态释放；参数是任务身份和原因，返回无；异常：无。"""

        self.release_calls.append((workflow_uuid, reason))


class _SharedInventory(_ToggleInventory):
    """支持共享来源只读选择的最小库存夹具。"""

    def __init__(self) -> None:
        super().__init__(available=True)
        self.select_calls: list[Any] = []

    def select_instance_for_requirement(self, requirement: Any) -> dict[str, str]:
        self.select_calls.append(requirement)
        return {"edge_uuid": MATERIAL_UUID}


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
    *, automatic: bool = False, custody_policy: str | None = None
) -> dict[str, Any]:
    """构造固定 existing 物料来源的协调器计划节点。

    参数：无。返回：包含冻结选择器和唯一实例需求的计划对象。异常：无。
    """

    node = {
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
            else [{"instance_uuid": MATERIAL_UUID}]
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
    if custody_policy is not None:
        node["custody_policy"] = custody_policy
    return node


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
) -> dict[str, Any]:
    """持久化含来源协调责任的待处理工作流任务。

    参数：``store`` 是唯一写权威；``with_action`` 决定准入后是否需要物理派发。
    返回：标准任务投影。异常：数据库约束原样传播。
    """

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="F05.4-C14 来源准入",
        tags=[],
        description=None,
        meta_data={},
    )
    nodes = [_source_plan_node(automatic=automatic)]
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
        "material": {"uuid": MATERIAL_UUID, "resource_template_uuid": TEMPLATE_UUID}
    }
    assert inventory.release_calls == [(TASK_UUID, "workflow_succeeded")]
    assert inventory.consume_calls == [(TASK_UUID, SOURCE_NODE_UUID)]


def test_shared_source_is_selected_without_exclusive_reservation() -> None:
    """共享来源应复用稳定实例而不把它推进到 RESERVED。

    参数：无；使用最小库存夹具和来源计划。返回：无。异常：若共享策略退化为
    独占预留，``reserve_calls`` 或绑定 UUID 断言会失败。
    """

    class _Projection:
        def __init__(self) -> None:
            self.admitted: list[tuple[str, dict[str, Any]]] = []
            self.blocked: list[str] = []

        def project_material_source_admission(
            self, task_uuid: str, bindings: dict[str, Any]
        ) -> None:
            self.admitted.append((task_uuid, bindings))

        def project_material_source_blocked(self, task_uuid: str) -> None:
            self.blocked.append(task_uuid)

    inventory = _SharedInventory()
    projection = _Projection()
    node = _source_plan_node(automatic=True, custody_policy="shared_source")
    task = {
        "uuid": TASK_UUID,
        "execution_plan": {"nodes": [node], "edges": []},
    }
    jobs = [
        {
            "uuid": SOURCE_JOB_UUID,
            "workflow_node_uuid": SOURCE_NODE_UUID,
            "executor_kind": "material_source",
        }
    ]

    result = MaterialSourceResolutionCoordinator(
        inventory=inventory,
        projection=projection,
    ).reconcile(task, jobs)

    assert result.status == "admitted"
    assert inventory.reserve_calls == []
    assert len(inventory.select_calls) == 1
    assert projection.blocked == []
    assert projection.admitted == [
        (
            TASK_UUID,
            {
                SOURCE_NODE_UUID: {
                    "uuid": MATERIAL_UUID,
                    "resource_template_uuid": TEMPLATE_UUID,
                }
            },
        )
    ]


def test_blocked_admission_retry_reuses_task_and_job_identities(
    store: WorkflowStore,
) -> None:
    """受阻后补料必须以同一任务和作业身份完成准入重试。

    参数：``store`` 是隔离任务权威。返回无；断言第一次零派发且全部待处理，
    第二次准入重试（AdmissionRetry）只推进原身份并派发原动作作业。
    """

    task = _seed_task(store, with_action=True)
    inventory = _ToggleInventory(available=False)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        blocked = bridge.submit(task)
        inventory.available = True
        admitted = bridge.retry_admission(TASK_UUID)
    finally:
        bridge.close()

    assert blocked["task"]["status"] == "pending"
    assert [job["uuid"] for job in blocked["jobs"]] == [
        SOURCE_JOB_UUID,
        ACTION_JOB_UUID,
    ]
    assert [job["status"] for job in blocked["jobs"]] == ["pending", "pending"]
    assert [call[0] for call in inventory.reserve_calls] == [TASK_UUID, TASK_UUID]
    assert admitted["jobs"][0]["uuid"] == SOURCE_JOB_UUID
    assert admitted["jobs"][0]["status"] == "succeeded"
    assert dispatcher.dispatched[0]["job_id"] == ACTION_JOB_UUID


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
                }
            },
        )
    ]
    assert dispatcher.dispatched[0]["job_id"] == ACTION_JOB_UUID


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
        }
    }
    assert action_job["param"] == {"plate": {"uuid": MATERIAL_UUID}}
    assert dispatcher.dispatched[0]["action_args"] == {
        "plate": {"uuid": MATERIAL_UUID}
    }


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


def test_failed_material_task_releases_source_reservations(
    store: WorkflowStore,
) -> None:
    """带来源任务失败后也必须释放协调器持有的短期预留。

    参数：``store`` 是隔离任务权威。返回无；断言普通设备动作明确失败并把父任务
    推进到失败终态后，调度桥以同一工作流任务（WorkflowTask）身份释放物料来源
    （MaterialSource）预留，避免一次设备故障永久占住自动分配候选。
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
        scheduler.on_job_finished(ACTION_JOB_UUID, False, {"success": False})
    finally:
        bridge.close()

    assert store.get_task(TASK_UUID)["status"] == "failed"
    assert inventory.release_calls == [(TASK_UUID, "workflow_failed")]
def test_shared_source_quantity_is_reserved_as_lot_without_reserving_instance() -> None:
    """共享试剂只读选择实体身份，但任务用量仍进入 lot 预留。"""

    class _Projection:
        def project_material_source_admission(self, task_uuid: str, bindings: dict[str, Any]) -> None:
            del task_uuid, bindings

        def project_material_source_blocked(self, task_uuid: str) -> None:
            raise AssertionError(f"unexpected blocked task: {task_uuid}")

    inventory = _SharedInventory()
    node = _source_plan_node(automatic=True, custody_policy="shared_source")
    node["material_requirements"].append(
        {"template_id": TEMPLATE_UUID, "quantity": 100.0, "unit": "ml"}
    )
    task = {"uuid": TASK_UUID, "execution_plan": {"nodes": [node], "edges": []}}
    jobs = [{"uuid": SOURCE_JOB_UUID, "workflow_node_uuid": SOURCE_NODE_UUID, "executor_kind": "material_source"}]

    result = MaterialSourceResolutionCoordinator(
        inventory=inventory,
        projection=_Projection(),
    ).reconcile(task, jobs)

    assert result.status == "admitted"
    assert len(inventory.select_calls) == 1
    assert len(inventory.reserve_calls) == 1
    reserved = inventory.reserve_calls[0][1][SOURCE_NODE_UUID]
    assert [requirement.quantity for requirement in reserved] == [100.0]
