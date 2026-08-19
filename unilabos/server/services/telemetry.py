"""以 ``telemetry.db`` 为唯一权威的设备遥测服务。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from unilabos.server.models.telemetry import (
    DeviceStateLatestRecord,
    TelemetryEventRecord,
    TelemetrySourceCursorRecord,
)
from unilabos.server.protocol.common import canonical_hash
from unilabos.server.protocol.telemetry import (
    DeviceStateSnapshot,
    TelemetryEventQuery,
    TelemetryEventWrite,
    TelemetryIngestRequest,
    TelemetryIngestResult,
)
from unilabos.server.repositories.telemetry import TelemetryRepository


class TelemetryServiceError(RuntimeError):
    code = "telemetry_error"


class TelemetryConflictError(TelemetryServiceError):
    code = "conflict"


class StaleTelemetryError(TelemetryConflictError):
    code = "stale_source_position"


class TelemetryValidationError(TelemetryServiceError):
    code = "invalid_telemetry"


class TelemetryService:
    """原子追加 event、推进 source cursor，并按需更新最新设备快照。"""

    def __init__(self, repository: TelemetryRepository | str | Path):
        self.repository = (
            repository
            if isinstance(repository, TelemetryRepository)
            else TelemetryRepository(repository)
        )

    def close(self) -> None:
        self.repository.close()

    @staticmethod
    def _event_record(event: TelemetryEventWrite) -> TelemetryEventRecord:
        payload_hash = canonical_hash(event.payload)
        if event.payload_hash is not None and event.payload_hash != payload_hash:
            raise TelemetryValidationError("payload_hash does not match event payload")
        values = event.model_dump(mode="json", exclude={"payload_hash"})
        return TelemetryEventRecord(**values, payload_hash=payload_hash)

    @staticmethod
    def _same_event(
        existing: TelemetryEventRecord, incoming: TelemetryEventRecord
    ) -> bool:
        ignored = {"sequence", "received_at_ms"}
        return existing.model_dump(mode="json", exclude=ignored) == (
            incoming.model_dump(mode="json", exclude=ignored)
        )

    @staticmethod
    def _state_hash(snapshot: DeviceStateSnapshot) -> str:
        return canonical_hash(
            {
                "state": snapshot.state,
                "properties": snapshot.properties,
                "connection_state": snapshot.connection_state,
                "alarms": snapshot.alarms,
            }
        )

    def _state_record(
        self,
        event: TelemetryEventRecord,
        snapshot: DeviceStateSnapshot,
    ) -> DeviceStateLatestRecord:
        state_hash = self._state_hash(snapshot)
        if snapshot.state_hash is not None and snapshot.state_hash != state_hash:
            raise TelemetryValidationError(
                "state_hash does not match the complete device snapshot"
            )
        previous = self.repository.get_device_state(
            event.endpoint_uuid, snapshot.device_uuid
        )
        return DeviceStateLatestRecord(
            endpoint_uuid=event.endpoint_uuid,
            device_uuid=snapshot.device_uuid,
            source_event_uuid=event.event_uuid,
            source_epoch=event.source_epoch,
            source_generation=event.source_generation,
            source_sequence=event.source_sequence,
            state=snapshot.state,
            properties=snapshot.properties,
            connection_state=snapshot.connection_state,
            alarms=snapshot.alarms,
            state_hash=state_hash,
            observed_at_ms=snapshot.observed_at_ms,
            received_at_ms=event.received_at_ms,
            version=1 if previous is None else previous.version + 1,
        )

    def _validate_source_position(
        self,
        event: TelemetryEventRecord,
        cursor: Optional[TelemetrySourceCursorRecord],
    ) -> None:
        if cursor is None:
            return
        if event.source_epoch != cursor.source_epoch:
            if self.repository.source_epoch_exists(
                event.endpoint_uuid, event.source_epoch
            ):
                raise StaleTelemetryError(
                    "source epoch was already superseded for this endpoint"
                )
            return
        incoming = (event.source_generation, event.source_sequence)
        current = (cursor.source_generation, cursor.source_sequence)
        if incoming <= current:
            raise StaleTelemetryError(
                "source generation/sequence did not advance monotonically"
            )

    def _replay_result(
        self,
        request: TelemetryIngestRequest,
        existing: TelemetryEventRecord,
    ) -> TelemetryIngestResult:
        incoming = self._event_record(request.event)
        if not self._same_event(existing, incoming):
            raise TelemetryConflictError(
                "event_uuid was already used for different telemetry"
            )
        cursor = self.repository.get_source_cursor(existing.endpoint_uuid)
        if cursor is None:  # pragma: no cover - 仅防御手工破坏后的数据库
            raise TelemetryConflictError("replayed event has no source cursor")
        state = None
        if request.device_state is not None:
            current = self.repository.get_device_state(
                existing.endpoint_uuid, request.device_state.device_uuid
            )
            if current is not None and current.source_event_uuid == existing.event_uuid:
                expected_hash = self._state_hash(request.device_state)
                if current.state_hash != expected_hash:
                    raise TelemetryConflictError(
                        "replayed event carries a different device snapshot"
                    )
                state = current
        return TelemetryIngestResult(
            replayed=True,
            event=existing,
            cursor=cursor,
            device_state=state,
        )

    def ingest(self, request: TelemetryIngestRequest) -> TelemetryIngestResult:
        event = self._event_record(request.event)
        try:
            with self.repository.write():
                existing = self.repository.get_event(event.event_uuid)
                if existing is not None:
                    return self._replay_result(request, existing)

                position_event = self.repository.get_event_at_source_position(
                    endpoint_uuid=event.endpoint_uuid,
                    source_epoch=event.source_epoch,
                    source_generation=event.source_generation,
                    source_sequence=event.source_sequence,
                )
                if position_event is not None:
                    raise TelemetryConflictError(
                        "source position was already used by another event"
                    )

                previous_cursor = self.repository.get_source_cursor(event.endpoint_uuid)
                self._validate_source_position(event, previous_cursor)
                saved_event = self.repository.append_event(event)

                saved_state = None
                if request.device_state is not None:
                    saved_state = self.repository.upsert_device_state(
                        self._state_record(saved_event, request.device_state)
                    )

                cursor = TelemetrySourceCursorRecord(
                    endpoint_uuid=event.endpoint_uuid,
                    source_epoch=event.source_epoch,
                    source_generation=event.source_generation,
                    source_sequence=event.source_sequence,
                    last_event_uuid=event.event_uuid,
                    last_received_at_ms=(
                        event.received_at_ms
                        if previous_cursor is None
                        else max(
                            previous_cursor.last_received_at_ms,
                            event.received_at_ms,
                        )
                    ),
                    version=(
                        1 if previous_cursor is None else previous_cursor.version + 1
                    ),
                )
                self.repository.save_source_cursor(
                    cursor,
                    expected_version=(
                        None if previous_cursor is None else previous_cursor.version
                    ),
                )
                return TelemetryIngestResult(
                    event=saved_event,
                    cursor=cursor,
                    device_state=saved_state,
                )
        except sqlite3.IntegrityError as exc:
            raise TelemetryConflictError(str(exc)) from exc

    def ingest_event(
        self,
        event: TelemetryEventWrite,
        *,
        device_state: Optional[DeviceStateSnapshot] = None,
    ) -> TelemetryIngestResult:
        return self.ingest(
            TelemetryIngestRequest(event=event, device_state=device_state)
        )

    def get_source_cursor(
        self, endpoint_uuid: str
    ) -> Optional[TelemetrySourceCursorRecord]:
        return self.repository.get_source_cursor(endpoint_uuid)

    def get_device_state(
        self, endpoint_uuid: str, device_uuid: str
    ) -> Optional[DeviceStateLatestRecord]:
        return self.repository.get_device_state(endpoint_uuid, device_uuid)

    def query_events(
        self, query: Optional[TelemetryEventQuery] = None, **filters: object
    ) -> list[TelemetryEventRecord]:
        if query is not None and filters:
            raise TelemetryValidationError(
                "pass a TelemetryEventQuery or keyword filters, not both"
            )
        resolved = query or TelemetryEventQuery.model_validate(filters)
        return self.repository.query_events(resolved)


__all__ = [
    "StaleTelemetryError",
    "TelemetryConflictError",
    "TelemetryService",
    "TelemetryServiceError",
    "TelemetryValidationError",
]
