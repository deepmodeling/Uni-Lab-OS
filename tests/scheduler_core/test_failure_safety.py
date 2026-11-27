"""调度核心失败、超时、重复与不确定结果合同。"""

from __future__ import annotations

import json

import pytest

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid
from unilabos.app.scheduler.dispatch import CancelDispatchState, CommittedJobOutcome
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridgeError


def outcome(state: str, *, unknown: list[str] | None = None) -> CommittedJobOutcome:
    """构造 Edge 已提交的保真执行结果。"""

    return CommittedJobOutcome(
        outcome=state,
        return_info={"return_value": {"state": state}},
        error_info=[] if state == "succeeded" else [{"message": state}],
        unknown_command_ids=list(unknown or []),
    )


@pytest.mark.parametrize(
    ("terminal", "expected_task"),
    [("failed", "running"), ("timeout", "running")],
)
def test_terminal_failure_stops_successors_and_releases_permit(
    core_runtime: CoreRuntime,
    terminal: str,
    expected_task: str,
) -> None:
    """明确失败类结果不得推进后继节点，并应释放已确定的库存 Claim。"""

    task_name = f"terminal-{terminal}"
    aggregate = core_runtime.submit(
        task_name=task_name,
        devices=["reactor-a", "reactor-b"],
        edges=[(0, 1)],
    )
    first_job = stable_uuid(f"job:{task_name}:0")
    second_job = stable_uuid(f"job:{task_name}:1")

    core_runtime.scheduler.on_job_outcome(first_job, outcome(terminal))

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [
        first_job
    ]
    assert (
        core_runtime.workflow_store.get_task(aggregate["task"]["uuid"])["status"]
        == expected_task
    )
    assert core_runtime.workflow_store.get_job(first_job)["status"] == terminal
    assert core_runtime.workflow_store.get_job(second_job)["status"] == "pending"
    assert core_runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim ORDER BY job_uuid"
    ) == [{"state": "released"}]


def test_restart_archives_uncertainty_and_releases_interrupted_task_claims(
    core_runtime: CoreRuntime,
) -> None:
    """兄弟 Job 崩溃不能释放此前失败转运仍待对账的资源。"""

    task_name = "restart-with-transfer-reconciliation"
    aggregate = core_runtime.submit(
        task_name=task_name,
        devices=["reactor-a", "reactor-b"],
    )
    task_uuid = str(aggregate["task"]["uuid"])
    transfer_job_uuid = stable_uuid(f"job:{task_name}:0")
    sibling_job_uuid = stable_uuid(f"job:{task_name}:1")
    with core_runtime.workflow_store.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_node_job
            SET executor_kind='material_transfer', expected_change_set=?
            WHERE uuid=?
            """,
            (
                json.dumps(
                    {
                        "kind": "material_transfer",
                        "material_uuid": core_runtime.device_materials["reactor-a"],
                        "source_site_uuid": "source-site",
                        "target_site_uuid": "target-site",
                    }
                ),
                transfer_job_uuid,
            ),
        )

    core_runtime.scheduler.on_job_outcome(transfer_job_uuid, outcome("failed"))

    projection = TaskRuntimeProjection(core_runtime.workflow_store)
    assert core_runtime.workflow_store.get_task(task_uuid)["control_status"] == "error_waiting"
    assert core_runtime.workflow_store.get_job(sibling_job_uuid)["status"] == "running"
    assert core_runtime.workflow_store.get_job(transfer_job_uuid)[
        "uncertainty_reason"
    ] == "material_transfer_inventory_reconciliation_required"
    assert projection.get_execution_claim(transfer_job_uuid)["state"] == "uncertain"
    assert core_runtime.inventory_store.query_one(
        "SELECT state FROM station_execution_claim WHERE job_uuid=?",
        (transfer_job_uuid,),
    ) == {"state": "uncertain"}

    core_runtime.scheduler.on_execution_process_restarted((sibling_job_uuid,))

    failed_task = core_runtime.workflow_store.get_task(task_uuid)
    assert failed_task["status"] == "failed"
    assert failed_task["cleanup_status"] == "settled"
    assert not failed_task.get("attention_reason")
    for job_uuid in (transfer_job_uuid, sibling_job_uuid):
        assert core_runtime.workflow_store.get_job(job_uuid)["status"] == "failed"
        assert not core_runtime.workflow_store.get_job(job_uuid).get(
            "uncertainty_reason"
        )
        assert projection.get_execution_claim(job_uuid)["state"] == "released"
        assert core_runtime.inventory_store.query_one(
            "SELECT state FROM station_execution_claim WHERE job_uuid=?", (job_uuid,)
        ) == {"state": "released"}
    assert core_runtime.bridge.active_or_uncertain_job_ids() == set()
    core_runtime.submit(task_name="after-runtime-crash", devices=["reactor-a"])
    assert core_runtime.dispatcher.dispatched[-1]["job_id"] == stable_uuid(
        "job:after-runtime-crash:0"
    )


def test_unknown_then_certain_result_never_replays_physical_dispatch(
    core_runtime: CoreRuntime,
) -> None:
    """结果含未知命令时先保持运行；随后的明确成功结算任务，且不重新派发。"""

    aggregate = core_runtime.submit(
        task_name="unknown-result",
        devices=["reactor-a"],
    )
    job_uuid = stable_uuid("job:unknown-result:0")

    uncertain = core_runtime.scheduler.on_job_outcome(
        job_uuid,
        outcome("failed", unknown=["device-command-1"]),
    )
    assert uncertain["state"] == "running"
    assert core_runtime.workflow_store.get_job(job_uuid)["status"] == "running"
    assert core_runtime.workflow_store.get_job(job_uuid)["uncertainty_reason"] == (
        "edge_reported_unknown_commands:device-command-1"
    )
    assert core_runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim"
    ) == [{"state": "uncertain"}]

    core_runtime.scheduler.on_job_outcome(job_uuid, outcome("succeeded"))
    core_runtime.scheduler.on_job_outcome(job_uuid, outcome("succeeded"))

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [job_uuid]
    assert (
        core_runtime.workflow_store.get_task(aggregate["task"]["uuid"])["status"]
        == "succeeded"
    )
    assert core_runtime.workflow_store.get_job(job_uuid)["status"] == "succeeded"


def test_dispatch_exception_freezes_claim_and_accepts_one_late_result(
    core_runtime: CoreRuntime,
) -> None:
    """派发受理不明时不重试，迟到的明确结果只结算原 Job。"""

    task = core_runtime.persist(task_name="dispatch-unknown", devices=["reactor-a"])
    job_uuid = stable_uuid("job:dispatch-unknown:0")
    core_runtime.dispatcher.dispatch_error = RuntimeError("executor disconnected")

    with pytest.raises(TaskSchedulerBridgeError, match="派发结果不确定"):
        core_runtime.bridge.submit(task)

    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [job_uuid]
    job = core_runtime.workflow_store.get_job(job_uuid)
    assert job["status"] == "running"
    assert job["uncertainty_reason"] == "local_dispatch_acceptance_unknown"
    assert core_runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim"
    ) == [{"state": "uncertain"}]

    core_runtime.dispatcher.dispatch_error = None
    replay = core_runtime.bridge.submit(task)
    assert replay["task"]["status"] == "running"
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [job_uuid]

    core_runtime.scheduler.on_job_outcome(job_uuid, outcome("succeeded"))

    assert core_runtime.workflow_store.get_task(task["uuid"])["status"] == "running"
    assert core_runtime.workflow_store.get_job(job_uuid)["status"] == "succeeded"
    assert [item["job_id"] for item in core_runtime.dispatcher.dispatched] == [job_uuid]
    assert core_runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim"
    ) == [{"state": "released"}]


def test_unsupported_cancel_keeps_running_job_for_reconciliation(
    core_runtime: CoreRuntime,
) -> None:
    """执行器不支持取消时不得伪造 canceled 终态或释放资源。"""

    aggregate = core_runtime.submit(
        task_name="cancel-unavailable",
        devices=["reactor-a"],
    )
    job_uuid = stable_uuid("job:cancel-unavailable:0")
    core_runtime.dispatcher.cancel_state = CancelDispatchState.UNAVAILABLE

    canceled = core_runtime.bridge.cancel(
        aggregate["task"]["uuid"],
        command_uuid=stable_uuid("command:cancel-unavailable"),
    )

    job = core_runtime.workflow_store.get_job(job_uuid)
    assert canceled["task"]["status"] == "canceling"
    assert job["status"] == "running"
    assert job["uncertainty_reason"] == "local_cancel_acceptance_unavailable"
    assert core_runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim"
    ) == [{"state": "uncertain"}]
