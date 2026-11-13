"""资源准入的持久等待顺序（SC-N4）：空闲资源先留给排在前面的等待者，优先级随等待老化，
重启后顺序不变，让行永远不会造成死锁。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from unilabos.protocol.runtime.resource_intervals import DeviceTenancyTransition
from unilabos.server.backend.scheduler.models import MaterialLockClaim
from unilabos.server.services.runtime.workflow.execution_claim import ExecutionClaims
from unilabos.server.services.runtime.workflow.resource_intervals import ResourceIntervals
from unilabos.server.services.runtime.workflow.resource_waiters import WAITER_AGING_INTERVAL_S
from unilabos.server.services.runtime.workflow.service import WorkflowService
from tests.server.test_workflow_resource_intervals import make_task, request_for


@pytest.fixture
def service(tmp_path):
    value = WorkflowService(tmp_path / "runtime.db")
    yield value
    value.close()


def _admission(service):
    return ResourceIntervals(service), ExecutionClaims(service).activate_authority()


def _finish(service, intervals, request):
    service.record_workflow_node_job_terminal(request.owner_uuid, status="succeeded", return_info={}, error_info=[])
    intervals.release_job(request.owner_uuid)


def _with_material(request, material_uuid):
    return request.model_copy(update={"material_lock_claims": [MaterialLockClaim(material_uuid=material_uuid)]})


def _enqueued_at(service, request):
    row = service.connection.execute("SELECT enqueued_at FROM workflow_resource_waiter WHERE job_uuid=?",
                                     (request.owner_uuid,)).fetchone()
    return row and row[0]


def test_free_device_goes_to_the_older_waiter_not_to_a_newcomer(service):
    intervals, epoch = _admission(service)
    holder, older, newcomer = (make_task(service, ("reactor",)) for _ in range(3))
    assert intervals.admit(request_for(service, holder), epoch)
    assert not intervals.admit(request_for(service, older), epoch)
    _finish(service, intervals, request_for(service, holder))

    # 修复前：新来者看到设备空闲直接拿走，等了更久的任务继续等。
    assert not intervals.admit(request_for(service, newcomer), epoch)
    blockers = service.connection.execute("SELECT blockers FROM workflow_resource_waiter WHERE job_uuid=?",
                                          (request_for(service, newcomer).owner_uuid,)).fetchone()[0]
    assert json.loads(blockers) == [older["uuid"]]  # 等待图里能看到排在谁后面
    assert intervals.admit(request_for(service, older), epoch)
    _finish(service, intervals, request_for(service, older))
    assert intervals.admit(request_for(service, newcomer), epoch)


def test_multi_resource_waiter_is_not_starved_by_single_resource_newcomers(service):
    intervals, epoch = _admission(service)
    material = str(uuid4())
    holder = make_task(service, ("balance",))
    older = make_task(service, ("reactor",))
    assert intervals.admit(_with_material(request_for(service, holder), material), epoch)
    # 先到者要 reactor + 物料，物料被占：等待。
    assert not intervals.admit(_with_material(request_for(service, older), material), epoch)
    # 后到者只要 reactor（此刻空闲）：让给先到者，不再一个接一个插队。
    for _ in range(3):
        newcomer = make_task(service, ("reactor",))
        assert not intervals.admit(request_for(service, newcomer), epoch)
    _finish(service, intervals, _with_material(request_for(service, holder), material))
    assert intervals.admit(_with_material(request_for(service, older), material), epoch)


def test_high_priority_goes_first_until_the_normal_waiter_has_aged(service):
    intervals, epoch = _admission(service)
    holder = make_task(service, ("reactor",))
    normal = make_task(service, ("reactor",))
    assert intervals.admit(request_for(service, holder), epoch)
    assert not intervals.admit(request_for(service, normal), epoch)
    high = service.create_workflow_task(workflow_uuid=normal["workflow_uuid"], run_mode="normal", target_node_uuid=None,
                                        input_value={}, description=None, meta_data={}, priority="high")
    assert not intervals.admit(request_for(service, high), epoch)
    _finish(service, intervals, request_for(service, holder))

    assert not intervals.admit(request_for(service, normal), epoch)  # high 排在前面

    # normal 已经等了两个老化周期：有效优先级 0 + 2 > 1，轮到它。
    aged = (datetime.now(timezone.utc) - timedelta(seconds=2 * WAITER_AGING_INTERVAL_S + 1)).isoformat().replace("+00:00", "Z")
    with service.transaction() as conn:
        conn.execute("UPDATE workflow_resource_waiter SET enqueued_at=? WHERE job_uuid=?",
                     (aged, request_for(service, normal).owner_uuid))
    assert not intervals.admit(request_for(service, high), epoch)
    assert intervals.admit(request_for(service, normal), epoch)


def test_queue_position_survives_retries_and_restart(tmp_path):
    path = tmp_path / "runtime.db"
    service = WorkflowService(path)
    intervals, epoch = _admission(service)
    holder, older = make_task(service, ("reactor",)), make_task(service, ("reactor",))
    assert intervals.admit(request_for(service, holder), epoch)
    assert not intervals.admit(request_for(service, older), epoch)
    first = _enqueued_at(service, request_for(service, older))
    assert first
    assert not intervals.admit(request_for(service, older), epoch)
    assert _enqueued_at(service, request_for(service, older)) == first
    _finish(service, intervals, request_for(service, holder))
    service.close()

    service = WorkflowService(path)
    try:
        intervals, epoch = _admission(service)
        newcomer = make_task(service, ("reactor",))
        assert _enqueued_at(service, request_for(service, older)) == first
        assert not intervals.admit(request_for(service, newcomer), epoch)
        assert intervals.admit(request_for(service, older), epoch)
        assert _enqueued_at(service, request_for(service, older)) is None  # 准入后不再排队
    finally:
        service.close()


def test_paused_waiter_does_not_hold_its_place_against_active_tasks(service):
    intervals, epoch = _admission(service)
    holder, paused, newcomer = (make_task(service, ("reactor",)) for _ in range(3))
    assert intervals.admit(request_for(service, holder), epoch)
    assert not intervals.admit(request_for(service, paused), epoch)
    _finish(service, intervals, request_for(service, holder))
    with service.transaction() as conn:
        conn.execute("UPDATE workflow_task SET control_status='paused' WHERE uuid=?", (paused["uuid"],))
    assert intervals.admit(request_for(service, newcomer), epoch)


def test_yielding_never_waits_on_a_waiter_that_waits_on_us(service):
    """C 把样品装进 reactor（托管到卸载为止），下一步只要空闲的 balance；先到的 A 要 balance + reactor。

    C 若让行给 A：A 等 C 托管的 reactor，C 等 A 让出的 balance——死锁。C 必须直接取得 balance。
    """
    intervals, epoch = _admission(service)
    custody = make_task(service, ("robot", "balance"))
    sample = str(uuid4())
    load = request_for(service, custody, 0).model_copy(update={
        "device_tenancy": DeviceTenancyTransition(material_uuid=sample, acquire_device_id="reactor")})
    assert intervals.admit(load, epoch)
    _finish(service, intervals, load)  # 样品仍托管在 reactor 里

    contender = make_task(service, ("balance", "reactor"), explicit=True)
    assert not intervals.admit(request_for(service, contender, 0), epoch)  # reactor 被托管
    assert intervals.admit(request_for(service, custody, 1), epoch)
