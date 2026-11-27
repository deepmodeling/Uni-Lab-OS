"""物理模式重启保留无 Claim 的手动动作证据，不阻断整个工站启动。"""

from __future__ import annotations

import json

import pytest

from tests.workflow.test_device_action_run_bridge import (
    DEVICE_A_UUID,
    JOB_A_UUID,
    NODE_A_UUID,
    TASK_A_UUID,
    _insert_run,
)
from unilabos.app.scheduler.dispatch import CallbackDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import StoreConflict, WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


@pytest.mark.parametrize("status", ["running", "canceled", "failed", "timeout"])
def test_physical_recovery_keeps_unclaimed_manual_action_without_redispatch(tmp_path, status):
    store = WorkflowStore(tmp_path / "workflow.db")
    dispatched = []
    scheduler = EdgeScheduler(dispatcher=CallbackDispatcher(dispatched.append))
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        _insert_run(store, task_uuid=TASK_A_UUID, job_uuid=JOB_A_UUID,
                    node_uuid=NODE_A_UUID, device_material_uuid=DEVICE_A_UUID)
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_task SET status=?,cleanup_status='pending' WHERE uuid=?",
                         (status, TASK_A_UUID))
            conn.execute("UPDATE workflow_station_control SET mode='PAUSED' WHERE station_id='local'")
            conn.execute("UPDATE workflow_node_job SET execution_source='error_manual',status=?,"
                         "start_state='STARTED',uncertainty_reason='device_cancel_unavailable',"
                         "return_info=?,meta_data=? WHERE uuid=?",
                         (status, json.dumps({"device_feedback": "completion_pending"}),
                          json.dumps({"execution_source": "error_manual"}), JOB_A_UUID))
        before = store.get_job(JOB_A_UUID)
        assert scheduler.physical_dispatch_enabled
        bridge.recover_active_tasks()
        after = store.get_job(JOB_A_UUID)
        assert after["status"] == before["status"]
        assert after["uncertainty_reason"] == before["uncertainty_reason"]
        assert after["return_info"] == before["return_info"]
        assert after["control_data"] == before["control_data"]
        assert after["start_state"] == "STARTED"
        assert bridge._projection.get_execution_claim(JOB_A_UUID) is None
        assert bridge.error_control.snapshot()["mode"] == "PAUSED"
        assert not bridge.error_control.recovering
        assert dispatched == []
    finally:
        bridge.close()
        store.close()


def test_physical_recovery_still_rejects_unclaimed_workflow_with_manual_history(tmp_path):
    """手动重试会保留来源元数据；只有执行来源列才定义无 Claim 手动通道。"""
    store = WorkflowStore(tmp_path / "workflow.db")
    dispatched = []
    scheduler = EdgeScheduler(dispatcher=CallbackDispatcher(dispatched.append))
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        _insert_run(store, task_uuid=TASK_A_UUID, job_uuid=JOB_A_UUID,
                    node_uuid=NODE_A_UUID, device_material_uuid=DEVICE_A_UUID)
        with store.transaction() as conn:
            conn.execute("UPDATE workflow_task SET status='running' WHERE uuid=?", (TASK_A_UUID,))
            conn.execute("UPDATE workflow_node_job SET execution_source='workflow',status='running',"
                         "start_state='STARTED',uncertainty_reason='edge_result_unknown',meta_data=? WHERE uuid=?",
                         (json.dumps({"execution_source": "error_manual"}), JOB_A_UUID))
        with pytest.raises(StoreConflict, match="等待物理结算的作业缺少库存 Claim"):
            bridge.recover_active_tasks()
        assert bridge.error_control.recovering
        assert store.get_job(JOB_A_UUID)["uncertainty_reason"] == "edge_result_unknown"
        assert dispatched == []
    finally:
        bridge.close()
        store.close()


def test_physical_recovery_freezes_existing_manual_claim(tmp_path):
    from tests.workflow.test_device_action_run_bridge import _PermitInventory
    from unilabos.app.scheduler.device_target import ResolvedDeviceTarget

    class RecordingInventory(_PermitInventory):
        def __init__(self):
            self.transitions = []

        def release_unprojected_dispatch_permits(self, *, known_claim_uuids):
            assert known_claim_uuids == ("50000000-0000-4000-8000-000000000001",)
            return ()

        def transition_dispatch_permit(self, claim_uuid, *, target_state):
            super().transition_dispatch_permit(claim_uuid, target_state=target_state)
            self.transitions.append((claim_uuid, target_state))

    store = WorkflowStore(tmp_path / "workflow.db")
    inventory = RecordingInventory()
    first = TaskSchedulerBridge(store, scheduler=EdgeScheduler(
        dispatcher=CallbackDispatcher(lambda payload: None), station_resources=inventory,
        device_target_resolver=lambda selector, _action, _busy: ResolvedDeviceTarget(
            local_device_id=str(selector["local_device_id"]),
            material_uuid=str(selector.get("material_uuid") or ""),
        )))
    try:
        aggregate = _insert_run(store, task_uuid=TASK_A_UUID, job_uuid=JOB_A_UUID,
                                node_uuid=NODE_A_UUID, device_material_uuid=DEVICE_A_UUID)
        first.submit(aggregate["task"])
    finally:
        first.close()
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_station_control SET mode='PAUSED' WHERE station_id='local'")
        conn.execute("UPDATE workflow_node_job SET execution_source='error_manual',"
                     "uncertainty_reason='edge_result_unknown',start_state='STARTED' WHERE uuid=?", (JOB_A_UUID,))
    inventory.transitions.clear()
    dispatched = []
    bridge = TaskSchedulerBridge(store, scheduler=EdgeScheduler(
        dispatcher=CallbackDispatcher(dispatched.append), station_resources=inventory))
    try:
        bridge.recover_active_tasks()
        assert inventory.transitions == [("50000000-0000-4000-8000-000000000001", "uncertain")]
        assert store.get_job(JOB_A_UUID)["uncertainty_reason"] == "edge_result_unknown"
        assert dispatched == []
    finally:
        bridge.close()
        store.close()
