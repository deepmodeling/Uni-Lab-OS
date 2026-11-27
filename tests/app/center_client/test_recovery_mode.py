"""灾备模式跨重启保留，只恢复原事件，不创建任务、作业或新控制会话。"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INVOCATION, FakeTransport, config
from tests.app.center_client.test_event_envelope import draft, store_at
from tests.app.center_client.test_event_history import response, session
from unilabos.app.center_client.client import CenterClient, CenterClientError
from unilabos.app.center_client.runtime import CenterRuntime
from unilabos.persistence.recovery_mode import EvidenceOnlyError, enter, status
from unilabos.workflow.center_boundary import apply_reconciliation
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


class ForbiddenService:
    def __getattr__(self, name):
        raise AssertionError(f"灾备模式不能调用执行服务：{name}")


@pytest.mark.parametrize("status_code", [413, 415, 431])
def test_recovery_request_hold_does_not_block_later_evidence(tmp_path, status_code):
    from unilabos.app.center_client.transport import CenterRequestHeldError

    source = store_at(tmp_path / "events.db")
    session(source)
    first = source.record_event(INVOCATION, terminal=False, body=draft())
    second = source.record_event(INVOCATION, terminal=True, body=draft(terminal=True))
    with source._db.transaction():
        enter(source._db, "一条历史请求等待核实，其他证据仍须推进")
    transport = FakeTransport([CenterRequestHeldError(first["event_digest"], status_code), response(second, prefix=0)])
    run = CenterRuntime(config(tmp_path), CenterClient(config(tmp_path), transport), source, ForbiddenService())
    try:
        run.poll_once()
        assert [call["body"]["event_sequence"] for call in transport.calls] == [1, 2]
        states = dict(source._db.execute("SELECT event_sequence,state FROM center_event_history_recovery"))
        assert states == {1: "needs_review", 2: "recovered"}
        assert source._db.execute("SELECT acknowledged FROM center_boundary_outbox WHERE event_sequence=1").fetchone()[0] == 0
    finally:
        run.close()


def test_mode_persists_and_replays_original_evidence_with_missing_task_and_job(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    event = source.record_event(INVOCATION, terminal=False, body=draft())
    with source._db.transaction():
        entered = enter(source._db, "恢复旧备份；只核对证据")
        assert enter(source._db, "重复启动") == entered
    epoch = source.event_epoch_uuid
    source.close()
    source = store_at(path)
    assert source.recovery_status()["mode"] == "evidence_only"
    assert source.event_epoch_uuid == epoch
    transport = FakeTransport([response(event, prefix=1)])
    runtime = CenterRuntime(config(tmp_path), CenterClient(config(tmp_path), transport), source,
                            ForbiddenService(), handoffs=ForbiddenService())
    runtime.materials = ForbiddenService()
    runtime.poll_once()
    assert len(transport.calls) == 1
    assert transport.calls[0]["path"].endswith("/event-history")
    assert transport.calls[0]["body"]["identity"] == {
        "event_uuid": event["event_uuid"], "event_digest": event["event_digest"]}
    assert source._db.execute("SELECT COUNT(*) FROM center_invocation_inbox").fetchone()[0] == 0
    assert not source._db.execute("SELECT name FROM sqlite_master WHERE name IN ('workflow_task','workflow_node_job')").fetchall()
    assert json.loads(source._db.execute("SELECT event_json FROM center_boundary_outbox").fetchone()[0]) == event
    with pytest.raises(EvidenceOnlyError):
        runtime._process({})
    runtime.close()


def test_missing_session_does_not_open_new_control_session(tmp_path):
    source = store_at(tmp_path / "events.db")
    with source._db.transaction():
        enter(source._db, "实例损坏，缺少原会话")
    transport = FakeTransport([])
    runtime = CenterRuntime(config(tmp_path), CenterClient(config(tmp_path), transport), source, ForbiddenService())
    with pytest.raises(CenterClientError, match="原历史会话"):
        runtime.poll_once()
    assert transport.calls == []
    runtime.close()


def test_missing_local_event_can_be_recovered_in_mode_without_new_identity(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    event = source.record_event(INVOCATION, terminal=False, body=draft())
    with source._db.transaction():
        source._db.execute("DELETE FROM center_boundary_outbox")
        enter(source._db, "恢复本地事件缺口")
    source.close()
    source = store_at(path)
    transport = FakeTransport([response(event, prefix=1)])
    runtime = CenterRuntime(config(tmp_path), CenterClient(config(tmp_path), transport), source, ForbiddenService())
    runtime.poll_once()
    assert transport.calls[0]["body"]["identity"] is None
    assert source._db.execute("SELECT last_event_sequence FROM center_runtime_state").fetchone()[0] == 1
    assert json.loads(source._db.execute("SELECT event_json FROM center_boundary_outbox").fetchone()[0]) == event
    assert source.recovery_status()["mode"] == "evidence_only"
    runtime.close()


def test_workflow_entry_recovery_and_physical_dispatch_fail_closed(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    with store.transaction() as conn:
        enter(conn, "恢复期间禁止所有新执行")
    assert store.evidence_only()
    with pytest.raises(EvidenceOnlyError):
        store.create_task_with_jobs(workflow_uuid=str(uuid4()), task_uuid=str(uuid4()), run_mode="normal",
                                    target_node_uuid=None, description=None, meta_data={},
                                    plan_builder=lambda _: pytest.fail("不能编译或创建作业"))
    with store.transaction() as conn:
        with pytest.raises(EvidenceOnlyError):
            apply_reconciliation(conn, str(uuid4()))
        assert conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 0
    # 在最终派发钩子与启动恢复钩子调用真实实现，后续字段都故意不存在。
    bridge = object.__new__(TaskSchedulerBridge)
    bridge._store, bridge._closed = store, False
    assert bridge._on_job_pre_dispatch({}) is False
    assert bridge.recover_active_tasks() == []
    with pytest.raises(StoreConflict, match="灾备模式"):
        bridge._on_manual_continuation_dispatching(str(uuid4()))
    store.close()
    reopened = WorkflowStore(tmp_path / "workflow.db")
    assert reopened.evidence_only()
    reopened.close()


def test_mode_entry_rollback_and_corruption_are_not_normal(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    with pytest.raises(RuntimeError, match="回滚"):
        with store.transaction() as conn:
            enter(conn, "中断进入")
            raise RuntimeError("回滚")
    assert not store.evidence_only()
    with store.transaction() as conn:
        enter(conn, "正式进入")
        conn.execute("DELETE FROM center_recovery_mode")
    with store.read() as conn, pytest.raises(EvidenceOnlyError, match="不完整"):
        status(conn)
    store.close()


def retained(event, *, owner="unproven"):
    return {**{key: event[key] for key in ("event_uuid", "event_digest", "event_epoch_uuid", "event_sequence", "station_uuid", "instance_uuid")},
            "receipt_uuid": str(uuid4()), "recovery_revision": 1, "owner_verification": owner,
            "disposition": "retained_needs_review"}


def test_missing_subject_retention_never_acknowledges_or_recreates_task(tmp_path):
    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    event = source.record_event(INVOCATION, terminal=False, body=draft())
    before = dict(source._db.execute("SELECT * FROM center_boundary_outbox").fetchone())
    with source._db.transaction():
        enter(source._db, "中心原调用丢失，仅保管证据")
    receipt = retained(event)
    transport = FakeTransport([response(event, status="missing"), receipt])
    runtime = CenterRuntime(config(tmp_path), CenterClient(config(tmp_path), transport), source, ForbiddenService())
    runtime.poll_once()
    assert [call["path"].split("/")[-1] for call in transport.calls] == ["event-history", "recovery-evidence"]
    assert transport.calls[-1]["body"] == {"envelope": event}
    assert dict(source._db.execute("SELECT * FROM center_boundary_outbox").fetchone()) == before
    row = source._db.execute("SELECT * FROM center_event_history_recovery").fetchone()
    assert row["state"] == "retained_needs_review"
    assert source.events() == []
    assert source.event_held(event["event_uuid"])
    assert json.loads(row["retention_receipt_json"]) == receipt
    assert source._db.execute("SELECT COUNT(*) FROM center_invocation_inbox").fetchone()[0] == 0
    runtime.close()


@pytest.mark.parametrize("field,value", [("event_sequence", True),
                                         ("event_uuid", "11111111-1111-4111-8111-111111111111"),
                                         ("disposition", "applied"), ("owner_verification", "authorized")])
def test_retention_receipt_must_match_exact_original_identity(tmp_path, field, value):
    from unilabos.center_protocol.events import EventEnvelopeError
    from unilabos.center_protocol.recovery_evidence import validate_receipt

    source = store_at(tmp_path / "events.db")
    event = source.record_event(INVOCATION, terminal=False, body=draft())
    receipt = retained(event)
    receipt[field] = value
    with pytest.raises(EventEnvelopeError):
        validate_receipt(receipt, event)
    source.close()


def test_recovery_held_command_is_a_persistent_nonexecution_tombstone(tmp_path):
    from copy import deepcopy

    from tests.app.center_client.test_runtime import Center
    from unilabos.app.center_client.runtime import validate_command_page

    source = store_at(tmp_path / "events.db")
    command = Center().add()
    command["disposition"] = "recovery_held"
    command["disposition_revision"] = 1
    page = {"commands": [command], "has_more": False, "next_sequence": 1}
    validate_command_page(page, source.station_uuid, 0)
    source.receive(command)
    transport = FakeTransport([])
    runtime = CenterRuntime(config(tmp_path), CenterClient(config(tmp_path), transport), source, ForbiddenService())
    runtime._process(command)
    assert source.commands() == []
    assert transport.calls == []
    row = source._db.execute("SELECT error FROM center_command_inbox").fetchone()
    assert row["error"] == "RECOVERY_EXECUTION_HELD"
    replay = deepcopy(command)
    replay["disposition"] = "active"
    replay["disposition_revision"] = 2
    with pytest.raises(CenterClientError):
        source.receive(replay)
    runtime.close()


def resume_request(snapshot, checkpoint_uuid=None):
    return {"expected_revision": snapshot["status"]["revision"], "expected_evidence_digest": snapshot["evidence_digest"],
            "reviewed_by": "模拟验收操作者", "reason": "已核对原执行与库存，旧身份继续隔离", "evidence_refs": ["fixture://recovery-evidence"],
            "previous_work": "keep_isolated", "checkpoint_uuid": checkpoint_uuid}


def test_reviewed_station_resume_keeps_missing_original_identity_and_old_commands_isolated(tmp_path):
    from unilabos.persistence.recovery_mode import command_held, held, require_execution, resume, review

    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    with source._db.transaction():
        mode = enter(source._db, "旧工站备份恢复")
    checkpoint = {"checkpoint_uuid": str(uuid4()), "recovery_uuid": mode["recovery_uuid"],
                  "station_uuid": source.station_uuid, "instance_uuid": source.instance_uuid,
                  "event_epoch_uuid": source.event_epoch_uuid, "command_high_watermark": 10, "event_high_watermark": 0,
                  "invocations": [{"invocation_uuid": INVOCATION, "global_task_uuid": str(uuid4()), "invocation_key": "original-node"}],
                  "reason": "中心已隔离原身份并 fence 原进程", "created_at_ms": 1234}
    transport = FakeTransport([checkpoint])
    client = CenterClient(config(tmp_path), transport)
    client.restore_evidence_session(source.session())
    verified = client.recovery_checkpoint(checkpoint["checkpoint_uuid"], event_epoch_uuid=source.event_epoch_uuid,
                                          recovery_uuid=mode["recovery_uuid"])
    assert transport.calls[0]["method"] == "GET"
    with source._db.transaction():
        snapshot = review(source._db)
        after = resume(source._db, resume_request(snapshot, checkpoint["checkpoint_uuid"]), checkpoint=verified)
        assert after["mode"] == "normal"
        assert held(source._db, "invocation", INVOCATION)
        assert command_held(source._db, {"command_uuid": str(uuid4()), "command_sequence": 9, "payload": {}})
        with pytest.raises(EvidenceOnlyError):
            require_execution(source._db, invocation_uuid=INVOCATION)
        with pytest.raises(EvidenceOnlyError):
            item = checkpoint["invocations"][0]
            require_execution(source._db, backend_task_uuid=item["global_task_uuid"], invocation_key=item["invocation_key"])
        require_execution(source._db, invocation_uuid=str(uuid4()))
    assert source._db.execute("SELECT COUNT(*) FROM center_invocation_inbox").fetchone()[0] == 0
    source.close()
    workflow = WorkflowStore(path)
    item = checkpoint["invocations"][0]
    with pytest.raises(EvidenceOnlyError):
        workflow.create_task_with_jobs(workflow_uuid=str(uuid4()), task_uuid=str(uuid4()), run_mode="normal",
                                        target_node_uuid=None, description=None, meta_data={},
                                        backend_task_uuid=item["global_task_uuid"], invocation_key=item["invocation_key"],
                                        plan_builder=lambda _: pytest.fail("不能重建旧 Task/Job"))
    with workflow.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 0
    workflow.close()


def test_stale_local_review_or_missing_center_proof_cannot_resume(tmp_path):
    from unilabos.persistence.recovery_mode import resume, review

    source = store_at(tmp_path / "events.db")
    with source._db.transaction():
        enter(source._db, "等待恢复核实")
        snapshot = review(source._db)
    with source._db.transaction():
        source._db.execute("UPDATE center_recovery_mode SET reason='新事实需要核实'")
    with source._db.transaction(), pytest.raises(EvidenceOnlyError, match="快照"):
        resume(source._db, resume_request(snapshot))
    with source._db.transaction():
        snapshot = review(source._db)
    from unilabos.center_protocol.events import EventEnvelopeError
    with source._db.transaction(), pytest.raises(EventEnvelopeError, match="基线"):
        resume(source._db, resume_request(snapshot))
    assert source.recovery_status()["mode"] == "evidence_only"
    source.close()


@pytest.mark.parametrize("source_of_high", ["inbox", "previous_baseline"])
def test_checkpoint_command_watermark_cannot_roll_back_local_facts(tmp_path, source_of_high):
    from tests.app.center_client.test_runtime import Center
    from unilabos.persistence.recovery_mode import resume, review

    source = store_at(tmp_path / "events.db")
    if source_of_high == "inbox":
        source.receive(Center().add())
    with source._db.transaction():
        mode = enter(source._db, "命令账本倒退检查")
        if source_of_high == "previous_baseline":
            source._db.execute("INSERT INTO center_recovery_baseline VALUES (?, '{}', 1)", (str(uuid4()),))
        snapshot = review(source._db)
    checkpoint = {"checkpoint_uuid": str(uuid4()), "recovery_uuid": mode["recovery_uuid"],
                  "station_uuid": source.station_uuid, "instance_uuid": source.instance_uuid,
                  "event_epoch_uuid": source.event_epoch_uuid, "command_high_watermark": 0,
                  "event_high_watermark": 0, "invocations": [], "reason": "旧备份基线", "created_at_ms": 1}
    with pytest.raises(EvidenceOnlyError, match="命令高水位"), source._db.transaction():
        resume(source._db, resume_request(snapshot, checkpoint["checkpoint_uuid"]), checkpoint=checkpoint)
    assert source.recovery_status()["mode"] == "evidence_only"
    source.close()


def test_missing_runtime_with_center_tables_cannot_resume_as_standalone(tmp_path):
    from unilabos.persistence.recovery_mode import resume, review

    source = store_at(tmp_path / "events.db")
    with source._db.transaction():
        enter(source._db, "实例账本损坏")
        source._db.execute("DROP TABLE center_runtime_state")
        snapshot = review(source._db)
    with pytest.raises(EvidenceOnlyError, match="不能按独立工站"), source._db.transaction():
        resume(source._db, resume_request(snapshot))
    assert source.recovery_status()["mode"] == "evidence_only"
    source.close()


def test_workflow_service_reports_recovery_as_conflict_without_creating_task(tmp_path):
    from unilabos.workflow.service import WorkflowConflict, WorkflowService

    store = WorkflowStore(tmp_path / "workflow.db")
    identity = str(uuid4())
    store.create_workflow(workflow_uuid=identity, name="灾备执行门", tags=[], description=None, meta_data={})
    service = WorkflowService(store)
    try:
        with store.transaction() as conn:
            enter(conn, "禁止本地运行入口创建任务")
        with pytest.raises(WorkflowConflict) as caught:
            service.create_workflow_task(workflow_uuid=identity, run_mode="normal", target_node_uuid=None,
                                         input_value={}, description=None, meta_data={})
        assert caught.value.status == 409
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 0
    finally:
        service.close()



def test_reviewed_resume_rotates_epoch_preserving_retained_outbox_and_history(tmp_path):
    from unilabos.app.center_client.event_history import EventHistoryRecovery, EventHistoryStore
    from unilabos.persistence.recovery_mode import resume, review

    path = tmp_path / "events.db"
    source = store_at(path)
    session(source)
    event = source.record_event(INVOCATION, terminal=False, body=draft())
    original = dict(source._db.execute("SELECT * FROM center_boundary_outbox").fetchone())
    with source._db.transaction():
        mode = enter(source._db, "从旧备份恢复")
    client = CenterClient(config(tmp_path), FakeTransport([response(event, status="missing"), retained(event)]))
    client.restore_evidence_session(source.session())
    history = EventHistoryStore(path, station_uuid=source.station_uuid, instance_uuid=source.instance_uuid)
    try:
        EventHistoryRecovery(client, history, retain_missing=True).recover(1)
    finally:
        history.close()
    checkpoint = {"checkpoint_uuid": str(uuid4()), "recovery_uuid": mode["recovery_uuid"],
                  "station_uuid": source.station_uuid, "instance_uuid": source.instance_uuid,
                  "event_epoch_uuid": source.event_epoch_uuid, "command_high_watermark": 0,
                  "event_high_watermark": 1, "invocations": [], "reason": "保留旧调用隔离", "created_at_ms": 1}
    with source._db.transaction():
        snapshot = review(source._db)
        resume(source._db, resume_request(snapshot, checkpoint["checkpoint_uuid"]), checkpoint=checkpoint)
    assert dict(source._db.execute("SELECT * FROM center_boundary_outbox").fetchone()) == original
    source.close()
    source = store_at(path)
    assert source.event_epoch_uuid != event["event_epoch_uuid"]
    assert source.events() == []  # 保管责任保留，但不会阻塞新纪元命令补拉。
    new_event = source.record_event(str(uuid4()), terminal=False, body=draft())
    assert new_event["event_sequence"] == 1
    assert new_event["event_epoch_uuid"] == source.event_epoch_uuid
    assert source.record_event(INVOCATION, terminal=False, body=draft()) == event
    assert source._db.execute("SELECT COUNT(*) FROM center_boundary_outbox WHERE event_sequence=1").fetchone()[0] == 2
    source.close()
    # 重启验证两个纪元的连续性；历史查询不重写原信封或高水位。
    source = store_at(path)
    history = EventHistoryStore(path, station_uuid=source.station_uuid, instance_uuid=source.instance_uuid,
                                event_epoch_uuid=event["event_epoch_uuid"])
    try:
        assert history.prepare(1) == event
        assert history.gaps() == []
        assert history.session() is not None
    finally:
        history.close()
        source.close()


@pytest.mark.parametrize("exit_mode", [False, True])
def test_old_retry_and_repeat_cannot_create_jobs_even_after_review(tmp_path, exit_mode):
    from tests.workflow.test_error_control import decision_request, seed_job
    from unilabos.persistence.recovery_mode import resume, review
    from unilabos.workflow.error_control import StationExecutionControl
    from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection

    store = WorkflowStore(tmp_path / "workflow.db")
    task, job = seed_job(store)
    control = StationExecutionControl(store)
    decision = control.report_error(job, phase="execution", message="待核实原执行")
    projection = TaskRuntimeProjection(store)
    with store.transaction() as conn:
        enter(conn, "旧任务禁止生成新作业")
        if exit_mode:
            resume(conn, resume_request(review(conn)))
    try:
        with pytest.raises(StoreConflict, match="灾备|隔离"):
            control.decide(decision["decision_id"], decision_request(control, decision, "retry_current_node"),
                           idempotency_key="recovery-retry", operator_id="reviewer")
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_node_job SET executor_kind='repeat_until' WHERE uuid=?", (job,))
        with pytest.raises(StoreConflict, match="灾备|隔离"):
            projection.materialize_repeat_iteration(control_job_uuid=job, iteration_index=0,
                                                    control_path="original-loop", jobs=[{"job_uuid": str(uuid4())}])
        assert len(store.list_jobs(task)) == 1
        assert store.get_job(job)["current_attempt"] is True
    finally:
        store.close()


def test_public_device_action_creation_does_not_persist_or_dispatch_in_recovery(tmp_path):
    from tests.registry.test_template_projection import DEVICE_MATERIAL_UUID, FakeInventoryStore, FakeRegistry
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.composition import compose_local_workflow_template_runtime, reset_workflow_service_for_test
    from unilabos.workflow.service import WorkflowConflict

    reset_workflow_service_for_test()
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    try:
        service, projection = compose_local_workflow_template_runtime(
            tmp_path, inventory_store=FakeInventoryStore(), registry=FakeRegistry(), scheduler=scheduler)
        action = projection.snapshot().require_action("lab.devices:Pump", "transfer").template
        with service.runtime_store.transaction() as conn:
            enter(conn, "禁止设备单动作入口创建 Task/Job")
        with pytest.raises(WorkflowConflict) as caught:
            service.create_device_action_run(material_uuid=DEVICE_MATERIAL_UUID,
                workflow_node_template_uuid=action["uuid"], param={"volume": 2.0}, execution_policy={},
                idempotency_key="recovery-action", description=None, meta_data={})
        assert caught.value.status == 409
        assert dispatcher.dispatched == []
        with service.runtime_store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 0
    finally:
        reset_workflow_service_for_test()
