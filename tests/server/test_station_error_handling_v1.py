"""V1 工站错误处置合同：错误快照、进入手动处理、手动动作、核对、完成会话、重试并恢复。"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from tests.api_envelope import error_of, ok_data
from tests.server.v1_lifecycle_support import Stack, wait_for

BASE = "/api/v1/stations/local"


@pytest.fixture
def stack():
    value = Stack()
    delivered = []
    value.service._intervention_delivery = lambda decision_id, payload: delivered.append((decision_id, payload)) or True
    value.delivered = delivered
    value.service.station_handling._action_catalog = lambda device_id: [{
        "action_id": "home", "action_version": "sha256:home-v1", "name": "home", "display_name": "回原点",
        "action_type": "UniLabJsonCommand", "parameter_schema": {"type": "object"}, "defaults": {},
        "device_binding": {"material_uuid": device_id, "edge_local_id": device_id},
    }]
    yield value
    value.close()


def _post(stack, path, body, key=None):
    return stack.client.post(f"{BASE}{path}", json=body, headers={"Idempotency-Key": key or str(uuid4())})


def _fail(stack, task):
    job_uuid = stack.executor.dispatched[0]["job_id"]
    stack.service.mark_workflow_node_job_decision_pending(job_uuid, {
        "decision_id": "station-decision-1", "job_id": job_uuid, "task_id": task["uuid"], "device_id": "device-a",
        "error_message": "移液头堵塞", "options": [{"action": "retry"}, {"action": "abort"}],
    })
    return job_uuid


def test_station_snapshot_lists_the_error_and_rejects_unknown_station(stack: Stack) -> None:
    empty = ok_data(stack.client.get(f"{BASE}/error-handling"))
    assert empty["mode"] == "RUNNING" and empty["errors"] == [] and empty["active_session"] is None
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    job_uuid = _fail(stack, task)
    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    assert station["station_id"] == "local" and station["error_epoch"] == empty["error_epoch"] + 1
    assert station["snapshot_id"] == f"local:{station['station_version']}:{station['error_epoch']}"
    error = station["errors"][0]
    assert (error["decision_id"], error["source_task_uuid"], error["source_job_uuid"], error["stage"]) == (
        "station-decision-1", task["uuid"], job_uuid, "DECISION_REQUIRED")
    assert error["meta_data"]["message"] == "移液头堵塞" and error["decision_version"] == 1
    assert {"control_commands", "manual_actions", "event_cursor", "station_version"} <= set(station)
    error_of(stack.client.get("/api/v1/stations/remote/error-handling"), 4001)


def test_manual_handling_session_then_retry_and_resume(stack: Stack) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    _fail(stack, task)
    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    error = station["errors"][0]
    body = {"expected_station_version": station["station_version"], "action": "enter_manual_handling",
            "expected_decision_version": error["decision_version"], "observed_error_epoch": station["error_epoch"]}
    error_of(stack.client.post(f"{BASE}/errors/{error['decision_id']}/decision", json=body), 1000)  # 缺 Idempotency-Key
    stale = {**body, "expected_station_version": station["station_version"] + 5}
    error_of(_post(stack, f"/errors/{error['decision_id']}/decision", stale), 3003)
    response = _post(stack, f"/errors/{error['decision_id']}/decision", body, key="enter-1")
    assert response.status_code == 202
    entered = ok_data(response, status=202)
    assert entered["status"] == "APPLIED" and entered["result"]["action"] == "enter_manual_handling"
    session_id = entered["result"]["session_id"]
    assert entered["station"]["mode"] == "PAUSED" and entered["station"]["active_session_id"] == session_id
    replay = ok_data(_post(stack, f"/errors/{error['decision_id']}/decision", body, key="enter-1"), status=202)
    assert replay["command_uuid"] == entered["command_uuid"]
    error_of(_post(stack, f"/errors/{error['decision_id']}/decision", {**body, "reason": "x"}, key="enter-1"), 3003)
    assert ok_data(stack.client.get(f"{BASE}/control-commands/{entered['command_uuid']}"))["status"] == "APPLIED"

    # 工站暂停：普通任务不派发
    other = stack.create_task(["device-b"])
    time.sleep(0.15)
    assert len(stack.executor.dispatched) == 1

    session = ok_data(stack.client.get(f"{BASE}/sessions/{session_id}"))
    assert session["stage"] == "MANUAL_HANDLING" and session["decision"]["decision_id"] == error["decision_id"]
    catalog = ok_data(stack.client.get(f"{BASE}/sessions/{session_id}/devices/device-a/actions"))
    assert catalog[0]["action_id"] == "home"

    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    manual = {"expected_station_version": station["station_version"], "expected_session_version": session["version"],
              "device_id": "device-a", "action_id": "home", "action_version": "sha256:home-v1", "parameters": {}}
    error_of(_post(stack, f"/sessions/{session_id}/manual-actions", {**manual, "action_version": "old"}), 3003)
    executed = ok_data(_post(stack, f"/sessions/{session_id}/manual-actions", manual, key="manual-1"), status=202)
    command_id = executed["result"]["manual_command_id"]
    assert executed["manual_action"]["execution_job"]["execution_source"] == "error_manual"
    # 手动动作在暂停中照常派发；普通任务仍在等待
    wait_for(lambda: len(stack.executor.dispatched) == 2)
    assert stack.executor.dispatched[1]["action"] == "home"
    detail = ok_data(stack.client.get(f"{BASE}/manual-actions/{command_id}"))
    assert detail["source_task_uuid"] == task["uuid"] and detail["execution_job"]["meta_data"]["session_id"] == session_id

    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    complete = {"expected_station_version": station["station_version"], "expected_session_version": session["version"],
                "summary": "已清理"}
    error_of(_post(stack, f"/sessions/{session_id}/complete", complete), 3003)  # 手动动作还在执行
    stack.executor.finish(1)
    wait_for(lambda: ok_data(stack.client.get(f"{BASE}/manual-actions/{command_id}"))["execution_job"]["status"] == "succeeded")
    context = ok_data(stack.client.get(f"{BASE}/manual-action-jobs/{detail['execution_job']['uuid']}/review"))
    assert context["session_id"] == session_id and context["result_reviewed"] is False
    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    completed = ok_data(_post(stack, f"/sessions/{session_id}/complete",
                              {**complete, "expected_station_version": station["station_version"]}))
    assert completed["status"] == "APPLIED"
    session = ok_data(stack.client.get(f"{BASE}/sessions/{session_id}"))
    assert session["stage"] == "DECISION_REQUIRED" and session["summary"] == "已清理"

    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    error = station["errors"][0]
    decision = {"expected_station_version": station["station_version"], "action": "retry_current_node",
                "expected_decision_version": error["decision_version"], "observed_error_epoch": station["error_epoch"],
                "session_id": session_id, "expected_session_version": session["version"],
                "resume_requested": True, "operator_confirmed": True, "confirm_snapshot_id": station["snapshot_id"]}
    applied = ok_data(_post(stack, f"/errors/{error['decision_id']}/decision", decision), status=202)
    assert applied["status"] == "APPLIED" and applied["result"]["application_started"] is True
    assert stack.delivered[-1][0] == "station-decision-1" and stack.delivered[-1][1]["action"] == "retry"
    assert applied["station"]["mode"] == "RUNNING" and applied["station"]["errors"] == []
    assert applied["station"]["active_session_id"] is None
    # 恢复后普通任务继续派发
    wait_for(lambda: any(item["task_id"] == other["uuid"] for item in stack.executor.dispatched))

    events = ok_data(stack.client.get(f"{BASE}/events"))
    assert events["reset_required"] is False and events["items"]
    assert {item["event"] for item in events["items"]} == {"station.error_handling.changed"}
    later = ok_data(stack.client.get(f"{BASE}/events", params={"after": events["next_cursor"]}))
    assert later["items"] == []


def test_cancel_decision_without_session_and_undelivered_command_can_be_reapplied(stack: Stack) -> None:
    accept = {"value": False}
    stack.service._intervention_delivery = lambda decision_id, payload: accept["value"]
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    _fail(stack, task)
    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    error = station["errors"][0]
    body = {"expected_station_version": station["station_version"], "action": "cancel_task",
            "expected_decision_version": error["decision_version"], "observed_error_epoch": station["error_epoch"]}
    pending = ok_data(_post(stack, f"/errors/{error['decision_id']}/decision", body), status=202)
    assert pending["status"] == "PENDING" and pending["application_error"]
    assert pending["station"]["errors"][0]["stage"] == "APPLYING"
    assert any(item["needs_apply"] for item in pending["station"]["control_commands"])
    accept["value"] = True
    applied = ok_data(stack.client.post(f"{BASE}/control-commands/{pending['command_uuid']}/apply"), status=202)
    assert applied["status"] == "APPLIED"
    wait_for(lambda: stack.task(task)["status"] in {"canceling", "canceled"})


def test_resume_and_resource_views(stack: Stack) -> None:
    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    body = {"expected_station_version": station["station_version"], "observed_error_epoch": station["error_epoch"],
            "operator_confirmed": True, "confirm_snapshot_id": "stale"}
    error_of(_post(stack, "/resume", body), 3003)
    resumed = ok_data(_post(stack, "/resume", {**body, "confirm_snapshot_id": station["snapshot_id"]}))
    assert resumed["station"]["mode"] == "RUNNING" and resumed["result"] == {"resume_requested": True}
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    usage = ok_data(stack.client.get(f"{BASE}/resource-usage"))
    assert set(usage) == {"captured_at", "occupancies", "material_bindings", "jobs"}
    assert any(job["task_uuid"] == task["uuid"] for job in usage["jobs"])
    occupancies = ok_data(stack.client.get(f"{BASE}/resource-dispositions"))
    assert occupancies and occupancies[0]["occupancy_kind"] == "execution_lock"
    station = ok_data(stack.client.get(f"{BASE}/error-handling"))
    release = {"expected_station_version": station["station_version"], "decision_id": "missing",
               "occupancy_ids": [occupancies[0]["uuid"]], "expected_versions": {occupancies[0]["uuid"]: "x"},
               "operation": "release", "reason": "核实"}
    error_of(_post(stack, "/resource-dispositions", release), 4001)
    error_of(stack.client.get(f"{BASE}/sessions/{uuid4()}"), 4001)
    error_of(stack.client.get(f"{BASE}/control-commands/{uuid4()}"), 4001)
