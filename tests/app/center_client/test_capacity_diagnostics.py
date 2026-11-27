"""容量报告区分合法 Job 尝试、传输重放和真正重复，建连统计覆盖 TLS 之前。"""

from __future__ import annotations

import asyncio
import json
import ssl
from uuid import uuid4

from tests.app.center_client.test_center_client import INSTANCE, STATION
from tests.app.center_client.test_event_envelope import store_at
from tests.app.center_client.test_websocket import Connection, hello, setup_receiver
from tests.workflow.test_error_control import seed_job
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.workflow.store import WorkflowStore, utc_now


def duplicate_row(connection, table, original, **changes):
    # 仅在临时测试库注入已存在行的变体，不经过生产修复或修改原始身份。
    row = dict(connection.execute(f"SELECT * FROM {table} WHERE uuid=?", (original,)).fetchone())
    row.update(uuid=str(uuid4()), **changes)
    names = ",".join(row)
    connection.execute(f"INSERT INTO {table}({names}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
    return row["uuid"]


def test_workload_report_preserves_legitimate_retries_and_detects_corrupt_duplicates(tmp_path):
    path = tmp_path / "workload.db"
    workflow = WorkflowStore(path)
    task, job = seed_job(workflow)
    local_task, _local_job = seed_job(workflow)
    store = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    try:
        with workflow.transaction() as db:
            db.execute("UPDATE workflow_task SET backend_task_uuid='global',invocation_key='node' WHERE uuid=?", (task,))
            db.execute("UPDATE workflow_node_job SET current_attempt=0 WHERE uuid=?", (job,))
            retried = duplicate_row(db, "workflow_node_job", job, attempt=2, current_attempt=1, retry_of_job_uuid=job)
            expanded = duplicate_row(db, "workflow_node_job", job, current_attempt=1,
                                     control_data=json.dumps({"runtime_node_id": "expanded-node-2"}))
        first = store.workload_diagnostics()
        assert first["available"]
        assert (first["tasks_total"], first["jobs_total"]) == (2, 4)
        assert (first["center_tasks_total"], first["center_jobs_total"]) == (1, 3)
        assert first["duplicate_task_groups"] == first["duplicate_job_attempt_groups"] == first["duplicate_current_job_groups"] == 0
        with workflow.transaction() as db:
            # 旧/损坏数据库可以绕过约束；诊断必须从真实行检测，不能硬填零。
            db.execute("DROP INDEX ux_workflow_task_station_invocation")
            db.execute("UPDATE workflow_task SET backend_task_uuid='global',invocation_key='node',deleted_at=? WHERE uuid=?",
                       (utc_now(), local_task))
            duplicate_row(db, "workflow_node_job", retried, current_attempt=0)
            db.execute("DROP INDEX ux_workflow_node_current_attempt")
            duplicate_row(db, "workflow_node_job", expanded)
        second = store.workload_diagnostics()
        assert second["duplicate_task_groups"] == 1  # 软删也不能释放中心调用身份。
        assert second["duplicate_job_attempt_groups"] == 2
        assert second["duplicate_current_job_groups"] == 1
        assert second["jobs_total"] == 6
        again = store.workload_diagnostics()
        assert {k: v for k, v in again.items() if k != "observed_at_ms"} == {
            k: v for k, v in second.items() if k != "observed_at_ms"}
    finally:
        store.close()
        workflow.close()


def test_missing_workflow_schema_is_unknown_instead_of_zero_duplicates(tmp_path):
    store = store_at(tmp_path / "center-only.db")
    try:
        assert store.workload_diagnostics() == {"available": False, "reason": "WORKFLOW_SCHEMA_UNAVAILABLE"}
    finally:
        store.close()


def test_connection_statistics_count_pre_tls_errors_valid_hello_and_reconnects(tmp_path, monkeypatch):
    _server, client, store, receiver = setup_receiver(tmp_path)
    calls, pauses = [], []

    async def connect(session):
        calls.append(session)
        if len(calls) in {1, 3}:
            raise ssl.SSLError("模拟 TLS 建连失败")
        greeting = hello(client.session)
        if len(calls) == 4:
            body = json.loads(greeting)
            body["payload"]["station_uuid"] = str(uuid4())
            greeting = json.dumps(body)
        await receiver._exchange(Connection([greeting]), session)

    async def sleep(seconds):
        pauses.append(seconds)
        if len(calls) == 5:
            receiver._stop.set()

    monkeypatch.setattr(receiver, "_connect", connect)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr("unilabos.app.center_client.websocket.random.uniform", lambda *_args: 1.0)
    try:
        asyncio.run(receiver._run_async())
        report = receiver.statistics()
        assert report["connect_attempts"] == 5
        assert (report["connect_successes"], report["connect_failures"], report["connect_cancellations"]) == (2, 3, 0)
        assert (report["reconnect_attempts"], report["reconnect_successes"], report["reconnect_failures"]) == (3, 1, 2)
        assert report["reconnect_completed_success_ratio"] == 1 / 3
        assert report["connections_closed"] == 2
        assert not report["connected"] and not report["connect_in_flight"]
        assert report["last_connect_ms"] is not None
        assert pauses == [1, 1, 2, 4, 1]  # hello 成功后断连也重置退避。
    finally:
        store.close()


def test_cancellation_during_connect_is_not_a_failed_handshake(tmp_path, monkeypatch):
    _server, _client, store, receiver = setup_receiver(tmp_path)

    async def connect(_session):
        assert receiver.statistics()["connect_in_flight"]
        raise asyncio.CancelledError

    monkeypatch.setattr(receiver, "_connect", connect)
    try:
        asyncio.run(receiver._run_async())
        report = receiver.statistics()
        assert report["connect_cancellations"] == 1
        assert report["connect_failures"] == 0
        assert not report["connect_in_flight"]
        assert report["reconnect_completed_success_ratio"] is None
    finally:
        store.close()
