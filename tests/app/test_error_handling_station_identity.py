"""中心注册身份与本地错误处置事实共用 HTTP 入口。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.error_handling_api import create_error_handling_router
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.persistence.inspection import table_exists
from unilabos.workflow.error_control import StationExecutionControl
from unilabos.workflow.error_handling import ErrorHandlingService
from unilabos.workflow.store import WorkflowStore, utc_now
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

STATION_UUID = "11111111-1111-4111-8111-111111111111"
INSTANCE_UUID = "22222222-2222-4222-8222-222222222222"
OTHER_UUID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def error_runtime(tmp_path):
    path = tmp_path / "workflow.db"
    store = WorkflowStore(path)
    bridge = TaskSchedulerBridge(
        store, scheduler=EdgeScheduler(dispatcher=RecordingDispatcher())
    )
    application = FastAPI()
    application.include_router(
        create_error_handling_router(ErrorHandlingService(store, bridge, None)),
        prefix="/api/v1",
    )
    try:
        with TestClient(application) as client:
            yield path, store, bridge, client
    finally:
        bridge.close()
        store.close()


def seed_failed_job(store, control):
    """按正常任务、作业事实准备一个尚待人工决定的错误。"""
    workflow, task, node, job = (str(uuid4()) for _ in range(4))
    store.create_workflow(
        workflow_uuid=workflow, name="工站身份", tags=[], description=None, meta_data={}
    )
    now = utc_now()
    with store.transaction() as conn:
        conn.execute(
            """INSERT INTO workflow_task(uuid,create_time,update_time,meta_data,
            workflow_uuid,status,workflow_snapshot,execution_plan,run_mode,control_status,
            cleanup_status,trace_context,input,output,error_info)
            VALUES(?,?,?,'{}',?,'running','{}','{}','normal','active','none','{}','{}','{}','[]')""",
            (task, now, now, workflow),
        )
        conn.execute(
            """INSERT INTO workflow_node_job(uuid,create_time,update_time,meta_data,
            workflow_task_uuid,workflow_node_uuid,feedback_sequence,topological_index,
            executor_kind,execution_policy,execution_timeout_seconds,status,attempt,param,
            feedback_data,return_info,control_data,error_info)
            VALUES(?,?,?,'{}',?,?,0,0,'device_action','{}',0,'running',1,'{}','{}','{}','{}','[]')""",
            (job, now, now, task, node),
        )
    return control.report_error(job, phase="execution", message="设备离线")


def register_center_identity(path):
    """通过正式中心账本初始化入口保存身份，然后关闭该连接。"""
    DurableCenterStore(
        path, station_uuid=STATION_UUID, instance_uuid=INSTANCE_UUID
    ).close()


def test_registered_station_reads_the_existing_local_error_snapshot(error_runtime):
    path, store, bridge, client = error_runtime
    error = seed_failed_job(store, bridge.error_control)
    local = client.get("/api/v1/stations/local/error-handling")
    assert local.status_code == 200
    assert local.json()["data"]["errors"][0]["decision_id"] == error["decision_id"]
    register_center_identity(path)

    registered = client.get(f"/api/v1/stations/{STATION_UUID}/error-handling")

    assert registered.status_code == 200
    assert registered.json() == local.json()
    assert bridge.error_control.station_id == "local"
    assert client.get(f"/api/v1/stations/{OTHER_UUID}/error-handling").status_code == 404
    with store.read() as conn:
        assert [row[0] for row in conn.execute(
            "SELECT station_id FROM workflow_station_control"
        )] == ["local"]
        assert conn.execute(
            "SELECT station_id FROM workflow_intervention WHERE decision_id=?",
            (error["decision_id"],),
        ).fetchone()[0] == "local"


def test_registered_station_decision_updates_the_same_local_control(error_runtime):
    path, store, bridge, client = error_runtime
    error = seed_failed_job(store, bridge.error_control)
    register_center_identity(path)
    snapshot = client.get("/api/v1/stations/local/error-handling").json()["data"]
    request = {
        "action": "enter_manual_handling",
        "expected_station_version": snapshot["station_version"],
        "expected_decision_version": error["decision_version"],
        "observed_error_epoch": snapshot["error_epoch"],
    }
    suffix = f"/errors/{error['decision_id']}/decision"
    headers = {"Idempotency-Key": "center-manual-choice"}
    rejected = client.post(
        f"/api/v1/stations/{OTHER_UUID}{suffix}", json=request, headers=headers
    )
    assert rejected.status_code == 404
    assert bridge.error_control.snapshot() == snapshot

    accepted = client.post(
        f"/api/v1/stations/{STATION_UUID}{suffix}", json=request, headers=headers
    )

    assert accepted.status_code == 202
    result = accepted.json()["data"]
    assert result["status"] == "APPLIED"
    local = client.get("/api/v1/stations/local/error-handling").json()["data"]
    registered = client.get(
        f"/api/v1/stations/{STATION_UUID}/error-handling"
    ).json()["data"]
    assert local == registered == result["station"]
    assert local["mode"] == "PAUSED"
    assert local["active_session"]["stage"] == "MANUAL_HANDLING"
    replayed = client.post(
        f"/api/v1/stations/local{suffix}", json=request, headers=headers
    )
    assert replayed.status_code == 202
    assert replayed.json()["data"]["command_uuid"] == result["command_uuid"]
    with store.read() as conn:
        assert conn.execute(
            "SELECT station_id FROM workflow_station_control_command WHERE command_uuid=?",
            (result["command_uuid"],),
        ).fetchone()[0] == "local"


@pytest.mark.parametrize("station_id", ["local", "configured-edge"])
def test_standalone_only_accepts_its_configured_local_identity(tmp_path, station_id):
    store = WorkflowStore(tmp_path / "workflow.db")
    control = StationExecutionControl(store, station_id=station_id)
    application = FastAPI()
    application.include_router(
        create_error_handling_router(SimpleNamespace(control=control)),
        prefix="/api/v1",
    )
    try:
        with TestClient(application) as client:
            assert client.get(
                f"/api/v1/stations/{station_id}/error-handling"
            ).status_code == 200
            assert client.get(
                f"/api/v1/stations/{STATION_UUID}/error-handling"
            ).status_code == 404
            assert client.get("/api/v1/stations/unknown/error-handling").status_code == 404
            if station_id != "local":
                assert client.get("/api/v1/stations/local/error-handling").status_code == 404
        with store.read() as conn:
            assert not table_exists(conn, "center_runtime_state")
    finally:
        store.close()
