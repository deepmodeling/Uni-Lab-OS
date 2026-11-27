"""人工库存事实与迟到设备结果共存的调度公共入口测试。"""

import json

from tests.app.test_inventory_dispatch_admission import (
    GRIPPER_SITE,
    TARGET_SITE,
    station_inventory,
)
from tests.workflow.test_f05_task_scheduler_bridge import (
    JOB_UUID,
    TASK_UUID,
    _seed_task,
)
from unilabos.app.scheduler.dispatch import CommittedJobOutcome, RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


def test_scheduler_records_success_without_overwriting_manual_inventory(
    tmp_path, station_inventory
):
    inventory_store, inventory, ids = station_inventory
    store = WorkflowStore(tmp_path / "workflow.db")
    _seed_task(store, with_material=False)
    with store.transaction() as conn:
        task = conn.execute(
            "SELECT execution_plan FROM workflow_task WHERE uuid=?", (TASK_UUID,)
        ).fetchone()
        plan = json.loads(task["execution_plan"])
        node = plan["nodes"][0]
        node.update(
            kind="material_transfer",
            material_uuid=ids["robot"],
            device_id="robot",
            param={
                "resource": {"uuid": ids["vessel"]},
                "mount_resource": {"uuid": ids["target_device"]},
                "site_uuid": TARGET_SITE,
            },
            action_resource_contract={
                "version": 1,
                "transfer": {
                    "material_param": "resource",
                    "source_owner_param": "",
                    "source_site_uuid_param": "",
                    "source_site_name_param": "",
                    "target_owner_param": "mount_resource",
                    "target_site_uuid_param": "site_uuid",
                    "target_site_name_param": "",
                    "gripper_site_role": "robot.gripper",
                },
            },
        )
        conn.execute(
            "UPDATE workflow_task SET execution_plan=? WHERE uuid=?",
            (json.dumps(plan), TASK_UUID),
        )
        conn.execute(
            "UPDATE workflow_node_job SET executor_kind='material_transfer',param=? WHERE uuid=?",
            (json.dumps(node["param"]), JOB_UUID),
        )
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
        station_resources=inventory.station_resources,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    try:
        bridge.submit(store.get_task(TASK_UUID))
        assert store.get_job(JOB_UUID)["status"] == "running"
        snapshot = inventory.station_resources.error_handling_inventory()
        instance = next(
            item for item in snapshot["instances"] if item["edge_uuid"] == ids["vessel"]
        )
        site = next(item for item in snapshot["sites"] if item["uuid"] == GRIPPER_SITE)
        inventory.correct_inventory(
            "correct-location",
            {
                "changes": [
                    {
                        "material_uuid": ids["vessel"],
                        "parent_uuid": ids["robot"],
                        "site_uuid": GRIPPER_SITE,
                    }
                ],
                "expected_inventory_versions": {
                    ids["vessel"]: instance["version"],
                    GRIPPER_SITE: str(site["update_time"]),
                },
                "operator_id": "tester",
                "session_id": "session",
                "source_job_uuid": JOB_UUID,
                "reason": "实物已确认",
            },
        )
        outcome = CommittedJobOutcome(
            outcome="succeeded",
            return_info={"device": "completed"},
            error_info=[],
            unknown_command_ids=[],
        )
        scheduler.on_job_outcome(JOB_UUID, outcome)
        first = store.get_job(JOB_UUID)
        assert first["status"] == "succeeded"
        assert first["return_info"] == {"device": "completed"}
        assert (
            first["control_data"]["inventory_reconciliation"]["code"]
            == "inventory_fact_changed"
        )
        assert store.get_task(TASK_UUID)["cleanup_status"] == "requires_attention"
        assert bridge.error_control.snapshot()["mode"] == "RUNNING"
        scheduler.on_job_outcome(JOB_UUID, outcome)
        assert store.get_job(JOB_UUID)["status"] == "succeeded"
        assert (
            next(
                item
                for item in inventory.station_resources.error_handling_inventory()[
                    "instances"
                ]
                if item["edge_uuid"] == ids["vessel"]
            )["parent_uuid"]
            == ids["robot"]
        )
    finally:
        bridge.close()
        store.close()
