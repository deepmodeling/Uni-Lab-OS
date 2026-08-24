"""``history.v1`` payload 与统一历史流协议。"""

from __future__ import annotations

import base64
import binascii
from typing import Literal, Optional

from pydantic import Field, field_serializer, field_validator, model_validator

from unilabos.server.database.tables.history import INLINE_PAYLOAD_LIMIT_BYTES
from unilabos.server.database.tables.history import PayloadObjectRecord
from unilabos.server.database.tables.base import (
    JsonObject,
    NonEmptyStr,
    ServerObject,
    UnixMilliseconds,
)


HISTORY_PROTOCOL_VERSION = "history.v1"

HistoryEventType = Literal[
    "job_transition",
    "action_availability",
    "job_feedback",
    "job_result",
    "job_log",
    "error_snapshot",
    "decision_audit",
]


def _decode_base64_bytes(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("inline_payload must be valid Base64") from exc


def _encode_base64_bytes(value: Optional[bytes]) -> Optional[str]:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


class InlinePayloadWrite(ServerObject):
    """由微后端直接保存在 ``history.db`` 的小 payload。"""

    protocol_version: Literal["history.v1"] = HISTORY_PROTOCOL_VERSION
    storage_kind: Literal["inline"] = "inline"
    payload_uuid: Optional[NonEmptyStr] = None
    media_type: NonEmptyStr
    encoding: NonEmptyStr = "binary"
    compression: Optional[str] = None
    inline_payload: bytes
    created_at_ms: Optional[UnixMilliseconds] = None
    expires_at_ms: Optional[UnixMilliseconds] = None

    @field_validator("inline_payload", mode="before")
    @classmethod
    def _decode_inline_payload(cls, value: object) -> object:
        return _decode_base64_bytes(value)

    @field_serializer("inline_payload", when_used="json")
    def _encode_inline_payload(self, value: bytes) -> str:
        encoded = _encode_base64_bytes(value)
        assert encoded is not None
        return encoded

    @model_validator(mode="after")
    def _validate_inline_payload(self) -> "InlinePayloadWrite":
        if len(self.inline_payload) > INLINE_PAYLOAD_LIMIT_BYTES:
            raise ValueError(
                "inline payload exceeds the configured limit; use external storage"
            )
        if (
            self.created_at_ms is not None
            and self.expires_at_ms is not None
            and self.expires_at_ms < self.created_at_ms
        ):
            raise ValueError("expires_at_ms cannot precede created_at_ms")
        return self


class ExternalPayloadWrite(ServerObject):
    """大 payload 的不可变外部对象引用。"""

    protocol_version: Literal["history.v1"] = HISTORY_PROTOCOL_VERSION
    storage_kind: Literal["external"] = "external"
    payload_uuid: Optional[NonEmptyStr] = None
    media_type: NonEmptyStr
    encoding: NonEmptyStr = "binary"
    compression: Optional[str] = None
    byte_length: int = Field(ge=0)
    sha256: NonEmptyStr
    external_uri: NonEmptyStr
    created_at_ms: Optional[UnixMilliseconds] = None
    expires_at_ms: Optional[UnixMilliseconds] = None

    @model_validator(mode="after")
    def _validate_expiry(self) -> "ExternalPayloadWrite":
        if (
            self.created_at_ms is not None
            and self.expires_at_ms is not None
            and self.expires_at_ms < self.created_at_ms
        ):
            raise ValueError("expires_at_ms cannot precede created_at_ms")
        return self


PayloadWrite = InlinePayloadWrite | ExternalPayloadWrite


# 表模型本身已定义稳定的 Base64 JSON 表示，读取协议无需再复制字段或校验。
PayloadObjectRead = PayloadObjectRecord


class HistoryEventAppend(ServerObject):
    """追加一条历史事件；``sequence`` 只由 ``history.db`` 分配。"""

    protocol_version: Literal["history.v1"] = HISTORY_PROTOCOL_VERSION
    event_uuid: Optional[NonEmptyStr] = None
    event_type: HistoryEventType
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
    occurred_at_ms: Optional[UnixMilliseconds] = None
    recorded_at_ms: Optional[UnixMilliseconds] = None

    @model_validator(mode="after")
    def _validate_event(self) -> "HistoryEventAppend":
        if (
            self.occurred_at_ms is not None
            and self.recorded_at_ms is not None
            and self.recorded_at_ms < self.occurred_at_ms
        ):
            raise ValueError("recorded_at_ms cannot precede occurred_at_ms")
        if self.supersedes_event_uuid is not None and (
            not self.actor_type or not self.actor_uuid
        ):
            raise ValueError("a replacement event requires actor_type and actor_uuid")
        return self


class ManualResultReplacement(ServerObject):
    """人工结果替换请求；job 和 action 归属从被替换事件继承。"""

    protocol_version: Literal["history.v1"] = HISTORY_PROTOCOL_VERSION
    supersedes_event_uuid: NonEmptyStr
    event_uuid: Optional[NonEmptyStr] = None
    payload_uuid: Optional[NonEmptyStr] = None
    summary: JsonObject = Field(default_factory=dict)
    event_key: Optional[NonEmptyStr] = None
    state_version: Optional[int] = Field(default=None, ge=1)
    severity: Optional[str] = None
    actor_type: NonEmptyStr = "human"
    actor_uuid: NonEmptyStr
    occurred_at_ms: Optional[UnixMilliseconds] = None
    recorded_at_ms: Optional[UnixMilliseconds] = None

    @model_validator(mode="after")
    def _validate_timestamps(self) -> "ManualResultReplacement":
        if (
            self.occurred_at_ms is not None
            and self.recorded_at_ms is not None
            and self.recorded_at_ms < self.occurred_at_ms
        ):
            raise ValueError("recorded_at_ms cannot precede occurred_at_ms")
        return self


class HistoryEventQuery(ServerObject):
    """按全局追加序列稳定分页查询历史事件。"""

    protocol_version: Literal["history.v1"] = HISTORY_PROTOCOL_VERSION
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)
    event_types: list[HistoryEventType] = Field(default_factory=list)
    job_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: Optional[NonEmptyStr] = None
    device_uuid: Optional[NonEmptyStr] = None
    event_key: Optional[NonEmptyStr] = None
    occurred_from_ms: Optional[UnixMilliseconds] = None
    occurred_through_ms: Optional[UnixMilliseconds] = None

    @model_validator(mode="after")
    def _validate_time_range(self) -> "HistoryEventQuery":
        if (
            self.occurred_from_ms is not None
            and self.occurred_through_ms is not None
            and self.occurred_through_ms < self.occurred_from_ms
        ):
            raise ValueError("history query time range is reversed")
        return self


__all__ = [
    "ExternalPayloadWrite",
    "HISTORY_PROTOCOL_VERSION",
    "HistoryEventAppend",
    "HistoryEventQuery",
    "HistoryEventType",
    "InlinePayloadWrite",
    "ManualResultReplacement",
    "PayloadObjectRead",
    "PayloadWrite",
]
