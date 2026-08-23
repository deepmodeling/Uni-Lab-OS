"""``telemetry.db`` 的同步 Repository 和单写事务边界。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from unilabos.server.database.schema import initialize_database
from unilabos.server.database.tables.telemetry import TELEMETRY_DATABASE
from unilabos.server.database.tables.telemetry import (
    DeviceStateLatestRecord,
    TelemetryEventRecord,
    TelemetrySourceCursorRecord,
)
from unilabos.server.protocol.common import canonical_json
from unilabos.server.protocol.telemetry import TelemetryEventQuery


def _load_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return json.loads(str(value))


class TelemetryRepository:
    """Repository 独占一个 connection，所有写入经过同一个 writer 入口。"""

    def __init__(self, database: str | Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.connection = database
            self._owns_connection = False
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
        else:
            self.connection = initialize_database(database, TELEMETRY_DATABASE)
            self._owns_connection = True
        self._write_lock = threading.RLock()

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> "TelemetryRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """每个 telemetry.db 进程内只有这一个 writer 事务入口。"""

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

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

    def query_events(self, query: TelemetryEventQuery) -> list[TelemetryEventRecord]:
        clauses, params = self._event_filters(query)
        params.append(query.limit)
        order = "ASC" if query.order == "asc" else "DESC"
        rows = self.connection.execute(
            "SELECT * FROM telemetry_event WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY sequence {order} LIMIT ?",
            params,
        )
        return [self._event(row) for row in rows]

    def count_events(self, query: TelemetryEventQuery) -> int:
        clauses, params = self._event_filters(query)
        row = self.connection.execute(
            "SELECT COUNT(*) FROM telemetry_event WHERE " + " AND ".join(clauses),
            params,
        ).fetchone()
        return int(row[0])


__all__ = ["TelemetryRepository"]
