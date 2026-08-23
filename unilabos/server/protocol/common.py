"""微后端对外协议的公共信封与规范 JSON 工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import Field, model_validator

from unilabos.server.database.tables.base import JsonObject, NonEmptyStr, ServerObject


PROTOCOL_VERSION = "materials.v1"


def canonical_json(value: Any) -> str:
    """返回跨进程稳定的 JSON；哈希、幂等请求和快照均使用这一实现。"""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


AggregateType = Literal[
    "resource_template", "material", "site", "lot", "reservation"
]


class AggregatePrecondition(ServerObject):
    aggregate_type: AggregateType
    aggregate_uuid: NonEmptyStr
    expected_version: Optional[int] = Field(default=None, ge=0)
    expected_state_hash: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def _require_condition(self) -> "AggregatePrecondition":
        if self.expected_version is None and self.expected_state_hash is None:
            raise ValueError("precondition requires expected_version or state hash")
        return self


class InventoryMutation(ServerObject):
    """所有写请求共用的幂等信封。"""

    protocol_version: Literal["materials.v1"] = PROTOCOL_VERSION
    command_uuid: NonEmptyStr
    effect_key: NonEmptyStr
    operation: NonEmptyStr
    actor_type: NonEmptyStr = "edge"
    actor_uuid: Optional[NonEmptyStr] = None
    job_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: int = Field(default=0, ge=0)
    preconditions: list[AggregatePrecondition] = Field(default_factory=list)
    payload: JsonObject = Field(default_factory=dict)


class AggregateVersion(ServerObject):
    aggregate_type: AggregateType
    aggregate_uuid: NonEmptyStr
    version: int = Field(ge=1)
    state_hash: NonEmptyStr


class InventoryChange(ServerObject):
    sequence: int = Field(ge=1)
    event_uuid: NonEmptyStr
    aggregate_type: AggregateType
    aggregate_uuid: NonEmptyStr
    operation: NonEmptyStr
    previous_version: int = Field(ge=0)
    aggregate_version: int = Field(ge=1)
    state_hash: NonEmptyStr
    delta: JsonObject = Field(default_factory=dict)
    job_uuid: Optional[NonEmptyStr] = None
    command_uuid: Optional[NonEmptyStr] = None
    effect_key: Optional[NonEmptyStr] = None
    actor_type: NonEmptyStr
    actor_uuid: Optional[NonEmptyStr] = None
    occurred_at_ms: int = Field(ge=0)
    delivery_status: Literal["pending", "sent", "acknowledged", "dead_letter"]


ResultT = TypeVar("ResultT")


class MutationResult(ServerObject, Generic[ResultT]):
    protocol_version: Literal["materials.v1"] = PROTOCOL_VERSION
    command_uuid: NonEmptyStr
    effect_key: NonEmptyStr
    replayed: bool = False
    changed: bool = True
    ledger_sequence_start: int = Field(ge=1)
    ledger_sequence_end: int = Field(ge=1)
    affected: list[AggregateVersion] = Field(default_factory=list)
    data: ResultT

    @model_validator(mode="after")
    def _validate_ledger_range(self) -> "MutationResult[ResultT]":
        if self.ledger_sequence_end < self.ledger_sequence_start:
            raise ValueError("ledger sequence range is reversed")
        return self


__all__ = [
    "AggregatePrecondition",
    "AggregateType",
    "AggregateVersion",
    "InventoryMutation",
    "InventoryChange",
    "MutationResult",
    "PROTOCOL_VERSION",
    "canonical_hash",
    "canonical_json",
]
