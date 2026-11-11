"""真实 SQLite 事务与调度恢复：丢回执不能释放设备，跨进程占用不可被新任务抢走。"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from unilabos.registry.workflows import upsert_workflow
from unilabos.server.backend.scheduler.models import ActionLockClaim, MaterialLockClaim, SchedulerResourceRequest
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.database import initialize_database
from unilabos.server.database.tables.runtime import RUNTIME_DATABASE, RUNTIME_MIGRATIONS
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.errors import StoreConflict
from unilabos.server.services.runtime.workflow.service import WorkflowService
from tests.server.test_workflow_parallel_execution import Executor, wait_for


def make_task(service):
    record = upsert_workflow(service, {"workflow_uuid": str(uuid4()), "name": "派发持久性", "description": "", "tags": [],
        "nodes": [{"uuid": str(uuid4()), "name": "操作", "type": "device_action", "material_uuid": str(uuid4()),
                   "action_name": "operate", "meta_data": {"target_device_id": "reactor"}, "param": {}}], "edges": []})
    task = service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal", target_node_uuid=None,
        input_value={}, description=None, meta_data={})
    run = service.list_workflow_node_runs(task["uuid"])[0]
    return task, run["current_job_uuid"]


def request(task, job, *, material=False):
    return SchedulerResourceRequest(request_uuid=f"resource:{job}", owner_uuid=job, task_uuid=task["uuid"],
        current_action=ActionLockClaim(device_id="reactor", action_name="operate"),
        material_lock_claims=[MaterialLockClaim(material_uuid="sample")] if material else [])


def finish(service, job):
    service.record_workflow_node_job_terminal(job, status="succeeded", return_info={"return_value": {}}, error_info=[])


def test_claim_survives_reopen_replay_keeps_fence_and_next_owner_increments(tmp_path):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    task1, job1 = make_task(service)
    task2, job2 = make_task(service)
    claims = ExecutionClaims(service)
    original = claims.reserve(request(task1, job1, material=True))
    assert claims.reserve(request(task1, job1, material=True)) == original
    service.close()
    service = WorkflowService(path)
    try:
        claims = ExecutionClaims(service)
        assert claims.get(job1) == original
        assert not claims.release_if_terminal(job1)
        with pytest.raises(StoreConflict, match="持久占用"):
            claims.reserve(request(task2, job2, material=True))
        finish(service, job1)
        assert claims.release_if_terminal(job1)
        following = claims.reserve(request(task2, job2, material=True))
        assert all(following["fences"][key] > token for key, token in original["fences"].items())
        with pytest.raises(StoreConflict, match="失效"):
            claims.require_current(job1, original["claim_uuid"])
    finally:
        service.close()


def test_claim_insertion_failure_rolls_back_all_fences_and_leases(tmp_path):
    service = WorkflowService(tmp_path / "runtime.db")
    try:
        task, job = make_task(service)
        claims = ExecutionClaims(service)
        with service.transaction() as conn:
            conn.execute("CREATE TRIGGER fail_material_lease BEFORE INSERT ON workflow_execution_lease "
                         "WHEN NEW.lock_key LIKE '%material%' BEGIN SELECT RAISE(ABORT, '注入写入失败'); END")
        with pytest.raises(sqlite3.IntegrityError, match="注入写入失败"):
            claims.reserve(request(task, job, material=True))
        for table in ("workflow_execution_claim", "workflow_execution_fence", "workflow_execution_lease"):
            assert service.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        service.close()


def test_lease_corruption_and_request_drift_refuse_execution(tmp_path):
    service = WorkflowService(tmp_path / "runtime.db")
    try:
        task, job = make_task(service)
        claims = ExecutionClaims(service)
        claim = claims.reserve(request(task, job))
        with pytest.raises(StoreConflict, match="资源集合"):
            claims.reserve(request(task, job, material=True))
        with service.transaction() as conn:
            conn.execute("UPDATE workflow_execution_lease SET token=token+1")
        with pytest.raises(StoreConflict, match="租约"):
            claims.require_current(job, claim["claim_uuid"])
        with pytest.raises(StoreConflict, match="租约"):
            claims.restore_requests()
    finally:
        service.close()


def test_terminal_commit_before_release_recovers_without_stuck_lock(tmp_path):
    service = WorkflowService(tmp_path / "runtime.db")
    try:
        task, job = make_task(service)
        claims = ExecutionClaims(service)
        claims.reserve(request(task, job))
        finish(service, job)
        assert claims.restore_requests() == []
        assert claims.get(job)["state"] == "released"
    finally:
        service.close()


class LostReplyExecutor(Executor):
    def dispatch(self, payload):
        super().dispatch(payload)
        raise ConnectionError("已交给执行端，连接随后中断")


def test_uncertain_dispatch_and_scheduler_stop_keep_locks_until_late_result(tmp_path):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    task1, job1 = make_task(service)
    task2, job2 = make_task(service)
    executor = LostReplyExecutor()
    scheduler = BackendScheduler(service, executor)
    scheduler.submit(task1["uuid"])
    try:
        wait_for(lambda: service.get_workflow_node_job(job1)["status"] == "execution_unknown")
        scheduler.submit(task2["uuid"])
        wait_for(lambda: service.get_workflow_task(task2["uuid"])["status"] == "running")
        assert len(executor.dispatched) == 1
        assert ExecutionClaims(service).get(job1)["state"] == "uncertain"
    finally:
        scheduler.stop()
        service.close()
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    try:
        # 此时尚未恢复任何任务，旧占用已经存在；先提交新任务也不能抢到。
        assert scheduler.resources.held_by(job1)
        scheduler.submit(task2["uuid"])
        time.sleep(0.05)
        assert executor.dispatched == []
        scheduler.submit(task1["uuid"])
        wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
        scheduler._on_executor_finished(job1, True, {"done": True}, "normal", {})
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["job_id"] == job2
        executor.finish(0, {})
        wait_for(lambda: service.get_workflow_task(task2["uuid"])["status"] == "succeeded")
        assert ExecutionClaims(service).get(job1)["state"] == "released"
    finally:
        scheduler.stop()
        service.close()


def test_runtime_v4_migration_preserves_workflow_and_adds_claim_tables(tmp_path):
    path = tmp_path / "runtime.db"
    v4 = replace(RUNTIME_DATABASE, tables=tuple(t for t in RUNTIME_DATABASE.tables if not t.name.startswith(("workflow_execution_", "workflow_resource_"))),
                 schema_version=4, migrations=RUNTIME_MIGRATIONS[:3])
    conn = initialize_database(path, v4)
    with conn:
        conn.execute("INSERT INTO workflow(uuid,create_time,update_time,meta_data,name,tags,revision) "
                     "VALUES ('82000000-0000-4000-8000-000000000001', 't', 't', '{}', '已有流程', '[]', 7)")
    conn.close()
    service = WorkflowService(path)
    try:
        assert service.get_workflow("82000000-0000-4000-8000-000000000001")["revision"] == 7
        assert ExecutionClaims(service).restore_requests() == []
        assert service.connection.execute("PRAGMA user_version").fetchone()[0] == RUNTIME_DATABASE.schema_version
        assert list(tmp_path.glob(f"runtime.db.pre-v{RUNTIME_DATABASE.schema_version}-*.bak"))
        assert service.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        service.close()


def test_legacy_inflight_blocks_new_dispatch_until_reconciled(tmp_path):
    service = WorkflowService(tmp_path / "runtime.db")
    old_task, old_job = make_task(service)
    new_task, new_job = make_task(service)
    service.mark_workflow_node_job_running(old_job)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    try:
        assert "legacy_execution_reconciliation" in scheduler.dispatch_pause_reasons
        scheduler.resume_dispatch(reason="legacy_execution_reconciliation")
        assert scheduler.dispatch_paused
        scheduler.submit(new_task["uuid"])
        scheduler.submit(old_task["uuid"])
        wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
        assert not executor.dispatched
        decision = scheduler.list_reconciliation_decisions()[0]
        assert scheduler.resolve_reconciliation_decision(decision["decision_id"],
            {"action": "operator_intervention", "result": {"checked": True}})
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["job_id"] == new_job
        assert not scheduler.dispatch_paused
        executor.finish(0, {})
        wait_for(lambda: service.get_workflow_task(new_task["uuid"])["status"] == "succeeded")
    finally:
        scheduler.stop()
        service.close()
