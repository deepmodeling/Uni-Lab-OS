"""``history.db`` 的 payload 与统一历史事件模型。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from unilabos.server.database.history import INLINE_PAYLOAD_LIMIT_BYTES
from unilabos.server.models.base import (
    JsonObject,
    NonEmptyStr,
    ServerObject,
    UnixMilliseconds,
)


class PayloadObjectRecord(ServerObject):
    payload_uuid: NonEmptyStr
    media_type: NonEmptyStr
    encoding: NonEmptyStr
    compression: Optional[str] = None
    byte_length: int = Field(ge=0)
    sha256: NonEmptyStr
    storage_kind: Literal["inline", "external"]
    inline_payload: Optional[bytes] = None
    external_uri: Optional[NonEmptyStr] = None
    created_at_ms: UnixMilliseconds
    expires_at_ms: Optional[UnixMilliseconds] = None

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


class HistoryEventRecord(ServerObject):
    sequence: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    event_type: Literal[
        "job_transition",
        "action_availability",
        "job_feedback",
        "job_result",
        "job_log",
        "error_snapshot",
        "decision_audit",
    ]
    job_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: Optional[NonEmptyStr] = None
    device_uuid: Optional[NonEmptyStr] = None
    action_name: Optional[NonEmptyStr] = None
    event_key: Optional[NonEmptyStr] = None
    job_sequence: Optional[int] = Field(default=None, ge=0)
    state_version: Optional[int] = Field(default=None, ge=1)
    payload_uuid: Optional[NonEmptyStr] = None
    summary: JsonObject = Field(default_factory=dict)
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


__all__ = ["HistoryEventRecord", "PayloadObjectRecord"]
