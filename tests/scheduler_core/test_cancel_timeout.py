"""调度核心取消截止时间的确定性合同。"""

from __future__ import annotations

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid


def test_cancel_ack_timeout_is_driven_by_manual_clock(
    core_runtime: CoreRuntime,
) -> None:
    """推进 Manual Clock 后立即持久化不确定状态，不创建真实计时线程。"""

    task = core_runtime.submit(task_name="cancel-timeout", devices=["reactor-a"])
    job_uuid = stable_uuid("job:cancel-timeout:0")
    core_runtime.bridge.cancel(
        task["task"]["uuid"],
        command_uuid=stable_uuid("command:cancel-timeout"),
    )

    core_runtime.clock.advance(9.9)
    assert (
        core_runtime.workflow_store.get_job(job_uuid).get("uncertainty_reason") is None
    )

    core_runtime.clock.advance(0.1)
    job = core_runtime.workflow_store.get_job(job_uuid)
    assert job["status"] == "running"
    assert job["uncertainty_reason"] == "local_cancel_acceptance_timeout"
    assert (
        core_runtime.workflow_store.get_task(task["task"]["uuid"])["cleanup_status"]
        == "requires_attention"
    )
    assert core_runtime.inventory_store.query_all(
        "SELECT state FROM station_execution_claim"
    ) == [{"state": "uncertain"}]
