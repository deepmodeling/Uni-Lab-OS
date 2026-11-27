"""F05.3-C 工作流任务调度桥（TaskSchedulerBridge）的纵向行为合同。"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.scheduler.dispatch import CommittedJobOutcome, RecordingDispatcher
from unilabos.app.scheduler.inventory.dispatch_admission import (
    DispatchAdmissionDecision,
    DispatchFence,
    DispatchPermit,
)
from unilabos.app.scheduler.inventory.domain import InsufficientStock
from unilabos.app.scheduler.inventory.station_resource import (
    StationResourceError,
    StationSiteTarget,
    TransferResourceFacts,
)
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

WORKFLOW_UUID = "11000000-0000-4000-8000-000000000001"
TASK_UUID = "21000000-0000-4000-8000-000000000001"
NODE_UUID = "31000000-0000-4000-8000-000000000001"
JOB_UUID = "41000000-0000-4000-8000-000000000001"
MATERIAL_UUID = "51000000-0000-4000-8000-000000000001"
SECOND_NODE_UUID = "31000000-0000-4000-8000-000000000002"
SECOND_JOB_UUID = "41000000-0000-4000-8000-000000000002"
SOURCE_HANDLE_UUID = "61000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "61000000-0000-4000-8000-000000000002"
_CREATED_AT = "2026-08-05T00:00:00Z"


class _ToggleInventory:
    """模拟可由补料改变结果的本地库存权威（Inventory Authority）。"""

    def __init__(self, *, available: bool) -> None:
        """设置物料可用性。

        参数：``available`` 表示整任务物料是否可以一次预留。返回无；不访问真实
        SQLite。``reserve_calls`` 记录准入重试（AdmissionRetry）的同一稳定身份。
        """

        self.available = available
        self.reserve_calls: list[tuple[str, dict[str, Any]]] = []

    def reserve_workflow(
        self,
        workflow_uuid: str,
        requirements: dict[str, Any],
    ) -> None:
        """模拟遗留整图物料预留。

        参数：``workflow_uuid`` 是工作流任务稳定身份，``requirements`` 是按节点
        汇总的物料需求。返回无；不可用时抛 ``InsufficientStock``。
        """

        self.reserve_calls.append((workflow_uuid, requirements))
        if not self.available:
            raise InsufficientStock("测试物料不足")

    def consume_reservation(self, workflow_uuid: str, node_uuid: str) -> None:
        """模拟派发前消费预留；参数是任务与节点身份，返回无。"""

    def quarantine_reservation(self, workflow_uuid: str, node_uuid: str) -> None:
        """模拟失败隔离；参数是任务与节点身份，返回无。"""

    def release_workflow(self, workflow_uuid: str, *, reason: str) -> None:
        """模拟终态释放；参数是任务身份和释放原因，返回无。"""


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    """创建隔离工作流存储（WorkflowStore）。

    参数：``tmp_path`` 是 pytest 临时目录。产生：本测试唯一任务写权威；结束时
    关闭数据库连接。
    """

    # ``opened_store`` 是任务与作业标准事实的唯一持久化位置。
    opened_store = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield opened_store
    finally:
        opened_store.close()


def _seed_task(
    store: WorkflowStore,
    *,
    with_material: bool,
    run_mode: str = "normal",
    with_successor: bool = False,
) -> dict[str, Any]:
    """持久化一个带冻结执行计划（ExecutionPlan）的待处理任务。

    参数：``store`` 是工作流写权威；``with_material`` 决定计划是否包含遗留短期
    物料需求。返回：标准工作流任务投影。异常：数据库写入错误原样传播。
    """

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="F05.3-C 调度桥",
        tags=[],
        description=None,
        meta_data={},
    )
    # ``material_requirements`` 是短期交给高靖库存预留路径的冻结需求，不创建第二
    # 库存权威（Inventory Authority）。
    material_requirements = [{"instance_uuid": MATERIAL_UUID}] if with_material else []
    execution_plan = {
        "version": 1,
        "run_mode": run_mode,
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": NODE_UUID,
                "kind": "device_action",
                "device_id": "reactor-a",
                "action_name": "distribute",
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
                "material_requirements": material_requirements,
            },
            *(
                [
                    {
                        "uuid": SECOND_NODE_UUID,
                        "kind": "device_action",
                        "device_id": "reactor-b",
                        "action_name": "finish",
                        "action_type": "UniLabJsonCommand",
                        "param": {},
                        "param_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        },
                        "material_requirements": [],
                    }
                ]
                if with_successor
                else []
            ),
        ],
        "handles": [],
        "edges": (
            [
                {
                    "uuid": "71000000-0000-4000-8000-000000000001",
                    "source_node_uuid": NODE_UUID,
                    "target_node_uuid": SECOND_NODE_UUID,
                    "dependency_only": True,
                }
            ]
            if with_successor
            else []
        ),
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, execution_mode, target_node_uuid,
                control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', ?,
                      ?, ?, NULL, ?, 'none', '{}', '{}', '{}', '[]')
            """,
            (
                TASK_UUID,
                _CREATED_AT,
                _CREATED_AT,
                WORKFLOW_UUID,
                json.dumps(execution_plan),
                run_mode,
                "step" if run_mode == "step" else "normal",
                "paused" if run_mode == "step" else "active",
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status, attempt,
                param, feedback_data, return_info, control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (JOB_UUID, _CREATED_AT, _CREATED_AT, TASK_UUID, NODE_UUID),
        )
        if with_successor:
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status, attempt,
                    param, feedback_data, return_info, control_data, error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 1,
                          'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                          '{}', '{}', '[]')
                """,
                (
                    SECOND_JOB_UUID,
                    _CREATED_AT,
                    _CREATED_AT,
                    TASK_UUID,
                    SECOND_NODE_UUID,
                ),
            )
    return store.get_task(TASK_UUID)


def _bridge(store: WorkflowStore, scheduler: EdgeScheduler) -> Any:
    """构造待测工作流任务调度桥（TaskSchedulerBridge）。

    参数：``store`` 是标准任务写权威，``scheduler`` 是既有本地调度器。返回：
    只绑定这两个权威的桥实例。异常：RED 阶段模块不存在时保留导入错误。
    """

    # ``bridge_module`` 是本轮新增的唯一生产模块接缝。
    bridge_module = importlib.import_module("unilabos.workflow.task_scheduler_bridge")
    return bridge_module.TaskSchedulerBridge(store, scheduler=scheduler)


def test_cancel_persisted_task_before_scheduler_submission(store: WorkflowStore) -> None:
    """创建事实后、派发前收到取消，无内存 DAG 也应关闭原 Task/Job。"""
    _seed_task(store, with_material=False)
    dispatcher = RecordingDispatcher()
    bridge = _bridge(store, EdgeScheduler(dispatcher=dispatcher))
    try:
        aggregate = bridge.cancel(TASK_UUID)
        assert aggregate["task"]["status"] == "canceled"
        assert store.get_job(JOB_UUID)["status"] == "canceled"
        assert dispatcher.dispatched == []
    finally:
        bridge.close()


def test_recovery_freezes_only_jobs_waiting_for_physical_settlement(
    store: WorkflowStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态恢复不能要求普通成功作业持有库存占用（Claim）。

    参数：``store`` 是隔离工作流权威；``monkeypatch`` 记录执行锁查询。
    返回：无；断言同一失败任务中的工作流输入等普通成功作业被跳过，只有声明
    ``uncertainty_reason`` 的物理作业被冻结。异常：恢复误查无 Claim 的普通作业
    时测试保持 RED，对应真实 Backend 重启失败。
    """

    class _RecoveryInventory:
        """记录恢复阶段的库存占用状态转换。"""

        store = None

        def __init__(self) -> None:
            """初始化空转换记录；参数与异常均为空。"""

            self.transitions: list[tuple[str, str]] = []

        def transition_dispatch_permit(
            self,
            claim_uuid: str,
            *,
            target_state: str,
        ) -> None:
            """记录 Claim 身份和目标状态；返回与异常均为空。"""

            self.transitions.append((claim_uuid, target_state))

    inventory = _RecoveryInventory()
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
        station_resources=inventory,
    )
    bridge = _bridge(store, scheduler)
    queried_jobs: list[str] = []

    def _execution_claim(job_uuid: str) -> dict[str, Any] | None:
        """只为等待物理结算的作业返回生产形状 Claim。"""

        queried_jobs.append(job_uuid)
        if job_uuid != "job-awaiting-settlement":
            return None
        return {"claim_uuid": "claim-awaiting-settlement", "state": "uncertain"}

    monkeypatch.setattr(bridge._projection, "get_execution_claim", _execution_claim)
    try:
        bridge._mark_inventory_claims_uncertain(
            {
                "jobs": [
                    {
                        "uuid": "workflow-input-job",
                        "status": "succeeded",
                        "uncertainty_reason": None,
                    },
                    {
                        "uuid": "job-awaiting-settlement",
                        "status": "failed",
                        "uncertainty_reason": (
                            "material_transfer_inventory_reconciliation_required"
                        ),
                    },
                ]
            }
        )
    finally:
        bridge.close()

    assert queried_jobs == ["job-awaiting-settlement"]
    assert inventory.transitions == [
        ("claim-awaiting-settlement", "uncertain")
    ]


def _seed_recoverable_test_mode_task(store: WorkflowStore) -> None:
    """持久化一个已完成取料、待执行放料的测试模式任务。

    参数：``store`` 是隔离工作流存储（WorkflowStore）。返回无；
    任务的首个回执故意缺少同名物料（Material）输出，模拟旧
    ``--test_mode`` 进程中断后的持久事实。
    """

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="F05.3-C 可恢复调度桥",
        tags=[],
        description=None,
        meta_data={},
    )
    resource_schema = {
        "type": "object",
        "properties": {"uuid": {"type": "string", "format": "uuid"}},
        "required": ["uuid"],
        "additionalProperties": False,
    }
    execution_plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": NODE_UUID,
                "kind": "device_action",
                "device_id": "robot-a",
                "action_name": "pick",
                "action_type": "UniLabJsonCommand",
                "param": {"resource": {"uuid": MATERIAL_UUID}},
                "param_schema": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "object",
                            "properties": {"resource": resource_schema},
                        },
                        "result": {
                            "type": "object",
                            "properties": {"resource": resource_schema},
                        },
                    },
                },
                "material_requirements": [],
            },
            {
                "uuid": SECOND_NODE_UUID,
                "kind": "device_action",
                "device_id": "robot-a",
                "action_name": "place",
                "action_type": "UniLabJsonCommand",
                "param": {},
                "param_schema": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "object",
                            "properties": {"resource": resource_schema},
                        }
                    },
                },
                "material_requirements": [],
            },
        ],
        "handles": [
            {
                "uuid": SOURCE_HANDLE_UUID,
                "node_uuid": NODE_UUID,
                "io_type": "source",
                "handle_key": "resource",
                "data_key": "resource",
                "data_source": "executor",
                "type": "ResourceSlot",
                "required": False,
            },
            {
                "uuid": TARGET_HANDLE_UUID,
                "node_uuid": SECOND_NODE_UUID,
                "io_type": "target",
                "handle_key": "resource",
                "data_key": "resource",
                "data_source": "goal",
                "type": "ResourceSlot",
                "required": True,
            },
        ],
        "edges": [
            {
                "uuid": "71000000-0000-4000-8000-000000000001",
                "source_node_uuid": NODE_UUID,
                "target_node_uuid": SECOND_NODE_UUID,
                "source_handle_uuid": SOURCE_HANDLE_UUID,
                "target_handle_uuid": TARGET_HANDLE_UUID,
                "source_data_key": "resource",
                "target_data_key": "resource",
                "source_type": "ResourceSlot",
                "target_type": "ResourceSlot",
            }
        ],
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'running', '{}', ?,
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
        for job_uuid, node_uuid, status, param, return_info in (
            (
                JOB_UUID,
                NODE_UUID,
                "succeeded",
                {"resource": {"uuid": MATERIAL_UUID}},
                {"action_name": "pick", "test_mode": True},
            ),
            (SECOND_JOB_UUID, SECOND_NODE_UUID, "pending", {}, {}),
        ):
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status, attempt,
                    param, feedback_data, return_info, control_data, error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?,
                          'device_action', '{}', 0, ?, 1, ?, '{}', ?, '{}', '[]')
                """,
                (
                    job_uuid,
                    _CREATED_AT,
                    _CREATED_AT,
                    TASK_UUID,
                    node_uuid,
                    0 if node_uuid == NODE_UUID else 1,
                    status,
                    json.dumps(param),
                    json.dumps(return_info),
                ),
            )


def test_persisted_task_compiles_and_dispatches_with_stable_identities(
    store: WorkflowStore,
) -> None:
    """已持久化任务必须复用稳定任务/作业身份并先投影再物理派发。

    参数：``store`` 是隔离任务权威。返回无；断言设备命令沿用既有 UUID，且命令
    被记录时标准任务已为 ``running``、作业已为 ``dispatched``。
    """

    task = _seed_task(store, with_material=False)
    observed_states: list[tuple[str, str]] = []

    class _ObservingDispatcher(RecordingDispatcher):
        """在执行适配器边界观察标准任务/作业状态。"""

        def dispatch(self, payload: Any) -> None:
            """记录派发瞬间状态；参数是设备命令，返回无。"""

            observed_states.append(
                (store.get_task(TASK_UUID)["status"], store.get_job(JOB_UUID)["status"])
            )
            super().dispatch(payload)

    dispatcher = _ObservingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        aggregate = bridge.submit(task)
    finally:
        bridge.close()

    assert observed_states == [("running", "dispatched")]
    assert dispatcher.dispatched[0]["task_id"] == TASK_UUID
    assert dispatcher.dispatched[0]["job_id"] == JOB_UUID
    assert dispatcher.dispatched[0]["attempt"] == 1
    assert (
        dispatcher.dispatched[0]["command_uuid"]
        == store.get_job(JOB_UUID)["edge_command_uuid"]
    )
    assert dispatcher.dispatched[0]["claim_uuid"]
    assert dispatcher.dispatched[0]["fences"] == [
        {
            "lock_key": "/devices/reactor-a",
            "fencing_token": 1,
        }
    ]
    assert aggregate["task"]["status"] == "running"


def test_bridge_persists_the_scheduler_workflow_trace_context(
    store: WorkflowStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调度器接收 Task 后必须把根 Trace 身份写回同一个持久任务。"""

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    original_submit = scheduler.submit_workflow
    trace_context = {
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "trace_id": "0123456789abcdef0123456789abcdef",
        "span_id": "0123456789abcdef",
    }

    def submit_with_trace(spec: Any) -> dict[str, Any]:
        result = original_submit(spec)
        return {**result, "trace_context": trace_context}

    monkeypatch.setattr(scheduler, "submit_workflow", submit_with_trace)
    bridge = _bridge(store, scheduler)
    try:
        aggregate = bridge.submit(task)

        assert aggregate["task"]["trace_context"] == trace_context
        assert store.get_task(TASK_UUID)["trace_context"] == trace_context
    finally:
        bridge.close()


def test_scheduler_wait_projects_authoritative_structured_resources(
    store: WorkflowStore,
) -> None:
    """桥必须把调度器给出的实际阻塞资源写入标准 Job 等待原因。"""

    _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    wait_resources = [
        {
            "scope": "material",
            "material_uuid": "72000000-0000-4000-8000-000000000001",
        },
        {
            "scope": "material_site",
            "material_uuid": "73000000-0000-4000-8000-000000000001",
            "site_uuid": "71000000-0000-4000-8000-000000000001",
        },
    ]
    try:
        bridge._task_by_job[JOB_UUID] = TASK_UUID
        bridge._on_job_execution_wait(
            {
                "job_id": JOB_UUID,
                "workflow_id": TASK_UUID,
                "execution_locks": [],
                "blocking_job_id": None,
                "blocking_workflow_id": None,
                "wait_code": "transfer_source_site_missing",
                "wait_message": "待搬物料尚无来源库位",
                "wait_resources": wait_resources,
            }
        )
    finally:
        bridge.close()

    assert store.get_job(JOB_UUID)["wait_reason"]["resources"] == wait_resources


def test_scheduler_lock_wait_derives_and_names_resources_from_lock_requests(
    store: WorkflowStore,
) -> None:
    """真实锁竞争没有显式 resources 时也要持久化具体物料名称。"""

    class _WaitInventory:
        store = None

        def describe_wait_resources(
            self,
            resources: list[dict[str, str]],
        ) -> tuple[dict[str, str], ...]:
            return tuple(
                {**resource, "material_name": "样品瓶 A"}
                for resource in resources
            )

    _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        station_resources=_WaitInventory(),
    )
    bridge = _bridge(store, scheduler)
    material_uuid = "72000000-0000-4000-8000-000000000001"
    try:
        bridge._task_by_job[JOB_UUID] = TASK_UUID
        bridge._on_job_execution_wait(
            {
                "job_id": JOB_UUID,
                "workflow_id": TASK_UUID,
                "execution_locks": [
                    {
                        "lock_key": f"material/{material_uuid}/exclusive",
                        "scope": "material",
                        "material_uuid": material_uuid,
                    },
                ],
                "blocking_job_id": SECOND_JOB_UUID,
                "blocking_workflow_id": "other-task",
            }
        )
    finally:
        bridge.close()

    assert store.get_job(JOB_UUID)["wait_reason"]["resources"] == [
        {
            "scope": "material",
            "material_uuid": material_uuid,
            "material_name": "样品瓶 A",
        }
    ]


def test_step_task_stays_paused_until_bridge_step_dispatches_one_job(
    store: WorkflowStore,
) -> None:
    task = _seed_task(store, with_material=False, run_mode="step")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        aggregate = bridge.submit(task)
        assert aggregate["task"]["status"] == "pending"
        assert aggregate["task"]["control_status"] == "paused"
        assert dispatcher.dispatched == []

        result = bridge.step(TASK_UUID)
        assert [item["job_id"] for item in result["dispatched"]] == [JOB_UUID]
        assert len(dispatcher.dispatched) == 1
        assert store.get_task(TASK_UUID)["control_status"] == "paused"
    finally:
        bridge.close()


def test_step_uses_scheduler_runtime_when_bridge_route_ledger_is_stale(
    store: WorkflowStore,
) -> None:
    """桥接层临时路由账本丢失时，单步仍以调度器运行事实为准。"""

    task = _seed_task(store, with_material=False, run_mode="step")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(task)
        bridge._submitted_tasks.clear()

        result = bridge.step(TASK_UUID)

        assert [item["job_id"] for item in result["dispatched"]] == [JOB_UUID]
        assert len(dispatcher.dispatched) == 1
    finally:
        bridge.close()


def test_step_command_api_is_idempotent_and_dispatches_once(
    store: WorkflowStore,
) -> None:
    task = _seed_task(store, with_material=False, run_mode="step")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    client = TestClient(create_workflow_app(service))
    try:
        bridge.submit(task)
        body = {"type": "step", "idempotency_key": "step-once", "meta_data": {}}

        first = client.post(f"/api/v1/workflow-tasks/{TASK_UUID}/commands", json=body)
        replay = client.post(f"/api/v1/workflow-tasks/{TASK_UUID}/commands", json=body)

        assert first.status_code == 201, first.text
        assert first.json()["data"]["status"] == "succeeded"
        assert replay.json()["data"]["uuid"] == first.json()["data"]["uuid"]
        assert len(dispatcher.dispatched) == 1
        assert store.count_rows("workflow_task_command") == 1
    finally:
        bridge.close()


def test_step_state_api_is_authoritative_and_blocks_overlapping_step(
    store: WorkflowStore,
) -> None:
    """Task 详情只使用后端候选，上一 Job 在途时第二次 Step 被拒绝。"""

    task = _seed_task(store, with_material=False, run_mode="step")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    client = TestClient(create_workflow_app(service))
    try:
        bridge.submit(task)
        before = client.get(f"/api/v1/workflow-tasks/{TASK_UUID}/step-state")
        assert before.status_code == 200
        assert before.json()["data"] == {
            "workflow_task_uuid": TASK_UUID,
            "execution_mode": "step",
            "control_status": "paused",
            "in_flight_job_count": 0,
            "requires_selection": False,
            "can_step": True,
            "blocked_candidates": [],
            "candidates": [
                {
                    "node_uuid": NODE_UUID,
                    "name": "distribute",
                    "kind": "device_action",
                    "device_id": "reactor-a",
                    "action_name": "distribute",
                }
            ],
        }

        first = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "step", "idempotency_key": "step-first"},
        )
        second = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "step", "idempotency_key": "step-overlap"},
        )

        assert first.status_code == 201, first.text
        assert first.json()["data"]["status"] == "succeeded"
        assert second.json()["data"]["status"] == "rejected"
        assert "step is still in progress" in second.json()["data"]["result"][
            "reason"
        ]
        during = client.get(f"/api/v1/workflow-tasks/{TASK_UUID}/step-state")
        assert during.json()["data"]["can_step"] is False
        assert during.json()["data"]["in_flight_job_count"] == 1
    finally:
        bridge.close()


def test_paused_automatic_task_steps_one_node_and_returns_to_step(
    store: WorkflowStore,
) -> None:
    """暂停中的自动运行再点单步时，先切回单步并只派发一个节点。"""

    task = _seed_task(store, with_material=False, with_successor=True, run_mode="step")
    plan = task["execution_plan"]
    for node in plan["nodes"]:
        node["param_schema"] = {
            "type": "object",
            "properties": {"goal": {"type": "object", "properties": {}}},
        }
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET execution_plan=? WHERE uuid=?",
            (json.dumps(plan), TASK_UUID),
        )
    task = store.get_task(TASK_UUID)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    client = TestClient(create_workflow_app(service))
    try:
        bridge.submit(task)
        switched = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "resume", "idempotency_key": "resume-automatic"},
        )
        assert switched.status_code == 201, switched.text
        assert switched.json()["data"]["status"] == "succeeded"
        assert [item["job_id"] for item in dispatcher.dispatched] == [JOB_UUID]

        paused = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "pause", "idempotency_key": "pause-while-running"},
        )
        assert paused.json()["data"]["status"] == "succeeded"
        blocked = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "step", "idempotency_key": "step-while-in-flight"},
        )
        assert blocked.json()["data"]["status"] == "rejected"
        assert "still in progress" in blocked.json()["data"]["result"]["reason"]
        assert store.get_task(TASK_UUID)["execution_mode"] == "normal"

        scheduler.on_job_finished(JOB_UUID, True, {})
        assert [item["job_id"] for item in dispatcher.dispatched] == [JOB_UUID]

        stepped = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "step", "idempotency_key": "step-after-pause"},
        )
        assert stepped.status_code == 201, stepped.text
        assert stepped.json()["data"]["status"] == "succeeded"
        assert store.get_task(TASK_UUID)["execution_mode"] == "step"
        assert store.get_task(TASK_UUID)["control_status"] == "paused"
        assert len(dispatcher.dispatched) == 2
    finally:
        bridge.close()


def test_failed_switch_to_step_restores_a_preexisting_pause(
    store: WorkflowStore,
) -> None:
    """单步切换失败时保留调用前的暂停闸门。"""

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    client = TestClient(create_workflow_app(service))
    try:
        bridge.submit(task)
        paused = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "pause", "idempotency_key": "pause-before-switch"},
        )
        assert paused.status_code == 201, paused.text
        assert store.get_task(TASK_UUID)["dispatch_gate"] == "paused"

        def fail_switch(_task_uuid: str) -> dict[str, Any]:
            raise ValueError("scheduler unavailable")

        scheduler.switch_to_step = fail_switch  # type: ignore[method-assign]
        failed = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "switch_to_step", "idempotency_key": "switch-fails"},
        )

        assert failed.status_code == 201
        assert failed.json()["data"]["status"] == "rejected"
        restored = store.get_task(TASK_UUID)
        assert restored["execution_mode"] == "normal"
        assert restored["control_status"] == "paused"
        assert restored["dispatch_gate"] == "paused"
    finally:
        bridge.close()


def test_paused_former_step_task_resumes_as_normal_mode(
    store: WorkflowStore,
) -> None:
    """单步任务恢复自动运行后再暂停，继续命令必须沿普通暂停路径恢复。"""

    task = _seed_task(store, with_material=False, run_mode="step")
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    client = TestClient(create_workflow_app(service))
    try:
        bridge.submit(task)
        switched = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "resume", "idempotency_key": "resume-automatic"},
        )
        assert switched.status_code == 201, switched.text
        assert switched.json()["data"]["status"] == "succeeded"
        automatic = store.get_task(TASK_UUID)
        assert automatic["run_mode"] == "step"
        assert automatic["execution_mode"] == "normal"
        assert automatic["control_status"] == "active"

        paused = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "pause", "idempotency_key": "pause-after-automatic"},
        )
        assert paused.status_code == 201, paused.text
        assert paused.json()["data"]["status"] == "succeeded"
        paused_task = store.get_task(TASK_UUID)
        assert paused_task["execution_mode"] == "normal"
        assert paused_task["control_status"] == "paused"
        assert paused_task["dispatch_gate"] == "paused"

        resumed = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "resume", "idempotency_key": "resume-after-pause"},
        )
        assert resumed.status_code == 201, resumed.text
        assert resumed.json()["data"]["status"] == "succeeded"
        assert "not in step mode" not in resumed.text
        final = store.get_task(TASK_UUID)
        assert final["execution_mode"] == "normal"
        assert final["control_status"] == "active"
        assert final["dispatch_gate"] == "active"
    finally:
        bridge.close()


def test_pause_resume_command_api_updates_standard_task_control_status(
    store: WorkflowStore,
) -> None:
    task = _seed_task(store, with_material=False, with_successor=True)
    plan = task["execution_plan"]
    plan["nodes"][1]["param_schema"] = {"type": "object", "properties": {"goal": {"type": "object", "properties": {}}}}
    with store.transaction() as connection:
        connection.execute("UPDATE workflow_task SET execution_plan=? WHERE uuid=?", (json.dumps(plan), TASK_UUID))
    task = store.get_task(TASK_UUID)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    client = TestClient(create_workflow_app(service))
    try:
        bridge.submit(task)
        paused = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "pause", "idempotency_key": "pause-once"},
        )
        assert paused.status_code == 201, paused.text
        assert paused.json()["data"]["status"] == "succeeded"
        paused_replay = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "pause", "idempotency_key": "pause-once"},
        )
        assert paused_replay.json()["data"] == paused.json()["data"]
        paused_task = store.get_task(TASK_UUID)
        assert paused_task["control_status"] == "paused"
        assert paused_task["execution_mode"] == "normal"
        assert paused_task["dispatch_gate"] == "paused"
        matrix_items = client.get(
            "/api/v1/workflow-task-presentations?view=matrix"
        ).json()["data"]["items"]
        matrix_task = next(item for item in matrix_items if item["uuid"] == TASK_UUID)
        assert matrix_task["dispatch_gate"] == "paused"

        scheduler.on_job_finished(JOB_UUID, True, {})
        paused_after_finish = store.get_task(TASK_UUID)
        assert paused_after_finish["execution_mode"] == "normal"
        assert paused_after_finish["dispatch_gate"] == "paused"
        assert paused_after_finish["run_mode"] == "normal"

        resumed = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "resume", "idempotency_key": "resume-once"},
        )
        assert resumed.status_code == 201, resumed.text
        assert resumed.json()["data"]["status"] == "succeeded"
        resumed_replay = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "resume", "idempotency_key": "resume-once"},
        )
        assert resumed_replay.json()["data"] == resumed.json()["data"]
        automatic = store.get_task(TASK_UUID)
        assert automatic["control_status"] == "active"
        assert automatic["execution_mode"] == "normal"
        assert automatic["dispatch_gate"] == "active"
        assert automatic["run_mode"] == "normal"
    finally:
        bridge.close()


def test_paused_task_can_update_only_pending_job_param(
    store: WorkflowStore,
) -> None:
    """暂停后只允许修改尚未下发 Job，恢复时内存调度态同步新参数。"""

    task = _seed_task(store, with_material=False, with_successor=True)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    service = WorkflowService(store, task_scheduler_bridge=bridge)
    client = TestClient(create_workflow_app(service))
    try:
        bridge.submit(task)
        paused = client.post(
            f"/api/v1/workflow-tasks/{TASK_UUID}/commands",
            json={"type": "pause", "idempotency_key": "pause-param"},
        )
        assert paused.status_code == 201, paused.text
        updated = client.patch(
            f"/api/v1/workflow-tasks/{TASK_UUID}/jobs/{SECOND_JOB_UUID}/param",
            json={"param": {"goal": {"volume": 12}}},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["job"]["param"] == {"goal": {"volume": 12}}
        assert store.get_job(SECOND_JOB_UUID)["param"] == {"goal": {"volume": 12}}
        # The same patch is applied to the in-memory WorkflowRun while the
        # dispatch gate is closed; resuming must use this value rather than the
        # stale execution-plan snapshot.
        assert scheduler._workflows[TASK_UUID].node(SECOND_NODE_UUID).param == {
            "goal": {"volume": 12}
        }
        merged = client.patch(
            f"/api/v1/workflow-tasks/{TASK_UUID}/jobs/{SECOND_JOB_UUID}/param",
            json={"param": {"goal": {"temperature": 4}}},
        )
        assert merged.status_code == 200, merged.text
        assert store.get_job(SECOND_JOB_UUID)["param"] == {
            "goal": {"volume": 12, "temperature": 4}
        }
        assert scheduler._workflows[TASK_UUID].node(SECOND_NODE_UUID).param == {
            "goal": {"volume": 12, "temperature": 4}
        }

        dispatched = client.patch(
            f"/api/v1/workflow-tasks/{TASK_UUID}/jobs/{JOB_UUID}/param",
            json={"param": {"goal": {"volume": 99}}},
        )
        assert dispatched.status_code == 200, dispatched.text
        assert dispatched.json()["code"] == 3003
        assert store.get_job(JOB_UUID)["param"] == {}
    finally:
        bridge.close()


def _seed_two_node_debug_task(store: WorkflowStore) -> dict[str, Any]:
    """安装两个真实调度作业与一份持久 Debug Configuration。"""

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="debug bridge",
        tags=[],
        description=None,
        meta_data={},
    )
    plan = {
        "version": 1,
        "run_mode": "step",
        "nodes": [
            {
                "uuid": NODE_UUID,
                "kind": "device_action",
                "device_id": "debug-device",
                "action_name": "first",
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
            },
            {
                "uuid": SECOND_NODE_UUID,
                "kind": "device_action",
                "device_id": "debug-device",
                "action_name": "second",
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
            },
        ],
        "handles": [],
        "edges": [
            {
                "uuid": "71000000-0000-4000-8000-000000000001",
                "source_node_uuid": NODE_UUID,
                "target_node_uuid": SECOND_NODE_UUID,
                "dependency_only": True,
            }
        ],
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{"debug":true}', ?, 'pending',
                      '{}', ?, 'step', NULL, 'paused', 'none', '{}', '{}',
                      '{}', '[]')
            """,
            (TASK_UUID, _CREATED_AT, _CREATED_AT, WORKFLOW_UUID, json.dumps(plan)),
        )
        for index, (job_uuid, node_uuid) in enumerate(
            ((JOB_UUID, NODE_UUID), (SECOND_JOB_UUID, SECOND_NODE_UUID))
        ):
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status,
                    attempt, param, feedback_data, return_info, control_data,
                    error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?,
                          'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                          '{}', '{}', '[]')
                """,
                (job_uuid, _CREATED_AT, _CREATED_AT, TASK_UUID, node_uuid, index),
            )
    store.create_debug_configuration(
        task_uuid=TASK_UUID,
        start_node_uuids=[NODE_UUID],
        breakpoint_node_uuids=[SECOND_NODE_UUID],
    )
    return store.get_task(TASK_UUID)


def test_restart_preserves_pending_paused_step_task_without_dispatch(
    store: WorkflowStore,
) -> None:
    _seed_task(store, with_material=False, run_mode="step")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        recovered = bridge.recover_active_tasks()
        assert [item["task"]["uuid"] for item in recovered] == [TASK_UUID]
        assert dispatcher.dispatched == []

        assert store.get_task(TASK_UUID)["status"] == "pending"
        assert store.get_task(TASK_UUID)["control_status"] == "paused"
        assert store.get_job(JOB_UUID)["status"] == "pending"
        assert store.get_job(JOB_UUID)["error_info"] == []
    finally:
        bridge.close()


def test_restart_preserves_pending_paused_normal_task_without_dispatch(
    store: WorkflowStore,
) -> None:
    """普通暂停意图在调度器重建后仍关闭新 Job 派发。"""

    _seed_task(store, with_material=False, run_mode="normal")
    store.set_task_dispatch_gate(TASK_UUID, dispatch_gate="paused")
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        recovered = bridge.recover_active_tasks()

        assert [item["task"]["uuid"] for item in recovered] == [TASK_UUID]
        assert dispatcher.dispatched == []
        recovered_task = store.get_task(TASK_UUID)
        assert recovered_task["status"] == "pending"
        assert recovered_task["run_mode"] == "normal"
        assert recovered_task["execution_mode"] == "normal"
        assert recovered_task["control_status"] == "paused"
        assert recovered_task["dispatch_gate"] == "paused"
        assert store.get_job(JOB_UUID)["status"] == "pending"
    finally:
        bridge.close()


def test_material_task_without_scheduler_inventory_fails_closed(
    store: WorkflowStore,
) -> None:
    """带物料需求但没有库存服务时必须在物理派发前关闭失败。

    参数：``store`` 是隔离任务权威。返回无；断言无设备命令且标准任务/作业仍为
    ``pending``。异常：桥必须抛稳定错误而不是让旧调度器无预留继续执行。
    """

    task = _seed_task(store, with_material=True)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        with pytest.raises(RuntimeError, match="库存"):
            bridge.submit(task)
    finally:
        bridge.close()

    assert dispatcher.dispatched == []
    assert store.get_task(TASK_UUID)["status"] == "pending"
    assert store.get_job(JOB_UUID)["status"] == "pending"


def test_insufficient_stock_stays_pending_and_reuses_scheduler_inventory(
    store: WorkflowStore,
) -> None:
    """物料不足只形成内部等料，外部标准事实保持待处理。

    参数：``store`` 是隔离任务权威。返回无；断言桥实际调用调度器持有的同一库存
    服务且不派发。
    """

    task = _seed_task(store, with_material=True)
    inventory = _ToggleInventory(available=False)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = _bridge(store, scheduler)
    try:
        aggregate = bridge.submit(task)
    finally:
        bridge.close()

    assert [call[0] for call in inventory.reserve_calls] == [TASK_UUID, TASK_UUID]
    assert dispatcher.dispatched == []
    assert aggregate["task"]["status"] == "pending"
    assert {job["status"] for job in aggregate["jobs"]} == {"pending"}


def test_admission_retry_keeps_task_and_job_identities(store: WorkflowStore) -> None:
    """补料后的准入重试（AdmissionRetry）必须复用既有任务与作业。

    参数：``store`` 是隔离任务权威。返回无；断言通过调度器重排恢复派发，数据库
    没有新增身份。
    """

    task = _seed_task(store, with_material=True)
    inventory = _ToggleInventory(available=False)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(task)
        inventory.available = True
        aggregate = bridge.retry_admission(TASK_UUID)
    finally:
        bridge.close()

    assert aggregate["task"]["uuid"] == TASK_UUID
    assert [job["uuid"] for job in aggregate["jobs"]] == [JOB_UUID]
    assert dispatcher.dispatched[0]["job_id"] == JOB_UUID
    assert len(store.list_jobs(TASK_UUID)) == 1


def test_predispatch_projection_conflict_blocks_dispatcher(
    store: WorkflowStore,
) -> None:
    """派发前投影冲突不得越过执行适配器边界。

    参数：``store`` 是隔离任务权威。返回无；先制造作业终态冲突，再断言桥抛错且
    设备派发器没有收到命令。
    """

    task = _seed_task(store, with_material=False)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node_job SET status = 'failed' WHERE uuid = ?",
            (JOB_UUID,),
        )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        with pytest.raises(RuntimeError):
            bridge.submit(task)
    finally:
        bridge.close()

    assert dispatcher.dispatched == []


def test_success_callback_projects_standard_succeeded_state(
    store: WorkflowStore,
) -> None:
    """明确成功回调必须聚合为标准 ``succeeded`` 终态。

    参数：``store`` 是隔离任务权威。返回无；断言旧调度器 ``success`` 被适配为
    工作流任务（WorkflowTask）与作业的规范成功状态和结果对象。
    """

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(task)
        scheduler.on_job_finished(JOB_UUID, True, {"volume": 2.0})
    finally:
        bridge.close()

    assert store.get_task(TASK_UUID)["status"] == "succeeded"
    assert store.get_job(JOB_UUID)["status"] == "succeeded"
    assert store.get_job(JOB_UUID)["return_info"] == {"volume": 2.0}


def test_failure_callback_projects_standard_error_details(
    store: WorkflowStore,
) -> None:
    """明确失败回调必须写入标准 ``failed`` 与稳定错误详情。

    参数：``store`` 是隔离任务权威。返回无；断言失败不会被包装成成功，也不会
    丢失旧调度器的人工处理分类。
    """

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(task)
        scheduler.on_job_finished(
            JOB_UUID,
            False,
            {"reason": "pump_error"},
            "operator_intervention",
        )
    finally:
        bridge.close()

    job = store.get_job(JOB_UUID)
    assert store.get_task(TASK_UUID)["status"] == "running"
    assert store.get_task(TASK_UUID)["control_status"] == "error_waiting"
    assert job["status"] == "failed"
    assert job["return_info"] == {"reason": "pump_error"}
    assert job["error_info"] == [
        {
            "code": "legacy_edge_scheduler_action_failed",
            "message": "设备动作执行失败",
            "suc_type": "operator_intervention",
        }
    ]


def test_cancel_after_error_releases_quantity_before_marking_cleanup_settled(
    store: WorkflowStore,
) -> None:
    """异常终态必须先释放库存数量预留，再声明物理清理已经完成。

    参数：``store`` 是隔离任务权威。返回无；断言失败作业完成后调用顺序严格为
    ``release_task`` 后 ``project_cleanup_settled``。若释放失败，清理状态不得
    提前变成 settled，避免重启恢复跳过仍活动的库存预留。
    """

    class _QuantityReleaseRecorder:
        """记录测试任务的数量预留释放，不访问库存数据库。"""

        def release_task(self, task_uuid: str, *, reason: str) -> None:
            """记录任务和原因；参数均来自终态清理，返回无且不抛异常。"""

            assert task_uuid == TASK_UUID
            assert reason == "workflow_canceled"
            calls.append("quantity")

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    calls: list[str] = []
    original_cleanup = bridge._projection.project_cleanup_settled

    def project_cleanup_settled(task_uuid: str) -> dict[str, Any]:
        """记录最终清理提交；参数是任务身份，返回真实持久投影结果。"""

        calls.append("cleanup")
        return original_cleanup(task_uuid)

    bridge._quantity_inventory = _QuantityReleaseRecorder()
    bridge._projection.project_cleanup_settled = project_cleanup_settled
    try:
        bridge.submit(task)
        scheduler.on_job_finished(
            JOB_UUID,
            False,
            {"reason": "pump_error"},
            "operator_intervention",
        )
        assert calls == []
        bridge.cancel(TASK_UUID, reason="operator_cancel_after_error")
    finally:
        bridge.close()

    assert calls == ["quantity", "cleanup"]
    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"


def test_restart_finishes_terminal_inventory_cleanup_without_reexecution(
    store: WorkflowStore,
) -> None:
    """重启必须补扫已终态但尚未完成库存释放的任务。

    参数：``store`` 是隔离任务权威。返回无；先模拟失败结果已提交、进程在 settled
    回调前退出，再启动桥并恢复。断言只释放原任务预留并提交 cleanup settled，
    不派发任何新设备动作。
    """

    class _QuantityReleaseRecorder:
        """记录恢复扫描触发的数量预留释放。"""

        def recover_pending(self) -> None:
            """模拟跨库 Saga 已无待重放项；参数与返回均为空。"""

        def pending_consumption_reconciliations(self) -> list[dict[str, str]]:
            """该恢复场景没有尚待人工核对的消费意图。"""

            return []

        def release_task(self, task_uuid: str, *, reason: str) -> None:
            """记录终态释放；参数为任务身份和恢复原因，返回无。"""

            calls.append((task_uuid, reason))

    task = _seed_task(store, with_material=False)
    first_scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    first_bridge = _bridge(store, first_scheduler)
    first_bridge.submit(task)
    first_scheduler.remove_job_settled_listener(first_bridge._on_job_settled)
    first_scheduler.on_job_finished(
        JOB_UUID,
        False,
        {"reason": "pump_error"},
        "operator_intervention",
    )
    # 兼容历史已终态任务的清理；新错误等待不会自行终态化。
    with store.transaction() as connection:
        connection.execute("UPDATE workflow_task SET status='failed',control_status='active' WHERE uuid=?", (TASK_UUID,))
    first_bridge.close()
    assert store.get_task(TASK_UUID)["status"] == "failed"
    assert store.get_task(TASK_UUID)["cleanup_status"] == "none"

    dispatcher = RecordingDispatcher()
    restarted = _bridge(store, EdgeScheduler(dispatcher=dispatcher))
    calls: list[tuple[str, str]] = []
    restarted._quantity_inventory = _QuantityReleaseRecorder()
    try:
        recovered = restarted.recover_active_tasks()
    finally:
        restarted.close()

    assert recovered == []
    assert calls == [(TASK_UUID, "workflow_failed_recovery")]
    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
    assert dispatcher.dispatched == []


def test_restart_finishes_reconciled_attention_cleanup_without_reexecution(
    store: WorkflowStore,
) -> None:
    """重启补扫已完成物理对账、但仍标记 requires_attention 的失败任务。"""

    _seed_task(store, with_material=False)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET status = 'failed', "
            "cleanup_status = 'requires_attention' WHERE uuid = ?",
            (TASK_UUID,),
        )
        connection.execute(
            "UPDATE workflow_node_job SET status = 'failed', "
            "uncertainty_reason = NULL WHERE workflow_task_uuid = ?",
            (TASK_UUID,),
        )

    dispatcher = RecordingDispatcher()
    restarted = _bridge(store, EdgeScheduler(dispatcher=dispatcher))
    try:
        recovered = restarted.recover_active_tasks()
    finally:
        restarted.close()

    assert recovered == []
    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
    assert dispatcher.dispatched == []


@pytest.mark.parametrize(
    ("outcome", "task_status"),
    [("failed", "running"), ("canceled", "canceled"), ("timeout", "running")],
)
def test_edge_http_outcome_projects_exact_terminal_evidence(
    store: WorkflowStore,
    outcome: str,
    task_status: str,
) -> None:
    """Edge HTTP 终态必须不经旧回调压缩地写入标准任务事实。

    参数：``store`` 是隔离任务权威；``outcome`` 与 ``task_status`` 覆盖 Backend
    允许的三种非成功结果。返回无；断言作业终态、任务终态、返回对象与错误数组
    均逐字段保真。异常：任何投影冲突都会使测试失败，且不会触发物理重做。
    """

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    error_info = [{"code": f"device_{outcome}", "message": "设备终态"}]
    try:
        bridge.submit(task)
        scheduler.on_job_outcome(
            JOB_UUID,
            CommittedJobOutcome(
                outcome=outcome,
                return_info={"last_step": 3},
                error_info=error_info,
                unknown_command_ids=[],
            ),
        )
    finally:
        bridge.close()

    job = store.get_job(JOB_UUID)
    assert store.get_task(TASK_UUID)["status"] == task_status
    assert job["status"] == outcome
    assert job["return_info"] == {"last_step": 3}
    assert job["error_info"] == error_info


@pytest.mark.parametrize(
    "outcome",
    ["succeeded", "failed", "canceled", "timeout", "connection_lost"],
)
def test_edge_material_transfer_settles_only_after_inventory_is_certain(
    store: WorkflowStore,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """双进程转运只有物料位置明确后才能释放调度侧 Claim。

    参数：``store`` 是隔离任务权威；``outcome`` 覆盖成功与失败停止证明。返回无；
    断言成功结果先结算位置再释放，失败结果冻结 Claim/Fence，直到操作员提交实际
    ChangeSet 才释放。异常：库存结算丢失、身份漂移或提前释放会使测试失败。
    """

    class _TransferInventory:
        """记录物料移动且不启用数量库存表。"""

        store = None

        def __init__(self) -> None:
            """创建空调用列表；参数、返回和异常均为空。"""

            self.moves: list[dict[str, Any]] = []
            self.claim_states: list[str] = []

        def resolve_target_site(self, request: Any) -> StationSiteTarget:
            """返回测试冻结的唯一目标库位。

            参数：``request`` 是生产目标选择条件。返回：明确 UUID、名称和父设备。
            异常：无；该替身只服务本纵向结果测试。
            """

            return StationSiteTarget(
                uuid=request.site_uuid,
                name="A1",
                owner_material_uuid=request.owner_material_uuid,
            )

        def resolve_transfer_resources(self, request: Any) -> TransferResourceFacts:
            """返回完整来源、两端设备和夹爪事实。

            参数：``request`` 是转运资源解析请求。返回：测试固定资源集合。异常：
            无；库存原子复验由专门集成测试覆盖。
            """

            return TransferResourceFacts(
                source_site_uuid="72000000-0000-4000-8000-000000000001",
                source_site_name="SOURCE",
                source_owner_material_uuid=("73000000-0000-4000-8000-000000000001"),
                source_device_material_uuid=("73000000-0000-4000-8000-000000000001"),
                target_device_material_uuid=request.target_owner_material_uuid,
                gripper_site_uuid="74000000-0000-4000-8000-000000000001",
            )

        def acquire_dispatch_permit(self, request: Any) -> DispatchAdmissionDecision:
            """签发与完整锁集合一一对应的测试 Permit。

            参数：``request`` 是桥生成的不可变派发请求。返回：稳定 Claim 和每项
            Fence。异常：无；字段漂移会由生产投影再次校验。
            """

            permit = DispatchPermit(
                effect_uuid=request.effect_uuid,
                claim_uuid="75000000-0000-4000-8000-000000000001",
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
            return DispatchAdmissionDecision(permit=permit)

        def transition_dispatch_permit(
            self,
            claim_uuid: str,
            *,
            target_state: str,
        ) -> None:
            """记录测试 Claim 的生命周期转换。

            参数：Claim 身份和目标状态。返回：无。异常：身份不匹配时断言失败。
            """

            assert claim_uuid == "75000000-0000-4000-8000-000000000001"
            self.claim_states.append(target_state)

        def release_unprojected_dispatch_permits(
            self,
            *,
            known_claim_uuids: tuple[str, ...],
        ) -> tuple[str, ...]:
            """恢复扫描只观察当前 Workflow Claim，不改写测试 Permit。"""

            assert known_claim_uuids == (
                "75000000-0000-4000-8000-000000000001",
            )
            return ()

        def move_instance(self, material_uuid: str, **kwargs: Any) -> dict[str, Any]:
            """记录转移结算命令并返回同一事实。

            参数：物料 UUID 与目标/审计字段。返回：完整记录。异常：无。
            """

            move = {"material_uuid": material_uuid, **kwargs}
            self.moves.append(move)
            return move

        def settle_material_transfer(self, command: Any) -> dict[str, Any]:
            """通过工站资源窄接口记录转运结算命令。

            参数：``command`` 是生产代码冻结的物料转移命令。返回：兼容原断言的
            移动事实。异常：无。
            """

            return self.move_instance(
                command.material_uuid,
                parent_uuid=command.target_owner_material_uuid,
                slot_id=(
                    "A1"
                    if command.target_site_uuid
                    == "71000000-0000-4000-8000-000000000001"
                    else command.target_site_name
                ),
                actor=command.actor,
                causation_id=command.causation_id,
            )

    _seed_task(store, with_material=False)
    parent_uuid = "52000000-0000-4000-8000-000000000001"
    robot_uuid = "70000000-0000-4000-8000-000000000001"
    target_site_uuid = "71000000-0000-4000-8000-000000000001"
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT execution_plan FROM workflow_task WHERE uuid=?",
            (TASK_UUID,),
        ).fetchone()
        plan = json.loads(str(row["execution_plan"]))
        plan["nodes"][0]["kind"] = "material_transfer"
        plan["nodes"][0]["material_uuid"] = robot_uuid
        plan["nodes"][0]["action_resource_contract"] = {
            "version": 1,
            "transfer": {
                "material_param": "resource",
                "source_owner_param": "",
                "source_site_uuid_param": "",
                "source_site_name_param": "",
                "target_owner_param": "mount_resource",
                "target_site_uuid_param": "site_uuid",
                "target_site_name_param": "",
                "gripper_site_role": "robot.gripper",
            },
        }
        plan["nodes"][0]["param"] = {
            "resource": {"uuid": MATERIAL_UUID},
            "mount_resource": {"uuid": parent_uuid},
            "site_uuid": target_site_uuid,
        }
        connection.execute(
            "UPDATE workflow_task SET execution_plan=? WHERE uuid=?",
            (json.dumps(plan), TASK_UUID),
        )
        connection.execute(
            "UPDATE workflow_node_job SET executor_kind='material_transfer', param=? "
            "WHERE uuid=?",
            (json.dumps(plan["nodes"][0]["param"]), JOB_UUID),
        )
    inventory = _TransferInventory()
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
        station_resources=inventory,
    )
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        expected_outcome = "failed" if outcome == "connection_lost" else outcome
        if outcome == "connection_lost":
            scheduler.on_execution_connection_lost((JOB_UUID,))
            scheduler.on_execution_connection_lost((JOB_UUID,))
            assert store.get_job(JOB_UUID)["status"] == "running"
            scheduler.on_job_outcome(JOB_UUID, CommittedJobOutcome("failed", {"result": "stopped"}, [{"code": "grip_failed"}], []))
        else:
            scheduler.on_job_outcome(
                JOB_UUID,
                CommittedJobOutcome(
                    outcome=outcome,
                    return_info={
                        "result": "ok" if outcome == "succeeded" else "stopped"
                    },
                    error_info=(
                        [] if outcome == "succeeded" else [{"code": "grip_failed"}]
                    ),
                    unknown_command_ids=[],
                ),
            )
        if expected_outcome != "succeeded":
            failed_job = store.get_job(JOB_UUID)
            assert failed_job["status"] == expected_outcome
            assert failed_job["uncertainty_reason"] == (
                "material_transfer_inventory_reconciliation_required"
            )
            assert inventory.moves == []
            assert inventory.claim_states == [
                "reserved",
                "running",
                "uncertain",
                *(["uncertain"] if outcome == "connection_lost" else []),
            ]
            assert {
                item["state"]
                for item in TaskRuntimeProjection(store).list_execution_locks(JOB_UUID)
            } == {"uncertain"}
            # 经真实 HTTP、服务和桥验证拒绝原因，同时确认拒绝不能释放占用。
            request = {
                "actual_change_set": {
                    "kind": "material_transfer",
                    "material_uuid": MATERIAL_UUID,
                    "target_owner_material_uuid": parent_uuid,
                    "target_site_uuid": target_site_uuid,
                },
                "reason": "现场核对物料实际位置",
            }
            locks_before = TaskRuntimeProjection(store).list_execution_locks(JOB_UUID)
            claims_before = list(inventory.claim_states)
            client = TestClient(create_workflow_app(
                WorkflowService(store, task_scheduler_bridge=bridge)
            ))
            try:
                # 缺少停止证明是桥接前置拒绝，没有库存细码也必须保留明确说明。
                control = failed_job["control_data"]
                without_proof = dict(control)
                without_proof.pop("physical_settlement")
                with store.transaction() as connection:
                    connection.execute(
                        "UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                        (json.dumps(without_proof), JOB_UUID),
                    )
                try:
                    response = client.post(
                        f"/api/v1/workflow-node-jobs/{JOB_UUID}/settle-material-transfer",
                        json=request,
                    )
                    assert response.status_code == 200
                    assert response.json() == {
                        "code": 3003,
                        "error": {"msg": "失败转运缺少匹配的预期变化或设备停止证明"},
                    }
                    assert store.get_job(JOB_UUID)["control_data"] == without_proof
                finally:
                    with store.transaction() as connection:
                        connection.execute(
                            "UPDATE workflow_node_job SET control_data=? WHERE uuid=?",
                            (json.dumps(control), JOB_UUID),
                        )
                for code, message in [
                    (
                        "settlement_actual_site_not_claimed",
                        "PhysicalSettlement 实际库位不在派发时冻结的 Claim/Fence 中",
                    ),
                    (
                        "settlement_change_set_mismatch",
                        "PhysicalSettlement 物料与派发时冻结的 ChangeSet 不一致",
                    ),
                    ("site_occupied", "PhysicalSettlement 目标库位已被其他物料占用"),
                    ("settlement_source_mismatch", "PhysicalSettlement 来源库位事实与派发快照不一致"),
                ]:
                    def reject_settlement(
                        command: Any, *, code: str = code, message: str = message
                    ) -> dict[str, Any]:
                        """仅替换库存边界，完整保留 HTTP、桥及工作流投影路径。"""
                        raise StationResourceError(code, message)

                    caplog.clear()
                    with monkeypatch.context() as patch:
                        patch.setattr(inventory, "settle_material_transfer", reject_settlement)
                        response = client.post(
                            f"/api/v1/workflow-node-jobs/{JOB_UUID}/settle-material-transfer",
                            json=request,
                        )
                    assert response.status_code == 200
                    assert response.json() == {
                        "code": 3003,
                        "error": {"msg": message, "details": {"reason_code": code}},
                    }
                    records = [
                        record for record in caplog.records
                        if record.name == "unilabos.workflow.service"
                    ]
                    assert len(records) == 1
                    assert all(
                        value in records[0].getMessage()
                        for value in [JOB_UUID, target_site_uuid, code, message]
                    )
                    assert store.get_job(JOB_UUID) == failed_job
                    assert TaskRuntimeProjection(store).list_execution_locks(JOB_UUID) == locks_before
                    assert inventory.moves == []
                    assert inventory.claim_states == claims_before
                response = client.post(
                    f"/api/v1/workflow-node-jobs/{JOB_UUID}/settle-material-transfer",
                    json=request,
                )
                assert response.status_code == 200
                assert response.json()["code"] == 0
                assert response.json()["data"]["status"] == expected_outcome
            finally:
                client.close()
    finally:
        bridge.close()

    assert inventory.moves == [
        {
            "material_uuid": MATERIAL_UUID,
            "parent_uuid": parent_uuid,
            "slot_id": "A1",
            "actor": (
                "station_scheduler.material_transfer"
                if outcome == "succeeded"
                else "physical_settlement"
            ),
            "causation_id": (
                f"workflow-node-job:{JOB_UUID}:material-transfer"
                if outcome == "succeeded"
                else JOB_UUID
            ),
        }
    ]
    assert store.get_job(JOB_UUID)["status"] == expected_outcome
    assert store.get_task(TASK_UUID)["status"] == ("running" if expected_outcome in {"failed", "timeout"} else expected_outcome)
    assert store.get_job(JOB_UUID).get("uncertainty_reason") is None
    if outcome == "succeeded":
        assert inventory.claim_states == ["reserved", "running", "released"]
    else:
        assert inventory.claim_states == [
            "reserved",
            "running",
            "uncertain",
            *(["uncertain"] if outcome == "connection_lost" else []),
            "released",
        ]
        assert store.get_task(TASK_UUID)["control_status"] == ("error_waiting" if expected_outcome in {"failed", "timeout"} else "active")
        assert store.get_task(TASK_UUID)["cleanup_status"] == ("none" if expected_outcome in {"failed", "timeout"} else "settled")
        assert {
            item["state"]
            for item in TaskRuntimeProjection(store).list_execution_locks(JOB_UUID)
        } == {"released"}


def test_gate7_selected_fallback_site_replaces_dispatch_args_and_local_locks(
    store: WorkflowStore,
) -> None:
    """Gate 7 选择备用库位后，派发参数、持久锁和调度器本地锁必须一致。"""

    first_site_uuid = "71000000-0000-4000-8000-000000000001"
    fallback_site_uuid = "71000000-0000-4000-8000-000000000002"
    parent_uuid = "52000000-0000-4000-8000-000000000001"
    robot_uuid = "70000000-0000-4000-8000-000000000001"

    class _CandidateInventory:
        """让首选在 Gate 7 竞争失败并原子签发第二候选 Permit。"""

        store = None

        def __init__(self) -> None:
            self.candidate_targets: list[str] = []
            self.target_identity_checks: list[tuple[str, bool]] = []

        def resolve_target_site(self, request: Any) -> StationSiteTarget:
            site_uuid = request.site_uuid or request.equivalent_site_uuids[0]
            self.target_identity_checks.append((site_uuid, request.require_available))
            return StationSiteTarget(
                uuid=site_uuid,
                name=("A1" if site_uuid == first_site_uuid else "B1"),
                owner_material_uuid=request.owner_material_uuid,
            )

        def resolve_transfer_resources(self, request: Any) -> TransferResourceFacts:
            return TransferResourceFacts(
                source_site_uuid="72000000-0000-4000-8000-000000000001",
                source_site_name="SOURCE",
                source_owner_material_uuid="73000000-0000-4000-8000-000000000001",
                source_device_material_uuid="73000000-0000-4000-8000-000000000001",
                target_device_material_uuid=request.target_owner_material_uuid,
                gripper_site_uuid="74000000-0000-4000-8000-000000000001",
            )

        def acquire_dispatch_permit_candidates(
            self,
            requests: Any,
        ) -> DispatchAdmissionDecision:
            self.candidate_targets = [
                request.expected_change_set["target_site_uuid"] for request in requests
            ]
            request = requests[1]
            return DispatchAdmissionDecision(
                permit=DispatchPermit(
                    effect_uuid=request.effect_uuid,
                    claim_uuid="75000000-0000-4000-8000-000000000002",
                    task_uuid=request.task_uuid,
                    job_uuid=request.job_uuid,
                    attempt=request.attempt,
                    parameter_hash=request.parameter_hash,
                    expected_change_set=request.expected_change_set,
                    fences=tuple(
                        DispatchFence(resource.lock_key, index)
                        for index, resource in enumerate(request.resources, start=1)
                    ),
                ),
                selected_candidate_index=1,
            )

        def transition_dispatch_permit(
            self,
            claim_uuid: str,
            *,
            target_state: str,
        ) -> None:
            assert claim_uuid == "75000000-0000-4000-8000-000000000002"
            assert target_state in {"reserved", "running"}

    _seed_task(store, with_material=False)
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT execution_plan FROM workflow_task WHERE uuid=?",
            (TASK_UUID,),
        ).fetchone()
        plan = json.loads(str(row["execution_plan"]))
        node = plan["nodes"][0]
        node.update(
            {
                "kind": "material_transfer",
                "material_uuid": robot_uuid,
                "action_resource_contract": {
                    "version": 1,
                    "transfer": {
                        "material_param": "resource",
                        "source_owner_param": "source_warehouse",
                        "source_site_uuid_param": "source_site_uuid",
                        "source_site_name_param": "source_site",
                        "target_owner_param": "mount_resource",
                        "target_site_uuid_param": "site_uuid",
                        "target_site_name_param": "site",
                        "gripper_site_role": "robot.gripper",
                    },
                },
                "execution_policy": {
                    "target_site_group": [first_site_uuid, fallback_site_uuid],
                    "target_site_selection": {
                        "version": 1,
                        "owner_material_uuid": parent_uuid,
                        "group_key": "process_input",
                        "requested_reference": "",
                        "strategy": "sort_order",
                        "site_uuids": [first_site_uuid, fallback_site_uuid],
                        "fingerprint": "sha256:test-selection",
                    },
                },
                "param": {
                    "resource": {"uuid": MATERIAL_UUID},
                    "mount_resource": {"uuid": parent_uuid},
                },
            }
        )
        connection.execute(
            "UPDATE workflow_task SET execution_plan=? WHERE uuid=?",
            (json.dumps(plan), TASK_UUID),
        )
        connection.execute(
            "UPDATE workflow_node_job "
            "SET executor_kind='material_transfer', execution_policy=?, param=? "
            "WHERE uuid=?",
            (
                json.dumps(node["execution_policy"]),
                json.dumps(node["param"]),
                JOB_UUID,
            ),
        )

    inventory = _CandidateInventory()
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        inventory=inventory,
        station_resources=inventory,
    )
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
    finally:
        bridge.close()

    assert inventory.candidate_targets == [first_site_uuid, fallback_site_uuid]
    # 候选选择及源/目标库位锁都需解析身份；占用检查统一留给 Gate 7 的原子 Permit。
    # 校验涉及的库位和检查模式，不把解析调用次数当成调度合同。
    assert {site for site, _ in inventory.target_identity_checks} == {
        first_site_uuid, fallback_site_uuid, "72000000-0000-4000-8000-000000000001",
    }
    assert all(not require_available for _, require_available in inventory.target_identity_checks)
    assert dispatcher.dispatched[0]["action_args"] == {
        "resource": {"uuid": MATERIAL_UUID},
        "mount_resource": {"uuid": parent_uuid},
        "source_warehouse": {"uuid": "73000000-0000-4000-8000-000000000001"},
        "source_site_uuid": "72000000-0000-4000-8000-000000000001",
        "source_site": "SOURCE",
        "site_uuid": fallback_site_uuid,
        "site": "B1",
    }
    fallback_lock = f"material/{parent_uuid}/site/{fallback_site_uuid}/exclusive"
    first_lock = f"material/{parent_uuid}/site/{first_site_uuid}/exclusive"
    assert fallback_lock in scheduler._job_resource_locks[JOB_UUID]
    assert first_lock not in scheduler._job_resource_locks[JOB_UUID]
    claim = TaskRuntimeProjection(store).get_execution_claim(JOB_UUID)
    assert claim is not None
    assert (
        store.get_job(JOB_UUID)["expected_change_set"]["target_site_uuid"]
        == fallback_site_uuid
    )
    assert store.get_job(JOB_UUID)["expected_change_set"]["site_selection"] == {
        "group_key": "process_input",
        "requested_reference": "",
        "strategy": "sort_order",
        "fingerprint": "sha256:test-selection",
        "selected_site_uuid": fallback_site_uuid,
    }


def test_exact_site_selection_audit_allows_an_empty_group_key(
    store: WorkflowStore,
) -> None:
    """精确库位选择无需伪造组名，仍应进入 Claim 的审计 ChangeSet。"""

    target_site_uuid = "71000000-0000-4000-8000-000000000001"
    target_owner_uuid = "52000000-0000-4000-8000-000000000001"
    _seed_task(store, with_material=False)
    bridge = _bridge(store, EdgeScheduler(dispatcher=RecordingDispatcher()))
    try:
        request = bridge._dispatch_admission_request(
            dispatching={
                "transfer_dispatch_condition": {
                    "material_uuid": MATERIAL_UUID,
                    "source_owner_material_uuid": (
                        "73000000-0000-4000-8000-000000000001"
                    ),
                    "source_site_uuid": "72000000-0000-4000-8000-000000000001",
                    "target_owner_material_uuid": target_owner_uuid,
                    "target_site_uuid": target_site_uuid,
                    "executor_material_uuid": ("70000000-0000-4000-8000-000000000001"),
                    "gripper_site_uuid": "74000000-0000-4000-8000-000000000001",
                },
                "site_selection": {
                    "version": 1,
                    "owner_material_uuid": target_owner_uuid,
                    "group_key": "",
                    "requested_reference": "s07.S0721",
                    "strategy": "sort_order",
                    "site_uuids": [target_site_uuid],
                    "fingerprint": "sha256:exact-selection",
                },
            },
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            resolved_args={"site": "S0721"},
            execution_locks=[],
        )
    finally:
        bridge.close()

    assert request.expected_change_set["site_selection"] == {
        "group_key": "",
        "requested_reference": "s07.S0721",
        "strategy": "sort_order",
        "fingerprint": "sha256:exact-selection",
        "selected_site_uuid": target_site_uuid,
    }


@pytest.mark.parametrize("stage", ["pick", "continuation", "unrelated"])
def test_split_transfer_custody_scope_follows_frozen_endpoint(store: WorkflowStore, stage: str) -> None:
    """取料和持料后继从冻结合同继承目标库位范围，其他动作不能借用该范围。"""
    _seed_task(store, with_material=False)
    owner = "52000000-0000-4000-8000-000000000001"
    site = "71000000-0000-4000-8000-000000000001"
    owner_key, site_key = f"material/{owner}/exclusive", f"material/{owner}/site/{site}/exclusive"
    with store.transaction() as connection:
        task = store.get_task(TASK_UUID)
        plan = task["execution_plan"]
        plan["resource_plan"] = {"resources": [
            {"alias": "target", "canonical_key": owner_key},
            {"alias": "site", "canonical_key": site_key},
        ], "metadata": {"transfers": [{
            "pick_node_uuid": NODE_UUID if stage == "pick" else SECOND_NODE_UUID,
            "target_resource": "target", "target_site": "site",
        }]}}
        connection.execute("UPDATE workflow_task SET execution_plan=? WHERE uuid=?", (json.dumps(plan), TASK_UUID))
    bridge = _bridge(store, EdgeScheduler(dispatcher=RecordingDispatcher()))
    try:
        request = bridge._dispatch_admission_request(
            dispatching={"resource_preheld_lock_keys": [owner_key, site_key] if stage == "continuation" else []},
            task_uuid=TASK_UUID, job_uuid=JOB_UUID, resolved_args={},
            execution_locks=[
                {"lock_key": owner_key, "scope": "material", "material_uuid": owner},
                {"lock_key": site_key, "scope": "material_site", "material_uuid": owner, "site_uuid": site},
            ],
        )
        assert request.custody_endpoint_site_uuids == (() if stage == "unrelated" else (site,))
        assert request.reserved_target_site_uuids == ((site,) if stage == "pick" else ())
    finally:
        bridge.close()


def test_edge_http_unknown_outcome_keeps_running_job_for_reconciliation(
    store: WorkflowStore,
) -> None:
    """结果不明证据保持 ``running``，不能伪装成失败终态。

    参数：``store`` 是隔离任务权威。返回无。异常：UNKNOWN 结果释放在途作业、
    推进任务终态或丢失物理对账占用时由断言失败；该路径禁止自动执行重试。
    """

    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    unknown_id = f"workflow-node-job:{JOB_UUID}"
    try:
        bridge.submit(task)
        scheduler.on_job_outcome(
            JOB_UUID,
            CommittedJobOutcome(
                outcome="failed",
                return_info={},
                error_info=[{"code": "edge_disconnected"}],
                unknown_command_ids=[unknown_id],
            ),
        )

        job = store.get_job(JOB_UUID)
        aggregate = store.get_task(TASK_UUID)
        assert job["status"] == "running"
        assert job["uncertainty_reason"].startswith("edge_reported_unknown_commands:")
        assert aggregate["status"] == "running"
        assert aggregate["control_status"] == "error_waiting"
        assert aggregate["cleanup_status"] == "requires_attention"
        assert JOB_UUID in scheduler.snapshot()["inflight_jobs"]
    finally:
        bridge.close()


def test_restart_preserves_unknown_execution_without_replay(
    store: WorkflowStore,
) -> None:
    """重启失败释放旧执行权，不再阻止新 runtime 安全停止。

    参数：``store`` 是隔离任务权威。返回：无；断言新调度器未重放物理动作，
    且排空状态不再报告原 Job。异常：旧 Claim 泄漏出重启边界时测试失败。
    """

    task = _seed_task(store, with_material=False)
    first_scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    first_bridge = _bridge(store, first_scheduler)
    try:
        first_bridge.submit(task)
        first_scheduler.on_job_outcome(
            JOB_UUID,
            CommittedJobOutcome(
                outcome="failed",
                return_info={},
                error_info=[{"code": "edge_disconnected"}],
                unknown_command_ids=[f"workflow-node-job:{JOB_UUID}"],
            ),
        )
    finally:
        first_bridge.close()

    restarted_scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    restarted_bridge = _bridge(store, restarted_scheduler)
    try:
        restarted_bridge.recover_active_tasks()
        drain = restarted_scheduler.begin_drain()
    finally:
        restarted_bridge.close()

    assert list(restarted_scheduler.snapshot()["inflight_jobs"]) == [JOB_UUID]
    assert drain["phase"] == "draining"
    assert drain["active_device_job_ids"] == [JOB_UUID]


def test_terminal_restart_recovery_keeps_released_claim_and_settled_cleanup(
    store: WorkflowStore,
) -> None:
    """重启终态恢复保持 Claim 已释放，清理状态已经结算。"""

    class _ClaimRecorder:
        """记录库存 Claim 生命周期转换的窄测试替身。"""

        def __init__(self) -> None:
            self.transitions: list[tuple[str, str]] = []

        def transition_dispatch_permit(
            self,
            claim_uuid: str,
            *,
            target_state: str,
        ) -> None:
            """记录 Claim 和目标状态；参数均原样保存，返回与异常均无。"""

            self.transitions.append((claim_uuid, target_state))

        def release_unprojected_dispatch_permits(
            self,
            *,
            known_claim_uuids: tuple[str, ...],
        ) -> tuple[str, ...]:
            """记录恢复扫描，不释放任何未投影身份。"""

            return ()

    _seed_task(store, with_material=False)
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[
            {"lock_key": "/devices/reactor-a", "scope": "device"},
        ],
    )
    projection.project_dispatch_accepted(JOB_UUID)
    projection.project_execution_process_restarted(TASK_UUID)
    claim = projection.get_execution_claim(JOB_UUID)
    assert claim is not None
    inventory = _ClaimRecorder()
    bridge = _bridge(
        store,
        EdgeScheduler(
            dispatcher=RecordingDispatcher(),
            station_resources=inventory,
        ),
    )
    try:
        assert bridge.recover_active_tasks() == []
    finally:
        bridge.close()

    assert inventory.transitions == [(claim["claim_uuid"], "released")]
    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
    assert projection.list_execution_locks(JOB_UUID)[0]["state"] == "released"


def test_runtime_restart_retries_failed_permit_cleanup_without_reconciliation(
    store: WorkflowStore,
) -> None:
    """Runtime 失败已提交但库存 Permit 释放失败时，重启只重试软件清理。"""

    class _FlakySettlementInventory:
        """在实际位置提交后让第一次 Permit 释放瞬时失败。"""

        store = None

        def __init__(self) -> None:
            self.fail_release_once = True
            self.claim_state = "uncertain"
            self.transitions: list[str] = []

        def settle_material_transfer(self, command: Any) -> dict[str, Any]:
            """返回已由库存权威提交的确定物料位置。"""

            return {
                "material_uuid": command.material_uuid,
                "parent_uuid": command.target_owner_material_uuid,
                "site_uuid": command.target_site_uuid,
            }

        def transition_dispatch_permit(
            self,
            claim_uuid: str,
            *,
            target_state: str,
        ) -> None:
            """第一次 released 模拟跨权威提交窗口，重试后幂等收敛。"""

            assert claim_uuid == "75000000-0000-4000-8000-000000000095"
            self.transitions.append(target_state)
            if target_state == "released" and self.fail_release_once:
                self.fail_release_once = False
                raise StationResourceError(
                    "inventory_temporarily_unavailable",
                    "库存 Permit 暂时无法释放",
                )
            self.claim_state = target_state

        def release_unprojected_dispatch_permits(
            self,
            *,
            known_claim_uuids: tuple[str, ...],
        ) -> tuple[str, ...]:
            """测试没有未投影的 prepared Permit。"""

            assert known_claim_uuids == ()
            return ()

    _seed_task(store, with_material=False)
    projection = TaskRuntimeProjection(store)
    material_lock_key = f"material/{MATERIAL_UUID}/exclusive"
    expected_change_set = {
        "kind": "material_transfer",
        "material_uuid": MATERIAL_UUID,
        "source_site_uuid": "72000000-0000-4000-8000-000000000095",
        "target_site_uuid": "71000000-0000-4000-8000-000000000095",
    }
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[
            {
                "lock_key": material_lock_key,
                "scope": "material",
                "material_uuid": MATERIAL_UUID,
            }
        ],
        dispatch_permit={
            "effect_uuid": "77000000-0000-4000-8000-000000000095",
            "claim_uuid": "75000000-0000-4000-8000-000000000095",
            "parameter_hash": "restart-settlement-release-window",
            "expected_change_set": expected_change_set,
            "fences": [{"lock_key": material_lock_key, "fencing_token": 1}],
        },
    )
    projection.project_dispatch_accepted(JOB_UUID)
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task_material_claim(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                workflow_node_job_uuid, material_uuid, status, revision,
                acquired_at, released_at
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, ?, 'active', 1,
                      ?, NULL)
            """,
            (
                "78000000-0000-4000-8000-000000000095",
                _CREATED_AT,
                _CREATED_AT,
                TASK_UUID,
                NODE_UUID,
                JOB_UUID,
                MATERIAL_UUID,
                _CREATED_AT,
            ),
        )
    projection.project_execution_process_restarted(TASK_UUID)

    inventory = _FlakySettlementInventory()
    first_bridge = _bridge(
        store,
        EdgeScheduler(
            dispatcher=RecordingDispatcher(),
            station_resources=inventory,
        ),
    )
    try:
        with pytest.raises(StationResourceError, match="库存 Permit 暂时无法释放"):
            first_bridge.recover_active_tasks(execution_process_restarted=True)
    finally:
        first_bridge.close()

    assert store.get_job(JOB_UUID).get("uncertainty_reason") is None
    assert store.get_task(TASK_UUID)["cleanup_status"] == "pending"
    with store.transaction() as connection:
        assert (
            connection.execute(
                "SELECT status FROM workflow_task_material_claim "
                "WHERE workflow_task_uuid=?",
                (TASK_UUID,),
            ).fetchone()[0]
            == "released"
        )

    restarted_bridge = _bridge(
        store,
        EdgeScheduler(
            dispatcher=RecordingDispatcher(),
            station_resources=inventory,
        ),
    )
    try:
        assert restarted_bridge.recover_active_tasks() == []
    finally:
        restarted_bridge.close()

    assert inventory.claim_state == "released"
    assert inventory.transitions == ["released", "released"]
    assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
    with store.transaction() as connection:
        assert connection.execute(
            "SELECT status FROM workflow_task_material_claim "
            "WHERE workflow_task_uuid=?",
            (TASK_UUID,),
        ).fetchone()[0] == "released"


def test_late_result_after_restart_is_ignored_as_stale_execution(
    store: WorkflowStore,
) -> None:
    """重启已冻结结果并释放旧执行权，迟到结果不得覆盖该终态。"""

    class _ClaimRecorder:
        def __init__(self) -> None:
            self.transitions: list[tuple[str, str]] = []

        def transition_dispatch_permit(
            self,
            claim_uuid: str,
            *,
            target_state: str,
        ) -> None:
            self.transitions.append((claim_uuid, target_state))

        def release_unprojected_dispatch_permits(
            self,
            *,
            known_claim_uuids: tuple[str, ...],
        ) -> tuple[str, ...]:
            return ()

    _seed_task(store, with_material=False)
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(
        task_uuid=TASK_UUID,
        job_uuid=JOB_UUID,
        execution_locks=[
            {"lock_key": "/devices/reactor-a", "scope": "device"},
        ],
    )
    projection.project_dispatch_accepted(JOB_UUID)
    projection.project_execution_process_restarted(TASK_UUID)
    claim = projection.get_execution_claim(JOB_UUID)
    assert claim is not None
    inventory = _ClaimRecorder()
    bridge = _bridge(
        store,
        EdgeScheduler(
            dispatcher=RecordingDispatcher(),
            station_resources=inventory,
        ),
    )
    try:
        assert bridge._task_by_job == {}
        bridge._on_job_outcome(
            JOB_UUID,
            CommittedJobOutcome(
                outcome="failed",
                return_info={},
                error_info=[{"code": "late_unknown"}],
                unknown_command_ids=[f"workflow-node-job:{JOB_UUID}"],
            ),
        )
        bridge._on_job_outcome(
            JOB_UUID,
            CommittedJobOutcome(
                outcome="canceled",
                return_info={},
                error_info=[],
                unknown_command_ids=[],
            ),
        )
        bridge._task_by_job[JOB_UUID] = TASK_UUID
        bridge._on_job_finished(JOB_UUID, True, {"late": True}, "normal")
    finally:
        bridge.close()

    assert store.get_job(JOB_UUID).get("uncertainty_reason") is None
    assert inventory.transitions == []
    assert projection.list_execution_locks(JOB_UUID)[0]["state"] == "released"
    assert store.get_task(TASK_UUID)["cleanup_status"] == "pending"


def test_close_is_idempotent_and_unregisters_scheduler_listeners(
    store: WorkflowStore,
) -> None:
    """关闭桥必须幂等注销全部调度生命周期监听器。

    参数：``store`` 是隔离任务权威。返回无；断言重复关闭后不残留派发前或完成
    回调，避免下一轮组合重复投影。
    """

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)

    assert scheduler._dispatch_admission_authority is not None
    assert len(scheduler._job_finished_listeners) == 1
    assert len(scheduler._job_settled_listeners) == 1

    bridge.close()
    bridge.close()

    assert scheduler._dispatch_admission_authority is None
    assert scheduler._job_finished_listeners == []
    assert scheduler._job_settled_listeners == []
    assert scheduler._execution_process_restarted_listeners == []


def test_connection_loss_keeps_inflight_and_pending_nodes(
    store: WorkflowStore,
) -> None:
    """连接中断保留原任务和占用，等待同一执行进程回连。

    参数：``store`` 是隔离工作流权威。返回无。异常：在途 Job 未失败、后继 Job
    未取消、内存 DAG 仍可推进，或 Claim/Fence 未释放时由断言失败。
    """

    _seed_two_node_debug_task(store)
    task = store.get_task(TASK_UUID)
    execution_plan = dict(task["execution_plan"])
    execution_plan["run_mode"] = "normal"
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_task
            SET execution_plan = ?, run_mode = 'normal', control_status = 'active'
            WHERE uuid = ?
            """,
            (json.dumps(execution_plan), TASK_UUID),
        )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        assert list(scheduler.snapshot()["inflight_jobs"]) == [JOB_UUID]

        scheduler.on_execution_connection_lost((JOB_UUID,))

        jobs = {job["uuid"]: job for job in store.list_jobs(TASK_UUID)}
        assert jobs[JOB_UUID]["status"] == "running"
        assert jobs[JOB_UUID]["uncertainty_reason"] == "execution_connection_lost"
        assert jobs[SECOND_JOB_UUID]["status"] == "pending"
        assert store.get_task(TASK_UUID)["status"] == "running"
        snapshot = scheduler.snapshot()
        assert snapshot["workflows"][TASK_UUID]["state"] == "running"
        assert list(snapshot["inflight_jobs"]) == [JOB_UUID]
        assert bridge.active_or_uncertain_job_ids() == {JOB_UUID}
        assert dispatcher.failed_restarted_jobs == []
    finally:
        bridge.close()


def test_connection_loss_preserves_waiting_tasks_and_existing_modes(
    store: WorkflowStore,
) -> None:
    """短暂断线保持尚未派发任务与运行模式。"""

    _seed_two_node_debug_task(store)
    waiting_task_uuid = "21000000-0000-4000-8000-000000000098"
    waiting_node_uuid = "31000000-0000-4000-8000-000000000098"
    waiting_job_uuid = "41000000-0000-4000-8000-000000000098"
    task = store.get_task(TASK_UUID)
    execution_plan = dict(task["execution_plan"])
    waiting_plan = {
        **execution_plan,
        "run_mode": "step",
        "nodes": [
            {
                **execution_plan["nodes"][0],
                "uuid": waiting_node_uuid,
                "device_id": "waiting-device",
            }
        ],
        "edges": [],
    }
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', ?,
                      'step', NULL, 'paused', 'none', '{}', '{}', '{}', '[]')
            """,
            (
                waiting_task_uuid,
                _CREATED_AT,
                _CREATED_AT,
                WORKFLOW_UUID,
                json.dumps(waiting_plan),
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status, attempt,
                param, feedback_data, return_info, control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (
                waiting_job_uuid,
                _CREATED_AT,
                _CREATED_AT,
                waiting_task_uuid,
                waiting_node_uuid,
            ),
        )
        execution_plan["run_mode"] = "normal"
        connection.execute(
            """
            UPDATE workflow_task
            SET execution_plan = ?, run_mode = 'normal', control_status = 'active'
            WHERE uuid = ?
            """,
            (json.dumps(execution_plan), TASK_UUID),
        )

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(store.get_task(waiting_task_uuid))
        bridge.submit(store.get_task(TASK_UUID))
        assert waiting_task_uuid in scheduler.snapshot()["workflows"]
        dispatched_before_restart = list(dispatcher.dispatched)

        scheduler.on_execution_connection_lost((JOB_UUID,))

        first_main_task = store.get_task(TASK_UUID)
        first_waiting_task = store.get_task(waiting_task_uuid)
        first_waiting_job = store.get_job(waiting_job_uuid)
        scheduler.on_execution_connection_lost((JOB_UUID,))
        assert store.get_task(TASK_UUID) == first_main_task
        assert store.get_task(waiting_task_uuid) == first_waiting_task
        assert store.get_job(waiting_job_uuid) == first_waiting_job
        assert dispatcher.dispatched == dispatched_before_restart
    finally:
        bridge.close()

    assert store.get_task(TASK_UUID)["status"] == "running"
    assert store.get_task(waiting_task_uuid)["status"] == "pending"
    assert store.get_job(waiting_job_uuid)["status"] == "pending"
    assert store.get_job(waiting_job_uuid)["error_info"] == []
    assert waiting_task_uuid in scheduler.snapshot()["workflows"]


def test_connection_loss_preserves_running_sibling_under_failed_task(
    store: WorkflowStore,
) -> None:
    """并行分支先失败父 Task 后，连接中断时仍保留在途 Job。"""

    _seed_two_node_debug_task(store)
    task = store.get_task(TASK_UUID)
    execution_plan = dict(task["execution_plan"])
    execution_plan["run_mode"] = "normal"
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET execution_plan=?,run_mode='normal' WHERE uuid=?",
            (json.dumps(execution_plan), TASK_UUID),
        )
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        assert store.get_job(JOB_UUID)["status"] == "running"
        original_finished_at = "2026-09-08T01:02:03.000000+00:00"
        original_error_info = [{"code": "parallel_branch_failed"}]
        with store.transaction() as connection:
            connection.execute(
                """
                UPDATE workflow_task
                SET status='failed', error_info=?, finished_at=?
                WHERE uuid=?
                """,
                (
                    json.dumps(original_error_info),
                    original_finished_at,
                    TASK_UUID,
                ),
            )

        scheduler.on_execution_connection_lost(())

        jobs = {job["uuid"]: job for job in store.list_jobs(TASK_UUID)}
        assert jobs[JOB_UUID]["status"] == "running"
        assert jobs[JOB_UUID]["uncertainty_reason"] == "execution_connection_lost"
        assert jobs[SECOND_JOB_UUID]["status"] == "pending"
        failed_task = store.get_task(TASK_UUID)
        assert failed_task["status"] == "failed"
        assert failed_task["error_info"] == original_error_info
        assert failed_task["finished_at"] == original_finished_at
        assert list(scheduler.snapshot()["inflight_jobs"]) == [JOB_UUID]
    finally:
        bridge.close()


def test_runtime_restart_releases_claim_without_reconciling_preheld_snapshot(
    store: WorkflowStore,
) -> None:
    """崩溃后旧预持有快照仅留作证据，不再阻断失败收口等待对账。"""
    _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_node_job SET control_data=?, uncertainty_reason=? WHERE uuid=?",
                (
                    json.dumps({"preheld_claims": {"legacy": "unavailable"}}),
                    "material_transfer_inventory_reconciliation_required",
                    JOB_UUID,
                ),
            )
        scheduler.on_execution_process_restarted(())
        assert store.get_task(TASK_UUID)["status"] == "failed"
        assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
        job = store.get_job(JOB_UUID)
        assert not job.get("uncertainty_reason")
        assert (
            job["control_data"]["runtime_interruption"]["previous_uncertainty_reason"]
            == "material_transfer_inventory_reconciliation_required"
        )
        assert bridge.active_or_uncertain_job_ids() == set()
    finally:
        bridge.close()


def test_connection_loss_retries_attention_projection(
    store: WorkflowStore, monkeypatch
) -> None:
    """投影瞬时失败时先关闭工站门禁，同一断连事件重放只创建一个决定。"""
    task = _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    original = TaskRuntimeProjection.project_execution_attention
    attempts = []
    def flaky(self, job_uuid, **kwargs):
        attempts.append(job_uuid)
        if len(attempts) == 1:
            raise RuntimeError("attention temporarily unavailable")
        return original(self, job_uuid, **kwargs)
    monkeypatch.setattr(TaskRuntimeProjection, "project_execution_attention", flaky)
    try:
        bridge.submit(task)
        with pytest.raises(RuntimeError, match="attention temporarily unavailable"):
            scheduler.on_execution_connection_lost(())
        assert bridge.error_control.snapshot()["mode"] == "PAUSED"
        scheduler.on_execution_connection_lost(())
        assert attempts == [JOB_UUID, JOB_UUID]
        assert bridge.error_control.snapshot()["error_epoch"] == 1
        assert store.get_job(JOB_UUID)["status"] == "running"
        assert store.get_job(JOB_UUID)["uncertainty_reason"] == "execution_connection_lost"
        assert JOB_UUID in scheduler.snapshot()["inflight_jobs"]
    finally:
        bridge.close()


def test_startup_restart_retries_task_marker_without_refreezing_provider(
    store: WorkflowStore,
) -> None:
    """启动跨库失败后仅凭 Task 标记重试，已交接 provider 保持 released。"""

    class _StrictFlakyRestartInventory:
        """拒绝 released→uncertain，并让首次 retained 转换瞬时失败。"""

        def __init__(self, retained_claim_uuid: str, provider_claim_uuid: str) -> None:
            self.states = {
                retained_claim_uuid: "running",
                provider_claim_uuid: "released",
            }
            self.transitions: list[tuple[str, str]] = []
            self.fail_once = True

        def release_unprojected_dispatch_permits(
            self,
            *,
            known_claim_uuids: tuple[str, ...],
        ) -> tuple[str, ...]:
            del known_claim_uuids
            return ()

        def transition_dispatch_permit(
            self,
            claim_uuid: str,
            *,
            target_state: str,
        ) -> None:
            self.transitions.append((claim_uuid, target_state))
            if self.states[claim_uuid] == "released" and target_state == "uncertain":
                raise AssertionError("released provider must not become uncertain")
            if self.fail_once and target_state == "uncertain":
                self.fail_once = False
                raise RuntimeError("inventory transition unavailable")
            self.states[claim_uuid] = target_state

    _seed_two_node_debug_task(store)
    retained_claim_uuid = "75000000-0000-4000-8000-000000000093"
    provider_claim_uuid = "75000000-0000-4000-8000-000000000094"
    retained_lock_key = "material/51000000-0000-4000-8000-000000000094/exclusive"
    handed_off_lock_key = (
        "resource/51000000-0000-4000-8000-000000000095/exclusive"
    )
    uncertainty_reason = "material_transfer_inventory_reconciliation_required"
    retained_control_data = {
        "dispatch_preheld_job_uuids": [SECOND_JOB_UUID],
        "dispatch_preheld_lock_keys": [handed_off_lock_key],
        "dispatch_fences": [
            {"lock_key": retained_lock_key, "fencing_token": 1},
            {"lock_key": handed_off_lock_key, "fencing_token": 1},
        ],
    }
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET status='running',control_status='active' "
            "WHERE uuid=?",
            (TASK_UUID,),
        )
        connection.execute(
            """
            UPDATE workflow_node_job
            SET status='failed', uncertainty_reason=?, control_data=?
            WHERE uuid=?
            """,
            (uncertainty_reason, json.dumps(retained_control_data), JOB_UUID),
        )
        connection.execute(
            "UPDATE workflow_node_job SET status='succeeded' WHERE uuid=?",
            (SECOND_JOB_UUID,),
        )
        connection.execute(
            """
            INSERT INTO execution_claim(
                claim_uuid, create_time, update_time, workflow_task_uuid,
                workflow_node_job_uuid, attempt, resource_keys, state,
                acquired_at, released_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 'uncertain', ?, NULL)
            """,
            (
                retained_claim_uuid,
                _CREATED_AT,
                _CREATED_AT,
                TASK_UUID,
                JOB_UUID,
                json.dumps([retained_lock_key, handed_off_lock_key]),
                _CREATED_AT,
            ),
        )
        for index, lock_key in enumerate((retained_lock_key, handed_off_lock_key)):
            connection.execute(
                """
                INSERT INTO execution_lock_lease(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_job_uuid,
                    lock_key, scope, material_uuid, site_uuid, state,
                    acquired_at, released_at, claim_uuid, fencing_token
                ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, 'resource', NULL,
                          NULL, 'uncertain', ?, NULL, ?, 1)
                """,
                (
                    f"76000000-0000-4000-8000-00000000009{4 + index}",
                    _CREATED_AT,
                    _CREATED_AT,
                    json.dumps(
                        {
                            "acquired_by_job_uuid": JOB_UUID,
                            "handoff_from_job_uuid": SECOND_JOB_UUID,
                        }
                    ),
                    TASK_UUID,
                    JOB_UUID,
                    lock_key,
                    _CREATED_AT,
                    retained_claim_uuid,
                ),
            )
        connection.execute(
            """
            INSERT INTO execution_claim(
                claim_uuid, create_time, update_time, workflow_task_uuid,
                workflow_node_job_uuid, attempt, resource_keys, state,
                acquired_at, released_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 'released', ?, ?)
            """,
            (
                provider_claim_uuid,
                _CREATED_AT,
                _CREATED_AT,
                TASK_UUID,
                SECOND_JOB_UUID,
                json.dumps([handed_off_lock_key]),
                _CREATED_AT,
                _CREATED_AT,
            ),
        )

    inventory = _StrictFlakyRestartInventory(
        retained_claim_uuid,
        provider_claim_uuid,
    )
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        station_resources=inventory,
    )
    bridge = _bridge(store, scheduler)
    try:
        with pytest.raises(RuntimeError, match="inventory transition unavailable"):
            bridge.recover_active_tasks()
        assert bridge.error_control.recovering is True
        bridge.recover_active_tasks()
        assert store.get_task(TASK_UUID)["status"] == "running"
        assert store.get_task(TASK_UUID)["control_status"] == "error_waiting"
        assert bridge.error_control.snapshot()["mode"] == "PAUSED"
        assert inventory.states == {retained_claim_uuid: "uncertain", provider_claim_uuid: "released"}
        assert inventory.transitions == [(retained_claim_uuid, "uncertain"), (retained_claim_uuid, "uncertain")]
        assert not bridge.error_control.recovering
    finally:
        bridge.close()


def test_execution_process_restart_repairs_succeeded_task_with_unfinished_jobs(
    store: WorkflowStore,
) -> None:
    """父 Task 误成成功但仍有在途 Job 时，崩溃恢复不能跳过物理动作。"""

    _seed_two_node_debug_task(store)
    task = store.get_task(TASK_UUID)
    execution_plan = dict(task["execution_plan"])
    execution_plan["run_mode"] = "normal"
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET execution_plan=?,run_mode='normal' WHERE uuid=?",
            (json.dumps(execution_plan), TASK_UUID),
        )
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        assert store.get_job(JOB_UUID)["status"] == "running"
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_task SET status='succeeded' WHERE uuid=?",
                (TASK_UUID,),
            )

        scheduler.on_execution_process_restarted(())

        jobs = {job["uuid"]: job for job in store.list_jobs(TASK_UUID)}
        assert jobs[JOB_UUID]["status"] == "failed"
        assert not jobs[JOB_UUID].get("uncertainty_reason")
        assert jobs[SECOND_JOB_UUID]["status"] == "canceled"
        assert store.get_task(TASK_UUID)["status"] == "failed"
        assert list(scheduler.snapshot()["inflight_jobs"]) == []
    finally:
        bridge.close()


def test_execution_process_restart_skips_stale_task_and_converges_active_task(
    store: WorkflowStore,
) -> None:
    """一个陈旧终态任务不得中断同批活跃任务的重启收敛。

    参数：``store`` 是隔离工作流权威。返回：无；断言陈旧 Job 被幂等跳过，后续
    活跃任务仍整体失败且下游跳过。异常：桥接器因单项无变化抛错或提前终止时
    测试失败。
    """

    _seed_two_node_debug_task(store)
    # ``active_plan`` 把待验证任务置为可真实派发状态，确保首节点越过执行边界。
    active_task = store.get_task(TASK_UUID)
    active_plan = dict(active_task["execution_plan"])
    active_plan["run_mode"] = "normal"
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_task
            SET execution_plan = ?, run_mode = 'normal', control_status = 'active'
            WHERE uuid = ?
            """,
            (json.dumps(active_plan), TASK_UUID),
        )
    # 两个稳定身份只用于构造已经完成的陈旧任务，不参与活跃任务的 DAG。
    stale_task_uuid = "21000000-0000-4000-8000-000000000099"
    stale_job_uuid = "41000000-0000-4000-8000-000000000099"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'failed', '{}',
                      '{"version":1,"nodes":[],"edges":[],"handles":[]}',
                      'normal', NULL, 'active', 'settled', '{}', '{}', '{}', '[]')
            """,
            (stale_task_uuid, _CREATED_AT, _CREATED_AT, WORKFLOW_UUID),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status, attempt,
                param, feedback_data, return_info, control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'device_action', '{}', 0, 'failed', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (stale_job_uuid, _CREATED_AT, _CREATED_AT, stale_task_uuid, NODE_UUID),
        )

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    bridge = _bridge(store, scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        scheduler.on_execution_process_restarted((stale_job_uuid, JOB_UUID))
    finally:
        bridge.close()

    jobs = {job["uuid"]: job for job in store.list_jobs(TASK_UUID)}
    assert jobs[JOB_UUID]["status"] == "failed"
    assert not jobs[JOB_UUID].get("uncertainty_reason")
    assert jobs[SECOND_JOB_UUID]["status"] == "canceled"
    assert store.get_task(TASK_UUID)["status"] == "failed"


def test_serial_restart_with_orphan_job_still_fails_all_tasks(
    store: WorkflowStore,
) -> None:
    """Edge 的脏 Job 身份不能阻止整站失败收敛，也不能缩小重启范围。"""

    _seed_task(store, with_material=False)
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        serial_queue=True,
    )
    bridge = _bridge(store, scheduler)
    orphan_job_uuid = "41000000-0000-4000-8000-000000000099"
    try:
        # 同时覆盖桥内遗留路由表；恢复完成后的取消超时扫描也必须能丢弃孤儿。
        bridge._task_by_job[orphan_job_uuid] = TASK_UUID
        scheduler.on_execution_process_restarted((orphan_job_uuid,))
        assert bridge.error_control.recovering is False
        assert store.get_task(TASK_UUID)["status"] == "failed"
        assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
        assert store.get_job(JOB_UUID)["status"] == "canceled"
        assert not store.get_job(JOB_UUID).get("uncertainty_reason")
        assert bridge._task_by_job == {}
    finally:
        bridge.close()


def test_restart_missing_predecessor_output_opens_error_without_physical_replay(
    store: WorkflowStore,
) -> None:
    """调度进程重启会失败处在两个节点之间的 running 任务。

    参数：``store`` 是隔离任务权威。返回无；断言已成功节点保持事实，未开始
    节点取消，父任务失败且没有物理重放。异常：恢复继续推进 DAG 会使测试失败。
    """

    _seed_recoverable_test_mode_task(store)
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = _bridge(store, scheduler)
    try:
        recovered = bridge.recover_active_tasks()
    finally:
        bridge.close()

    assert [item["task"]["uuid"] for item in recovered] == [TASK_UUID]
    assert dispatcher.dispatched == []
    assert store.get_job(JOB_UUID)["status"] == "succeeded"
    assert store.get_job(SECOND_JOB_UUID)["status"] == "failed"
    assert store.get_job(SECOND_JOB_UUID)["error_info"][0]["code"] == "node_preparation_failed"
    assert store.get_task(TASK_UUID)["status"] == "running"
    assert store.get_task(TASK_UUID)["control_status"] == "error_waiting"
