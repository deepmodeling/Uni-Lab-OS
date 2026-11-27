"""数量结算异常经真实工作流桥、HTTP 更正与重启恢复的合同。"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.app.scheduler.dispatch import CommittedJobOutcome, RecordingDispatcher
from unilabos.app.scheduler.inventory.dispatch_admission import DispatchAdmissionRequest, DispatchResource
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.workflow_quantity import WorkflowQuantityReservationError
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.physical_settlement_policy import QUANTITY_RECONCILIATION_REQUIRED
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import StoreConflict
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

from .quantity_inventory_test_support import TASK_UUID
from .test_quantity_inventory_reconciliation import _create_task, _scenario


@contextmanager
def _runtime(
    scenario: dict[str, Any],
) -> Iterator[tuple[TaskSchedulerBridge, TestClient, RecordingDispatcher]]:
    """接入真实库存、任务投影和结果桥，记录任何意外设备派发。"""

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=InventoryService(scenario["inventory"]))
    bridge = TaskSchedulerBridge(scenario["workflow"], scheduler=scheduler)
    client = TestClient(create_workflow_app(WorkflowService(scenario["workflow"], task_scheduler_bridge=bridge)))
    try:
        yield bridge, client, dispatcher
    finally:
        bridge.close()


def _running(bridge: TaskSchedulerBridge, scenario: dict[str, Any]) -> None:
    """还原已经受理的设备作业；测试从其成功回执进入真实结算链。"""

    for job_uuid in scenario["jobs"]:
        lock_key = f"resource:{job_uuid}"
        authority = bridge._scheduler.station_resource_inventory
        permit = authority.acquire_dispatch_permit(DispatchAdmissionRequest(
            effect_uuid=str(uuid4()), task_uuid=TASK_UUID, job_uuid=job_uuid,
            attempt=1, parameter_hash="quantity-test", expected_change_set={"kind": "no_inventory_change"},
            resources=(DispatchResource(lock_key=lock_key, scope="resource"),),
        )).permit
        assert permit is not None
        bridge._projection.project_pre_dispatch(
            task_uuid=TASK_UUID, job_uuid=job_uuid,
            execution_locks=[{"lock_key": lock_key, "scope": "resource"}],
            dispatch_permit=permit.as_mapping(),
        )
        authority.transition_dispatch_permit(permit.claim_uuid, target_state="reserved")
        authority.transition_dispatch_permit(permit.claim_uuid, target_state="running")
        bridge._projection.project_dispatch_accepted(job_uuid)


def _outcome(
    scenario: dict[str, Any], *, unit: str = "g", quantity: float = 2.0,
    duplicate: bool = False,
) -> CommittedJobOutcome:
    """构造同一设备已提交的成功证明，覆盖错单位与无效重复回执。"""

    item = {"inventory_type": "reagent", "inventory_uuid": scenario["reagent_uuid"],
            "actual_quantity": quantity, "quantity_unit": unit}
    return CommittedJobOutcome(
        outcome="succeeded", return_info={"return_value": {"device": "completed"}},
        error_info=[], unknown_command_ids=[], inventory_consumptions=[item] * (2 if duplicate else 1),
    )


def _correction(scenario: dict[str, Any], *, quantity: float = 2.0) -> dict[str, Any]:
    """显式核对全套实例及单位，不改变已提交的设备执行结果。"""

    return {"reason": "核对泵日志，实际单位为毫升", "consumptions": _outcome(scenario, unit="mL", quantity=quantity).inventory_consumptions}


def _quantity(scenario: dict[str, Any]) -> float:
    return scenario["inventory"].query_one("SELECT quantity FROM reagent WHERE uuid=?", (scenario["reagent_uuid"],))["quantity"]


@pytest.mark.parametrize("duplicate", [False, True])
def test_invalid_success_preserves_reservation_and_public_correction_is_idempotent(
    tmp_path: Path, duplicate: bool,
) -> None:
    """不能对账的成功回执保持运行占用；人工更正仅补记一次库存。"""

    with _scenario(tmp_path) as scenario, _runtime(scenario) as (bridge, client, dispatcher):
        _running(bridge, scenario)
        job_uuid = scenario["jobs"][0]
        with pytest.raises((ValueError, StoreConflict)):
            bridge._on_job_outcome(job_uuid, _outcome(scenario, duplicate=duplicate))
        job = scenario["workflow"].get_job(job_uuid)
        assert job["status"] == "running"
        assert job["uncertainty_reason"] == QUANTITY_RECONCILIATION_REQUIRED
        assert scenario["workflow"].get_task(TASK_UUID)["control_status"] == "waiting_reconciliation"
        assert _quantity(scenario) == 10
        assert {row["state"] for row in bridge._projection.list_execution_locks(job_uuid)} == {"uncertain"}
        assert scenario["inventory"].query_one("SELECT status FROM inventory_reservation")["status"] == "active"
        path = f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity"
        for _ in range(2):
            response = client.post(path, json=_correction(scenario))
            assert response.status_code == 200, response.text
            assert response.json()["code"] == 0, response.text
        assert _quantity(scenario) == 8
        assert scenario["workflow"].get_job(job_uuid)["status"] == "succeeded"
        assert scenario["workflow"].get_task(TASK_UUID)["status"] == "succeeded"
        assert len(bridge.list_job_inventory_consumptions(job_uuid)) == 1
        assert {row["state"] for row in bridge._projection.list_execution_locks(job_uuid)} == {"released"}
        assert client.post(path, json=_correction(scenario, quantity=1)).json()["code"] != 0
        assert _quantity(scenario) == 8
        assert dispatcher.dispatched == []


def test_restart_fails_all_tasks_but_preserves_pending_quantity_evidence(tmp_path: Path) -> None:
    """坏消费只延迟库存清理；所有旧任务立即失败，原成功证据仍可结算。"""

    with _scenario(tmp_path) as scenario:
        other_task, other_job = str(uuid4()), str(uuid4())
        other_prepared = replace(scenario["prepared"], jobs=[dict(scenario["prepared"].jobs[0], uuid=other_job)])
        _create_task(scenario, other_task, other_prepared)
        with _runtime(scenario) as (bridge, _, _):
            _running(bridge, scenario)
            bridge._replay_persisted_job_outcome(scenario["jobs"][0], _outcome(scenario))
        with _runtime(scenario) as (bridge, client, dispatcher):
            bridge.recover_active_tasks(execution_process_restarted=True)
            assert scenario["workflow"].get_task(other_task)["status"] == "failed"
            assert scenario["workflow"].get_task(TASK_UUID)["status"] == "failed"
            assert scenario["workflow"].get_job(scenario["jobs"][0])["status"] == "running"
            assert scenario["workflow"].get_task(TASK_UUID)["control_status"] == "waiting_reconciliation"
            failed = scenario["workflow"].get_task(TASK_UUID)
            bridge.recover_active_tasks(execution_process_restarted=True)
            repeated = scenario["workflow"].get_task(TASK_UUID)
            assert repeated["finished_at"] == failed["finished_at"]
            assert repeated["error_info"] == failed["error_info"]
            response = client.post(f"/api/v1/workflow-node-jobs/{scenario['jobs'][0]}/reconcile-quantity", json=_correction(scenario))
            assert response.json()["code"] == 0, response.text
            assert _quantity(scenario) == 8
            assert scenario["workflow"].get_task(TASK_UUID)["status"] == "failed"
            assert dispatcher.dispatched == []


@pytest.mark.parametrize("execution_process_restarted", [False, True])
def test_restart_replays_saved_success_after_correction_committed_before_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execution_process_restarted: bool,
) -> None:
    """跨库更正提交后投影崩溃，可在无 Edge 再次投递时恢复成功事实。"""

    with _scenario(tmp_path) as scenario:
        job_uuid = scenario["jobs"][0]
        with _runtime(scenario) as (bridge, _, _):
            _running(bridge, scenario)
            bridge._replay_persisted_job_outcome(job_uuid, _outcome(scenario))
            def crash(*args, **kwargs):
                raise RuntimeError("模拟库存已提交但工作流投影前退出")
            monkeypatch.setattr(bridge, "_complete_reconciled_quantity_result", crash)
            with pytest.raises(RuntimeError):
                bridge.reconcile_job_quantity(job_uuid, **_correction(scenario))
            assert _quantity(scenario) == 8
        with _runtime(scenario) as (bridge, client, dispatcher):
            bridge.recover_active_tasks(execution_process_restarted=execution_process_restarted)
            assert scenario["workflow"].get_job(job_uuid)["status"] == "succeeded"
            assert scenario["workflow"].get_task(TASK_UUID)["status"] == "succeeded"
            assert _quantity(scenario) == 8
            assert client.post(f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity", json=_correction(scenario)).json()["code"] == 0
            assert dispatcher.dispatched == []


def test_parallel_success_waits_for_first_correction_then_settles_each_job(tmp_path: Path) -> None:
    """并行结果不能覆盖失败消费意图；更正首个作业后逐个补齐原成功结果。"""

    with _scenario(tmp_path, quantities=(2.0, 2.0)) as scenario, _runtime(scenario) as (bridge, client, dispatcher):
        _running(bridge, scenario)
        first, second = scenario["jobs"]
        bridge._replay_persisted_job_outcome(first, _outcome(scenario))
        bridge._replay_persisted_job_outcome(second, _outcome(scenario, unit="mL"))
        assert _quantity(scenario) == 10
        assert all(scenario["workflow"].get_job(job)["uncertainty_reason"] == QUANTITY_RECONCILIATION_REQUIRED for job in (first, second))
        path = f"/api/v1/workflow-node-jobs/{first}/reconcile-quantity"
        assert client.post(path, json=_correction(scenario)).json()["code"] == 0
        assert _quantity(scenario) == 6
        assert scenario["workflow"].get_task(TASK_UUID)["status"] == "succeeded"
        assert all(scenario["workflow"].get_job(job)["status"] == "succeeded" for job in (first, second))
        assert client.post(path, json=_correction(scenario)).json()["code"] == 0
        assert _quantity(scenario) == 6
        assert dispatcher.dispatched == []


def test_same_process_recovery_preserves_quantity_reconciliation(tmp_path: Path) -> None:
    """调度桥重建不终结待对账任务，也不重新执行已经完成的设备动作。"""

    with _scenario(tmp_path) as scenario:
        job_uuid = scenario["jobs"][0]
        with _runtime(scenario) as (bridge, _, _):
            _running(bridge, scenario)
            bridge._replay_persisted_job_outcome(job_uuid, _outcome(scenario))
        with _runtime(scenario) as (bridge, client, dispatcher):
            bridge.recover_active_tasks()
            job = scenario["workflow"].get_job(job_uuid)
            assert job["status"] == "running"
            assert job["uncertainty_reason"] == QUANTITY_RECONCILIATION_REQUIRED
            assert scenario["workflow"].get_task(TASK_UUID)["control_status"] == "waiting_reconciliation"
            assert scenario["inventory"].query_one("SELECT status FROM inventory_reservation")["status"] == "active"
            assert {row["state"] for row in bridge._projection.list_execution_locks(job_uuid)} == {"uncertain"}
            response = client.post(f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity", json=_correction(scenario))
            assert response.json()["code"] == 0, response.text
            assert _quantity(scenario) == 8
            assert dispatcher.dispatched == []


@pytest.mark.parametrize("correction_committed", [False, True])
def test_connection_loss_preserves_quantity_evidence_until_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, correction_committed: bool,
) -> None:
    """断线不能覆盖数量对账原因，尤其更正落账后尚未投影的成功事实。"""

    with _scenario(tmp_path) as scenario:
        job_uuid = scenario["jobs"][0]
        with _runtime(scenario) as (bridge, _, dispatcher):
            _running(bridge, scenario)
            bridge._replay_persisted_job_outcome(job_uuid, _outcome(scenario))
            if correction_committed:
                def crash(*args, **kwargs):
                    raise RuntimeError("模拟库存更正落账后投影中断")

                with monkeypatch.context() as patch:
                    patch.setattr(bridge, "_complete_reconciled_quantity_result", crash)
                    with pytest.raises(RuntimeError):
                        bridge.reconcile_job_quantity(job_uuid, **_correction(scenario))
            before = scenario["workflow"].get_job(job_uuid)
            bridge._scheduler.on_execution_connection_lost((job_uuid,))
            after = scenario["workflow"].get_job(job_uuid)
            assert after["status"] == "running"
            assert after["uncertainty_reason"] == QUANTITY_RECONCILIATION_REQUIRED
            assert after["control_data"]["quantity_reconciliation"] == before["control_data"]["quantity_reconciliation"]
            assert dispatcher.dispatched == []
        with _runtime(scenario) as (bridge, client, dispatcher):
            bridge.recover_active_tasks(execution_process_restarted=True)
            if not correction_committed:
                response = client.post(f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity", json=_correction(scenario))
                assert response.json()["code"] == 0, response.text
            assert scenario["workflow"].get_job(job_uuid)["status"] == "succeeded"
            assert _quantity(scenario) == 8
            assert len(bridge.list_job_inventory_consumptions(job_uuid)) == 1
            assert dispatcher.dispatched == []


def test_task_unlock_rejects_pending_quantity_before_releasing_permits(tmp_path: Path) -> None:
    """数量对账未结束时，终态任务解锁也不能先释放库存 Permit。"""

    with _scenario(tmp_path) as scenario, _runtime(scenario) as (bridge, client, dispatcher):
        _running(bridge, scenario)
        job_uuid = scenario["jobs"][0]
        bridge._replay_persisted_job_outcome(job_uuid, _outcome(scenario))
        with scenario["workflow"].transaction() as connection:
            connection.execute("UPDATE workflow_task SET status='canceled' WHERE uuid=?", (TASK_UUID,))
            connection.execute("UPDATE workflow_node_job SET status='canceled' WHERE uuid=?", (job_uuid,))
        permits = scenario["inventory"].query_all("SELECT * FROM station_execution_claim")
        inventory_locks = scenario["inventory"].query_all("SELECT * FROM station_execution_lock_lease")
        workflow_locks = bridge._projection.list_execution_locks(job_uuid)
        response = client.post(f"/api/v1/workflow-tasks/{TASK_UUID}/commands", json={
            "type": "unlock_resources", "target_node_uuid": None,
            "idempotency_key": "quantity-pending-unlock", "description": "核对设备已停止",
            "meta_data": {"confirmed_physical_safe": True},
        })
        assert response.status_code == 201, response.text
        assert response.json()["data"]["status"] == "rejected", response.text
        assert scenario["inventory"].query_all("SELECT * FROM station_execution_claim") == permits
        assert scenario["inventory"].query_all("SELECT * FROM station_execution_lock_lease") == inventory_locks
        assert bridge._projection.list_execution_locks(job_uuid) == workflow_locks
        assert scenario["inventory"].query_one("SELECT status FROM inventory_reservation")["status"] == "active"
        assert _quantity(scenario) == 10
        assert dispatcher.dispatched == []


def test_concurrent_corrections_keep_committed_request_and_reject_losing_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两人更正同一作业时，失败请求不得覆盖审计身份或在重试时伪装成成功。"""

    with _scenario(tmp_path) as scenario, _runtime(scenario) as (bridge, client, dispatcher):
        _running(bridge, scenario)
        job_uuid = scenario["jobs"][0]
        bridge._replay_persisted_job_outcome(job_uuid, _outcome(scenario))
        staged_a, started_b, staged_b, applied_a = Event(), Event(), Event(), Event()
        original_stage = bridge._projection.stage_quantity_reconciliation
        original_reconcile = bridge._quantity_inventory.reconcile_consumption

        def stage(*args, **kwargs):
            quantity = kwargs["consumptions"][0]["actual_quantity"]
            if quantity == 1:
                assert staged_a.wait(5)
            result = original_stage(*args, **kwargs)
            if quantity == 2:
                staged_a.set()
                assert started_b.wait(5)
                # 缺少桥接层串行保护时，让 B 覆盖暂存请求后 A 才落账。
                # 有保护时 B 尚未进入暂存阶段，等待到期后 A 正常完成。
                staged_b.wait(0.25)
            else:
                staged_b.set()
            return result

        def reconcile(*args, **kwargs):
            quantity = kwargs["consumptions"][0]["actual_quantity"]
            if quantity == 1:
                assert applied_a.wait(5)
            result = original_reconcile(*args, **kwargs)
            if quantity == 2:
                applied_a.set()
            return result

        monkeypatch.setattr(bridge._projection, "stage_quantity_reconciliation", stage)
        monkeypatch.setattr(bridge._quantity_inventory, "reconcile_consumption", reconcile)

        def correct(quantity):
            if quantity == 1:
                assert staged_a.wait(5)
                started_b.set()
            try:
                return bridge.reconcile_job_quantity(job_uuid, **_correction(scenario, quantity=quantity))
            except StoreConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(correct, 2)
            second = workers.submit(correct, 1)
            assert first.result(timeout=10)["status"] == "succeeded"
            assert isinstance(second.result(timeout=10), StoreConflict)
        monkeypatch.setattr(bridge._projection, "stage_quantity_reconciliation", original_stage)
        monkeypatch.setattr(bridge._quantity_inventory, "reconcile_consumption", original_reconcile)

        job = scenario["workflow"].get_job(job_uuid)
        assert job["control_data"]["quantity_reconciliation"]["requested_correction"] == _correction(scenario, quantity=2)
        assert _quantity(scenario) == 8
        path = f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity"
        assert client.post(path, json=_correction(scenario, quantity=1)).json()["code"] != 0
        assert client.post(path, json=_correction(scenario, quantity=2)).json()["code"] == 0
        assert _quantity(scenario) == 8
        assert len(bridge.list_job_inventory_consumptions(job_uuid)) == 1
        assert dispatcher.dispatched == []


def test_staged_correction_cannot_claim_ordinary_result_consumption_as_its_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """暂存更正不等于已经更正；普通结果先落账后，原暂存请求必须被拒绝。"""

    with _scenario(tmp_path) as scenario, _runtime(scenario) as (bridge, client, dispatcher):
        _running(bridge, scenario)
        job_uuid = scenario["jobs"][0]
        outcome = _outcome(scenario, unit="mL", quantity=2)
        authority = bridge._quantity_inventory._authority
        original_apply = authority.apply_consumption

        def temporarily_rejected(*args, **kwargs):
            raise WorkflowQuantityReservationError("暂时无法读取已核对库存")

        monkeypatch.setattr(authority, "apply_consumption", temporarily_rejected)
        bridge._replay_persisted_job_outcome(job_uuid, outcome)
        assert _quantity(scenario) == 10
        assert scenario["workflow"].get_job(job_uuid)["uncertainty_reason"] == QUANTITY_RECONCILIATION_REQUIRED
        correction = _correction(scenario, quantity=1)
        bridge._projection.stage_quantity_reconciliation(
            job_uuid, **correction, can_replace=True
        )

        # 暂存之后尚未调用人工更正端口，库存恢复让原始 2 mL 成功回执先落账。
        monkeypatch.setattr(authority, "apply_consumption", original_apply)
        bridge._replay_persisted_job_outcome(job_uuid, outcome)
        assert _quantity(scenario) == 8
        assert scenario["workflow"].get_job(job_uuid)["status"] == "succeeded"
        assert scenario["workflow"].get_job(job_uuid)["control_data"]["quantity_reconciliation"]["requested_correction"] == correction

        path = f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity"
        response = client.post(path, json=correction)
        assert response.status_code == 200
        assert response.json()["code"] != 0
        assert _quantity(scenario) == 8
        assert len(bridge.list_job_inventory_consumptions(job_uuid)) == 1
        assert dispatcher.dispatched == []


def test_runtime_restart_retains_material_and_quantity_until_correction(
    tmp_path: Path,
) -> None:
    """已有内存运行的重启通知不能绕过数量门禁释放任一类物料预留。"""

    from unilabos.app.scheduler.inventory.domain import MaterialRequirement
    from unilabos.app.scheduler.models import DispatchedJob, WorkflowNode, WorkflowSpec

    with _scenario(tmp_path) as scenario, _runtime(scenario) as (bridge, client, dispatcher):
        _running(bridge, scenario)
        job_uuid = scenario["jobs"][0]
        bridge._replay_persisted_job_outcome(job_uuid, _outcome(scenario))
        job = scenario["workflow"].get_job(job_uuid)
        bottle = scenario["prepared"].workflow_snapshot["nodes"][0]["material_uuid"]
        scheduler = bridge._scheduler
        spec = WorkflowSpec(workflow_id=TASK_UUID, nodes=[WorkflowNode(
            id=job["workflow_node_uuid"], job_id=job_uuid,
            device_id="quantity-fixture", action_name="dispense",
            material_requirements=[MaterialRequirement(instance_uuid=bottle)],
        )])
        scheduler.restore_workflow(spec, {}, [DispatchedJob(
            job_id=job_uuid, workflow_id=TASK_UUID,
            node_id=job["workflow_node_uuid"], device_id="quantity-fixture",
            device_action_key="/devices/quantity-fixture/dispense",
        )], execution_facts=[job])
        assert job_uuid in scheduler.snapshot()["inflight_jobs"]
        reservations = scenario["inventory"].query_all(
            "SELECT node_id,status FROM inventory_reservation WHERE workflow_id=? ORDER BY node_id",
            (TASK_UUID,),
        )
        assert len(reservations) == 2
        assert {row["status"] for row in reservations} == {"active"}

        scheduler.on_execution_process_restarted(())

        assert scenario["workflow"].get_task(TASK_UUID)["status"] == "failed"
        retained_reservations = scenario["inventory"].query_all(
            "SELECT node_id,status FROM inventory_reservation WHERE workflow_id=? ORDER BY node_id",
            (TASK_UUID,),
        )
        assert len(retained_reservations) == 2
        assert next(row for row in retained_reservations if row["node_id"] == job_uuid)["status"] == "active"
        # 已保存的设备成功可先结算实例需求；终态通知仍不得把预留直接释放。
        assert all(row["status"] != "released" for row in retained_reservations)
        assert bridge._quantity_inventory.pending_consumption_reconciliations()
        retained = scenario["workflow"].get_job(job_uuid)
        assert retained["status"] == "running"
        assert retained["uncertainty_reason"] == QUANTITY_RECONCILIATION_REQUIRED
        assert {row["state"] for row in bridge._projection.list_execution_locks(job_uuid)} == {"uncertain"}
        assert _quantity(scenario) == 10
        assert dispatcher.dispatched == []

        path = f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity"
        for _ in range(2):
            corrected = client.post(path, json=_correction(scenario))
            assert corrected.status_code == 200
            assert corrected.json()["code"] == 0, corrected.text
        assert scenario["workflow"].get_job(job_uuid)["status"] == "succeeded"
        assert scenario["workflow"].get_task(TASK_UUID)["status"] == "failed"
        assert _quantity(scenario) == 8
        assert len(bridge.list_job_inventory_consumptions(job_uuid)) == 1
        assert not bridge._quantity_inventory.pending_consumption_reconciliations()
        assert not scenario["inventory"].query_all(
            "SELECT node_id FROM inventory_reservation WHERE workflow_id=? AND status='active'",
            (TASK_UUID,),
        )
        assert {row["state"] for row in bridge._projection.list_execution_locks(job_uuid)} == {"released"}
        assert dispatcher.dispatched == []


@pytest.mark.parametrize("execution_process_restarted", [False, True])
def test_quantity_correction_after_bridge_rebuild_preserves_restart_kind(
    tmp_path: Path, execution_process_restarted: bool,
) -> None:
    """数量待核对跨桥重建后，真实重启取消后继；仅调度重建继续原任务。"""

    import json

    from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService

    with _scenario(tmp_path, quantities=(2.0, 2.0)) as scenario:
        store = scenario["workflow"]
        resources = BackendResourceService(scenario["inventory"])
        template = resources.sync_resource_templates([{
            "id": "quantity-fixture.device", "display_name": "数量恢复设备",
            "registry_type": "resource", "class": {},
        }])["templates"][0]
        device = resources.create_material({
            "resource_template_uuid": template["uuid"], "name": "quantity-fixture",
            "barcode": "QUANTITY-RESTORE-DEVICE",
        })
        with scenario["inventory"].transaction() as connection:
            connection.execute("UPDATE material SET type='device' WHERE uuid=?", (device["uuid"],))
        first, second = scenario["jobs"]
        jobs = store.list_jobs(TASK_UUID)
        plan = dict(scenario["prepared"].execution_plan)
        plan["nodes"] = [{
            "uuid": job["workflow_node_uuid"], "kind": "device_action",
            "device_id": "quantity-fixture", "action_name": "dispense",
            "material_uuid": device["uuid"],
            "action_type": "UniLabJsonCommand", "param": {},
            "param_schema": {"type": "object", "properties": {"goal": {"type": "object", "properties": {}}}},
            "topological_index": index,
        } for index, job in enumerate(jobs)]
        plan["edges"] = [{
            "uuid": str(uuid4()), "source_node_uuid": jobs[0]["workflow_node_uuid"],
            "target_node_uuid": jobs[1]["workflow_node_uuid"], "dependency_only": True,
        }]
        with store.transaction() as connection:
            connection.execute("UPDATE workflow_task SET execution_plan=? WHERE uuid=?", (json.dumps(plan), TASK_UUID))
        with _runtime(scenario) as (bridge, _, dispatcher):
            _running(bridge, {**scenario, "jobs": [first]})
            bridge._replay_persisted_job_outcome(first, _outcome(scenario))
            bridge.recover_active_tasks(execution_process_restarted=execution_process_restarted)
            assert store.get_job(second)["status"] == "pending"
            assert dispatcher.dispatched == []
        with _runtime(scenario) as (bridge, client, dispatcher):
            bridge.recover_active_tasks()
            assert store.get_job(second)["status"] == "pending"
            bridge.reconcile_job_quantity(first, **_correction(scenario))
            assert store.get_job(first)["status"] == "succeeded"
            assert _quantity(scenario) == 8
            if execution_process_restarted:
                assert store.get_job(second)["status"] == "canceled", store.get_job(second)["error_info"]
                assert store.get_task(TASK_UUID)["status"] == "failed"
                assert dispatcher.dispatched == []
            else:
                assert store.get_job(second)["status"] == "running", store.get_job(second)["error_info"]
                assert store.get_task(TASK_UUID)["status"] == "running"
                assert [job["job_id"] for job in dispatcher.dispatched] == [second]


def test_completed_quantity_result_recovers_deferred_restart_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更正结果已成功但延迟重启清理崩溃时，新桥仍能发现并完成原清理。"""

    with _scenario(tmp_path) as scenario:
        store = scenario["workflow"]
        job_uuid = scenario["jobs"][0]
        with _runtime(scenario) as (bridge, _, dispatcher):
            _running(bridge, scenario)
            bridge._replay_persisted_job_outcome(job_uuid, _outcome(scenario))
            bridge.recover_active_tasks(execution_process_restarted=True)
            restart = store.get_job(job_uuid)["control_data"]["quantity_reconciliation"]
            assert restart["runtime_restart_pending"] is True
            observed_at = restart["runtime_restart_observed_at"]

            def interrupted_cleanup(*args, **kwargs):
                raise RuntimeError("已提交成功结果但清理尚未完成")

            monkeypatch.setattr(bridge, "_release_runtime_restart_resources", interrupted_cleanup)
            with pytest.raises(RuntimeError, match="清理尚未完成"):
                bridge.reconcile_job_quantity(job_uuid, **_correction(scenario))
            assert store.get_task(TASK_UUID)["status"] == "failed"
            assert store.get_job(job_uuid)["status"] == "succeeded"
            assert store.get_job(job_uuid)["control_data"]["quantity_reconciliation"]["runtime_restart_pending"] is True
            assert _quantity(scenario) == 8
            assert dispatcher.dispatched == []
        with _runtime(scenario) as (bridge, client, dispatcher):
            bridge.recover_active_tasks()
            restored = store.get_job(job_uuid)
            assert restored["status"] == "succeeded"
            assert restored["control_data"]["quantity_reconciliation"]["runtime_restart_pending"] is False
            assert restored["control_data"]["quantity_reconciliation"]["runtime_restart_observed_at"] == observed_at
            assert store.get_task(TASK_UUID)["cleanup_status"] == "settled"
            response = client.post(f"/api/v1/workflow-node-jobs/{job_uuid}/reconcile-quantity", json=_correction(scenario))
            assert response.json()["code"] == 0, response.text
            assert _quantity(scenario) == 8
            assert len(bridge.list_job_inventory_consumptions(job_uuid)) == 1
            assert dispatcher.dispatched == []
