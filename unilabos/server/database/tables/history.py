"""``history.db`` 的 SQLModel 表记录。"""

from __future__ import annotations

import base64
import binascii
from typing import ClassVar, Literal, Optional

from pydantic import field_serializer, field_validator, model_validator
from sqlalchemy import Column, LargeBinary, Text
from sqlmodel import Field

from unilabos.server.database.schema import (
    SCHEMA_IDENTITY_TABLE,
    DatabaseSpec,
    TableSpec,
)
from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    TableObject,
    UnixMilliseconds,
    json_text_column,
)


class PayloadObjectRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "payload_object"

    payload_uuid: NonEmptyStr = Field(primary_key=True)
    media_type: NonEmptyStr
    encoding: NonEmptyStr
    compression: Optional[str] = None
    byte_length: int = Field(ge=0)
    sha256: NonEmptyStr
    storage_kind: Literal["inline", "external"] = Field(sa_type=Text)
    inline_payload: Optional[bytes] = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    external_uri: Optional[NonEmptyStr] = None
    created_at_ms: UnixMilliseconds
    expires_at_ms: Optional[UnixMilliseconds] = None

    @field_validator("inline_payload", mode="before")
    @classmethod
    def _decode_inline_payload(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise ValueError("inline_payload must be valid Base64") from exc

    @field_serializer("inline_payload", when_used="json")
    def _encode_inline_payload(self, value: Optional[bytes]) -> Optional[str]:
        if value is None:
            return None
        return base64.b64encode(value).decode("ascii")

    @model_validator(mode="after")
    def _validate_storage(self) -> "PayloadObjectRecord":
        if self.storage_kind == "inline":
            if self.inline_payload is None or self.external_uri is not None:
                raise ValueError("inline payload storage shape is invalid")
            if len(self.inline_payload) != self.byte_length:
                raise ValueError("inline payload byte_length does not match content")
            if self.byte_length > INLINE_PAYLOAD_LIMIT_BYTES:
                raise ValueError("inline payload exceeds the configured limit")
        elif self.inline_payload is not None or self.external_uri is None:
            raise ValueError("external payload storage shape is invalid")
        return self


class HistoryEventRecord(TableObject, table=True):
    __tablename__: ClassVar[str] = "history_event"

    sequence: Optional[int] = Field(default=None, ge=1, primary_key=True)
    event_uuid: NonEmptyStr
    event_type: Literal[
        "job_transition",
        "action_availability",
        "job_feedback",
        "job_result",
        "job_log",
        "error_snapshot",
        "decision_audit",
    ] = Field(sa_type=Text)
    job_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: Optional[NonEmptyStr] = None
    device_uuid: Optional[NonEmptyStr] = None
    action_name: Optional[NonEmptyStr] = None
    event_key: Optional[NonEmptyStr] = None
    job_sequence: Optional[int] = Field(default=None, ge=0)
    state_version: Optional[int] = Field(default=None, ge=1)
    payload_uuid: Optional[NonEmptyStr] = None
    summary: JsonObject = Field(
        default_factory=dict,
        sa_column=json_text_column("summary_json", default_json="{}"),
    )
    severity: Optional[str] = None
    actor_type: Optional[str] = None
    actor_uuid: Optional[str] = None
    supersedes_event_uuid: Optional[NonEmptyStr] = None
    occurred_at_ms: UnixMilliseconds
    recorded_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "HistoryEventRecord":
        if self.recorded_at_ms < self.occurred_at_ms:
            raise ValueError("recorded_at_ms cannot precede occurred_at_ms")
        return self


HISTORY_TABLE_MODELS = (
    PayloadObjectRecord,
    HistoryEventRecord,
)


INLINE_PAYLOAD_LIMIT_BYTES = 262_144


HISTORY_TABLES = (
    SCHEMA_IDENTITY_TABLE,
    TableSpec(
        "payload_object",
        f"""
        CREATE TABLE IF NOT EXISTS payload_object (
            payload_uuid TEXT PRIMARY KEY CHECK (TRIM(payload_uuid) <> ''),
            media_type TEXT NOT NULL CHECK (TRIM(media_type) <> ''),
            encoding TEXT NOT NULL CHECK (TRIM(encoding) <> ''),
            compression TEXT,
            byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
            sha256 TEXT NOT NULL CHECK (TRIM(sha256) <> ''),
            storage_kind TEXT NOT NULL CHECK (storage_kind IN ('inline','external')),
            inline_payload BLOB,
            external_uri TEXT,
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            expires_at_ms INTEGER CHECK (expires_at_ms >= created_at_ms),
            CHECK (
                (storage_kind = 'inline' AND inline_payload IS NOT NULL
                    AND external_uri IS NULL
                    AND length(inline_payload) = byte_length
                    AND byte_length <= {INLINE_PAYLOAD_LIMIT_BYTES})
                OR (storage_kind = 'external' AND inline_payload IS NULL
                    AND external_uri IS NOT NULL AND TRIM(external_uri) <> '')
            )
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_payload_expiry
            ON payload_object(expires_at_ms, payload_uuid)
            WHERE expires_at_ms IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "history_event",
        """
        CREATE TABLE IF NOT EXISTS history_event (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'job_transition','action_availability','job_feedback','job_result',
                'job_log','error_snapshot','decision_audit'
            )),
            job_uuid TEXT,
            endpoint_uuid TEXT,
            device_uuid TEXT,
            action_name TEXT,
            event_key TEXT,
            job_sequence INTEGER CHECK (job_sequence >= 0),
            state_version INTEGER CHECK (state_version > 0),
            payload_uuid TEXT,
            summary_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(summary_json) AND json_type(summary_json) = 'object'
            ),
            severity TEXT,
            actor_type TEXT,
            actor_uuid TEXT,
            supersedes_event_uuid TEXT,
            occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            CHECK (event_key IS NULL OR TRIM(event_key) <> ''),
            CHECK (recorded_at_ms >= occurred_at_ms),
            FOREIGN KEY(payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(supersedes_event_uuid) REFERENCES history_event(event_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_history_event_job
            ON history_event(job_uuid, occurred_at_ms, sequence)
            WHERE job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_history_event_type_time
            ON history_event(event_type, occurred_at_ms, sequence)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_history_event_job_sequence
            ON history_event(job_uuid, event_type, job_sequence)
            WHERE job_uuid IS NOT NULL AND job_sequence IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_history_event_state_version
            ON history_event(job_uuid, event_type, state_version)
            WHERE job_uuid IS NOT NULL AND state_version IS NOT NULL
            """,
        ),
    ),
)


HISTORY_DATABASE = DatabaseSpec(
    key="history",
    filename="history.db",
    role="large payload and append-only audit history",
    synchronous="NORMAL",
    tables=HISTORY_TABLES,
)

__all__ = [
    "HISTORY_DATABASE",
    "HISTORY_TABLE_MODELS",
    "HISTORY_TABLES",
    "HistoryEventRecord",
    "INLINE_PAYLOAD_LIMIT_BYTES",
    "PayloadObjectRecord",
]
