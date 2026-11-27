"""工作流作业反馈与不可变结果证据的合同测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.persistence.inspection import table_exists
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.station_event_outbox import StationEventOutboxStore
from unilabos.workflow.station_event_publisher import StationEventPublisher
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000101"
TASK_UUID = "20000000-0000-4000-8000-000000000101"
NODE_UUID = "30000000-0000-4000-8000-000000000101"
JOB_UUID = "40000000-0000-4000-8000-000000000101"
CREATED_AT = "2026-08-26T00:00:00Z"


def _create_legacy_station_event_outbox(
    connection: sqlite3.Connection,
) -> None:
    """在兼容性测试中显式准备 v4 遗留发件箱表。

    新版运行库不会主动创建该表；只有验证旧数据库仍可读取和 ACK 时，测试
    才应该把旧表作为迁移输入注入，而不是直接假定它存在。
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_station_event_outbox (
            station_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_uuid TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_uuid TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            acked_at TEXT
        )
        """
    )


def _read_legacy_station_events(
    connection: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    """读取遗留发件箱；新版没有该表时返回空集合。"""

    if not table_exists(connection, "workflow_station_event_outbox"):
        return []
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT * FROM workflow_station_event_outbox ORDER BY station_sequence"
        )
    ]


def _seed_legacy_station_event(
    connection: sqlite3.Connection,
    *,
    index: int,
    source_kind: str = "test",
    source_uuid: str | None = None,
) -> None:
    """直接装入旧版本事件，验证停写后历史记录的兼容读取与 ACK。"""

    connection.execute(
        """
        INSERT INTO workflow_station_event_outbox(
            event_uuid, create_time, update_time, event_type, aggregate_type,
            aggregate_uuid, source_kind, source_uuid, idempotency_key, payload
        ) VALUES (?, ?, ?, ?, 'workflow_node_job', ?, ?, ?, ?, '{}')
        """,
        (
            f"70000000-0000-4000-8000-{index:012d}",
            CREATED_AT,
            CREATED_AT,
            (
                "job.outcome_committed"
                if source_kind == "job_result"
                else "job.feedback_committed"
            ),
            JOB_UUID,
            source_kind,
            source_uuid or f"legacy-source-{index}",
            f"legacy-event-{index}",
        ),
    )


def _seed_job(store: WorkflowStore) -> None:
    """创建一个可以进入派发阶段的标准任务与作业。"""

    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="反馈证据测试",
        tags=[],
        description=None,
        meta_data={},
    )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'pending', '{}', '{}',
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (TASK_UUID, CREATED_AT, CREATED_AT, WORKFLOW_UUID),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status,
                attempt, param, feedback_data, return_info, control_data,
                error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'device_action', '{}', 0, 'pending', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (JOB_UUID, CREATED_AT, CREATED_AT, TASK_UUID, NODE_UUID),
        )


def test_feedback_is_ordered_idempotent_and_queryable(tmp_path) -> None:
    """反馈须先持久化、可重放，并通过公共 HTTP 接口分页读取。"""

    store = WorkflowStore(tmp_path / "job_evidence.db")
    try:
        _seed_job(store)
        projection = TaskRuntimeProjection(store)
        projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=JOB_UUID)

        first = projection.project_feedback(
            job_uuid=JOB_UUID,
            sequence=1,
            feedback_type="progress",
            data={"percent": 20},
            observed_at="2026-08-26T00:00:01Z",
            idempotency_key="feedback-1",
        )
        replay = projection.project_feedback(
            job_uuid=JOB_UUID,
            sequence=1,
            feedback_type="progress",
            data={"percent": 20},
            observed_at="2026-08-26T00:00:01Z",
            idempotency_key="feedback-1",
        )
        projection.project_feedback(
            job_uuid=JOB_UUID,
            sequence=3,
            feedback_type="temperature",
            data={"celsius": 37.0},
            observed_at="2026-08-26T00:00:03Z",
            idempotency_key="feedback-3",
        )

        assert first == {"through_sequence": 1, "created": 1}
        assert replay == {"through_sequence": 1, "created": 0}
        client = TestClient(create_workflow_app(WorkflowService(store)))
        page = client.get(
            f"/api/v1/workflow-node-jobs/{JOB_UUID}/feedback",
            params={"page": 1, "page_size": 1},
        ).json()["data"]
        assert page["page"] == 1
        assert page["page_size"] == 1
        assert page["has_more"] is True
        assert page["items"][0]["sequence"] == 1
        assert page["items"][0]["data"] == {"percent": 20}

        with pytest.raises(StoreConflict, match="内容冲突"):
            projection.project_feedback(
                job_uuid=JOB_UUID,
                sequence=1,
                feedback_type="progress",
                data={"percent": 30},
                observed_at="2026-08-26T00:00:01Z",
                idempotency_key="feedback-1",
            )
    finally:
        store.close()


def test_terminal_result_is_immutable_and_feedback_closes(tmp_path) -> None:
    """明确作业结果只冻结一次，终态之后拒绝新增过程反馈。"""

    store = WorkflowStore(tmp_path / "job_result.db")
    try:
        _seed_job(store)
        projection = TaskRuntimeProjection(store)
        projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=JOB_UUID)
        projection.project_feedback(
            job_uuid=JOB_UUID,
            sequence=1,
            feedback_type="progress",
            data={"percent": 100},
            observed_at="2026-08-26T00:00:01Z",
            idempotency_key="feedback-1",
        )
        projection.project_job_finished(
            job_uuid=JOB_UUID,
            scheduler_state="success",
            return_info={"message": "done"},
        )
        projection.project_job_finished(
            job_uuid=JOB_UUID,
            scheduler_state="success",
            return_info={"message": "done"},
        )

        with store.transaction() as connection:
            result = connection.execute(
                """
                SELECT outcome, return_info FROM workflow_node_job_result
                WHERE workflow_node_job_uuid = ?
                """,
                (JOB_UUID,),
            ).fetchone()
            count = connection.execute(
                """
                SELECT COUNT(*) FROM workflow_node_job_result
                WHERE workflow_node_job_uuid = ?
                """,
                (JOB_UUID,),
            ).fetchone()[0]
        assert tuple(result) == ("succeeded", '{"message":"done"}')
        assert count == 1

        with pytest.raises(StoreConflict, match="终态"):
            projection.project_feedback(
                job_uuid=JOB_UUID,
                sequence=2,
                feedback_type="late",
                data={},
                observed_at="2026-08-26T00:00:02Z",
                idempotency_key="feedback-2",
            )
    finally:
        store.close()


@pytest.mark.parametrize("with_legacy_events", [False, True])
def test_local_job_lifecycle_does_not_append_station_events(
    tmp_path: Path,
    with_legacy_events: bool,
) -> None:
    """持续反馈、结果重放和重开数据库保留本地事实，且不新增或改写旧发件箱。"""

    database = tmp_path / "station-outbox.db"
    store = WorkflowStore(database)
    try:
        _seed_job(store)
        if with_legacy_events:
            with store.transaction() as connection:
                _create_legacy_station_event_outbox(connection)
                for index in (1, 2):
                    _seed_legacy_station_event(connection, index=index)
            outbox = StationEventOutboxStore(store)
            first = outbox.list_pending()[0]
            outbox.acknowledge(
                event_uuid=first["event_uuid"],
                station_sequence=first["station_sequence"],
            )
        with store.read() as connection:
            assert table_exists(connection, "workflow_station_event_outbox") is with_legacy_events
            original_events = _read_legacy_station_events(connection)
        projection = TaskRuntimeProjection(store)
        actual_param = {
            "device_uuid": "50000000-0000-4000-8000-000000000101",
            "target_site_uuid": "60000000-0000-4000-8000-000000000101",
        }
        projection.project_pre_dispatch(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            resolved_param=actual_param,
            execution_locks=[
                {"lock_key": "/devices/reactor-a", "scope": "device"},
            ],
        )
        for sequence in range(1, 101):
            assert projection.project_feedback(
                job_uuid=JOB_UUID,
                sequence=sequence,
                feedback_type="progress",
                data={"percent": sequence},
                observed_at="2026-08-26T00:00:01Z",
                idempotency_key=f"feedback-{sequence}",
            ) == {"through_sequence": sequence, "created": 1}
        assert projection.project_feedback(
            job_uuid=JOB_UUID,
            sequence=100,
            feedback_type="progress",
            data={"percent": 100},
            observed_at="2026-08-26T00:00:01Z",
            idempotency_key="feedback-100",
        ) == {"through_sequence": 100, "created": 0}
        projection.project_job_finished(
            job_uuid=JOB_UUID,
            scheduler_state="success",
            return_info={"mass_g": 12.5},
        )
        projection.project_job_finished(
            job_uuid=JOB_UUID,
            scheduler_state="success",
            return_info={"mass_g": 12.5},
        )

        store.close()
        store = WorkflowStore(database)
        job = store.get_job(JOB_UUID)
        assert job["status"] == "succeeded"
        assert job["param"] == actual_param
        assert job["feedback_sequence"] == 100
        assert job["feedback_data"] == {"percent": 100}
        assert job["return_info"] == {"mass_g": 12.5}
        assert store.get_task(TASK_UUID)["status"] == "succeeded"
        with store.read() as connection:
            assert table_exists(connection, "workflow_station_event_outbox") is with_legacy_events
            events = _read_legacy_station_events(connection)
            assert events == original_events
            feedback = connection.execute(
                "SELECT sequence, data, published_at FROM workflow_node_job_feedback_history "
                "WHERE workflow_node_job_uuid = ? ORDER BY sequence",
                (JOB_UUID,),
            ).fetchall()
            assert [row["sequence"] for row in feedback] == list(range(1, 101))
            assert feedback[-1]["data"] == '{"percent":100}'
            assert all(row["published_at"] is None for row in feedback)
            results = connection.execute(
                "SELECT outcome, return_info, consumed_at FROM workflow_node_job_result "
                "WHERE workflow_node_job_uuid = ?",
                (JOB_UUID,),
            ).fetchall()
            assert [tuple(row) for row in results] == [
                ("succeeded", '{"mass_g":12.5}', None),
            ]
            assert connection.execute(
                "SELECT COUNT(*) FROM workflow_runtime_journal "
                "WHERE workflow_node_job_uuid = ? AND kind = 'feedback_committed'",
                (JOB_UUID,),
            ).fetchone()[0] == 100
    finally:
        store.close()


def test_legacy_station_event_acknowledges_original_evidence(tmp_path: Path) -> None:
    """停写后仍能读取旧反馈与结果事件，并在精确 ACK 后标记对应本地证据。"""

    store = WorkflowStore(tmp_path / "legacy-station-outbox.db")
    try:
        _seed_job(store)
        projection = TaskRuntimeProjection(store)
        projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=JOB_UUID)
        projection.project_feedback(
            job_uuid=JOB_UUID,
            sequence=1,
            feedback_type="progress",
            data={"percent": 100},
            observed_at="2026-08-26T00:00:01Z",
            idempotency_key="feedback-1",
        )
        projection.project_job_finished(
            job_uuid=JOB_UUID,
            scheduler_state="success",
            return_info={"mass_g": 12.5},
        )
        with store.transaction() as connection:
            _create_legacy_station_event_outbox(connection)
            feedback_uuid = connection.execute(
                "SELECT uuid FROM workflow_node_job_feedback_history "
                "WHERE workflow_node_job_uuid = ? AND sequence = 1",
                (JOB_UUID,),
            ).fetchone()[0]
            result_uuid = connection.execute(
                "SELECT uuid FROM workflow_node_job_result WHERE workflow_node_job_uuid = ?",
                (JOB_UUID,),
            ).fetchone()[0]
            _seed_legacy_station_event(
                connection,
                index=1,
                source_kind="job_feedback",
                source_uuid=feedback_uuid,
            )
            _seed_legacy_station_event(
                connection,
                index=2,
                source_kind="job_result",
                source_uuid=result_uuid,
            )

        outbox = StationEventOutboxStore(store)
        events = outbox.list_pending()
        assert [event["station_sequence"] for event in events] == [1, 2]

        attempted = outbox.mark_delivery_attempt(
            event_uuid=events[0]["event_uuid"],
            station_sequence=events[0]["station_sequence"],
        )
        assert attempted["delivery_attempts"] == 1
        assert attempted["acked_at"] is None
        assert len(outbox.list_pending()) == 2

        for event in events:
            acked = outbox.acknowledge(
                event_uuid=event["event_uuid"],
                station_sequence=event["station_sequence"],
            )
            assert outbox.acknowledge(
                event_uuid=event["event_uuid"],
                station_sequence=event["station_sequence"],
            ) == acked
        assert outbox.list_pending() == []
        with store.read() as connection:
            feedback_published_at = connection.execute(
                """
                SELECT published_at FROM workflow_node_job_feedback_history
                WHERE workflow_node_job_uuid = ? AND sequence = 1
                """,
                (JOB_UUID,),
            ).fetchone()[0]
            result_consumed_at = connection.execute(
                """
                SELECT consumed_at FROM workflow_node_job_result
                WHERE workflow_node_job_uuid = ?
                """,
                (JOB_UUID,),
            ).fetchone()[0]
            assert connection.execute(
                "SELECT COUNT(*) FROM workflow_station_event_outbox"
            ).fetchone()[0] == 2
        assert feedback_published_at is not None
        assert result_consumed_at is not None
    finally:
        store.close()


def test_station_event_publisher_retries_and_accepts_only_exact_prefix(tmp_path) -> None:
    """Backend 断线保留事件，恢复后按精确连续前缀 ACK。

    参数：``tmp_path`` 提供隔离工作流库。返回无；断言发送异常、部分 ACK 和后续
    重放均不跳过序列。异常：发布器错误清理未 ACK 事件会使测试失败。
    """

    store = WorkflowStore(tmp_path / "station-publisher.db")
    try:
        with store.transaction() as connection:
            _create_legacy_station_event_outbox(connection)
            for index in (1, 2):
                _seed_legacy_station_event(connection, index=index)
        outbox = StationEventOutboxStore(store)

        calls = 0

        def sender(events):
            """首次模拟 Backend 断线，之后每次只确认当前批次第一条。"""

            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("backend disconnected")
            return [
                {
                    "event_uuid": events[0]["event_uuid"],
                    "station_sequence": events[0]["station_sequence"],
                }
            ]

        publisher = StationEventPublisher(outbox, sender)
        with pytest.raises(ConnectionError, match="disconnected"):
            publisher.flush_once()
        after_failure = outbox.list_pending()
        assert len(after_failure) == 2
        assert [event["delivery_attempts"] for event in after_failure] == [1, 1]

        assert publisher.flush_once() == 1
        remaining = outbox.list_pending()
        assert [event["station_sequence"] for event in remaining] == [2]
        assert publisher.flush_once() == 1
        assert outbox.list_pending() == []
    finally:
        store.close()


def test_station_event_publisher_stop_never_returns_while_sender_uses_store(
    tmp_path,
) -> None:
    """停止发布器必须在存储关闭前证明后台线程已经退出。

    参数：``tmp_path`` 提供隔离工作流库。返回：无；断言显式停止预算不足时抛出
    ``TimeoutError`` 而不是假装已停止，发送结束后重试可以正常退出。异常：若
    发布线程仍可能在 ``stop`` 正常返回后访问 SQLite，本测试失败。
    """

    store = WorkflowStore(tmp_path / "station-publisher-stop.db")
    sender_started = Event()
    sender_may_finish = Event()
    try:
        with store.transaction() as connection:
            _create_legacy_station_event_outbox(connection)
            _seed_legacy_station_event(connection, index=1)
        outbox = StationEventOutboxStore(store)

        def blocking_sender(events):
            """模拟仍处于有限 HTTP 请求中的 Backend 适配器。

            参数：``events`` 是当前按序投递批次。返回：释放测试门闩后确认首个
            事件。异常：等待超过测试预算时抛 ``AssertionError``，避免测试挂起。
            """

            sender_started.set()
            assert sender_may_finish.wait(timeout=2.0)
            return [
                {
                    "event_uuid": events[0]["event_uuid"],
                    "station_sequence": events[0]["station_sequence"],
                }
            ]

        publisher = StationEventPublisher(outbox, blocking_sender, poll_interval=0.01)
        publisher.start()
        assert sender_started.wait(timeout=1.0)
        with pytest.raises(TimeoutError, match="未在停止预算内退出"):
            publisher.stop(timeout=0.01)
        sender_may_finish.set()
        publisher.stop(timeout=1.0)
    finally:
        sender_may_finish.set()
        store.close()
