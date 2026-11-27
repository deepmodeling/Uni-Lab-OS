"""接口文档中的人工上下料、返回处置与恢复流程。"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_inventory_dispatch_admission import (
    SOURCE_SITE,
    TARGET_SITE,
    station_inventory,
)
from tests.workflow.test_f05_task_scheduler_bridge import (
    JOB_UUID,
    TASK_UUID,
    _seed_task,
)
from unilabos.app.error_handling_api import create_error_handling_router
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.error_handling import ErrorHandlingService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge


def test_document_manual_inventory_return_and_resume(
    database_mode, tmp_path, station_inventory
):
    inventory_store, inventory, ids = station_inventory
    store = WorkflowStore(tmp_path / "workflow.db")
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
        station_resources=inventory.station_resources,
    )
    bridge = TaskSchedulerBridge(store, scheduler=scheduler)
    _seed_task(store, with_material=False)
    task, job = TASK_UUID, JOB_UUID
    with store.transaction() as conn:
        plan = json.loads(
            conn.execute(
                "SELECT execution_plan FROM workflow_task WHERE uuid=?", (task,)
            ).fetchone()[0]
        )
        plan["nodes"][0].update(device_id=ids["robot"], material_uuid=ids["robot"])
        conn.execute(
            "UPDATE workflow_task SET execution_plan=? WHERE uuid=?",
            (json.dumps(plan), task),
        )
    bridge.submit(store.get_task(task))
    error = bridge.error_control.report_error(
        job, phase="execution", message="文档复验异常"
    )
    app = FastAPI()
    app.include_router(
        create_error_handling_router(ErrorHandlingService(store, bridge, None)),
        prefix="/api/v1",
    )
    base = "/api/v1/stations/local"
    try:
        with TestClient(app) as c:

            def get(path):
                r = c.get(base + path)
                assert r.status_code == 200, r.text
                return r.json()["data"]

            def post(path, body, key, status=200):
                r = c.post(base + path, json=body, headers={"Idempotency-Key": key})
                assert r.status_code == status, r.text
                return r.json()["data"]

            def versions():
                s = get("/error-handling")
                return {
                    "expected_station_version": s["station_version"],
                    "expected_session_version": s["active_session"]["version"],
                }

            state = get("/error-handling")
            entered = post(
                f"/errors/{error['decision_id']}/decision",
                {
                    "action": "enter_manual_handling",
                    "expected_decision_version": error["decision_version"],
                    "expected_station_version": state["station_version"],
                    "observed_error_epoch": state["error_epoch"],
                },
                "enter",
                202,
            )
            session = entered["station"]["active_session"]["session_id"]
            path = f"/sessions/{session}"
            for i, (parent, site) in enumerate(
                [
                    ("", ""),
                    (ids["target_device"], TARGET_SITE),
                    (ids["source_device"], SOURCE_SITE),
                ]
            ):
                current = get(path + "/inventory")
                material = next(
                    x for x in current["instances"] if x["edge_uuid"] == ids["vessel"]
                )
                expected = {ids["vessel"]: material["version"]}
                for s in current["sites"]:
                    if (
                        s["uuid"] == site
                        or s["occupied_material_uuid"] == ids["vessel"]
                    ):
                        expected[s["uuid"]] = str(s["update_time"])
                body = {
                    **versions(),
                    "changes": [
                        {
                            "material_uuid": ids["vessel"],
                            "parent_uuid": parent,
                            "site_uuid": site,
                        }
                    ],
                    "expected_inventory_versions": expected,
                    "reason": "文档复验：人工确认位置",
                }
                result = post(
                    path + "/inventory-corrections", body, f"correction-{i}", 202
                )
                after = next(
                    x
                    for x in get(path + "/inventory")["instances"]
                    if x["edge_uuid"] == ids["vessel"]
                )
                assert (after.get("parent_uuid") or "") == parent
                occupied = [
                    s["uuid"]
                    for s in get(path + "/inventory")["sites"]
                    if s["occupied_material_uuid"] == ids["vessel"]
                ]
                assert occupied == ([site] if site else [])
                assert (
                    post(path + "/inventory-corrections", body, f"correction-{i}", 202)
                    == result
                )
            get("/resource-dispositions")
            get("/resource-usage")
            post(
                path + "/complete",
                {**versions(), "summary": "位置已恢复"},
                "complete-1",
            )
            post(path + "/return", versions(), "return")
            post(
                path + "/complete", {**versions(), "summary": "复核完成"}, "complete-2"
            )
            state = get("/error-handling")
            error_now = next(
                x for x in state["errors"] if x["decision_id"] == error["decision_id"]
            )
            post(
                f"/errors/{error['decision_id']}/decision",
                {
                    **versions(),
                    "action": "cancel_task",
                    "expected_decision_version": error_now["decision_version"],
                    "observed_error_epoch": state["error_epoch"],
                    "session_id": session,
                },
                "cancel",
                202,
            )
            state = get("/error-handling")
            resumed = post(
                "/resume",
                {
                    "expected_station_version": state["station_version"],
                    "observed_error_epoch": state["error_epoch"],
                    "operator_confirmed": True,
                    "confirm_snapshot_id": state["snapshot_id"],
                },
                "resume",
            )
            assert get("/error-handling")["mode"] == "RUNNING"
    finally:
        bridge.close()
        store.close()
