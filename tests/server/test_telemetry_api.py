"""``telemetry.v1`` HTTP router 与 Local/HTTP client 契约测试。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.api.telemetry import (
    create_telemetry_router,
    install_telemetry_api,
)
from unilabos.server.clients.telemetry import (
    HTTPTelemetryClient,
    LocalTelemetryClient,
    TelemetryHTTPError,
)
from unilabos.server.protocol.telemetry import (
    DeviceStateSnapshot,
    TelemetryEventQuery,
    TelemetryEventWrite,
    TelemetryIngestRequest,
)
from unilabos.server.services.telemetry import TelemetryService


def _event(
    sequence: int,
    *,
    event_uuid: str | None = None,
    payload: object | None = None,
) -> TelemetryEventWrite:
    return TelemetryEventWrite(
        event_uuid=event_uuid or f"event-{sequence}",
        endpoint_uuid="endpoint",
        device_uuid="device",
        source_epoch="epoch",
        source_generation=1,
        source_sequence=sequence,
        event_type="state",
        payload=payload if payload is not None else {"sequence": sequence},
        observed_at_ms=sequence,
        received_at_ms=sequence + 100,
    )


def _state(sequence: int) -> DeviceStateSnapshot:
    return DeviceStateSnapshot(
        device_uuid="device",
        state={"status": "running"},
        properties={"temperature": sequence},
        connection_state="online",
        observed_at_ms=sequence,
    )


def test_router_install_exposes_ingest_and_all_read_surfaces(tmp_path) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    app = FastAPI()
    install_telemetry_api(app, service)
    request = TelemetryIngestRequest(event=_event(1), device_state=_state(1))
    try:
        with TestClient(app) as client:
            accepted = client.post(
                "/api/v1/telemetry/events",
                json=request.model_dump(mode="json"),
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["event"]["sequence"] == 1

            event = client.get("/api/v1/telemetry/events/event-1")
            cursor = client.get("/api/v1/telemetry/sources/endpoint/cursor")
            state = client.get("/api/v1/telemetry/states/endpoint/device")
            states = client.get(
                "/api/v1/telemetry/states",
                params={"endpoint_uuid": "endpoint"},
            )
            events = client.get(
                "/api/v1/telemetry/events",
                params={"event_type": "state", "limit": 1},
            )

            assert event.status_code == 200
            assert event.json()["event_uuid"] == "event-1"
            assert cursor.json()["source_sequence"] == 1
            assert state.json()["state"] == {"status": "running"}
            assert [item["device_uuid"] for item in states.json()] == ["device"]
            assert [item["event_uuid"] for item in events.json()] == ["event-1"]
    finally:
        service.close()


def test_router_maps_replay_conflict_validation_and_not_found(tmp_path) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    app = FastAPI()
    app.include_router(create_telemetry_router(service))
    request = TelemetryIngestRequest(event=_event(2), device_state=_state(2))
    try:
        with TestClient(app) as client:
            first = client.post(
                "/api/v1/telemetry/events",
                json=request.model_dump(mode="json"),
            )
            replay = client.post(
                "/api/v1/telemetry/events",
                json=request.model_dump(mode="json"),
            )
            conflicting = request.model_copy(
                update={
                    "event": request.event.model_copy(
                        update={"payload": {"different": True}}
                    )
                }
            )
            conflict = client.post(
                "/api/v1/telemetry/events",
                json=conflicting.model_dump(mode="json"),
            )
            invalid_range = client.get(
                "/api/v1/telemetry/events",
                params={"observed_from_ms": 10, "observed_to_ms": 1},
            )

            assert first.status_code == 200
            assert replay.status_code == 200
            assert replay.json()["replayed"] is True
            assert conflict.status_code == 409
            assert invalid_range.status_code == 422
            assert client.get("/api/v1/telemetry/events/missing").status_code == 404
            assert (
                client.get("/api/v1/telemetry/sources/missing/cursor").status_code
                == 404
            )
            assert (
                client.get("/api/v1/telemetry/states/endpoint/missing").status_code
                == 404
            )
    finally:
        service.close()


def test_local_client_exposes_the_same_command_and_reads(tmp_path) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    client = LocalTelemetryClient(service)
    try:
        accepted = client.ingest_event(_event(1), device_state=_state(1))

        assert client.get_event("event-1") == accepted.event
        assert client.get_source_cursor("endpoint") == accepted.cursor
        assert client.get_device_state("endpoint", "device") == (accepted.device_state)
        assert client.list_device_states("endpoint") == [accepted.device_state]
        assert client.query_events(TelemetryEventQuery(endpoint_uuid="endpoint")) == [
            accepted.event
        ]
    finally:
        service.close()


def test_http_client_exports_typed_contract_and_builds_paths(tmp_path) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    accepted = service.ingest_event(_event(1), device_state=_state(1))
    client = HTTPTelemetryClient("http://backend.example/api/v1")
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, body: Any = None) -> Any:
        calls.append((method, path, body))
        if method == "POST":
            return accepted.model_dump(mode="json")
        if path.startswith("/events?"):
            return [accepted.event.model_dump(mode="json")]
        if path.startswith("/events/"):
            return accepted.event.model_dump(mode="json")
        if path.startswith("/sources/"):
            return accepted.cursor.model_dump(mode="json")
        if path.startswith("/states/"):
            return accepted.device_state.model_dump(mode="json")
        return [accepted.device_state.model_dump(mode="json")]

    client._request = fake_request  # type: ignore[method-assign]
    try:
        request = TelemetryIngestRequest(event=_event(1), device_state=_state(1))
        assert client.ingest(request).event == accepted.event
        assert client.get_event("event-1") == accepted.event
        assert client.query_events(endpoint_uuid="endpoint") == [accepted.event]
        assert client.get_source_cursor("endpoint") == accepted.cursor
        assert client.get_device_state("endpoint", "device") == accepted.device_state
        assert client.list_device_states("endpoint") == [accepted.device_state]
        assert client.base_url == "http://backend.example/api/v1/telemetry"
        assert calls[0][:2] == ("POST", "/events")
        assert "endpoint_uuid=endpoint" in calls[2][1]
    finally:
        service.close()


def test_telemetry_http_error_keeps_status_and_detail() -> None:
    error = TelemetryHTTPError(409, "stale source position")

    assert error.status_code == 409
    assert error.detail == "stale source position"
    assert "409" in str(error)
