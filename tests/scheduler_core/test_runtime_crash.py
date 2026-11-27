"""Runtime 崩溃统一失败，不恢复旧任务或生成对账工作。"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.scheduler_core.conftest import CoreRuntime, build_core_runtime, stable_uuid
from tests.workflow.test_error_control import decision_request
from unilabos.app.scheduler.dispatch import CommittedJobOutcome
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


@pytest.fixture(params=[False, True], ids=["stable-order", "serial-queue"])
def core_runtime(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[CoreRuntime]:
    """重启合同同时覆盖并行执行和生产默认的串行队列。"""
    runtime = build_core_runtime(tmp_path, serial_queue=request.param)
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.parametrize("serial_queue", [False, True])
@pytest.mark.parametrize("cold_start", [False, True])
def test_restart_fails_pending_tasks_before_dispatch(
    tmp_path: Path, serial_queue: bool, cold_start: bool
) -> None:
    """排队任务在执行进程重启后必须失败，不能留在等待或重新派发。"""
    runtime = build_core_runtime(tmp_path, serial_queue=serial_queue)
    try:
        task = runtime.persist(task_name="pending-restart", devices=["reactor-a"])
        if cold_start:
            runtime.bridge.recover_active_tasks(execution_process_restarted=True)
        else:
            runtime.scheduler.on_execution_process_restarted(())
        assert runtime.workflow_store.get_task(task["uuid"])["status"] == "failed"
        assert runtime.workflow_store.list_jobs(task["uuid"])[0]["status"] == "canceled"
        assert runtime.dispatcher.dispatched == []
    finally:
        runtime.close()


@pytest.mark.parametrize("serial_queue", [False, True])
@pytest.mark.parametrize("control", ["running", "paused", "canceling"])
def test_restart_clears_inflight_tasks_and_releases_station(
    tmp_path: Path, serial_queue: bool, control: str
) -> None:
    """重启须终结在途与排队任务、清除旧锁，并允许工站接受新任务。"""
    runtime = build_core_runtime(tmp_path, serial_queue=serial_queue)
    try:
        aggregate = runtime.submit(
            task_name="inflight-restart",
            devices=["reactor-a", "reactor-b", "reactor-a"],
            edges=[(0, 1), (1, 2)],
        )
        task_uuid = aggregate["task"]["uuid"]
        completed_job, active_job, pending_job = (
            stable_uuid(f"job:inflight-restart:{index}") for index in range(3)
        )
        runtime.clock.advance(1)
        runtime.scheduler.on_job_finished(completed_job, True, {"completed": True})
        completed = runtime.workflow_store.get_job(completed_job)
        assert runtime.workflow_store.get_job(active_job)["status"] == "running"
        if control == "paused":
            runtime.bridge.pause(task_uuid)
        elif control == "canceling":
            runtime.bridge.cancel(task_uuid, command_uuid=stable_uuid("restart-cancel"))
            assert runtime.workflow_store.get_job(active_job)["status"] == "cancel_requested"
        queued = runtime.persist(task_name="queued-restart", devices=["reactor-b"])
        sent = list(runtime.dispatcher.dispatched)

        # 事件只携带一个在途 Job，仍须终结工站全部旧任务。
        runtime.scheduler.on_execution_process_restarted((active_job,))

        for old_task_uuid in (task_uuid, queued["uuid"]):
            task = runtime.workflow_store.get_task(old_task_uuid)
            assert task["status"] == "failed"
            assert task["cleanup_status"] == "settled"
            assert not task.get("attention_reason")
            assert runtime.inventory_store.query_all(
                "SELECT * FROM station_execution_claim WHERE task_uuid=? AND state!='released'",
                (old_task_uuid,),
            ) == []
        assert runtime.workflow_store.get_job(completed_job)["return_info"] == completed["return_info"]
        assert runtime.workflow_store.get_job(completed_job)["status"] == "succeeded"
        assert runtime.workflow_store.get_job(active_job)["status"] == "failed"
        assert runtime.workflow_store.get_job(pending_job)["status"] == "canceled"
        assert runtime.scheduler.snapshot()["inflight_jobs"] == {}
        assert runtime.bridge.active_or_uncertain_job_ids() == set()
        assert runtime.bridge.error_control.snapshot()["errors"] == []
        failed = runtime.workflow_store.get_job(active_job)
        runtime.scheduler.on_job_outcome(
            active_job, CommittedJobOutcome("succeeded", {"late": True}, [], [])
        )
        runtime.scheduler.on_execution_process_restarted(())
        runtime.bridge.reschedule()
        assert runtime.workflow_store.get_job(active_job) == failed
        assert runtime.dispatcher.dispatched == sent
        new_task = runtime.submit(
            task_name="after-restart", devices=["reactor-b"]
        )
        assert new_task["task"]["status"] == "running"
        assert runtime.dispatcher.dispatched[-1]["job_id"] == stable_uuid("job:after-restart:0")
    finally:
        runtime.close()


def test_crash_fails_all_tasks_and_never_replays_old_jobs(core_runtime):
    runtime = core_runtime
    first = runtime.submit(
        task_name="crash-a", devices=["reactor-a", "reactor-b"], edges=[(0, 1)]
    )
    second = runtime.submit(task_name="crash-b", devices=["warehouse-a"])
    first_job = stable_uuid("job:crash-a:0")
    first_tail = stable_uuid("job:crash-a:1")
    runtime.scheduler.on_job_finished(first_job, True, {"completed": True})
    completed = runtime.workflow_store.get_job(first_job)
    sent = list(runtime.dispatcher.dispatched)
    runtime.scheduler.on_execution_process_restarted((first_tail,))
    for aggregate in (first, second):
        task_uuid = aggregate["task"]["uuid"]
        task = runtime.workflow_store.get_task(task_uuid)
        assert task["status"] == "failed"
        assert task["cleanup_status"] == "settled"
        assert task["control_status"] == "active"
        assert not task.get("attention_reason")
        assert (
            runtime.inventory_store.query_all(
                "SELECT * FROM station_execution_claim WHERE task_uuid=? AND state!='released'",
                (task_uuid,),
            )
            == []
        )
    # 已确认结果保留；中断原因单独归档，不覆盖历史返回值。
    assert (
        runtime.workflow_store.get_job(first_job)["return_info"]
        == completed["return_info"]
    )
    assert runtime.workflow_store.get_job(first_job)["status"] == "succeeded"
    interrupted = runtime.workflow_store.get_job(first_tail)
    assert interrupted["status"] == "failed"
    assert not interrupted.get("uncertainty_reason")
    assert runtime.bridge.active_or_uncertain_job_ids() == set()
    assert runtime.scheduler.snapshot()["inflight_jobs"] == {}
    assert runtime.bridge.error_control.snapshot()["errors"] == []
    assert (
        runtime.bridge.error_control.begin_execution(
            first_tail, interrupted["edge_command_uuid"]
        )
        == "CANCELED"
    )
    runtime.scheduler.on_job_outcome(
        first_tail, CommittedJobOutcome("succeeded", {"late": True}, [], [])
    )
    runtime.scheduler.on_execution_process_restarted(())
    runtime.restart_scheduler()
    assert runtime.workflow_store.get_job(first_tail) == interrupted
    assert runtime.dispatcher.dispatched == sent
    runtime.submit(task_name="after-crash", devices=["reactor-a"])
    assert runtime.dispatcher.dispatched[-1]["job_id"] == stable_uuid(
        "job:after-crash:0"
    )


def test_cold_start_aborts_pending_successors_and_old_error_session(core_runtime):
    runtime = core_runtime
    task = runtime.submit(
        task_name="crash-session", devices=["reactor-a", "reactor-b"], edges=[(0, 1)]
    )["task"]
    head = stable_uuid("job:crash-session:0")
    tail = stable_uuid("job:crash-session:1")
    runtime.scheduler.on_job_finished(head, False, {})
    control = runtime.bridge.error_control
    decision = control.snapshot()["errors"][0]
    entered = runtime.bridge.decide_error(
        decision["decision_id"],
        decision_request(control, decision, "enter_manual_handling"),
        idempotency_key="enter-crash",
        operator_id="tester",
    )
    session_id = entered["station"]["active_session_id"]
    sent = list(runtime.dispatcher.dispatched)
    assert runtime.bridge.recover_active_tasks(execution_process_restarted=True) == []
    assert runtime.workflow_store.get_task(task["uuid"])["status"] == "failed"
    assert runtime.workflow_store.get_job(tail)["status"] == "canceled"
    assert control.snapshot()["errors"] == []
    assert control.snapshot()["active_session"] is None
    assert control.get_session(session_id)["stage"] == "RESOLVED"
    failed_head = runtime.workflow_store.get_job(head)
    runtime.scheduler.on_job_outcome(
        head, CommittedJobOutcome("succeeded", {"late": True}, [], [])
    )
    assert runtime.workflow_store.get_job(head) == failed_head
    assert control.snapshot()["errors"] == []
    runtime.bridge.reschedule()
    assert runtime.dispatcher.dispatched == sent


def test_disconnect_waits_but_confirmed_crash_finishes(core_runtime):
    runtime = core_runtime
    task = runtime.submit(task_name="disconnect", devices=["reactor-a"])["task"]
    job_uuid = stable_uuid("job:disconnect:0")
    runtime.scheduler.on_execution_connection_lost((job_uuid,))
    assert runtime.workflow_store.get_task(task["uuid"])["status"] == "running"
    assert runtime.bridge.error_control.snapshot()["mode"] == "PAUSED"
    runtime.scheduler.on_execution_process_restarted((job_uuid,))
    assert runtime.workflow_store.get_job(job_uuid)["status"] == "failed"
    assert not runtime.workflow_store.get_job(job_uuid).get("uncertainty_reason")
    assert runtime.bridge.error_control.snapshot()["errors"] == []


def test_restart_closes_multiple_error_records_for_the_same_job(core_runtime):
    runtime = core_runtime
    task = runtime.submit(task_name="crash-many-errors", devices=["reactor-a"])["task"]
    job_uuid = stable_uuid("job:crash-many-errors:0")
    control = runtime.bridge.error_control
    for event_key in ("execution", "cancel_unknown:first", "cancel_unknown:second"):
        control.report_error(
            job_uuid, phase="execution_unknown", message="测试连接中断", event_key=event_key
        )
    before = control.snapshot()["errors"]
    assert len(before) == 3
    runtime.scheduler.on_execution_process_restarted((job_uuid,))
    assert runtime.workflow_store.get_task(task["uuid"])["status"] == "failed"
    assert control.snapshot()["errors"] == []
    assert control.snapshot()["mode"] == "RUNNING"
    with runtime.workflow_store.read() as connection:
        rows = connection.execute(
            "SELECT decision_id, revision, stage, status FROM workflow_intervention "
            "WHERE workflow_node_job_uuid=?", (job_uuid,)
        ).fetchall()
    assert {row["decision_id"] for row in rows} == {item["decision_id"] for item in before}
    assert all(row["stage"] == "RESOLVED" and row["status"] == "superseded" for row in rows)
    assert len({row["revision"] for row in rows}) == 3
    assert min(row["revision"] for row in rows) > max(item["revision"] for item in before)
    runtime.scheduler.on_execution_process_restarted(())
    assert control.snapshot()["mode"] == "RUNNING"


def test_failed_crash_projection_keeps_dispatch_closed_and_retries(
    core_runtime, monkeypatch
):
    runtime = core_runtime
    task = runtime.submit(task_name="crash-write", devices=["reactor-a"])["task"]
    original = TaskRuntimeProjection.project_execution_process_restarted
    calls = []

    def fail_once(self, task_uuid):
        calls.append(task_uuid)
        if len(calls) == 1:
            raise RuntimeError("模拟写入失败")
        return original(self, task_uuid)

    monkeypatch.setattr(
        TaskRuntimeProjection, "project_execution_process_restarted", fail_once
    )
    with pytest.raises(RuntimeError, match="模拟写入失败"):
        runtime.scheduler.on_execution_process_restarted(())
    assert runtime.bridge.error_control.recovering
    assert not runtime.bridge.error_control.allows_task(task["uuid"])
    runtime.scheduler.on_execution_process_restarted(())
    assert runtime.workflow_store.get_task(task["uuid"])["status"] == "failed"
    assert not runtime.bridge.error_control.recovering


def test_runtime_restart_rejects_accepted_retry_intent(core_runtime):
    runtime = core_runtime
    runtime.submit(task_name="crash-retry", devices=["reactor-a"])
    head = stable_uuid("job:crash-retry:0")
    runtime.scheduler.on_job_finished(head, False, {})
    control = runtime.bridge.error_control
    decision = control.snapshot()["errors"][0]
    accepted = control.decide(
        decision["decision_id"],
        decision_request(
            control, decision, "retry_current_node", resume_requested=False
        ),
        idempotency_key="stale-retry",
        operator_id="tester",
    )
    assert accepted["status"] == "PENDING"
    runtime.scheduler.on_execution_process_restarted(())
    assert control.get_command(accepted["command_uuid"])["status"] == "FAILED"
    assert runtime.bridge.apply_error_command(accepted)["status"] == "FAILED"
    assert (
        runtime.workflow_store.get_job(accepted["result"]["retry_job_uuid"])["status"]
        == "canceled"
    )
    assert len(runtime.dispatcher.dispatched) == 1
