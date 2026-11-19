"""以 ``telemetry.db`` 为唯一权威的设备遥测服务（持连接 + 业务 API 单层）。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from unilabos.server.database.sqlite_domain import DomainDatabase, SqliteDomain
from unilabos.server.database.tables.telemetry import (
    TELEMETRY_DATABASE,
    DeviceStateLatestRecord,
    TelemetryEventRecord,
    TelemetrySourceCursorRecord,
)
from unilabos.protocol.base import canonical_hash, canonical_json
from unilabos.protocol.telemetry import (
    DeviceStateSnapshot,
    TelemetryEventQuery,
    TelemetryEventWrite,
    TelemetryIngestRequest,
    TelemetryIngestResult,
)


def _load_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return json.loads(str(value))


class TelemetryServiceError(RuntimeError):
    code = "telemetry_error"


class TelemetryConflictError(TelemetryServiceError):
    code = "conflict"


class StaleTelemetryError(TelemetryConflictError):
    code = "stale_source_position"


class TelemetryValidationError(TelemetryServiceError):
    code = "invalid_telemetry"


class TelemetryService(SqliteDomain):
    """原子追加 event、推进 source cursor，并按需更新最新设备快照。"""

    def __init__(self, database: DomainDatabase):
        super().__init__(database, TELEMETRY_DATABASE)

    # -- 行映射 -----------------------------------------------------------

    @staticmethod
    def _cursor(row: sqlite3.Row) -> TelemetrySourceCursorRecord:
        return TelemetrySourceCursorRecord.model_validate(dict(row))

    @staticmethod
    def _state(row: sqlite3.Row) -> DeviceStateLatestRecord:
        values = dict(row)
        values["state"] = _load_json(values.pop("state_json"))
        values["properties"] = _load_json(values.pop("properties_json"))
        values["alarms"] = _load_json(values.pop("alarms_json"))
        return DeviceStateLatestRecord.model_validate(values)

    @staticmethod
    def _event(row: sqlite3.Row) -> TelemetryEventRecord:
        values = dict(row)
        values["payload"] = _load_json(values.pop("payload_json"))
        return TelemetryEventRecord.model_validate(values)

    # -- source cursor ----------------------------------------------------

    def get_source_cursor(
        self, endpoint_uuid: str
    ) -> Optional[TelemetrySourceCursorRecord]:
        row = self.connection.execute(
            "SELECT * FROM telemetry_source_cursor WHERE endpoint_uuid=?",
            (endpoint_uuid,),
        ).fetchone()
        return self._cursor(row) if row is not None else None

    def save_source_cursor(
        self,
        record: TelemetrySourceCursorRecord,
        *,
        expected_version: Optional[int],
    ) -> None:
        values = record.model_dump(mode="json")
        if expected_version is None:
            self.connection.execute(
                """
                INSERT INTO telemetry_source_cursor(
                    endpoint_uuid,source_epoch,source_generation,source_sequence,
                    last_event_uuid,last_received_at_ms,version
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    values["endpoint_uuid"],
                    values["source_epoch"],
                    values["source_generation"],
                    values["source_sequence"],
                    values["last_event_uuid"],
                    values["last_received_at_ms"],
                    values["version"],
                ),
            )
            return
        cursor = self.connection.execute(
            """
            UPDATE telemetry_source_cursor SET
                source_epoch=?,source_generation=?,source_sequence=?,
                last_event_uuid=?,last_received_at_ms=?,version=?
            WHERE endpoint_uuid=? AND version=?
            """,
            (
                values["source_epoch"],
                values["source_generation"],
                values["source_sequence"],
                values["last_event_uuid"],
                values["last_received_at_ms"],
                values["version"],
                values["endpoint_uuid"],
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("telemetry source cursor version conflict")

    def source_epoch_exists(self, endpoint_uuid: str, source_epoch: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM telemetry_event
            WHERE endpoint_uuid=? AND source_epoch=? LIMIT 1
            """,
            (endpoint_uuid, source_epoch),
        ).fetchone()
        return row is not None

    # -- 设备快照 ----------------------------------------------------------

    def get_device_state(
        self, endpoint_uuid: str, device_uuid: str
    ) -> Optional[DeviceStateLatestRecord]:
        row = self.connection.execute(
            """
            SELECT * FROM device_state_latest
            WHERE endpoint_uuid=? AND device_uuid=?
            """,
            (endpoint_uuid, device_uuid),
        ).fetchone()
        return self._state(row) if row is not None else None

    def list_device_states(
        self, endpoint_uuid: Optional[str] = None
    ) -> list[DeviceStateLatestRecord]:
        if endpoint_uuid is None:
            rows = self.connection.execute(
                "SELECT * FROM device_state_latest ORDER BY endpoint_uuid,device_uuid"
            )
        else:
            rows = self.connection.execute(
                """
                SELECT * FROM device_state_latest
                WHERE endpoint_uuid=? ORDER BY device_uuid
                """,
                (endpoint_uuid,),
            )
        return [self._state(row) for row in rows]

    def upsert_device_state(
        self, record: DeviceStateLatestRecord
    ) -> DeviceStateLatestRecord:
        values = record.model_dump(mode="json")
        self.connection.execute(
            """
            INSERT INTO device_state_latest(
                endpoint_uuid,device_uuid,source_event_uuid,source_epoch,
                source_generation,source_sequence,state_json,properties_json,
                connection_state,alarms_json,state_hash,observed_at_ms,
                received_at_ms,version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(endpoint_uuid,device_uuid) DO UPDATE SET
                source_event_uuid=excluded.source_event_uuid,
                source_epoch=excluded.source_epoch,
                source_generation=excluded.source_generation,
                source_sequence=excluded.source_sequence,
                state_json=excluded.state_json,
                properties_json=excluded.properties_json,
                connection_state=excluded.connection_state,
                alarms_json=excluded.alarms_json,
                state_hash=excluded.state_hash,
                observed_at_ms=excluded.observed_at_ms,
                received_at_ms=excluded.received_at_ms,
                version=excluded.version
            """,
            (
                values["endpoint_uuid"],
                values["device_uuid"],
                values["source_event_uuid"],
                values["source_epoch"],
                values["source_generation"],
                values["source_sequence"],
                canonical_json(values["state"]),
                canonical_json(values["properties"]),
                values["connection_state"],
                canonical_json(values["alarms"]),
                values["state_hash"],
                values["observed_at_ms"],
                values["received_at_ms"],
                values["version"],
            ),
        )
        saved = self.get_device_state(record.endpoint_uuid, record.device_uuid)
        if saved is None:  # pragma: no cover - INSERT/UPDATE 成功后的防御性检查
            raise RuntimeError("device state upsert did not persist a row")
        return saved

    # -- event 读写 --------------------------------------------------------

    def get_event(self, event_uuid: str) -> Optional[TelemetryEventRecord]:
        row = self.connection.execute(
            "SELECT * FROM telemetry_event WHERE event_uuid=?", (event_uuid,)
        ).fetchone()
        return self._event(row) if row is not None else None

    def get_event_at_source_position(
        self,
        *,
        endpoint_uuid: str,
        source_epoch: str,
        source_generation: int,
        source_sequence: int,
    ) -> Optional[TelemetryEventRecord]:
        row = self.connection.execute(
            """
            SELECT * FROM telemetry_event
            WHERE endpoint_uuid=? AND source_epoch=?
              AND source_generation=? AND source_sequence=?
            ORDER BY sequence LIMIT 1
            """,
            (
                endpoint_uuid,
                source_epoch,
                source_generation,
                source_sequence,
            ),
        ).fetchone()
        return self._event(row) if row is not None else None

    def append_event(self, record: TelemetryEventRecord) -> TelemetryEventRecord:
        values = record.model_dump(mode="json")
        cursor = self.connection.execute(
            """
            INSERT INTO telemetry_event(
                event_uuid,endpoint_uuid,device_uuid,source_epoch,
                source_generation,source_sequence,event_type,event_key,payload_json,
                payload_hash,severity,source_job_uuid,source_command_uuid,
                observed_at_ms,received_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values["event_uuid"],
                values["endpoint_uuid"],
                values["device_uuid"],
                values["source_epoch"],
                values["source_generation"],
                values["source_sequence"],
                values["event_type"],
                values["event_key"],
                canonical_json(values["payload"]),
                values["payload_hash"],
                values["severity"],
                values["source_job_uuid"],
                values["source_command_uuid"],
                values["observed_at_ms"],
                values["received_at_ms"],
            ),
        )
        return record.model_copy(update={"sequence": int(cursor.lastrowid)})

    @staticmethod
    def _event_filters(query: TelemetryEventQuery) -> tuple[list[str], list[Any]]:
        clauses = ["sequence>?"]
        params: list[Any] = [query.after_sequence]
        for column, value in (
            ("endpoint_uuid", query.endpoint_uuid),
            ("device_uuid", query.device_uuid),
            ("event_type", query.event_type),
            ("event_key", query.event_key),
            ("source_epoch", query.source_epoch),
            ("source_generation", query.source_generation),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if query.observed_from_ms is not None:
            clauses.append("observed_at_ms>=?")
            params.append(query.observed_from_ms)
        if query.observed_to_ms is not None:
            clauses.append("observed_at_ms<=?")
            params.append(query.observed_to_ms)
        return clauses, params

    def query_events(
        self, query: Optional[TelemetryEventQuery] = None, **filters: object
    ) -> list[TelemetryEventRecord]:
        if query is not None and filters:
            raise TelemetryValidationError(
                "pass a TelemetryEventQuery or keyword filters, not both"
            )
        resolved = query or TelemetryEventQuery.model_validate(filters)
        clauses, params = self._event_filters(resolved)
        params.append(resolved.limit)
        order = "ASC" if resolved.order == "asc" else "DESC"
        rows = self.connection.execute(
            "SELECT * FROM telemetry_event WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY sequence {order} LIMIT ?",
            params,
        )
        return [self._event(row) for row in rows]

    def count_events(
        self, query: Optional[TelemetryEventQuery] = None, **filters: object
    ) -> int:
        if query is not None and filters:
            raise TelemetryValidationError(
                "pass a TelemetryEventQuery or keyword filters, not both"
            )
        resolved = query or TelemetryEventQuery.model_validate(filters)
        clauses, params = self._event_filters(resolved)
        row = self.connection.execute(
            "SELECT COUNT(*) FROM telemetry_event WHERE " + " AND ".join(clauses),
            params,
        ).fetchone()
        return int(row[0])

    # -- ingest 业务 -------------------------------------------------------

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
        previous = self.get_device_state(event.endpoint_uuid, snapshot.device_uuid)
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
            if self.source_epoch_exists(event.endpoint_uuid, event.source_epoch):
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
        cursor = self.get_source_cursor(existing.endpoint_uuid)
        if cursor is None:  # pragma: no cover - 仅防御手工破坏后的数据库
            raise TelemetryConflictError("replayed event has no source cursor")
        state = None
        if request.device_state is not None:
            current = self.get_device_state(
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
            with self.write():
                existing = self.get_event(event.event_uuid)
                if existing is not None:
                    return self._replay_result(request, existing)

                position_event = self.get_event_at_source_position(
                    endpoint_uuid=event.endpoint_uuid,
                    source_epoch=event.source_epoch,
                    source_generation=event.source_generation,
                    source_sequence=event.source_sequence,
                )
                if position_event is not None:
                    raise TelemetryConflictError(
                        "source position was already used by another event"
                    )

                previous_cursor = self.get_source_cursor(event.endpoint_uuid)
                self._validate_source_position(event, previous_cursor)
                saved_event = self.append_event(event)

                saved_state = None
                if request.device_state is not None:
                    saved_state = self.upsert_device_state(
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
                self.save_source_cursor(
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


__all__ = [
    "StaleTelemetryError",
    "TelemetryConflictError",
    "TelemetryService",
    "TelemetryServiceError",
    "TelemetryValidationError",
]
