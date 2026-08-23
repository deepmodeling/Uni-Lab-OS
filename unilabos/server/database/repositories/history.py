"""``history.db`` 的单连接同步 Repository。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from unilabos.server.database.tables.history import HISTORY_DATABASE
from unilabos.server.database.schema import initialize_database
from unilabos.server.database.tables.history import (
    HistoryEventRecord,
    PayloadObjectRecord,
)
from unilabos.server.protocol.common import canonical_json


class HistoryRepository:
    """独占一个 SQLite connection，并提供唯一的写事务入口。"""

    def __init__(self, database: str | Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.connection = database
            self._owns_connection = False
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
        else:
            self.connection = initialize_database(database, HISTORY_DATABASE)
            self._owns_connection = True
        self._connection_lock = threading.RLock()

    def close(self) -> None:
        with self._connection_lock:
            if self._owns_connection:
                self.connection.close()

    def __enter__(self) -> "HistoryRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """串行化本 Repository 的全部写入，并保持事件与 payload 原子性。"""

        with self._connection_lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    @staticmethod
    def _payload(row: sqlite3.Row) -> PayloadObjectRecord:
        values = dict(row)
        if values["inline_payload"] is not None:
            values["inline_payload"] = bytes(values["inline_payload"])
        return PayloadObjectRecord.model_validate(values)

    @staticmethod
    def _event(row: sqlite3.Row) -> HistoryEventRecord:
        values = dict(row)
        values["summary"] = json.loads(values.pop("summary_json"))
        return HistoryEventRecord.model_validate(values)

    def get_payload(self, payload_uuid: str) -> Optional[PayloadObjectRecord]:
        with self._connection_lock:
            row = self.connection.execute(
                "SELECT * FROM payload_object WHERE payload_uuid=?",
                (payload_uuid,),
            ).fetchone()
            return self._payload(row) if row is not None else None

    def find_payload_by_content(
        self, sha256: str, byte_length: int
    ) -> Optional[PayloadObjectRecord]:
        with self._connection_lock:
            row = self.connection.execute(
                """
                SELECT * FROM payload_object
                WHERE sha256=? AND byte_length=?
                ORDER BY created_at_ms,payload_uuid
                LIMIT 1
                """,
                (sha256, byte_length),
            ).fetchone()
            return self._payload(row) if row is not None else None

    def insert_payload(self, record: PayloadObjectRecord) -> None:
        values = record.model_dump(mode="python")
        self.connection.execute(
            """
            INSERT INTO payload_object(
                payload_uuid,media_type,encoding,compression,byte_length,sha256,
                storage_kind,inline_payload,external_uri,created_at_ms,expires_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values["payload_uuid"],
                values["media_type"],
                values["encoding"],
                values["compression"],
                values["byte_length"],
                values["sha256"],
                values["storage_kind"],
                values["inline_payload"],
                values["external_uri"],
                values["created_at_ms"],
                values["expires_at_ms"],
            ),
        )

    def get_event(self, event_uuid: str) -> Optional[HistoryEventRecord]:
        with self._connection_lock:
            row = self.connection.execute(
                "SELECT * FROM history_event WHERE event_uuid=?",
                (event_uuid,),
            ).fetchone()
            return self._event(row) if row is not None else None

    def get_superseding_event(self, event_uuid: str) -> Optional[HistoryEventRecord]:
        with self._connection_lock:
            row = self.connection.execute(
                """
                SELECT * FROM history_event
                WHERE supersedes_event_uuid=?
                ORDER BY sequence
                LIMIT 1
                """,
                (event_uuid,),
            ).fetchone()
            return self._event(row) if row is not None else None

    def latest_state_version(self, job_uuid: str, event_type: str) -> int:
        with self._connection_lock:
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(state_version),0)
                FROM history_event
                WHERE job_uuid=? AND event_type=?
                """,
                (job_uuid, event_type),
            ).fetchone()
            return int(row[0])

    def insert_event(self, record: HistoryEventRecord) -> HistoryEventRecord:
        values = record.model_dump(mode="python")
        cursor = self.connection.execute(
            """
            INSERT INTO history_event(
                event_uuid,event_type,job_uuid,endpoint_uuid,device_uuid,action_name,
                event_key,job_sequence,state_version,payload_uuid,summary_json,severity,
                actor_type,actor_uuid,supersedes_event_uuid,occurred_at_ms,recorded_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                values["event_uuid"],
                values["event_type"],
                values["job_uuid"],
                values["endpoint_uuid"],
                values["device_uuid"],
                values["action_name"],
                values["event_key"],
                values["job_sequence"],
                values["state_version"],
                values["payload_uuid"],
                canonical_json(values["summary"]),
                values["severity"],
                values["actor_type"],
                values["actor_uuid"],
                values["supersedes_event_uuid"],
                values["occurred_at_ms"],
                values["recorded_at_ms"],
            ),
        )
        return record.model_copy(update={"sequence": int(cursor.lastrowid)})

    def query_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        event_types: Sequence[str] = (),
        job_uuid: Optional[str] = None,
        endpoint_uuid: Optional[str] = None,
        device_uuid: Optional[str] = None,
        event_key: Optional[str] = None,
        occurred_from_ms: Optional[int] = None,
        occurred_through_ms: Optional[int] = None,
    ) -> list[HistoryEventRecord]:
        clauses = ["sequence>?"]
        params: list[Any] = [after_sequence]
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        for column, value in (
            ("job_uuid", job_uuid),
            ("endpoint_uuid", endpoint_uuid),
            ("device_uuid", device_uuid),
            ("event_key", event_key),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if occurred_from_ms is not None:
            clauses.append("occurred_at_ms>=?")
            params.append(occurred_from_ms)
        if occurred_through_ms is not None:
            clauses.append("occurred_at_ms<=?")
            params.append(occurred_through_ms)
        params.append(limit)
        with self._connection_lock:
            rows = self.connection.execute(
                "SELECT * FROM history_event WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?",
                params,
            ).fetchall()
            return [self._event(row) for row in rows]


__all__ = ["HistoryRepository"]
