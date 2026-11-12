"""持久区间在动作间隙、暂停、重启和未知执行期间都必须继续生效。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.registry.workflows import upsert_workflow
from unilabos.server.api.runtime.workflow import create_workflow_app
from unilabos.server.backend.scheduler.models import ActionLockClaim, SchedulerResourceRequest
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.database import initialize_database
from unilabos.server.database.tables.runtime import RUNTIME_DATABASE, RUNTIME_MIGRATIONS
from unilabos.server.services.runtime.workflow.errors import StoreConflict
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals
from unilabos.server.services.runtime.workflow.service import WorkflowService
from tests.server.test_workflow_task_cancel import Executor, command, wait_for


def make_task(service, devices=("reactor", "reactor"), *, explicit=False, parallel=False):
    nodes = []
    for device in devices:
        nodes.append({"uuid": str(uuid4()), "name": f"动作{len(nodes)}", "type": "device_action",
            "material_uuid": str(uuid4()), "action_name": "operate", "param": {},
            "meta_data": {"target_device_id": device},
            "execution_policy": {"depends_on": [] if parallel or not nodes else [nodes[-1]["uuid"]]}})
    record = upsert_workflow(service, {"workflow_uuid": str(uuid4()), "name": "持久资源区间",
        "description": "", "tags": [], "nodes": nodes, "edges": []})
    if explicit:
        service.declare_workflow_resource_scopes(record["uuid"], revision=record["revision"],
            scopes=[{"scope_id": "experiment", "kind": "root", "resources": sorted(set(devices)),
                     "node_uuids": [node["uuid"] for node in nodes]}])
    return service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
        target_node_uuid=None, input_value={}, description=None, meta_data={})


def request_for(service, task, index=0):
    plan = task["execution_plan"]
    node = plan["nodes"][index]
    run = next(row for row in service.list_workflow_node_runs(task["uuid"])
               if row["workflow_node_uuid"] == node["uuid"])
    source = next(row for row in task["workflow_snapshot"]["nodes"] if row["uuid"] == node["uuid"])
    return SchedulerResourceRequest(request_uuid=f"resource:{run['current_job_uuid']}",
        task_uuid=task["uuid"], owner_uuid=run["current_job_uuid"], interval_admission=True,
        current_action=ActionLockClaim(device_id=source["meta_data"]["target_device_id"], action_name="operate"))


@pytest.fixture
def service(tmp_path):
    value = WorkflowService(tmp_path / "runtime.db")
    yield value
    value.close()


def test_pause_between_adjacent_actions_and_restart_keep_interval(tmp_path):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    client = TestClient(create_workflow_app(service))
    owner = make_task(service)
    competitor = make_task(service, ("reactor",))
    scheduler.submit(owner["uuid"])
    wait_for(lambda: len(executor.dispatched) == 1)
    assert command(client, owner, "pause").status_code == 201
    executor.finish(0)
    wait_for(lambda: len(scheduler._waiting_step_nodes) == 1)
    intervals = ResourceIntervals(service)
    held = intervals.snapshot(owner["uuid"])
    assert len(held) == 1 and held[0]["state"] == "held"
    assert len(held[0]["definition"]["members"]) == 2
    assert service.connection.execute("SELECT count(*) FROM workflow_resource_use").fetchone()[0] == 0
    scheduler.submit(competitor["uuid"])
    wait_for(lambda: bool(scheduler._waiting_interval_jobs))
    assert len(executor.dispatched) == 1
    scheduler.stop()
    client.close()
    service.close()

    service = WorkflowService(path)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    client = TestClient(create_workflow_app(service))
    try:
        assert ResourceIntervals(service).snapshot(owner["uuid"]) == held
        scheduler.submit(competitor["uuid"])
        wait_for(lambda: bool(scheduler._waiting_interval_jobs))
        assert executor.dispatched == []
        scheduler.submit(owner["uuid"])
        wait_for(lambda: owner["uuid"] not in scheduler._scheduled)
        assert service.get_workflow_task(owner["uuid"])["control_status"] == "paused"
        assert executor.dispatched == []
        assert command(client, owner, "resume").status_code == 201
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["task_id"] == owner["uuid"]
        assert ResourceIntervals(service).snapshot(owner["uuid"])[0]["owner_uuid"] == held[0]["owner_uuid"]
        executor.finish(0)
        wait_for(lambda: len(executor.dispatched) == 2)
        assert executor.dispatched[1]["task_id"] == competitor["uuid"]
        executor.finish(1)
        wait_for(lambda: service.get_workflow_task(competitor["uuid"])["status"] == "succeeded")
        assert ResourceIntervals(service).snapshot() == []
    finally:
        scheduler.stop()
        client.close()
        service.close()


def test_explicit_parallel_scope_shares_owner_but_serializes_actual_device(service):
    task = make_task(service, explicit=True, parallel=True)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    try:
        scheduler.submit(task["uuid"])
        wait_for(lambda: len(executor.dispatched) == 1 and bool(scheduler._waiting_interval_jobs))
        held = ResourceIntervals(service).snapshot()
        assert len(held) == 1 and held[0]["definition"]["explicit"]
        executor.finish(0)
        wait_for(lambda: len(executor.dispatched) == 2)
        assert ResourceIntervals(service).snapshot()[0]["owner_uuid"] == held[0]["owner_uuid"]
        executor.finish(1)
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert ResourceIntervals(service).snapshot() == []
    finally:
        scheduler.stop()


def test_explicit_failure_requires_manual_release_and_wakes_competitor(service):
    task = make_task(service, explicit=True)
    next_task = make_task(service, ("reactor",))
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    with TestClient(create_workflow_app(service)) as client:
        try:
            scheduler.submit(task["uuid"])
            wait_for(lambda: len(executor.dispatched) == 1)
            scheduler.submit(next_task["uuid"])
            wait_for(lambda: bool(scheduler._waiting_interval_jobs))
            executor.finish(0, success=False)
            wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "failed")
            rows = client.get("/api/v1/workflow-resource-intervals", params={"task_uuid": task["uuid"]}).json()["data"]
            assert len(rows) == 1 and rows[0]["state"] == "retained"
            assert len(executor.dispatched) == 1
            row = rows[0]
            url = f"/api/v1/workflow-resource-intervals/{row['owner_uuid']}/release"
            for body in ({"expected_version": True, "reason": "已确认"},
                         {"expected_version": row["version"], "reason": "  "},
                         {"expected_version": row["version"] - 1, "reason": "已确认"}):
                assert client.post(url, json=body).json()["code"] != 0
            body = {"expected_version": row["version"], "reason": "已核实设备停止且样品已取走"}
            released = client.post(url, json=body)
            assert released.status_code == 200, released.text
            assert released.json()["data"]["state"] == "released"
            assert client.post(url, json=body).json()["data"] == released.json()["data"]
            wait_for(lambda: len(executor.dispatched) == 2)
            assert executor.dispatched[1]["task_id"] == next_task["uuid"]
            executor.finish(1)
            wait_for(lambda: service.get_workflow_task(next_task["uuid"])["status"] == "succeeded")
        finally:
            scheduler.stop()


def test_unknown_execution_retains_explicit_scope_and_cannot_be_manually_released(service):
    task = make_task(service, explicit=True)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    try:
        scheduler.submit(task["uuid"])
        wait_for(lambda: len(executor.dispatched) == 1)
        job = executor.dispatched[0]["job_id"]
        scheduler._reconcile_lost_attempt(job, {"uncertainty_reason": "Host 断开，动作状态未知"})
        row = ResourceIntervals(service).snapshot()[0]
        assert row["state"] == "retained"
        with pytest.raises(StoreConflict, match="未收敛"):
            ResourceIntervals(service).confirm_release(row["owner_uuid"], expected_version=row["version"], reason="核实请求")
        assert ExecutionClaims(service).get(job)["state"] == "uncertain"
        # 迟到的真实完成回执收敛 attempt，但不代替显式作用域的物理清场确认。
        assert command(TestClient(create_workflow_app(service)), task, "cancel").status_code == 201
        executor.finish(0, cancelled=True)
        wait_for(lambda: service.get_workflow_task(task["uuid"])["status"] == "canceled")
        assert ResourceIntervals(service).snapshot()[0]["state"] == "retained"
    finally:
        scheduler.stop()


def test_acquire_set_conflict_and_database_error_leave_no_partial_ownership(service):
    owner = make_task(service, ("camera",))
    candidate = make_task(service, ("reactor", "camera"), explicit=True)
    intervals = ResourceIntervals(service)
    epoch = ExecutionClaims(service).activate_authority()
    assert intervals.admit(request_for(service, owner), epoch)
    assert not intervals.admit(request_for(service, candidate), epoch)
    assert intervals.snapshot(candidate["uuid"]) == []
    assert service.connection.execute("SELECT count(*) FROM workflow_resource_admission").fetchone()[0] == 1
    intervals.withdraw(request_for(service, owner).owner_uuid)
    with service.transaction() as conn:
        conn.execute("CREATE TRIGGER fail_interval_insert BEFORE INSERT ON workflow_resource_interval "
                     "WHEN NEW.lock_key='/devices/reactor' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        intervals.admit(request_for(service, candidate), epoch)
    assert intervals.snapshot() == []
    assert service.connection.execute("SELECT count(*) FROM workflow_resource_admission").fetchone()[0] == 0


def test_withdraw_does_not_delete_shared_scope_already_used_by_sibling(service):
    task = make_task(service, ("reactor", "camera"), explicit=True, parallel=True)
    intervals = ResourceIntervals(service)
    epoch = ExecutionClaims(service).activate_authority()
    first, second = (request_for(service, task, i) for i in (0, 1))
    assert intervals.admit(first, epoch)
    assert intervals.admit(second, epoch)
    service.record_workflow_node_job_terminal(second.owner_uuid, status="succeeded", return_info={}, error_info=[])
    intervals.release_job(second.owner_uuid)
    intervals.withdraw(first.owner_uuid)
    assert len(intervals.snapshot()) == 2
    assert intervals.admit(first, epoch)
    intervals.require_admission(service.connection, first)


def test_terminal_admission_cleanup_can_precede_execution_claim_cleanup(service):
    task = make_task(service, ("reactor",))
    intervals = ResourceIntervals(service)
    claims = ExecutionClaims(service)
    epoch = claims.activate_authority()
    request = request_for(service, task)
    assert intervals.admit(request, epoch)
    claim = claims.reserve(request, authority_epoch=epoch)
    service.record_workflow_node_job_terminal(request.owner_uuid, status="succeeded", return_info={}, error_info=[])
    intervals.release_job(request.owner_uuid)
    assert claims.get(request.owner_uuid)["claim_uuid"] == claim["claim_uuid"]
    assert claims.restore_requests() == []
    assert claims.get(request.owner_uuid)["state"] == "released"


def test_lost_actual_use_or_frozen_scope_cannot_authorize_new_execution(service):
    task = make_task(service, ("reactor",), explicit=True)
    intervals = ResourceIntervals(service)
    claims = ExecutionClaims(service)
    epoch = claims.activate_authority()
    request = request_for(service, task)
    assert intervals.admit(request, epoch)
    claim = claims.reserve(request, authority_epoch=epoch)
    with service.transaction() as conn:
        conn.execute("UPDATE workflow_resource_interval SET state='retained',version=version+1")
    assert claims.get(request.owner_uuid)["claim_uuid"] == claim["claim_uuid"]
    with pytest.raises(StoreConflict, match="冻结"):
        claims.require_current(request.owner_uuid, claim["claim_uuid"])
    with service.transaction() as conn:
        conn.execute("DELETE FROM workflow_resource_use")
    with pytest.raises(StoreConflict, match="使用权"):
        claims.restore_requests()


def test_runtime_v6_upgrade_preserves_workflows_and_records_backup(tmp_path):
    path = tmp_path / "runtime.db"
    v6 = replace(RUNTIME_DATABASE, tables=tuple(t for t in RUNTIME_DATABASE.tables if t.name not in {"workflow_resource_interval", "workflow_resource_admission", "workflow_resource_use", "workflow_resource_loan", "workflow_device_tenancy", "workflow_tenancy_transition", "workflow_resource_waiter"}),
                 schema_version=6, migrations=RUNTIME_MIGRATIONS[:5])
    conn = initialize_database(path, v6)
    workflow = str(uuid4())
    with conn:
        conn.execute("INSERT INTO workflow(uuid,create_time,update_time,meta_data,name,tags,revision) "
                     "VALUES (?, 't', 't', '{}', '原有流程', '[]', 8)", (workflow,))
    conn.close()
    with_service = WorkflowService(path)
    try:
        assert with_service.get_workflow(workflow)["revision"] == 8
        assert ResourceIntervals(with_service).snapshot() == []
        assert with_service.connection.execute("PRAGMA user_version").fetchone()[0] == RUNTIME_DATABASE.schema_version
        assert [tuple(row) for row in with_service.connection.execute("SELECT version,description FROM schema_migration")] == [
            (7, "区间资源所有权和单次动作使用清单"), (8, "设备托管、资源借还和等待环路"),
            (9, "V1 任务生命周期：优先级、库存绑定、派发闸门、命令类型与人工交互"),
                           (10, "V1 工站错误处置：工站控制、控制命令、处置会话与工站错误")]
        assert list(tmp_path.glob(f"runtime.db.pre-v{RUNTIME_DATABASE.schema_version}-*.bak"))
        assert with_service.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        with_service.close()


def test_hostlink_loop_rounds_have_distinct_owners_and_release_all():
    from tests.server.test_workflow_loop_execution import _Stack
    from unilabos.registry.workflows import (DeviceCatalog, WorkflowDefinition,
        build_workflow_template_payload, materialize_workflow_template)

    def flow(ctx):
        with ctx.loop_for(3):
            ctx.run("loop-device/record", {"value": "{{loop.index}}"})
            ctx.run("loop-device/record", {"value": "{{loop.index}}"})

    stack = _Stack()
    try:
        definition = WorkflowDefinition(fn=flow, uuid=str(uuid4()), source_path="test:interval-rounds",
            display_name="循环区间", description="", tags=[], compiler="ast")
        catalog = DeviceCatalog()
        catalog.add("loop-device", "loop_demo_class", str(uuid4()))
        record = upsert_workflow(stack.service, materialize_workflow_template(build_workflow_template_payload(definition), catalog))
        task = stack.service.create_workflow_task(workflow_uuid=record["uuid"], run_mode="normal",
            target_node_uuid=None, input_value={}, description=None, meta_data={})
        wait_for(lambda: stack.service.get_workflow_task(task["uuid"])["status"] == "succeeded")
        assert stack.driver.calls == [0, 0, 1, 1, 2, 2]
        owners = [dict(row) for row in stack.service.connection.execute("SELECT * FROM workflow_resource_interval")]
        assert len(owners) == 3
        assert {row["state"] for row in owners} == {"released"}
        assert len({json.dumps(json.loads(row["definition"])["generation"]) for row in owners}) == 3
        assert ResourceIntervals(stack.service).snapshot() == []
    finally:
        stack.close()


def test_resource_scope_api_is_revisioned_validates_references_and_freezes_running_plan(service):
    task = make_task(service)
    graph = service.get_graph(task["workflow_uuid"])
    workflow = graph["workflow"]
    members = [node["uuid"] for node in graph["nodes"]]
    body = {"revision": workflow["revision"], "scopes": [{"scope_id": "experiment",
        "kind": "root", "resources": ["reactor"], "node_uuids": members}]}
    with TestClient(create_workflow_app(service)) as client:
        url = f"/api/v1/workflows/{workflow['uuid']}/resource-scopes"
        invalid = {**body, "scopes": [{**body["scopes"][0], "node_uuids": [str(uuid4())]}]}
        assert client.put(url, json=invalid).json()["code"] != 0
        saved = client.put(url, json=body)
        assert saved.status_code == 200, saved.text
        assert client.put(url, json=body).json()["code"] != 0
        assert saved.json()["data"]["workflow"]["meta_data"]["unilab"]["resource_scopes"][0]["resources"] == ["reactor"]
    # 旧任务仍持有原来的冻结计划；新声明只作用于后续实例。
    frozen = service.get_workflow_task(task["uuid"])["execution_plan"]["resource_intervals"]
    assert all(not interval["explicit_boundary"] for frame in frozen["frames"] for interval in frame["plan"]["intervals"])


def test_missing_reserved_member_is_rejected_even_if_current_action_use_is_valid(service):
    task = make_task(service, ("reactor", "camera"), explicit=True)
    intervals = ResourceIntervals(service)
    claims = ExecutionClaims(service)
    epoch = claims.activate_authority()
    request = request_for(service, task)
    assert intervals.admit(request, epoch)
    with service.transaction() as conn:
        row = conn.execute("SELECT ownerships FROM workflow_resource_admission WHERE job_uuid=?", (request.owner_uuid,)).fetchone()
        changed = json.loads(row[0])
        changed.pop("/devices/camera")
        conn.execute("UPDATE workflow_resource_admission SET ownerships=? WHERE job_uuid=?", (json.dumps(changed), request.owner_uuid))
    with pytest.raises(StoreConflict, match="冻结计划"):
        claims.reserve(request, authority_epoch=epoch)


def test_concurrent_connections_never_share_physical_use(service):
    from concurrent.futures import ThreadPoolExecutor
    tasks = [make_task(service, ("reactor",)) for _ in range(2)]
    requests = [request_for(service, task) for task in tasks]
    other = WorkflowService(service.path)
    epoch = ExecutionClaims(service).activate_authority()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ResourceIntervals(store).admit, request, epoch)
                       for store, request in zip((service, other), requests)]
            assert sorted(f.result() for f in futures) == [False, True]
        assert len(ResourceIntervals(service).snapshot()) == 1
        assert service.connection.execute("SELECT count(*) FROM workflow_resource_use").fetchone()[0] == 1
    finally:
        other.close()


def test_legacy_claim_without_intervals_blocks_new_dispatch_until_reconciled(service):
    old = make_task(service, ("reactor",))
    new = make_task(service, ("camera",))
    old_request = request_for(service, old).model_copy(update={"interval_admission": False})
    claims = ExecutionClaims(service)
    claims.reserve(old_request)
    service.mark_workflow_node_job_running(old_request.owner_uuid)
    claims.mark_dispatched(old_request.owner_uuid)
    executor = Executor()
    scheduler = BackendScheduler(service, executor)
    try:
        assert "legacy_resource_reconciliation" in scheduler.dispatch_pause_reasons
        scheduler.submit(new["uuid"])
        scheduler.submit(old["uuid"])
        wait_for(lambda: bool(scheduler.list_reconciliation_decisions()))
        scheduler.resume_dispatch(reason="legacy_resource_reconciliation")
        assert executor.dispatched == []
        scheduler._on_executor_finished(old_request.owner_uuid, True, {})
        wait_for(lambda: len(executor.dispatched) == 1)
        assert executor.dispatched[0]["task_id"] == new["uuid"]
        assert "legacy_resource_reconciliation" not in scheduler.dispatch_pause_reasons
        executor.finish(0)
        wait_for(lambda: service.get_workflow_task(new["uuid"])["status"] == "succeeded")
    finally:
        scheduler.stop()


def test_retry_cannot_hide_failure_or_release_scope_before_pending_steps_are_canceled(service):
    task = make_task(service, explicit=True)
    request = request_for(service, task)
    intervals = ResourceIntervals(service)
    epoch = ExecutionClaims(service).activate_authority()
    assert intervals.admit(request, epoch)
    service.mark_workflow_node_job_running(request.owner_uuid)
    outcome = service.record_workflow_node_job_terminal(request.owner_uuid, status="failed", return_info={},
        error_info=[{"code": "driver_failed"}], error_resolution={"selected_action": "retry"})
    assert outcome["next_job"] is not None
    intervals.release_job(request.owner_uuid)
    row = intervals.snapshot()[0]
    assert row["state"] == "retained"
    with pytest.raises(StoreConflict, match="待执行"):
        intervals.confirm_release(row["owner_uuid"], expected_version=row["version"], reason="设备已停止")
    assert not intervals.admit(request_for(service, task), epoch)
