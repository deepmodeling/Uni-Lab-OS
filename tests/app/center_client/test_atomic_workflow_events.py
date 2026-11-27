"""真实工作流与中心边界事件共用一次提交；故障后不能留下半个事实。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INSTANCE, INVOCATION, STATION, package_response
from tests.app.center_client.test_runtime import Center, runtime
from tests.workflow.test_c1_published_workflow_contract import _applied_snapshot
from unilabos.app.center_client.client import TaskPackage
from unilabos.app.center_client.digest import digest
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.runtime import submission
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.persistence import center_events
from unilabos.persistence.center_schema import TABLES as CENTER_TABLES
from unilabos.workflow.published_contract import PublishedWorkflowContractStore
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


class RecordingScheduler:
    """仅替代物理调度端口；任务与 Job 始终由正式服务和存储创建。"""

    def __init__(self, store: WorkflowStore) -> None:
        self.store = store
        self.submissions: list[str] = []

    def submit(self, task: dict[str, Any]) -> dict[str, Any]:
        self.submissions.append(task["uuid"])
        return {"task": self.store.get_task(task["uuid"]), "jobs": self.store.list_jobs(task["uuid"])}

    def close(self) -> None:
        pass


def _publish(store: WorkflowStore, *, pure: bool = False, jobs: int = 1, value: float = 7.5, outputs: bool = True) -> dict[str, Any]:
    graph = _applied_snapshot()
    graph["workflow"]["revision"] = 1
    if pure:
        graph["workflow"]["meta_data"]["unilab"]["output_bindings"] = {
            "result": {"kind": "workflow_input", "parameter": "value"},
        }
        graph["nodes"][0].update(type="group", meta_data={})
        graph["node_templates"][0].update(type="group", node_type="group", name="group")
        graph["handle_templates"] = []
    else:
        schema = {
            "type": "object", "required": ["goal"],
            "properties": {
                "goal": {"type": "object", "properties": {"value": {"type": "number"}}, "required": ["value"]},
                "result": {"type": "object", "properties": {"result": {"type": "number"}}, "required": ["result"]},
            },
        }
        graph["node_templates"][0].update(
            schema=schema["properties"]["goal"],
            meta_data={"unilab": {"contract_kind": "typed", "action_contract_schema": schema}},
        )
        node = graph["nodes"][0]
        node.update(action_name="measure", action_type="UniLabJsonCommand")
        node["meta_data"]["unilab"]["executor_binding"] = {"mode": "fixed", "device_id": "atomic-meter-1"}
        for index in range(1, jobs):
            extra = deepcopy(node)
            extra["uuid"] = str(uuid4())
            extra["meta_data"]["unilab"]["executor_binding"]["device_id"] = f"atomic-meter-{index + 1}"
            graph["nodes"].append(extra)
        # 输出依赖最后一个设备；先完成其他 Job 不应产生整体 outcome。
        graph["workflow"]["meta_data"]["unilab"]["output_bindings"]["result"]["workflow_node_uuid"] = graph["nodes"][-1]["uuid"]
    if not outputs:
        graph["workflow"]["meta_data"]["unilab"]["output_contract"]["outputs"] = []
        graph["workflow"]["meta_data"]["unilab"]["output_bindings"] = {}
    store.create_workflow(workflow_uuid=graph["workflow"]["uuid"], name="Atomic center workflow", tags=[],
                          description=None, meta_data={})
    published = PublishedWorkflowContractStore(store).publish(graph=graph, expected_revision=1)
    response = package_response()
    response["payload"].update(workflow_uuid=graph["workflow"]["uuid"], revision_fingerprint=published["source_hash"],
                               input={"value": value})
    response["payload_digest"] = digest(response["payload"])
    return response


@contextmanager
def _workflow(tmp_path: Path, *, pure: bool = False, jobs: int = 1, schedule: bool = False, value: float = 7.5, claim: bool = True, outputs: bool = True) -> Iterator[Any]:
    path = tmp_path / "workflow_history.db"
    store = WorkflowStore(path)
    response = _publish(store, pure=pure, jobs=jobs, value=value, outputs=outputs)
    scheduler = RecordingScheduler(store) if schedule else None
    service = WorkflowService(store, task_scheduler_bridge=scheduler)
    durable = DurableCenterStore(path, station_uuid=STATION, instance_uuid=INSTANCE)
    package = TaskPackage.from_response(response)
    durable.remember_package(package)
    if claim:
        durable.confirm_claim(INVOCATION)
    try:
        yield store, service, durable, package, scheduler
    finally:
        durable.close()
        service.close()


def _submit(service: WorkflowService, package: TaskPackage) -> dict[str, Any]:
    return service.submit_station_workflow(**submission(package), center_invocation_uuid=INVOCATION)


def _events(store: WorkflowStore) -> list[dict[str, Any]]:
    return [json.loads(row[0]) for row in store.db.execute(
        "SELECT event_json FROM center_boundary_outbox ORDER BY event_sequence"
    ).fetchall()]


def _snapshot(store: WorkflowStore) -> dict[str, list[str]]:
    tables = sorted(name for name in store.db.table_names()
                    if name.startswith(("workflow_", "center_")) or name == "frontend_event")
    return {name: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                         for row in store.db.execute(f'SELECT * FROM "{name}"').fetchall()) for name in tables}


def _count(connection: Any, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _fail_after_write(monkeypatch: Any, *, terminal: bool) -> list[dict[str, Any]]:
    original = center_events.append_event_in_transaction
    observed: list[dict[str, Any]] = []

    def append_then_fail(connection: Any, invocation_uuid: str, *, terminal: bool, body: dict[str, Any]) -> Any:
        event = original(connection, invocation_uuid, terminal=terminal, body=body)
        if terminal == fail_terminal:
            observed.append({
                "event": event,
                "tasks": _count(connection, "workflow_task"),
                "jobs": _count(connection, "workflow_node_job"),
                "results": _count(connection, "workflow_result"),
                "job_results": (_count(connection, "workflow_node_job_result")
                                if "workflow_node_job_result" in connection.table_names() else 0),
                "sequence": connection.execute("SELECT last_event_sequence FROM center_runtime_state").fetchone()[0],
            })
            raise RuntimeError("已写入中心信封后的模拟故障")
        return event

    fail_terminal = terminal
    monkeypatch.setattr(center_events, "append_event_in_transaction", append_then_fail)
    return observed


def _start(store: WorkflowStore, task: dict[str, Any], *, index: int = 0) -> tuple[TaskRuntimeProjection, str]:
    jobs = [job for job in store.list_jobs(task["uuid"]) if job["executor_kind"] == "device_action"]
    projection = TaskRuntimeProjection(store)
    projection.project_pre_dispatch(task_uuid=task["uuid"], job_uuid=jobs[index]["uuid"])
    projection.project_dispatch_accepted(jobs[index]["uuid"])
    return projection, jobs[index]["uuid"]


@pytest.mark.parametrize("pure,terminal", [(False, False), (True, False), (True, True)])
def test_submission_outbox_failure_rolls_back_task_jobs_binding_and_sequence(tmp_path, monkeypatch, pure, terminal):
    with _workflow(tmp_path, pure=pure) as (store, service, durable, package, _):
        before = _snapshot(store)
        observed = _fail_after_write(monkeypatch, terminal=terminal)
        with pytest.raises(RuntimeError, match="已写入中心信封"):
            _submit(service, package)
        assert observed and observed[0]["tasks"] == 1
        assert observed[0]["sequence"] == (2 if terminal else 1)
        if terminal:
            assert observed[0]["results"] == 1 and observed[0]["jobs"] > 0
        assert _snapshot(store) == before
        assert durable.events() == []
        assert service.recover_center_workflow_invocation(INVOCATION) is None


def test_pure_boundary_commits_accepted_result_and_outcome_together(tmp_path):
    with _workflow(tmp_path, pure=True) as (store, service, durable, package, _):
        task = _submit(service, package)
        assert task["status"] == "succeeded"
        assert service.get_station_workflow_result(task["uuid"]) == {"result": 7.5}
        events = _events(store)
        assert [event["event_type"] for event in events] == ["accepted", "outcome"]
        assert [event["event_sequence"] for event in events] == [1, 2]
        assert {event["local_task_uuid"] for event in events} == {task["uuid"]}
        assert events[-1]["payload"]["output"] == {"result": 7.5}
        assert events[-1]["payload"]["status"] == "succeeded"
        inbox = store.db.execute("SELECT * FROM center_invocation_inbox").fetchone()
        assert inbox["acceptance_version"] == 1 and inbox["terminal_recorded"] == 1
        assert inbox["local_task_uuid"] == task["uuid"]
        assert _submit(service, package)["uuid"] == task["uuid"]
        assert service.recover_center_workflow_invocation(INVOCATION)["uuid"] == task["uuid"]
        assert _events(store) == events
        assert len(durable.events()) == 2


def test_job_completion_outbox_failure_rolls_back_results_and_all_logs(tmp_path, monkeypatch):
    with _workflow(tmp_path) as (store, service, _, package, _):
        task = _submit(service, package)
        projection, job_uuid = _start(store, task)
        before = _snapshot(store)
        with monkeypatch.context() as fault:
            observed = _fail_after_write(fault, terminal=True)
            with pytest.raises(RuntimeError, match="已写入中心信封"):
                projection.project_job_finished(job_uuid=job_uuid, scheduler_state="success", return_info={"result": 21.5})
        assert observed and observed[0]["results"] == 1 and observed[0]["job_results"] == 1
        assert observed[0]["sequence"] == 2
        assert _snapshot(store) == before
        assert store.get_task(task["uuid"])["status"] == "running"
        assert service.get_station_workflow_result(task["uuid"]) is None
        projection.project_job_finished(job_uuid=job_uuid, scheduler_state="success", return_info={"result": 21.5})
        outcome = _events(store)[-1]
        assert outcome["payload"]["output"] == {"result": 21.5}
        assert store.get_task(task["uuid"])["status"] == "succeeded"


@pytest.mark.parametrize("terminal_path", ["failed", "timeout", "direct_cancel", "device_canceled", "restart_failed"])
def test_terminal_paths_persist_one_outcome_from_actual_task(tmp_path, terminal_path):
    with _workflow(tmp_path) as (store, service, _, package, _):
        task = _submit(service, package)
        projection = TaskRuntimeProjection(store)
        if terminal_path == "direct_cancel":
            finish = lambda: projection.project_canceled(task["uuid"])
        else:
            projection, job_uuid = _start(store, task)
            if terminal_path == "restart_failed":
                finish = lambda: projection.project_execution_process_restarted(task["uuid"])
            else:
                scheduler_state = "canceled" if terminal_path == "device_canceled" else terminal_path
                finish = lambda: projection.project_job_finished(
                    job_uuid=job_uuid, scheduler_state=scheduler_state,
                    return_info={"device_evidence": "stopped"}, error_info=[{"code": "actual_device_failure"}],
                )
        finish()
        persisted = store.get_task(task["uuid"])
        events = _events(store)
        assert len(events) == 2
        assert events[-1]["event_type"] == "outcome"
        expected = "canceled" if terminal_path in {"direct_cancel", "device_canceled"} else "failed"
        assert events[-1]["payload"]["status"] == expected
        assert events[-1]["payload"]["output"] == {
            "status": persisted["status"], "error_info": persisted["error_info"],
            **({"failure_code": "timeout"} if terminal_path == "timeout" else {}),
        }
        finish()
        service.recover_center_workflow_invocation(INVOCATION)
        assert _events(store) == events


def test_cancel_awaiting_device_does_not_invent_terminal_outcome(tmp_path):
    with _workflow(tmp_path) as (store, service, _, package, _):
        task = _submit(service, package)
        projection, job_uuid = _start(store, task)
        projection.project_cancel_requested(task["uuid"], command_uuid=str(uuid4()),
                                            ack_deadline_at="2099-01-01T00:00:00Z",
                                            complete_deadline_at="2099-01-01T00:01:00Z")
        projection.project_cancel_accepted(job_uuid)
        assert store.get_task(task["uuid"])["status"] == "canceling"
        service.recover_center_workflow_invocation(INVOCATION)
        assert [event["event_type"] for event in _events(store)] == ["accepted"]


def test_nonfinal_job_does_not_emit_outcome(tmp_path):
    with _workflow(tmp_path, jobs=2) as (store, service, _, package, _):
        task = _submit(service, package)
        projection, first_job = _start(store, task)
        projection.project_job_finished(job_uuid=first_job, scheduler_state="success", return_info={"result": 10.0})
        assert store.get_task(task["uuid"])["status"] == "running"
        assert [event["event_type"] for event in _events(store)] == ["accepted"]
        projection, last_job = _start(store, task, index=1)
        projection.project_job_finished(job_uuid=last_job, scheduler_state="success", return_info={"result": 12.0})
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert len(_events(store)) == 2


def _network_restart(tmp_path: Path, *, first_value: float = 7.5, replay_value: float | None = None) -> None:
    path = tmp_path / "workflow_history.db"
    store = WorkflowStore(path)
    response = _publish(store, value=first_value)
    first_scheduler = RecordingScheduler(store)
    service = WorkflowService(store, task_scheduler_bridge=first_scheduler)
    center = Center()
    center.package = response
    center.add()
    center.report_lost = True
    first = runtime(tmp_path, center, service)
    try:
        with pytest.raises(CenterTransportError):
            first.poll_once()
        assert len(first_scheduler.submissions) == 1
        task_uuid = first_scheduler.submissions[0]
        event = deepcopy(center.reports[-1])
        assert event["local_task_uuid"] == task_uuid
    finally:
        first.close()
        service.close()
    store = WorkflowStore(path)
    second_scheduler = RecordingScheduler(store)
    service = WorkflowService(store, task_scheduler_bridge=second_scheduler)
    if replay_value is not None:
        center.package["payload"]["input"] = {"value": replay_value}
        assert digest(center.package["payload"]) == center.package["payload_digest"]
    center.add()
    second = runtime(tmp_path, center, service)
    try:
        second.poll_once()
        assert center.reports[-1] == event
        assert second_scheduler.submissions == []
        assert center.claims == 1 and _count(store.db, "workflow_task") == 1
        assert type(store.get_task(task_uuid)["input"]["value"]) is type(first_value)
        assert service.recover_center_workflow_invocation(INVOCATION)["uuid"] == task_uuid
        assert second_scheduler.submissions == []
    finally:
        second.close()
        service.close()


def test_center_store_in_another_database_cannot_create_workflow_task(tmp_path):
    # 中心收件箱留在独立内存库，真实工作流分别走 SQLite/PostgreSQL 默认域。
    store = WorkflowStore(tmp_path / "workflow_history.db")
    response = _publish(store)
    service = WorkflowService(store)
    from unilabos.config.config import BasicConfig
    with pytest.MonkeyPatch.context() as configuration:
        configuration.setattr(BasicConfig, "database_type", "sqlite")
        configuration.setattr(BasicConfig, "database_url", "")
        durable = DurableCenterStore(":memory:", station_uuid=STATION, instance_uuid=INSTANCE)
    package = TaskPackage.from_response(response)
    durable.remember_package(package)
    durable.confirm_claim(INVOCATION)
    try:
        before = _snapshot(store)
        with pytest.raises(WorkflowConflict):
            _submit(service, package)
        assert _snapshot(store) == before
        assert durable.events() == []
    finally:
        durable.close()
        service.close()


def test_soft_deleted_task_keeps_permanent_center_identity(tmp_path):
    with _workflow(tmp_path, pure=True) as (store, service, _, package, _):
        task = _submit(service, package)
        events = _events(store)
        store.delete_task(task["uuid"])
        assert service.recover_center_workflow_invocation(INVOCATION)["uuid"] == task["uuid"]
        assert _submit(service, package)["uuid"] == task["uuid"]
        assert _count(store.db, "workflow_task") == 1
        assert _events(store) == events


@pytest.mark.parametrize("pure", [False, True])
def test_local_workflow_works_without_center_schema(tmp_path, pure):
    store = WorkflowStore(tmp_path / "local.db")
    # PostgreSQL 参数化夹具会保留空表；此用例只移除专属临时测试库的中心表。
    with store.transaction() as connection:
        for table in reversed(CENTER_TABLES):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
    response = _publish(store, pure=pure)
    service = WorkflowService(store)
    try:
        task = service.submit_station_workflow(**submission(TaskPackage.from_response(response)))
        if not pure:
            projection, job_uuid = _start(store, task)
            projection.project_job_finished(job_uuid=job_uuid, scheduler_state="success", return_info={"result": 7.5})
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert service.get_station_workflow_result(task["uuid"]) == {"result": 7.5}
        assert not any(name.startswith("center_") for name in store.db.table_names())
    finally:
        service.close()


@pytest.mark.parametrize("value", [1.0, 1e20])
def test_binary64_input_survives_binding_and_recovery(tmp_path, value):
    with _workflow(tmp_path, value=value) as (store, service, durable, package, _):
        task = _submit(service, package)
        persisted_package = durable.packages()[0]
        assert type(persisted_package.input["value"]) is float
        assert persisted_package.input["value"] == value
        assert type(task["input"]["value"]) is float
        first_event = _events(store)[0]
        assert service.recover_center_workflow_invocation(INVOCATION)["uuid"] == task["uuid"]
        assert _submit(service, persisted_package)["uuid"] == task["uuid"]
        assert _events(store) == [first_event]


def test_network_loss_and_restart_never_resubmit_committed_task(tmp_path):
    _network_restart(tmp_path)


@pytest.mark.parametrize("first_value,replay_value", [(1.0, 1), (1, 1.0)])
def test_equivalent_json_notification_keeps_first_frozen_input(tmp_path, first_value, replay_value):
    _network_restart(tmp_path, first_value=first_value, replay_value=replay_value)


@pytest.mark.parametrize("pure", [False, True])
def test_recover_legacy_task_events_is_atomic_and_never_schedules(tmp_path, monkeypatch, pure):
    with _workflow(tmp_path, pure=pure, schedule=True) as (store, service, _, package, scheduler):
        task = service.submit_station_workflow(**submission(package))
        assert _events(store) == []
        assert scheduler.submissions == ([] if pure else [task["uuid"]])
        before = _snapshot(store)
        with monkeypatch.context() as fault:
            observed = _fail_after_write(fault, terminal=pure)
            with pytest.raises(RuntimeError, match="已写入中心信封"):
                service.recover_center_workflow_invocation(INVOCATION)
        assert observed and _snapshot(store) == before
        recovered = service.recover_center_workflow_invocation(INVOCATION)
        assert recovered["uuid"] == task["uuid"]
        assert scheduler.submissions == ([] if pure else [task["uuid"]])
        after = _snapshot(store)
        assert {key: value for key, value in after.items() if not key.startswith("center_")} == {
            key: value for key, value in before.items() if not key.startswith("center_")
        }
        events = _events(store)
        assert [event["event_type"] for event in events] == (["accepted", "outcome"] if pure else ["accepted"])
        assert service.recover_center_workflow_invocation(INVOCATION)["uuid"] == task["uuid"]
        assert _events(store) == events


def test_unconfirmed_claim_cannot_bind_or_create_task(tmp_path):
    with _workflow(tmp_path, claim=False) as (store, service, _, package, _):
        before = _snapshot(store)
        with pytest.raises(WorkflowConflict):
            _submit(service, package)
        assert _snapshot(store) == before


def test_conflicting_permanent_request_cannot_be_adopted(tmp_path):
    with _workflow(tmp_path) as (store, service, _, package, _):
        wrong = {**submission(package), "input_value": {"value": 8.0}}
        task = service.submit_station_workflow(**wrong)
        before = _snapshot(store)
        with pytest.raises(WorkflowConflict):
            service.recover_center_workflow_invocation(INVOCATION)
        with pytest.raises(WorkflowConflict):
            _submit(service, package)
        assert _snapshot(store) == before
        assert _count(store.db, "workflow_task") == 1
        assert store.get_task(task["uuid"])["input"] == {"value": 8.0}


def test_recovery_cannot_rewrite_already_recorded_outcome(tmp_path):
    with _workflow(tmp_path, pure=True) as (store, service, _, package, _):
        task = _submit(service, package)
        events = _events(store)
        # 模拟外部写入造成的结果漂移；恢复必须保留原证据并明确失败。
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_result SET result=? WHERE workflow_task_uuid=?",
                               (json.dumps({"result": 99.0}), task["uuid"]))
        before = _snapshot(store)
        with pytest.raises(WorkflowConflict):
            service.recover_center_workflow_invocation(INVOCATION)
        assert _snapshot(store) == before
        assert _events(store) == events


@pytest.mark.parametrize("pure", [False, True])
def test_empty_output_contract_has_real_atomic_boundary(tmp_path, pure):
    with _workflow(tmp_path, pure=pure, jobs=2, outputs=False) as (store, service, _, package, _):
        task = _submit(service, package)
        if not pure:
            projection, first = _start(store, task)
            projection.project_job_finished(job_uuid=first, scheduler_state="success", return_info={"result": 10.0})
            assert store.get_task(task["uuid"])["status"] == "running"
            assert service.get_station_workflow_result(task["uuid"]) is None
            assert [event["event_type"] for event in _events(store)] == ["accepted"]
            projection, last = _start(store, task, index=1)
            projection.project_job_finished(job_uuid=last, scheduler_state="success", return_info={"result": 12.0})
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert service.get_station_workflow_result(task["uuid"]) == {}
        events = _events(store)
        assert [event["event_type"] for event in events] == ["accepted", "outcome"]
        assert events[-1]["payload"]["status"] == "succeeded"
        assert events[-1]["payload"]["output"] == {}
        assert _count(store.db, "workflow_result") == 1


@pytest.mark.parametrize("pure", [False, True])
def test_empty_output_contract_rolls_back_when_outcome_write_fails(tmp_path, monkeypatch, pure):
    with _workflow(tmp_path, pure=pure, jobs=2, outputs=False) as (store, service, _, package, _):
        if pure:
            complete = lambda: _submit(service, package)
        else:
            task = _submit(service, package)
            projection, first = _start(store, task)
            projection.project_job_finished(job_uuid=first, scheduler_state="success", return_info={"result": 10.0})
            projection, last = _start(store, task, index=1)
            complete = lambda: projection.project_job_finished(
                job_uuid=last, scheduler_state="success", return_info={"result": 12.0},
            )
        before = _snapshot(store)
        with monkeypatch.context() as fault:
            observed = _fail_after_write(fault, terminal=True)
            with pytest.raises(RuntimeError, match="已写入中心信封"):
                complete()
        assert observed and observed[0]["results"] == 1
        assert _snapshot(store) == before
        complete()
        assert _events(store)[-1]["payload"]["output"] == {}
        assert _count(store.db, "workflow_result") == 1


def test_composition_upgrades_accepted_legacy_task_before_recovery_with_center_disabled(tmp_path, monkeypatch):
    from unilabos.app.scheduler.dispatch import RecordingDispatcher
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow import composition

    composition.shutdown_workflow_runtime()
    with _workflow(tmp_path) as (store, service, _, package, _):
        task = _submit(service, package)
        _start(store, task)
        original_accepted = _events(store)[0]
        original_jobs = {job["uuid"] for job in store.list_jobs(task["uuid"])}
        with store.transaction() as connection:
            connection.execute("DROP INDEX ux_center_invocation_local_task")
            connection.execute("ALTER TABLE center_invocation_inbox DROP COLUMN acceptance_version")
    monkeypatch.setenv("CENTER_ENABLED", "0")
    dispatcher = RecordingDispatcher()
    # 只替代物理执行端；启动、调度桥、恢复和所有持久投影均走正式实现。
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    try:
        resumed = composition.compose_workflow_runtime(tmp_path, scheduler=scheduler, start_source_monitor=False)
        scheduler.on_execution_process_restarted(())
        persisted = resumed.runtime_store.get_task(task["uuid"])
        assert persisted["status"] == "failed"
        assert any(error.get("code") == "execution_process_restarted" for error in persisted["error_info"])
        events = _events(resumed.runtime_store)
        assert len(events) == 2 and events[0] == original_accepted
        assert events[1]["event_type"] == "outcome"
        assert events[1]["event_sequence"] == 2
        assert events[1]["event_epoch_uuid"] == original_accepted["event_epoch_uuid"]
        assert events[1]["payload"]["status"] == "failed"
        assert events[1]["payload"]["output"]["error_info"] == persisted["error_info"]
        assert {job["uuid"] for job in resumed.runtime_store.list_jobs(task["uuid"])} == original_jobs
        assert _count(resumed.runtime_store.db, "workflow_task") == 1
        assert dispatcher.dispatched == []
    finally:
        composition.shutdown_workflow_runtime()
        scheduler.close()


@pytest.mark.parametrize("value", [2**53, -(2**53), 2**63 - 1])
def test_unexportable_numeric_output_preserves_completion_and_holds_original_result(tmp_path, value):
    """输出超出中心协议时保留完成事实和精确原值，不重建事件或物理任务。"""
    path = tmp_path / "workflow_history.db"
    with _workflow(tmp_path, schedule=True) as (store, service, _, package, scheduler):
        task = _submit(service, package)
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": value})
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        assert store.get_job(job)["status"] == "succeeded"
        assert store.get_station_workflow_result(task["uuid"]) == {"result": value}
        pending = store.get_center_outcome_evidence_status(INVOCATION)
        assert pending["pending_reason"] == "output_not_exportable"
        assert pending["local_task_uuid"] == task["uuid"]
        assert pending["evidence_json"] is None
        events = _events(store)
        assert [event["event_type"] for event in events] == ["accepted"]
        jobs = store.list_jobs(task["uuid"])
        submitted = list(scheduler.submissions)
        for _ in range(2):
            service.recover_center_workflow_invocation(INVOCATION)
        assert store.list_jobs(task["uuid"]) == jobs
        assert scheduler.submissions == submitted
        assert _events(store) == events
    reopened = WorkflowStore(path)
    try:
        assert reopened.get_task(task["uuid"])["status"] == "succeeded"
        assert reopened.get_station_workflow_result(task["uuid"]) == {"result": value}
        assert reopened.get_center_outcome_evidence_status(INVOCATION)["pending_reason"] == "output_not_exportable"
    finally:
        reopened.close()


@pytest.mark.parametrize("value", [2**53 - 1, -(2**53 - 1), 1e20, -1e20])
def test_exportable_numeric_output_keeps_exact_value_and_terminal_event(tmp_path, value):
    """安全整数边界与合法 binary64 输出继续发出真实终态，不收窄浮点数范围。"""
    with _workflow(tmp_path) as (store, service, _, package, _):
        task = _submit(service, package)
        projection, job = _start(store, task)
        projection.project_job_finished(job_uuid=job, scheduler_state="success", return_info={"result": value})
        assert store.get_task(task["uuid"])["status"] == "succeeded"
        outcome = _events(store)[-1]
        assert outcome["event_type"] == "outcome"
        assert outcome["payload"]["output"] == {"result": value}
        assert type(outcome["payload"]["output"]["result"]) is type(value)
        assert store.get_center_outcome_evidence_status(INVOCATION) is None
