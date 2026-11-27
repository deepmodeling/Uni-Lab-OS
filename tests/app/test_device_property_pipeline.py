"""真实 HTTP 状态入口到属性历史与推送的持久链路。"""

import asyncio
import json
from threading import Event
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.edge_control.local_authority import (
    LocalEdgeAuthorityStore,
    LocalEdgeControlAuthority,
    create_local_edge_control_router,
)
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.monitor import MonitorBus


def authority(path):
    return LocalEdgeControlAuthority(LocalEdgeAuthorityStore(path), api_key="test-only")


def session(backend):
    registered = backend.store.register_session({
        "edge_key": "test", "instance_uuid": str(uuid4()),
        "devices": [{"local_id": "heater", "actions": []}],
    })
    backend.store.set_session_connected(registered["session_uuid"], True)
    return registered["session_uuid"]


def test_http_report_changes_history_and_sse_only_after_authentication(tmp_path, monkeypatch):
    from unilabos.app.scheduler import monitor

    bus = MonitorBus()
    monkeypatch.setattr(monitor, "monitor_bus", bus)
    backend = authority(tmp_path / "authority.db")
    state = DeviceStateStore(str(tmp_path / "properties.db"))
    backend.configure_device_state(state)
    current = session(backend)
    app = FastAPI()
    app.include_router(create_local_edge_control_router(backend))
    client = TestClient(app)
    url = f"/api/v1/edge/sessions/{current}/devices/heater/status"
    try:
        body = {"online": True, "status": {"temperature": 25.0}}
        assert client.put(url, json=body).status_code == 401
        assert not state.latest_all()
        headers = {"Authorization": "Bearer test-only"}
        for value in (25.0, 25.0, 26.0):
            body["status"]["temperature"] = value
            assert client.put(url, json=body, headers=headers).status_code == 200
        assert [r["value"] for r in state.history("heater", "temperature")] == [26.0, 25.0]
        assert [e["data"]["value"] for e in bus.recent("device", 10)] == [25.0, 26.0]
        before = state.latest_for("heater")
        body["status"] = {"temperature": 27.0, "invalid": {"nested": True}}
        assert client.put(url, json=body, headers=headers).status_code == 409
        assert state.latest_for("heater") == before
        backend.store.set_session_connected(current, False)
        body["status"] = {"temperature": 100.0}
        assert client.put(url, json=body, headers=headers).status_code == 409
        assert state.latest_for("heater") == before
    finally:
        state.close()
        backend.stop()


def test_accepted_properties_survive_backend_restart_and_projection_failure(tmp_path):
    path = tmp_path / "authority.db"
    backend = authority(path)
    current = session(backend)
    for value in (25.0, 26.0):
        backend.store.update_device_status(current, "heater", {
            "online": True, "dispatch_block_reason": "", "unknown_command_ids": [],
            "status": {"temperature": value},
        })
    backend.stop()
    restarted = authority(path)

    class Unavailable:
        def set_many(self, *args, **kwargs):
            raise OSError("database temporarily unavailable")

    restarted.device_state = Unavailable()
    with pytest.raises(OSError):
        restarted.project_device_properties()
    assert len(restarted.store.pending_device_property_reports()) == 2
    state = DeviceStateStore(str(tmp_path / "properties.db"))
    try:
        restarted.configure_device_state(state)
        assert [r["value"] for r in state.history("heater", "temperature")] == [26.0, 25.0]
        assert restarted.project_device_properties() == 0
        assert not restarted.store.pending_device_property_reports()
    finally:
        state.close()
        restarted.stop()


def test_invalid_batch_does_not_partially_write_and_type_changes_are_recorded():
    state = DeviceStateStore()
    try:
        with pytest.raises(TypeError):
            state.set_many("device", {"valid": 3, "invalid": []})
        assert state.latest_all() == {}
        for value in (float("nan"), float("inf")):
            with pytest.raises(ValueError):
                state.set("device", "value", value)
        state.set("device", "value", True)
        assert state.set("device", "value", 1)
        assert type(state.latest_for("device")["value"]["value"]) is int
    finally:
        state.close()


def test_offline_backend_drains_multiple_pages_after_history_recovers(tmp_path, monkeypatch):
    path = tmp_path / "authority.db"
    backend = authority(path)
    current = session(backend)
    report_count = 1005
    for value in range(report_count):
        backend.store.update_device_status(current, "heater", {
            "online": True, "dispatch_block_reason": "", "unknown_command_ids": [],
            "status": {"temperature": value},
        })
    backend.stop()

    class RecoveringHistory(DeviceStateStore):
        available = Event()
        failed = Event()

        def set_many(self, *args, **kwargs):
            if not self.available.is_set():
                self.failed.set()
                raise OSError("temporary history failure")
            return super().set_many(*args, **kwargs)

    recovered = authority(path)
    history = RecoveringHistory(str(tmp_path / "history.db"))
    drained = Event()
    acknowledged_count = 0
    acknowledge = recovered.store.acknowledge_device_property_report

    def acknowledge_and_signal(report_id):
        nonlocal acknowledged_count
        acknowledge(report_id)
        acknowledged_count += 1
        if acknowledged_count == report_count:
            drained.set()

    monkeypatch.setattr(recovered.store, "acknowledge_device_property_report", acknowledge_and_signal)
    try:
        recovered.configure_device_state(history)
        assert history.failed.is_set()
        recovered.start()
        # 恢复期间不注册、不连接、不再上报；后端消费者必须自行重试并翻页。
        history.available.set()
        assert drained.wait(timeout=30), f"恢复后只确认了 {acknowledged_count}/{report_count} 条属性报告"
        assert not recovered.store.pending_device_property_reports()
        assert history.latest_for("heater")["temperature"]["value"] == report_count - 1
        assert len(history.history("heater", "temperature", limit=1100)) == 1000
    finally:
        recovered.stop()
        history.close()


def test_unavailable_history_does_not_block_acceptance_or_job_recovery(tmp_path):
    class Unavailable:
        def set_many(self, *args, **kwargs):
            raise OSError("database temporarily unavailable")

    backend = authority(tmp_path / "authority.db")
    backend.configure_device_state(Unavailable())
    state = DeviceStateStore()
    app = FastAPI()
    app.include_router(create_local_edge_control_router(backend))
    try:
        response = TestClient(app).put(
            f"/api/v1/edge/sessions/{session(backend)}/devices/heater/status",
            headers={"Authorization": "Bearer test-only"},
            json={"online": True, "status": {"temperature": 26.5}},
        )
        assert response.status_code == 200
        assert backend.replay_pending_projections() == {"feedback": 0, "outcomes": 0}
        assert len(backend.store.pending_device_property_reports()) == 1
        backend.configure_device_state(state)
        assert state.latest_for("heater")["temperature"]["value"] == 26.5
        assert not backend.store.pending_device_property_reports()
    finally:
        state.close()
        backend.stop()


def test_explicitly_disabled_history_does_not_accumulate_pending_reports(tmp_path):
    backend = authority(tmp_path / "authority.db")
    backend.configure_device_state(None)
    try:
        backend.store.update_device_status(session(backend), "heater", {
            "online": True, "dispatch_block_reason": "", "unknown_command_ids": [],
            "status": {"temperature": 25.0},
        })
        assert not backend.store.pending_device_property_reports()
    finally:
        backend.stop()


def test_edge_property_reaches_monitor_sse_wire(tmp_path, monkeypatch):
    from unilabos.app.scheduler import api, monitor

    bus = MonitorBus()
    monkeypatch.setattr(monitor, "monitor_bus", bus)
    monkeypatch.setattr(api, "monitor_bus", bus)
    backend = authority(tmp_path / "authority.db")
    state = DeviceStateStore()
    backend.configure_device_state(state)
    app = api.create_app(device_state=state)
    app.include_router(create_local_edge_control_router(backend))
    current = session(backend)
    frames = []

    async def read_stream():
        disconnected = asyncio.Event()

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
            body = message.get("body", b"")
            frames.append(body)
            if b"event: device\n" in body:
                disconnected.set()

        await asyncio.wait_for(app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/api/v1/monitor/events", "root_path": "",
            "query_string": b"channels=device", "headers": [],
            "client": ("127.0.0.1", 1234), "server": ("test", 80),
        }, receive, send), timeout=2)

    try:
        response = TestClient(app).put(
            f"/api/v1/edge/sessions/{current}/devices/heater/status",
            headers={"Authorization": "Bearer test-only"},
            json={"online": True, "status": {"temperature": 26.5}},
        )
        assert response.status_code == 200
        asyncio.run(read_stream())
        event = next(frame for frame in frames if b"event: device\n" in frame)
        payload = json.loads(event.decode().split("data: ", 1)[1])
        assert payload["type"] == "device_property"
        assert payload["data"] == {"device_id": "heater", "property": "temperature", "value": 26.5}
        assert bus.subscriber_count == 0
    finally:
        state.close()
        backend.stop()
