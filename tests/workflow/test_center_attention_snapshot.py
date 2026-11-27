"""中心快照只投影 Uni-Lab-OS 当前仍需处理的事项。"""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from tests.app.center_client.test_center_client import INSTANCE, STATION, package_response
from unilabos.app.center_client.websocket import validate_attention_snapshot
from unilabos.center_protocol.digest import digest
from unilabos.persistence.center_schema import ensure_center_schema
from unilabos.workflow.center_boundary import bind_center_invocation
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.intervention import WorkflowInterventionStore
from unilabos.workflow.manual_confirmation import ensure_manual_confirmation_schema
from unilabos.workflow.station_workflow_submission import prepare_station_workflow_submission
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000901"
TASK_UUID = "20000000-0000-4000-8000-000000000901"
JOB_UUID = "30000000-0000-4000-8000-000000000901"
NODE_UUID = "40000000-0000-4000-8000-000000000901"


def _bind_center_task(store: WorkflowStore, task_uuid: str) -> None:
    """准备已经持久化的中心调用绑定，事项查询不依赖连接状态或元数据。"""
    package = package_response()
    invocation = str(uuid5(NAMESPACE_URL, f"attention:{task_uuid}"))
    package["payload"]["station_invocation_uuid"] = invocation
    package["payload"]["workflow_uuid"] = store.get_task(task_uuid)["workflow_uuid"]
    package["payload_digest"] = digest(package["payload"])
    task = store.get_task(task_uuid)
    graph = task["workflow_snapshot"]
    metadata = graph.setdefault("workflow", {}).setdefault("meta_data", {}).setdefault("unilab", {})
    metadata.setdefault("input_contract", {"type":"object", "properties":{}})
    metadata.setdefault("output_contract", {"type":"object", "properties":{}})
    payload = package["payload"]
    submission = prepare_station_workflow_submission(
        backend_task_uuid=payload["global_task_uuid"], invocation_key=payload["invocation_key"],
        workflow_name=None, workflow_id=payload["workflow_uuid"],
        revision_fingerprint=payload["revision_fingerprint"], input_value=payload["input"],
        priority="normal", deadline=None, inventory_bindings=None, description=None, meta_data=None,
    )
    with store.transaction() as connection:
        ensure_center_schema(connection, station_uuid=STATION, instance_uuid=INSTANCE)
        connection.execute(
            "INSERT INTO center_invocation_inbox(invocation_uuid,package_json,claim_confirmed,local_task_uuid) VALUES (?,?,1,?)",
            (invocation, json.dumps(package), task_uuid),
        )
        connection.execute(
            "UPDATE workflow_task SET backend_task_uuid=?,invocation_key=?,request_fingerprint=?,revision_fingerprint=? WHERE uuid=?",
            (submission.backend_task_uuid, submission.invocation_key, submission.request_fingerprint,
             submission.revision_fingerprint, task_uuid),
        )
        connection.execute("UPDATE workflow_task SET workflow_snapshot=? WHERE uuid=?",
                           (json.dumps(graph), task_uuid))
        bind_center_invocation(connection, invocation_uuid=invocation, task_uuid=task_uuid)


def _seed(store: WorkflowStore, *, center_created: bool = True) -> None:
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="快照测试",
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
                execution_plan, run_mode, control_status, cleanup_status,
                trace_context, input, output, error_info
            ) VALUES (?, '2026-10-17T00:00:00Z', '2026-10-17T00:00:00Z', NULL,
                      NULL, '{}', ?, 'running', '{}', '{}', 'normal',
                      'waiting_intervention', 'none', '{}', '{}', '{}', '[]')
            """,
            (TASK_UUID, WORKFLOW_UUID),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status,
                attempt, param, feedback_data, return_info, control_data,
                error_info, uncertainty_reason
            ) VALUES (?, '2026-10-17T00:00:00Z', '2026-10-17T00:00:00Z', NULL,
                      NULL, '{}', ?, ?, 0, 0, 'device_action', '{}', 0,
                      'running', 1, '{}', '{}', '{}', '{}', '[]', NULL)
            """,
            (JOB_UUID, TASK_UUID, NODE_UUID),
        )
        ensure_manual_confirmation_schema(connection)
        connection.execute(
            """
            INSERT INTO workflow_manual_confirmation(
                workflow_node_job_uuid, workflow_task_uuid, status,
                opened_at, deadline_at
            ) VALUES (?, ?, 'pending', '2026-10-17T00:00:00Z',
                      '2026-10-17T01:00:00Z')
            """,
            (JOB_UUID, TASK_UUID),
        )
    if center_created:
        _bind_center_task(store, TASK_UUID)


def test_center_attention_snapshot_excludes_local_tasks_without_a_center_binding(tmp_path):
    store = WorkflowStore(tmp_path / "local-attention.db")
    try:
        _seed(store, center_created=False)
        snapshot = store.center_attention_snapshot()
        assert snapshot == {"manual_confirmation_count": 0, "exception_count": 0,
                            "items": {"manual_confirmations": [], "exceptions": []}}
        assert store.get_task(TASK_UUID)["status"] == "running"
    finally:
        store.close()


def test_center_attention_snapshot_filters_local_tasks_in_a_connected_station(tmp_path):
    from tests.workflow.test_workflow_intervention import (
        JOB_UUID as CENTER_JOB,
    )
    from tests.workflow.test_workflow_intervention import (
        TASK_UUID as CENTER_TASK,
    )
    from tests.workflow.test_workflow_intervention import (
        _seed_running_job,
    )
    store = WorkflowStore(tmp_path / "mixed-attention.db")
    try:
        _seed(store, center_created=False)
        _seed_running_job(store)
        _bind_center_task(store, CENTER_TASK)
        with store.transaction() as conn:
            conn.execute("INSERT INTO workflow_manual_confirmation(workflow_node_job_uuid,workflow_task_uuid,status,opened_at,deadline_at) VALUES (?,?,'pending','2026-10-17T00:00:00Z','2026-10-17T01:00:00Z')", (CENTER_JOB, CENTER_TASK))
            conn.execute("UPDATE workflow_task SET meta_data=? WHERE uuid=?", (json.dumps({"center_invocation_uuid": "pretend-center-task"}), TASK_UUID))
        interventions = WorkflowInterventionStore(store)
        for task, job, decision in [(TASK_UUID, JOB_UUID, "local"), (CENTER_TASK, CENTER_JOB, "center")]:
            interventions.open_from_report({"task_id": task, "job_id": job, "decision_id": decision,
                "options": [{"id":"retry"}], "error_message":"设备异常", "exception_type":"CommunicationError"})
        snapshot = store.center_attention_snapshot()
        assert snapshot["manual_confirmation_count"] == 1
        assert snapshot["exception_count"] == 1
        assert [item["workflow_task_uuid"] for item in snapshot["items"]["manual_confirmations"]] == [CENTER_TASK]
        assert [item["workflow_task_uuid"] for item in snapshot["items"]["exceptions"]] == [CENTER_TASK]
        assert len([item for item in interventions.list(status="open", limit=10) if item["workflow_task_uuid"] == TASK_UUID]) == 1
    finally:
        store.close()


def test_center_attention_snapshot_contains_pending_manual_confirmation(tmp_path):
    store = WorkflowStore(tmp_path / "attention.db")
    try:
        _seed(store)

        snapshot = store.center_attention_snapshot()

        assert snapshot["manual_confirmation_count"] == 1
        assert snapshot["exception_count"] == 0
        item = snapshot["items"]["manual_confirmations"][0]
        assert item == {
            "workflow_node_job_uuid": JOB_UUID,
            "workflow_task_uuid": TASK_UUID,
            "status": "pending",
            "opened_at": "2026-10-17T00:00:00Z",
            "deadline_at": "2026-10-17T01:00:00Z",
            "actions": ["approve", "reject"],
        }
        assert "attention_uuid" not in item
        assert "job_uuid" not in item
        assert item["status"] == "pending"
    finally:
        store.close()


def test_center_attention_snapshot_tracks_runtime_addition_and_resolution(tmp_path):
    store = WorkflowStore(tmp_path / "attention-lifecycle.db")
    try:
        _seed(store)

        connected = store.center_attention_snapshot()
        assert connected["manual_confirmation_count"] == 1
        assert connected["exception_count"] == 0

        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_manual_confirmation SET status='approved' WHERE workflow_node_job_uuid=?",
                (JOB_UUID,),
            )
        intervention = WorkflowInterventionStore(store).open_from_report({
            "task_id": TASK_UUID,
            "job_id": JOB_UUID,
            "decision_id": "70000000-0000-4000-8000-000000000902",
            "options": [{"id": "retry", "label": "重试"}],
            "error_message": "运行中新增异常",
            "exception_type": "CommunicationError",
        })
        running = store.center_attention_snapshot()
        assert running["manual_confirmation_count"] == 0
        assert running["exception_count"] == 1
        assert running["items"]["exceptions"][0]["uuid"] == intervention["uuid"]

        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_intervention SET status='selected' WHERE uuid=?",
                (intervention["uuid"],),
            )
        completed = store.center_attention_snapshot()
        assert completed["manual_confirmation_count"] == 0
        assert completed["exception_count"] == 0
        assert completed["items"] == {"manual_confirmations": [], "exceptions": []}
    finally:
        store.close()


def test_center_attention_snapshot_contains_open_intervention_without_uncertain_job(tmp_path):
    store = WorkflowStore(tmp_path / "attention-exception.db")
    try:
        _seed(store)
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_manual_confirmation SET status='approved' WHERE workflow_node_job_uuid=?",
                (JOB_UUID,),
            )
            connection.execute(
                "UPDATE workflow_node_job SET uncertainty_reason=?, status='running' WHERE uuid=?",
                ("设备返回结果未知", JOB_UUID),
            )
        intervention = WorkflowInterventionStore(store).open_from_report({
            "task_id": TASK_UUID,
            "job_id": JOB_UUID,
            "decision_id": "70000000-0000-4000-8000-000000000901",
            "options": [{"id": "retry", "label": "重试"}],
            "error_message": "设备异常",
            "exception_type": "CommunicationError",
        })

        snapshot = store.center_attention_snapshot()

        assert snapshot["manual_confirmation_count"] == 0
        assert snapshot["exception_count"] == 1
        item = snapshot["items"]["exceptions"][0]
        assert item["decision_kind"] == "device_intervention"
        assert item["uuid"] == intervention["uuid"]
        assert item["workflow_task_uuid"] == TASK_UUID
        assert item["workflow_node_job_uuid"] == JOB_UUID
        assert item["status"] == "open"
        assert item["options"] == [{"id": "retry", "label": "重试"}]
        assert item["meta_data"]["error_message"] == "设备异常"
        assert "attention_uuid" not in item
        assert "subtype" not in item
        assert "reason" not in item
    finally:
        store.close()


def test_center_attention_snapshot_does_not_count_uncertain_job_as_exception(tmp_path):
    store = WorkflowStore(tmp_path / "attention-uncertain.db")
    try:
        _seed(store)
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_manual_confirmation SET status='approved' WHERE workflow_node_job_uuid=?",
                (JOB_UUID,),
            )
            connection.execute(
                "UPDATE workflow_node_job SET uncertainty_reason=?, status='running' WHERE uuid=?",
                ("设备返回结果未知", JOB_UUID),
            )

        snapshot = store.center_attention_snapshot()

        assert snapshot["manual_confirmation_count"] == 0
        assert snapshot["exception_count"] == 0
        assert snapshot["items"]["exceptions"] == []
    finally:
        store.close()


def test_center_attention_snapshot_contains_edge_node_error_native_fields(tmp_path):
    store = WorkflowStore(tmp_path / "attention-edge-error.db")
    try:
        _seed(store)
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_manual_confirmation SET status='approved' WHERE workflow_node_job_uuid=?",
                (JOB_UUID,),
            )
        decision = StationExecutionControl(store).report_error(
            JOB_UUID,
            phase="execution",
            message="设备断开",
        )

        snapshot = store.center_attention_snapshot()

        assert snapshot["exception_count"] == 1
        validate_attention_snapshot(snapshot)
        item = snapshot["items"]["exceptions"][0]
        assert item["decision_kind"] == "edge_node_error"
        assert item["decision_id"] == decision["decision_id"]
        assert item["workflow_task_uuid"] == TASK_UUID
        assert item["workflow_node_job_uuid"] == JOB_UUID
        assert item["meta_data"]["message"] == "设备断开"
        assert item["error_phase"] == "execution"
        assert item["observed_error_epoch"] >= 1
        assert "attention_uuid" not in item
        assert "subtype" not in item
    finally:
        store.close()


def test_center_attention_snapshot_excludes_terminal_jobs(tmp_path):
    store = WorkflowStore(tmp_path / "attention-terminal.db")
    try:
        _seed(store)
        with store.transaction() as connection:
            connection.execute(
                "UPDATE workflow_node_job SET status='failed', uncertainty_reason=NULL, error_info=? WHERE uuid=?",
                (json.dumps([{"message": "已失败"}]), JOB_UUID),
            )

        snapshot = store.center_attention_snapshot()

        assert snapshot["manual_confirmation_count"] == 1
        assert snapshot["exception_count"] == 0
        assert snapshot["items"]["exceptions"] == []
    finally:
        store.close()


def test_center_attention_snapshot_uses_native_edge_error_lifecycle(tmp_path):
    from tests.workflow.test_error_control import decision_request, seed_job
    store = WorkflowStore(tmp_path / "edge-lifecycle.db")
    try:
        task, job = seed_job(store)
        _bind_center_task(store, task)
        control = StationExecutionControl(store)
        error = control.report_error(job, phase="execution", message="设备离线")
        accepted = control.decide(error["decision_id"],
            decision_request(control, error, "cancel_task"),
            idempotency_key="cancel", operator_id="tester")
        snapshot = store.center_attention_snapshot()
        assert snapshot["exception_count"] == 1
        assert snapshot["items"]["exceptions"][0]["stage"] == "APPLYING"
        assert snapshot["items"]["exceptions"][0]["status"] == "selected"
        validate_attention_snapshot(snapshot)
        control.mark_applied(accepted["command_uuid"])
        assert control.snapshot()["errors"] == []
        assert store.center_attention_snapshot()["exception_count"] == 0
    finally:
        store.close()


def test_center_attention_snapshot_excludes_proved_resolved_open_error(tmp_path):
    from tests.workflow.test_error_control import seed_job
    store = WorkflowStore(tmp_path / "proved-success.db")
    try:
        task, job = seed_job(store)
        _bind_center_task(store, task)
        control = StationExecutionControl(store)
        control.report_error(job, phase="execution", message=f"执行结果未知：workflow-node-job:{job}")
        assert store.center_attention_snapshot()["exception_count"] == 1
        assert control.accept_proved_success(job)
        assert control.snapshot()["errors"] == []
        assert store.center_attention_snapshot()["exception_count"] == 0
    finally:
        store.close()
