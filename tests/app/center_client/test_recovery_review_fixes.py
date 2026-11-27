"""恢复复审的边界回归：旧执行提交身份、手动父任务和离线旧库升级。"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import EPOCH, INSTANCE, STATION, FakeTransport, config
from tests.app.center_client.test_recovery_mode import resume_request
from tests.workflow.test_error_control import decision_request, seed_job
from unilabos.app.center_client.client import CenterClient, ControlSession
from unilabos.persistence.recovery_mode import EvidenceOnlyError, enter, resume, review
from unilabos.workflow.device_action_run_store import DeviceActionRunStore
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.store import StoreConflict, WorkflowStore


def prepared_run(key):
    node = str(uuid4())
    return ({"uuid": str(uuid4()), "idempotency_key": key, "request_fingerprint": "same-action",
             "workflow_snapshot": {}, "execution_plan": {}, "target_node_uuid": node},
            {"uuid": str(uuid4()), "workflow_node_uuid": node, "material_uuid": str(uuid4()), "param": {}})


@pytest.mark.parametrize("exit_mode", [False, True])
def test_old_manual_session_cannot_create_new_execution(tmp_path, exit_mode):
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        task, job = seed_job(store)
        control = StationExecutionControl(store)
        error = control.report_error(job, phase="execution", message="旧设备错误")
        entered = control.decide(error["decision_id"], decision_request(control, error, "enter_manual_handling"),
                                 idempotency_key="manual-session", operator_id="operator")
        session = entered["station"]["active_session"]
        with store.transaction() as conn:
            enter(conn, "旧会话必须保持隔离")
            if exit_mode:
                resume(conn, resume_request(review(conn)))
        def prepare(request, *, idempotency_key, metadata):
            assert metadata["source_task_uuid"] == task
            return prepared_run(idempotency_key)
        with pytest.raises(StoreConflict, match="灾备|隔离"):
            control.accept_manual(session["session_id"], {
                "expected_station_version": entered["station"]["station_version"],
                "expected_session_version": session["version"],
            }, idempotency_key="new-command", operator_id="operator", prepare=prepare)
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.parametrize("delete_before_recovery", [False, True])
def test_deleted_device_action_submission_stays_isolated_after_resume(tmp_path, delete_before_recovery):
    store = WorkflowStore(tmp_path / "workflow.db")
    action_store = DeviceActionRunStore(store)
    def create(key):
        task, job = prepared_run(key)
        return action_store.create_or_reuse(task=task, job=job, idempotency_key=key,
                                            request_fingerprint=task["request_fingerprint"])
    try:
        original = create("original-key")
        task = original["task"]["uuid"]
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_task SET status='succeeded' WHERE uuid=?", (task,))
            conn.execute("UPDATE workflow_node_job SET status='succeeded' WHERE workflow_task_uuid=?", (task,))
        if delete_before_recovery:
            store.delete_task(task)
        with store.transaction() as conn:
            enter(conn, "隔离原动作的提交身份")
            resume(conn, resume_request(review(conn)))
        if not delete_before_recovery:
            store.delete_task(task)
        with pytest.raises(StoreConflict, match="隔离"):
            create("original-key")
        assert create("new-key")["created"] is True
        with store.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM workflow_node_job").fetchone()[0] == 2
    finally:
        store.close()


def old_center_database(path):
    """保留 T11 的零水位 schema；从未调用新版 DurableCenterStore。"""
    original_session = asdict(ControlSession(str(uuid4()), str(uuid4()), 1, STATION, INSTANCE))
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE center_runtime_state(singleton TEXT PRIMARY KEY, station_uuid TEXT NOT NULL,
                instance_uuid TEXT NOT NULL,event_epoch_uuid TEXT NOT NULL,session_json TEXT,last_event_sequence BIGINT NOT NULL DEFAULT 0);
            CREATE TABLE center_boundary_outbox(event_uuid TEXT PRIMARY KEY,invocation_uuid TEXT NOT NULL,
                phase INTEGER NOT NULL,event_json TEXT NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0,
                event_sequence BIGINT,application TEXT,UNIQUE(invocation_uuid,phase));
            CREATE UNIQUE INDEX ux_center_event_sequence ON center_boundary_outbox(event_sequence);
            CREATE TABLE center_handoff_outbox(event_uuid TEXT PRIMARY KEY,handoff_uuid TEXT NOT NULL,
                event_sequence BIGINT NOT NULL UNIQUE,event_json TEXT NOT NULL,ack_json TEXT);
        """)
        db.execute("INSERT INTO center_runtime_state VALUES('runtime',?,?,?,?,0)",
                   (STATION, INSTANCE, EPOCH, json.dumps(original_session)))
    return original_session


def test_offline_cli_upgrades_old_zero_watermark_before_review_and_resume(tmp_path, monkeypatch, capsys):
    from unilabos.app.center_client import client as client_module
    from unilabos.app.center_client.config import CenterClientConfig
    from unilabos.app.center_client.recovery_mode import main

    path = tmp_path / "old-workflow.db"
    original_session = old_center_database(path)
    def invoke(*args):
        monkeypatch.setattr(sys, "argv", ["recovery-mode", "--database", str(path), *args])
        main()
        return json.loads(capsys.readouterr().out)
    mode = invoke("enter", "--reason", "无需启动工站轮询的旧库恢复")
    snapshot = invoke("review")
    checkpoint = {"checkpoint_uuid": str(uuid4()), "recovery_uuid": mode["recovery_uuid"],
                  "station_uuid": STATION, "instance_uuid": INSTANCE, "event_epoch_uuid": EPOCH,
                  "command_high_watermark": 0, "event_high_watermark": 0,
                  "invocations": [], "reason": "中心已隔离旧执行", "created_at_ms": 1234}
    transport = FakeTransport([checkpoint])
    client = CenterClient(config(tmp_path), transport)
    monkeypatch.setattr(client_module, "CenterClient", lambda _: client)
    monkeypatch.setattr(CenterClientConfig, "from_env", lambda: config(tmp_path))
    verification = tmp_path / "review.json"
    verification.write_text(json.dumps(resume_request(snapshot, checkpoint["checkpoint_uuid"])))
    assert invoke("resume", "--verification-file", str(verification))["mode"] == "normal"
    assert len(transport.calls) == 1 and transport.calls[0]["method"] == "GET"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT event_epoch_uuid FROM center_runtime_state").fetchone()[0] != EPOCH
        archived = db.execute("SELECT event_epoch_uuid,last_event_sequence,session_json FROM center_epoch_history").fetchone()
        assert archived[:2] == (EPOCH, 0) and json.loads(archived[2]) == original_session
        assert db.execute("SELECT COUNT(*) FROM center_boundary_outbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM center_handoff_outbox").fetchone()[0] == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('workflow_task','workflow_node_job')").fetchall()


def test_checkpoint_higher_event_watermark_rejects_old_station_backup(tmp_path):
    from tests.app.center_client.test_event_envelope import store_at

    source = store_at(tmp_path / "old-backup.db")
    try:
        with source._db.transaction():
            mode = enter(source._db, "中心仍持有备份之后的原交接证据")
        checkpoint = {"checkpoint_uuid": str(uuid4()), "recovery_uuid": mode["recovery_uuid"],
                      "station_uuid": STATION, "instance_uuid": INSTANCE, "event_epoch_uuid": source.event_epoch_uuid,
                      "command_high_watermark": 0, "event_high_watermark": 1,
                      "invocations": [], "reason": "原交接信封仍存在", "created_at_ms": 1234}
        with pytest.raises(EvidenceOnlyError, match="高水位不一致"), source._db.transaction():
            resume(source._db, resume_request(review(source._db), checkpoint["checkpoint_uuid"]), checkpoint=checkpoint)
        assert source.recovery_status()["mode"] == "evidence_only"
    finally:
        source.close()


def test_offline_upgrade_rejects_invalid_evidence_and_rolls_back_schema(tmp_path, monkeypatch):
    from unilabos.app.center_client.recovery_mode import main
    from unilabos.persistence.center_schema import CenterSchemaError

    path = tmp_path / "invalid-old.db"
    old_center_database(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO center_handoff_outbox VALUES (?,?,1,'{}',NULL)", (str(uuid4()), str(uuid4())))
        db.execute("UPDATE center_runtime_state SET last_event_sequence=1")
    monkeypatch.setattr(sys, "argv", ["recovery-mode", "--database", str(path), "enter", "--reason", "检查旧证据"])
    with pytest.raises(CenterSchemaError):
        main()
    with sqlite3.connect(path) as db:
        assert "event_epoch_uuid" not in {row[1] for row in db.execute("PRAGMA table_info(center_handoff_outbox)")}
        assert db.execute("SELECT event_json FROM center_handoff_outbox").fetchone()[0] == "{}"
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('center_epoch_history','center_recovery_mode')").fetchall()
