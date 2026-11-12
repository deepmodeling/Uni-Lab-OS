"""V1 调度观测合同：reschedule、timeline、time-plan（无引擎形状）、monitor snapshot / events。"""

from __future__ import annotations

import pytest

from tests.api_envelope import error_of, ok_data
from tests.server.v1_lifecycle_support import Stack, wait_for


@pytest.fixture
def stack(monkeypatch):
    value = Stack()
    monkeypatch.setattr("unilabos.server.api.runtime.scheduling_v1._default_scheduler", lambda: value.scheduler)
    yield value
    value.close()


def test_time_plan_returns_the_v1_no_engine_shape(stack: Stack) -> None:
    assert ok_data(stack.client.get("/api/v1/scheduler/time-plan")) == {"enabled": False,
                                                                        "algorithm": "stable-ready-order"}


def test_timeline_reports_running_and_completed_with_observed_estimates(stack: Stack) -> None:
    first = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    stack.executor.finish(0)
    wait_for(lambda: stack.task(first)["status"] == "succeeded")
    stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 2)
    timeline = ok_data(stack.client.get("/api/v1/timeline", params={"window_s": 600}))
    assert set(timeline) == {"now", "window_s", "running", "completed", "time_schedule", "estimator"}
    assert timeline["time_schedule"] == {"enabled": False, "algorithm": "stable-ready-order"}
    assert timeline["estimator"]["mode"] == "observed" and timeline["estimator"]["default_s"] is None
    running = timeline["running"][0]
    assert running["device_id"] == "device-a" and running["action_name"] == "record"
    assert running["device_action_key"] == "device-a:record" and running["elapsed_s"] >= 0
    assert running["estimate_source"] in {"observed", "unknown"}
    assert any(entry["workflow_id"] == first["uuid"] and entry["state"] == "success"
               for entry in timeline["completed"])


def test_reschedule_requires_a_local_scheduler(stack: Stack, monkeypatch) -> None:
    result = ok_data(stack.client.post("/api/v1/reschedule"))
    assert result == {"dispatched": []}
    snapshot = ok_data(stack.client.get("/api/v1/monitor/snapshot"))
    assert snapshot["scheduler"]["reschedule_count"] == 1
    monkeypatch.setattr("unilabos.server.api.runtime.scheduling_v1._default_scheduler", lambda: None)
    error_of(stack.client.post("/api/v1/reschedule"), 5001)


def test_monitor_snapshot_and_event_backlog(stack: Stack) -> None:
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    snapshot = ok_data(stack.client.get("/api/v1/monitor/snapshot"))
    assert set(snapshot) == {"now", "devices", "scheduler", "recent"}
    assert set(snapshot["recent"]) == {"material", "device", "action", "scheduler"}
    assert snapshot["scheduler"]["inflight"] == 1 and snapshot["scheduler"]["workflow_states"]["running"] == 1
    busy = next(device for device in snapshot["devices"] if device["device_id"] == "device-a")
    assert busy["status"] == "busy" and busy["workflow_id"] == task["uuid"]
    assert snapshot["recent"]["action"] and snapshot["recent"]["action"][-1]["type"].startswith("job_")

    response = stack.client.get("/api/v1/monitor/events", params={"channels": "action", "follow": "false"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    frames = [block for block in response.text.split("\n\n") if block.startswith("id: ")]
    assert frames and all("event: action" in block for block in frames)


def test_settle_material_transfer_maps_to_physical_recovery(stack: Stack, monkeypatch) -> None:
    from uuid import uuid4

    calls = []

    class Recovery:
        def get(self, job_uuid):
            return {"version": 3, "job_uuid": job_uuid}

        def confirm_terminal_settlement(self, job_uuid, expected_version, reason, result):
            calls.append((job_uuid, expected_version, reason, result))
            return {"settled": True}

    from types import SimpleNamespace

    monkeypatch.setattr("unilabos.server.api.runtime.scheduling_v1._default_scheduler",
                        lambda: SimpleNamespace(_physical_recovery=Recovery()))
    task = stack.create_task(["device-a"])
    wait_for(lambda: len(stack.executor.dispatched) == 1)
    job_uuid = stack.executor.dispatched[0]["job_id"]
    body = {"actual_change_set": {"kind": "material_transfer", "material_uuid": "vial-1",
                                  "target_owner_material_uuid": "rack-1", "target_site_uuid": "site-2"},
            "reason": "现场核实在 rack-1/site-2"}
    result = ok_data(stack.client.post(f"/api/v1/workflow-node-jobs/{job_uuid}/settle-material-transfer", json=body))
    assert result == {"job_uuid": job_uuid, "settlement": {"settled": True}}
    assert calls == [(job_uuid, 3, body["reason"], {"material_transfer_receipts": [
        {"material_uuid": "vial-1", "target_owner_uuid": "rack-1", "target_site_uuid": "site-2"}]})]
    error_of(stack.client.post(f"/api/v1/workflow-node-jobs/{job_uuid}/settle-material-transfer",
                               json={**body, "actual_change_set": {**body["actual_change_set"], "kind": "x"}}), 1000)
    error_of(stack.client.post(f"/api/v1/workflow-node-jobs/{uuid4()}/settle-material-transfer", json=body), 3002)
    assert task["uuid"]
