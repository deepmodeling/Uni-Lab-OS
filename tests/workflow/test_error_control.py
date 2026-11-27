"""工站错误处置的持久公共状态转换。"""

from uuid import uuid4

import pytest

from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.job_evidence import ensure_job_evidence_schema
from unilabos.workflow.store import WorkflowStore, utc_now


def seed_job(store):
    """准备已有任务与作业，后续断言只读取公共投影。"""
    workflow, task, node, job = (str(uuid4()) for _ in range(4))
    store.create_workflow(
        workflow_uuid=workflow, name="错误处置", tags=[], description=None, meta_data={}
    )
    now = utc_now()
    with store.transaction() as conn:
        conn.execute(
            """INSERT INTO workflow_task(uuid,create_time,update_time,meta_data,
            workflow_uuid,status,workflow_snapshot,execution_plan,run_mode,control_status,
            cleanup_status,trace_context,input,output,error_info)
            VALUES(?,?,?,'{}',?,'running','{}','{}','normal','active','none','{}','{}','{}','[]')""",
            (task, now, now, workflow),
        )
        conn.execute(
            """INSERT INTO workflow_node_job(uuid,create_time,update_time,meta_data,
            workflow_task_uuid,workflow_node_uuid,feedback_sequence,topological_index,
            executor_kind,execution_policy,execution_timeout_seconds,status,attempt,param,
            feedback_data,return_info,control_data,error_info)
            VALUES(?,?,?,'{}',?,?,0,0,'device_action','{}',0,'running',1,'{}','{}','{}','{}','[]')""",
            (job, now, now, task, node),
        )
    return task, job


def test_prepare_material_io_error_fails_pending_job(tmp_path):
    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    task, job = seed_job(store)
    with store.transaction() as conn:
        ensure_job_evidence_schema(conn)
        conn.execute(
            "UPDATE workflow_node_job SET status='pending' WHERE uuid=?",
            (job,),
        )
    control = StationExecutionControl(store)
    control.report_error(
        job,
        phase="prepare",
        message="site 名称与物料实际库位不一致",
        error_code="invalid_parameter",
    )
    saved = store.get_job(job)
    assert saved["status"] == "failed"
    assert saved["error_info"] == [
        {
            "code": "invalid_parameter",
            "message": "site 名称与物料实际库位不一致",
        }
    ]
    assert store.get_task(task)["control_status"] == "error_waiting"
    store.close()


def test_proved_success_closes_unknown_error_without_touching_other_decisions(tmp_path):
    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    task, job = seed_job(store)
    other = str(uuid4())
    now = utc_now()
    with store.transaction() as conn:
        conn.execute(
            """INSERT INTO workflow_node_job(uuid,create_time,update_time,meta_data,
            workflow_task_uuid,workflow_node_uuid,feedback_sequence,topological_index,
            executor_kind,execution_policy,execution_timeout_seconds,status,attempt,param,
            feedback_data,return_info,control_data,error_info)
            VALUES(?,?,?,'{}',?,?,0,1,'device_action','{}',0,'running',1,'{}','{}','{}','{}','[]')""",
            (other, now, now, task, str(uuid4())),
        )
    control = StationExecutionControl(store)
    control.report_error(job, phase="execution", message=f"执行结果未知：workflow-node-job:{job}")
    control.report_error(other, phase="execution", message="设备离线")
    assert control.accept_proved_success(job) is True
    assert control.awaits_decision(job) is False
    assert control.awaits_decision(other) is True
    assert store.get_task(task)["control_status"] == "error_waiting"
    assert control.snapshot()["mode"] == "PAUSED"
    assert control.accept_proved_success(other) is False
    store.close()


def test_error_pauses_station_once_and_survives_restart(tmp_path):
    path = tmp_path / "workflow.db"
    with_store = WorkflowStore(path)
    task, job = seed_job(with_store)
    control = StationExecutionControl(with_store)
    decision = control.report_error(job, phase="execution", message="设备离线")
    snapshot = control.snapshot()
    assert snapshot["mode"] == "PAUSED"
    assert snapshot["error_epoch"] == 1
    assert snapshot["active_session"] is None
    assert [option["id"] for option in decision["options"]] == [
        "cancel_task",
        "retry_current_node",
        "enter_manual_handling",
    ]
    assert with_store.get_task(task)["status"] == "running"
    assert with_store.get_task(task)["control_status"] == "error_waiting"
    assert (
        control.report_error(job, phase="execution", message="设备离线")["decision_id"]
        == decision["decision_id"]
    )
    assert control.snapshot()["station_version"] == snapshot["station_version"]
    with_store.close()
    reopened = WorkflowStore(path)
    try:
        recovered = StationExecutionControl(reopened).snapshot()
        assert recovered["mode"] == "PAUSED"
        assert recovered["errors"][0]["decision_id"] == decision["decision_id"]
    finally:
        reopened.close()


def decision_request(control, decision, action, **extra):
    snapshot = control.snapshot()
    return dict(
        action=action,
        expected_decision_version=decision["decision_version"],
        expected_station_version=snapshot["station_version"],
        observed_error_epoch=snapshot["error_epoch"],
        **extra,
    )


def test_manual_session_has_two_stages_and_retry_can_leave_station_paused(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        task, job = seed_job(store)
        control = StationExecutionControl(store)
        error = control.report_error(job, phase="execution", message="失败")
        entered = control.decide(
            error["decision_id"],
            decision_request(control, error, "enter_manual_handling"),
            idempotency_key="enter",
            operator_id="local-user",
        )
        session = entered["station"]["active_session"]
        assert session["stage"] == "MANUAL_HANDLING"
        complete = control.complete(
            session["session_id"],
            {
                "expected_session_version": session["version"],
                "expected_station_version": entered["station"]["station_version"],
                "summary": "现场已调整",
            },
            idempotency_key="complete",
            operator_id="local-user",
        )
        assert complete["station"]["mode"] == "PAUSED"
        assert (
            complete["station"]["active_session"]["session_id"] == session["session_id"]
        )
        error = complete["station"]["errors"][0]
        request = decision_request(
            control,
            error,
            "retry_current_node",
            resume_requested=False,
            session_id=session["session_id"],
            expected_session_version=complete["station"]["active_session"]["version"],
        )
        accepted = control.decide(
            error["decision_id"],
            request,
            idempotency_key="retry",
            operator_id="local-user",
        )
        assert accepted["status"] == "PENDING"
        assert accepted["station"]["errors"][0]["stage"] == "APPLYING"
        assert len(store.list_jobs(task)) == 2
        assert store.get_job(job)["current_attempt"] is False
        assert (
            control.decide(
                error["decision_id"],
                request,
                idempotency_key="retry",
                operator_id="local-user",
            )["command_uuid"]
            == accepted["command_uuid"]
        )
        applied = control.mark_applied(accepted["command_uuid"])
        assert applied["station"]["active_session"] is None
        assert applied["station"]["mode"] == "PAUSED"
    finally:
        store.close()


def test_new_error_invalidates_older_resume_confirmation(tmp_path):
    """另一个 task 出错后，旧页面的恢复确认不得放开工站。"""
    from unilabos.workflow.store import StoreConflict

    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        _, first = seed_job(store)
        _, second = seed_job(store)
        control = StationExecutionControl(store)
        control.report_error(first, phase="execution", message="第一处错误")
        snapshot = control.snapshot()
        request = {
            "expected_station_version": snapshot["station_version"],
            "observed_error_epoch": snapshot["error_epoch"],
            "operator_confirmed": True,
            "confirm_snapshot_id": snapshot["snapshot_id"],
        }
        control.report_error(second, phase="execution", message="第二处错误")
        with pytest.raises(StoreConflict, match="station_version_conflict"):
            control.resume(
                request, idempotency_key="outdated-resume", operator_id="tester"
            )
        assert control.snapshot()["mode"] == "PAUSED"
        assert len(control.snapshot()["errors"]) == 2
    finally:
        store.close()


def test_cancel_settles_multiple_error_facts_for_same_job(tmp_path):
    """取消已受理后又有取消不确定事实，原命令仍须可幂等收尾。"""
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        task, job = seed_job(store)
        control = StationExecutionControl(store)
        error = control.report_error(job, phase="execution", message="执行失败")
        accepted = control.decide(
            error["decision_id"],
            decision_request(control, error, "cancel_task"),
            idempotency_key="cancel",
            operator_id="tester",
        )
        control.report_error(
            job, phase="cancel", event_key="cancel-unknown", message="取消回执未知"
        )
        assert len(control.snapshot()["errors"]) == 2
        applied = control.mark_applied(accepted["command_uuid"])
        assert applied["status"] == "APPLIED"
        assert applied["station"]["errors"] == []
        assert applied["station"]["mode"] == "PAUSED"
        assert control.mark_applied(accepted["command_uuid"])["status"] == "APPLIED"
        assert len(store.list_jobs(task)) == 1
    finally:
        store.close()


def test_manual_transitions_with_multiple_error_facts_keep_distinct_revisions(tmp_path):
    """同一 Job 存在多个错误事实时，较早事实仍可进入和完成手动处置。"""
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        _, job = seed_job(store)
        control = StationExecutionControl(store)
        first = control.report_error(job, phase="execution", message="执行失败")
        control.report_error(
            job, phase="cancel", event_key="cancel-unknown", message="取消未知"
        )
        entered = control.decide(
            first["decision_id"],
            decision_request(control, first, "enter_manual_handling"),
            idempotency_key="enter",
            operator_id="tester",
        )
        session = entered["station"]["active_session"]
        control.report_error(
            job, phase="other", event_key="additional-fact", message="另一个错误事实"
        )
        completed = control.complete(
            session["session_id"],
            {
                "expected_session_version": session["version"],
                "expected_station_version": control.snapshot()["station_version"],
                "summary": "已核对",
            },
            idempotency_key="complete",
            operator_id="tester",
        )
        assert completed["station"]["active_session"]["stage"] == "DECISION_REQUIRED"
        returned = control.return_to_manual(
            session["session_id"],
            {
                "expected_session_version": completed["station"]["active_session"][
                    "version"
                ],
                "expected_station_version": completed["station"]["station_version"],
            },
            idempotency_key="return",
            operator_id="tester",
        )
        assert returned["station"]["active_session"]["stage"] == "MANUAL_HANDLING"
        assert len(returned["station"]["errors"]) == 3
    finally:
        store.close()
