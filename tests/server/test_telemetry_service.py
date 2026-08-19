"""新 telemetry authority 的 cursor、latest 和事件流测试。"""

from __future__ import annotations

import pytest

from unilabos.server.protocol.telemetry import (
    DeviceStateSnapshot,
    TelemetryEventQuery,
    TelemetryEventWrite,
)
from unilabos.server.services.telemetry import (
    StaleTelemetryError,
    TelemetryConflictError,
    TelemetryService,
    TelemetryValidationError,
)


def _event(
    sequence: int,
    *,
    event_uuid: str | None = None,
    epoch: str = "epoch-1",
    generation: int = 1,
    event_type: str = "state",
    payload: object | None = None,
) -> TelemetryEventWrite:
    return TelemetryEventWrite(
        event_uuid=event_uuid or f"event-{epoch}-{generation}-{sequence}",
        endpoint_uuid="endpoint",
        device_uuid="device",
        source_epoch=epoch,
        source_generation=generation,
        source_sequence=sequence,
        event_type=event_type,
        payload=payload if payload is not None else {"sequence": sequence},
        observed_at_ms=sequence,
        received_at_ms=sequence + 100,
    )


def _state(value: str, *, observed_at_ms: int) -> DeviceStateSnapshot:
    return DeviceStateSnapshot(
        device_uuid="device",
        state={"status": value},
        properties={"temperature": observed_at_ms},
        connection_state="online",
        alarms=[],
        observed_at_ms=observed_at_ms,
    )


def test_ingest_atomically_advances_cursor_appends_event_and_upserts_latest(
    tmp_path,
) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    try:
        first = service.ingest_event(
            _event(1), device_state=_state("idle", observed_at_ms=1)
        )
        second = service.ingest_event(
            _event(2), device_state=_state("running", observed_at_ms=2)
        )

        assert first.event.sequence == 1
        assert second.event.sequence == 2
        assert second.cursor.source_sequence == 2
        assert second.cursor.last_event_uuid == second.event.event_uuid
        assert second.cursor.version == 2
        assert second.device_state is not None
        assert second.device_state.state == {"status": "running"}
        assert second.device_state.properties == {"temperature": 2}
        assert second.device_state.version == 2
        assert service.get_device_state("endpoint", "device") == (second.device_state)
    finally:
        service.close()


def test_exact_event_retry_replays_without_new_row_or_version(tmp_path) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    try:
        event = _event(1, event_uuid="stable-event")
        state = _state("idle", observed_at_ms=1)
        accepted = service.ingest_event(event, device_state=state)
        retry = service.ingest_event(
            event.model_copy(update={"received_at_ms": 999}),
            device_state=state,
        )

        assert retry.replayed is True
        assert retry.event.sequence == accepted.event.sequence
        assert retry.cursor.version == 1
        assert retry.device_state is not None
        assert retry.device_state.version == 1
        assert len(service.query_events()) == 1
    finally:
        service.close()


def test_invalid_latest_snapshot_rolls_back_event_and_cursor(tmp_path) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    try:
        invalid_state = _state("idle", observed_at_ms=1).model_copy(
            update={"state_hash": "wrong-hash"}
        )

        with pytest.raises(TelemetryValidationError, match="state_hash"):
            service.ingest_event(_event(1), device_state=invalid_state)

        assert service.query_events() == []
        assert service.get_source_cursor("endpoint") is None
        assert service.get_device_state("endpoint", "device") is None
    finally:
        service.close()


def test_duplicate_event_or_source_position_with_different_content_is_rejected(
    tmp_path,
) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    try:
        service.ingest_event(_event(1, event_uuid="event"))

        with pytest.raises(TelemetryConflictError, match="event_uuid"):
            service.ingest_event(
                _event(1, event_uuid="event", payload={"different": True})
            )
        with pytest.raises(TelemetryConflictError, match="source position"):
            service.ingest_event(_event(1, event_uuid="other-event"))

        assert len(service.query_events()) == 1
        assert service.get_source_cursor("endpoint").source_sequence == 1
    finally:
        service.close()


def test_stale_sequence_is_rejected_but_generation_and_new_epoch_can_restart(
    tmp_path,
) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    try:
        service.ingest_event(_event(3))
        with pytest.raises(StaleTelemetryError, match="monotonically"):
            service.ingest_event(_event(2))

        next_generation = service.ingest_event(_event(0, generation=2))
        assert (
            next_generation.cursor.source_generation,
            next_generation.cursor.source_sequence,
        ) == (
            2,
            0,
        )

        next_epoch = service.ingest_event(_event(0, epoch="epoch-2", generation=0))
        assert next_epoch.cursor.source_epoch == "epoch-2"
        with pytest.raises(StaleTelemetryError, match="superseded"):
            service.ingest_event(_event(4, epoch="epoch-1", generation=2))
    finally:
        service.close()


def test_event_query_filters_append_stream_and_pages_by_database_sequence(
    tmp_path,
) -> None:
    service = TelemetryService(tmp_path / "telemetry.db")
    try:
        service.ingest_event(_event(1, event_type="state"))
        service.ingest_event(_event(2, event_type="alarm"))
        service.ingest_event(_event(3, event_type="state"))

        alarms = service.query_events(TelemetryEventQuery(event_type="alarm"))
        page = service.query_events(after_sequence=1, limit=1)

        assert [item.event_type for item in alarms] == ["alarm"]
        assert [item.source_sequence for item in page] == [2]
        assert [item.sequence for item in service.query_events()] == [1, 2, 3]
    finally:
        service.close()
