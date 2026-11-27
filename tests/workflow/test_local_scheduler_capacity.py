"""Local 调度容量、排队与重启恢复的 Backend 语义合同。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import StoreNotFound, WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge
from unilabos.workflow.workflow_spec_compiler import WorkflowSpecCompiler


def _uuid(value: int) -> str:
    """返回测试使用的稳定 UUID。"""

    return str(UUID(int=value))


def _seed_task(
    store: WorkflowStore,
    *,
    workflow_uuid: str,
    task_uuid: str,
    created_at: str,
    node_count: int = 1,
) -> list[str]:
    """写入一份可由公共调度桥恢复的冻结 Task/Jobs。"""

    try:
        store.get_workflow(workflow_uuid)
    except StoreNotFound:
        store.create_workflow(
            workflow_uuid=workflow_uuid,
            name=f"capacity-{workflow_uuid}",
            tags=[],
            description=None,
            meta_data={},
        )
    node_uuids = [_uuid(UUID(task_uuid).int + index + 1000) for index in range(node_count)]
    job_uuids = [_uuid(UUID(task_uuid).int + index + 2000) for index in range(node_count)]
    plan = {
        "version": 1,
        "run_mode": "normal",
        "target_node_uuid": None,
        "nodes": [
            {
                "uuid": node_uuid,
                "kind": "device_action",
                "device_id": f"device-{task_uuid[-4:]}-{index}",
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
                "material_requirements": [],
            }
            for index, node_uuid in enumerate(node_uuids)
        ],
        "handles": [],
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
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (task_uuid, created_at, created_at, workflow_uuid, json.dumps(plan)),
        )
        for index, (node_uuid, job_uuid) in enumerate(zip(node_uuids, job_uuids)):
            connection.execute(
                """
                INSERT INTO workflow_node_job(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_task_uuid, workflow_node_uuid,
                    feedback_sequence, topological_index, executor_kind,
                    execution_policy, execution_timeout_seconds, status, attempt,
                    param, feedback_data, return_info, control_data, error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, ?,
                          'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                          '{}', '{}', '[]')
                """,
                (job_uuid, created_at, created_at, task_uuid, node_uuid, index),
            )
    return job_uuids


def _open_store(path: Path) -> WorkflowStore:
    """打开隔离的本地工作流权威。"""

    return WorkflowStore(path / "workflow_history.db")


def test_capacity_defaults_and_validation_match_backend() -> None:
    """默认容量与 Backend 一致，且工作流级上限不能超过全局上限。"""

    scheduler = EdgeScheduler()
    assert scheduler.max_in_flight_jobs == 100
    assert scheduler.max_active_tasks == 500
    assert scheduler.max_tasks_per_workflow == 100

    with pytest.raises(ValueError, match="不能大于"):
        EdgeScheduler(max_active_tasks=2, max_tasks_per_workflow=3)


def test_scheduler_fair_order_uses_persisted_task_creation_time(
    tmp_path: Path,
) -> None:
    """编译后的公平排序时间来自 Task 持久事实，重启不会重置排队年龄。"""

    store = _open_store(tmp_path)
    task_uuid = _uuid(91)
    created_at = "2026-08-27T01:00:00Z"
    _seed_task(
        store,
        workflow_uuid=_uuid(9),
        task_uuid=task_uuid,
        created_at=created_at,
    )
    try:
        spec = WorkflowSpecCompiler().compile(
            store.get_task(task_uuid),
            store.list_jobs(task_uuid),
        )
        assert spec.submitted_at == datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        ).timestamp()
    finally:
        store.close()


def test_restart_restores_not_started_task_without_new_job(
    tmp_path: Path,
) -> None:
    """调度前重启恢复原 pending Job，在允许调度时首次执行且不创建新身份。"""

    store = _open_store(tmp_path)
    task_uuid = _uuid(101)
    job_uuid = _seed_task(
        store,
        workflow_uuid=_uuid(1),
        task_uuid=task_uuid,
        created_at="2026-08-27T01:00:00Z",
    )[0]
    dispatcher = RecordingDispatcher()
    bridge = TaskSchedulerBridge(store, scheduler=EdgeScheduler(dispatcher=dispatcher))
    try:
        recovered = bridge.recover_active_tasks()

        assert [item["task"]["uuid"] for item in recovered] == [task_uuid]
        assert [item["job_id"] for item in dispatcher.dispatched] == [job_uuid]
        bridge.recover_active_tasks()
        assert [item["job_id"] for item in dispatcher.dispatched] == [job_uuid]
        assert store.get_job(job_uuid)["status"] == "running"
        assert store.get_task(task_uuid)["status"] == "running"
        assert store.count_rows("workflow_task") == 1
        assert store.count_rows("workflow_node_job") == 1
    finally:
        bridge.close()
        store.close()


def test_global_task_capacity_releases_and_activates_oldest_pending_task(
    tmp_path: Path,
) -> None:
    """全局容量满时保持 pending；持有者终结后按创建顺序启动下一 Task。"""

    store = _open_store(tmp_path)
    first_task = _uuid(201)
    second_task = _uuid(202)
    first_job = _seed_task(
        store,
        workflow_uuid=_uuid(11),
        task_uuid=first_task,
        created_at="2026-08-27T01:00:00Z",
    )[0]
    second_job = _seed_task(
        store,
        workflow_uuid=_uuid(12),
        task_uuid=second_task,
        created_at="2026-08-27T01:00:01Z",
    )[0]
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        max_active_tasks=1,
        max_tasks_per_workflow=1,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(first_task))
        bridge.submit(store.get_task(second_task))

        assert [item["job_id"] for item in dispatcher.dispatched] == [first_job]
        assert store.get_task(first_task)["status"] == "running"
        assert store.get_task(second_task)["status"] == "pending"

        scheduler.on_job_finished(first_job, True, {"ok": True})

        assert [item["job_id"] for item in dispatcher.dispatched] == [
            first_job,
            second_job,
        ]
        assert store.get_task(second_task)["status"] == "running"
    finally:
        bridge.close()
        store.close()


def test_per_workflow_capacity_does_not_block_another_workflow(
    tmp_path: Path,
) -> None:
    """同一工作流定义超额时，其他工作流仍可使用剩余全局容量。"""

    store = _open_store(tmp_path)
    shared_workflow = _uuid(21)
    first_task = _uuid(301)
    blocked_task = _uuid(302)
    independent_task = _uuid(303)
    first_job = _seed_task(
        store,
        workflow_uuid=shared_workflow,
        task_uuid=first_task,
        created_at="2026-08-27T01:00:00Z",
    )[0]
    blocked_job = _seed_task(
        store,
        workflow_uuid=shared_workflow,
        task_uuid=blocked_task,
        created_at="2026-08-27T01:00:01Z",
    )[0]
    independent_job = _seed_task(
        store,
        workflow_uuid=_uuid(22),
        task_uuid=independent_task,
        created_at="2026-08-27T01:00:02Z",
    )[0]
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        max_active_tasks=2,
        max_tasks_per_workflow=1,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(first_task))
        bridge.submit(store.get_task(blocked_task))
        bridge.submit(store.get_task(independent_task))

        assert [item["job_id"] for item in dispatcher.dispatched] == [
            first_job,
            independent_job,
        ]
        assert store.get_task(blocked_task)["status"] == "pending"

        scheduler.on_job_finished(first_job, True, {"ok": True})

        assert [item["job_id"] for item in dispatcher.dispatched] == [
            first_job,
            independent_job,
            blocked_job,
        ]
    finally:
        bridge.close()
        store.close()


def test_job_dispatch_capacity_is_durable_and_released_by_terminal_result(
    tmp_path: Path,
) -> None:
    """同一轮多个就绪 Job 受全局派发容量限制，明确终态后立即补位。"""

    store = _open_store(tmp_path)
    task_uuid = _uuid(401)
    first_job, second_job = _seed_task(
        store,
        workflow_uuid=_uuid(31),
        task_uuid=task_uuid,
        created_at="2026-08-27T01:00:00Z",
        node_count=2,
    )
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        max_in_flight_jobs=1,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(task_uuid))

        assert [item["job_id"] for item in dispatcher.dispatched] == [first_job]
        assert store.get_job(second_job)["status"] == "pending"

        scheduler.on_job_finished(first_job, True, {"ok": True})

        assert [item["job_id"] for item in dispatcher.dispatched] == [
            first_job,
            second_job,
        ]
    finally:
        bridge.close()
        store.close()


def test_restart_preserves_inflight_and_waiting_tasks_without_redispatch(
    tmp_path: Path,
) -> None:
    """重启恢复在途作业与容量等待；旧执行不重发，回执到达后调度等待者。"""

    first_store = _open_store(tmp_path)
    first_task = _uuid(501)
    waiting_task = _uuid(502)
    first_job = _seed_task(
        first_store,
        workflow_uuid=_uuid(41),
        task_uuid=first_task,
        created_at="2026-08-27T01:00:00Z",
    )[0]
    waiting_job = _seed_task(
        first_store,
        workflow_uuid=_uuid(42),
        task_uuid=waiting_task,
        created_at="2026-08-27T01:00:01Z",
    )[0]
    first_dispatcher = RecordingDispatcher()
    first_bridge = TaskSchedulerBridge(
        first_store,
        scheduler=EdgeScheduler(
            dispatcher=first_dispatcher,
            max_in_flight_jobs=1,
        ),
    )
    first_bridge.submit(first_store.get_task(first_task))
    first_bridge.submit(first_store.get_task(waiting_task))
    assert [item["job_id"] for item in first_dispatcher.dispatched] == [first_job]
    assert first_store.get_job(waiting_job)["status"] == "pending"
    first_bridge.close()
    first_store.close()

    recovered_store = _open_store(tmp_path)
    recovered_dispatcher = RecordingDispatcher()
    recovered_bridge = TaskSchedulerBridge(
        recovered_store,
        scheduler=EdgeScheduler(
            dispatcher=recovered_dispatcher,
            max_in_flight_jobs=1,
        ),
    )
    try:
        recovered_bridge.recover_active_tasks()

        assert recovered_store.get_job(first_job)["status"] == "running"
        assert recovered_store.get_task(first_task)["status"] == "running"
        assert recovered_store.get_job(waiting_job)["status"] == "pending"
        assert recovered_store.get_task(waiting_task)["status"] == "pending"
        assert recovered_dispatcher.dispatched == []
        recovered_bridge._scheduler.on_job_finished(first_job, True, {"late": True})
        assert recovered_store.get_task(first_task)["status"] == "succeeded"
        assert [item["job_id"] for item in recovered_dispatcher.dispatched] == [waiting_job]
    finally:
        recovered_bridge.close()
        recovered_store.close()
