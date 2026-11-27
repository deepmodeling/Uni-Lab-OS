"""苏州补料回归：派发准入冲突只让单节点失败；material_io 失败原因如实展示。"""

from __future__ import annotations

from typing import Any

import pytest

from tests.scheduler_core.conftest import CoreRuntime, stable_uuid
from tests.scheduler_core.test_material_io_runtime import (
    _install_load_target,
    _load_param,
    _submit_material_io,
)
from unilabos.app.scheduler.inventory import station_resource
from unilabos.app.scheduler.inventory.dispatch_admission import DispatchAdmissionConflict
from unilabos.app.scheduler.inventory.material_io import MaterialIOError, MaterialIOService
from unilabos.workflow.service import WorkflowService

POISON_REASON = "物理派发前库存 Claim 资源集合不一致：绑定物料已被删除"


def _poison(monkeypatch, *job_uuids: str) -> list[str]:
    """让指定作业的库存派发准入抛永久冲突（如绑定物料已被整盒卸下删除）。"""

    real = station_resource.acquire_dispatch_permit
    attempts: list[str] = []

    def acquire(connection, request):
        if request.job_uuid in job_uuids:
            attempts.append(request.job_uuid)
            raise DispatchAdmissionConflict(POISON_REASON)
        return real(connection, request)

    monkeypatch.setattr(station_resource, "acquire_dispatch_permit", acquire)
    return attempts


def _assert_node_failed_into_station_error(runtime: CoreRuntime, job_uuid: str) -> None:
    job = runtime.workflow_store.get_job(job_uuid)
    assert job["status"] == "failed", job
    assert POISON_REASON in str(job["error_info"])
    errors = runtime.bridge.error_control.snapshot()["errors"]
    assert [error["source_job_uuid"] for error in errors] == [job_uuid]
    assert errors[0]["error_phase"] == "prepare"
    assert job_uuid not in [payload["job_id"] for payload in runtime.dispatcher.dispatched]


def test_dispatch_admission_conflict_fails_only_that_node(core_runtime: CoreRuntime, monkeypatch) -> None:
    """设备空出后重排尝试派发被毒化的作业：该节点失败进入工站错误处置，重排不抛异常。"""

    blocker = core_runtime.submit(task_name="blocker", devices=["reactor-a"])
    blocker_job = stable_uuid("job:blocker:0")
    assert [p["job_id"] for p in core_runtime.dispatcher.dispatched] == [blocker_job]
    poisoned_job = stable_uuid("job:poisoned:0")
    attempts = _poison(monkeypatch, poisoned_job)
    core_runtime.submit(task_name="poisoned", devices=["reactor-a"])
    assert attempts == []  # 设备被占用，尚未尝试准入

    core_runtime.scheduler.on_job_finished(blocker_job, True, {})
    assert attempts == [poisoned_job]
    _assert_node_failed_into_station_error(core_runtime, poisoned_job)
    assert core_runtime.workflow_store.get_task(blocker["task"]["uuid"])["status"] == "succeeded"
    core_runtime.scheduler.reschedule()
    core_runtime.bridge.reschedule()
    assert attempts == [poisoned_job]
    assert [p["job_id"] for p in core_runtime.dispatcher.dispatched] == [blocker_job]


def test_dispatch_admission_conflict_on_submit_does_not_abort_submission(
    core_runtime: CoreRuntime, monkeypatch
) -> None:
    """提交即尝试派发时遇到永久准入冲突：提交成功、任务保留，节点失败待人工处置。"""

    poisoned_job = stable_uuid("job:poisoned-submit:0")
    _poison(monkeypatch, poisoned_job)
    aggregate = core_runtime.submit(task_name="poisoned-submit", devices=["reactor-a"])
    _assert_node_failed_into_station_error(core_runtime, poisoned_job)
    task = core_runtime.workflow_store.get_task(aggregate["task"]["uuid"])
    assert task["status"] not in {"canceled", "succeeded"}, task


def test_restart_recovery_with_dispatch_admission_conflict_does_not_raise(
    core_runtime: CoreRuntime, monkeypatch
) -> None:
    """毒化状态持久化后重启：恢复末尾的重排不能抛出，否则 Edge 起不来。"""

    poisoned_job = stable_uuid("job:poisoned-restart:0")
    core_runtime.persist(task_name="poisoned-restart", devices=["reactor-a"])
    attempts = _poison(monkeypatch, poisoned_job)
    core_runtime.restart_scheduler()
    assert attempts == [poisoned_job]
    _assert_node_failed_into_station_error(core_runtime, poisoned_job)


# ---------------------------------------------------------------- material_io 失败原因

@pytest.mark.parametrize("code,message", [
    ("site_occupied", "库位 SOURCE 已被物料 X 占用"),
    ("invalid_parameter", "unload 的 disposition=detach 不能与 scope=material_with_contents 同用"),
])
def test_material_io_settlement_failure_error_info_shows_inventory_reason(
    core_runtime: CoreRuntime, monkeypatch, code: str, message: str
) -> None:
    """material_io 不派发设备：失败作业的 error_info 展示库存拒绝的具体 code/message。"""

    template_uuid, mount_uuid, site_uuid = _install_load_target(core_runtime, f"reason-{code}")
    before = core_runtime.inventory_store.query_all("SELECT uuid FROM material ORDER BY uuid")

    def reject(self, **_kwargs: Any):
        raise MaterialIOError(code, message)

    monkeypatch.setattr(MaterialIOService, "settle_load", reject)
    _aggregate, job_uuid = _submit_material_io(
        core_runtime, task_name=f"material-io-reason-{code}",
        param=_load_param(template_uuid, mount_uuid, site_uuid),
    )
    service = WorkflowService(core_runtime.workflow_store, task_scheduler_bridge=core_runtime.bridge)
    service.decide_manual_confirmation(job_uuid, action="approve")
    job = core_runtime.workflow_store.get_job(job_uuid)
    assert job["status"] == "failed", job
    assert job["error_info"] == [{"code": code, "message": message, "suc_type": "material_io_failed"}]
    assert job["return_info"]["code"] == code
    assert core_runtime.dispatcher.dispatched == []
    assert core_runtime.inventory_store.query_all("SELECT uuid FROM material ORDER BY uuid") == before
