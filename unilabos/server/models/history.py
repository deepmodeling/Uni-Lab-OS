"""``history.db`` 的追加历史与大 payload 记录。"""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import Field, StringConstraints, model_validator

from unilabos.server.database.history import MAX_INLINE_PAYLOAD_BYTES
from unilabos.server.models.base import (
    JsonObject,
    NonEmptyStr,
    PositiveVersion,
    ServerObject,
    UnixMilliseconds,
)


Sha256Hex = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
]
JobStatus = Literal[
    "accepted",
    "dispatch_pending",
    "dispatched",
    "running",
    "failure_waiting",
    "terminal_waiting",
    "succeeded",
    "failed",
    "canceled",
    "execution_unknown",
    "rejected",
]
ActionState = Literal["free", "busy", "unknown"]


class PayloadObjectRecord(ServerObject):
    payload_uuid: NonEmptyStr
    payload_kind: NonEmptyStr
    media_type: NonEmptyStr
    codec: NonEmptyStr
    storage_kind: Literal["inline", "external"]
    size_bytes: int = Field(ge=0)
    sha256: Sha256Hex
    inline_data: Optional[bytes] = None
    external_uri: Optional[NonEmptyStr] = None
    retention_class: NonEmptyStr
    expires_at_ms: Optional[UnixMilliseconds] = None
    created_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_storage(self) -> "PayloadObjectRecord":
        if self.storage_kind == "inline":
            if self.inline_data is None or self.external_uri is not None:
                raise ValueError("inline payload requires inline_data only")
            if self.size_bytes > MAX_INLINE_PAYLOAD_BYTES:
                raise ValueError("inline payload exceeds the SQLite size limit")
        elif self.inline_data is not None or self.external_uri is None:
            raise ValueError("external payload requires external_uri only")
        if self.inline_data is not None and len(self.inline_data) != self.size_bytes:
            raise ValueError("inline_data length must equal size_bytes")
        if self.expires_at_ms is not None and self.expires_at_ms < self.created_at_ms:
            raise ValueError("expires_at_ms cannot precede created_at_ms")
        return self


class JobTransitionRecord(ServerObject):
    sequence: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    job_version: PositiveVersion
    from_status: Optional[JobStatus] = None
    to_status: JobStatus
    source: NonEmptyStr
    command_uuid: Optional[NonEmptyStr] = None
    source_event_uuid: Optional[NonEmptyStr] = None
    payload_uuid: Optional[NonEmptyStr] = None
    occurred_at_ms: UnixMilliseconds
    recorded_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _status_must_change(self) -> "JobTransitionRecord":
        if self.from_status == self.to_status:
            raise ValueError("job transition must change status")
        return self


class ActionAvailabilityEventRecord(ServerObject):
    sequence: Optional[int] = Field(default=None, ge=1)
    event_uuid: NonEmptyStr
    endpoint_uuid: NonEmptyStr
    device_uuid: NonEmptyStr
    action_name: NonEmptyStr
    availability_version: PositiveVersion
    from_state: Optional[ActionState] = None
    to_state: ActionState
    active_job_uuid: Optional[NonEmptyStr] = None
    source: NonEmptyStr
    source_event_uuid: NonEmptyStr
    discovery_epoch: NonEmptyStr
    discovery_generation: int = Field(ge=0)
    payload_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: UnixMilliseconds
    recorded_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_state_change(self) -> "ActionAvailabilityEventRecord":
        if self.to_state == "free" and self.active_job_uuid is not None:
            raise ValueError("free action event cannot reference an active job")
        return self


class JobFeedbackRecord(ServerObject):
    feedback_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    feedback_sequence: int = Field(ge=1)
    feedback_type: NonEmptyStr
    source_event_uuid: NonEmptyStr
    payload_uuid: NonEmptyStr
    observed_at_ms: UnixMilliseconds
    received_at_ms: UnixMilliseconds
    recorded_at_ms: UnixMilliseconds


class JobResultRecord(ServerObject):
    result_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    result_version: PositiveVersion
    result_origin: Literal["adapter", "failure_release", "manual_replacement"]
    outcome: Literal["succeeded", "failed", "canceled"]
    supersedes_result_uuid: Optional[NonEmptyStr] = None
    supersedes_result_version: Optional[PositiveVersion] = None
    source_event_uuid: Optional[NonEmptyStr] = None
    decision_uuid: Optional[NonEmptyStr] = None
    return_payload_uuid: Optional[NonEmptyStr] = None
    error_payload_uuid: Optional[NonEmptyStr] = None
    summary_json: JsonObject = Field(default_factory=dict)
    result_hash: NonEmptyStr
    committed_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _validate_result_lineage(self) -> "JobResultRecord":
        if self.supersedes_result_uuid == self.result_uuid:
            raise ValueError("result cannot supersede itself")
        if self.result_version == 1:
            if (
                self.supersedes_result_uuid is not None
                or self.supersedes_result_version is not None
            ):
                raise ValueError("first result version cannot supersede another result")
        elif (
            self.supersedes_result_uuid is None
            or self.supersedes_result_version != self.result_version - 1
        ):
            raise ValueError("later result version must supersede the prior version")
        if self.result_origin == "adapter":
            if self.source_event_uuid is None or self.decision_uuid is not None:
                raise ValueError("adapter result requires only source_event_uuid")
        elif self.decision_uuid is None:
            raise ValueError("released or replaced result requires decision_uuid")
        return self


class JobLogRecord(ServerObject):
    log_id: Optional[int] = Field(default=None, ge=1)
    log_uuid: NonEmptyStr
    job_uuid: Optional[NonEmptyStr] = None
    endpoint_uuid: Optional[NonEmptyStr] = None
    device_uuid: Optional[NonEmptyStr] = None
    stream_uuid: Optional[NonEmptyStr] = None
    stream_sequence: Optional[int] = Field(default=None, ge=0)
    level: Literal["debug", "info", "warning", "error", "critical"]
    logger_name: Optional[str] = None
    message: NonEmptyStr
    context_payload_uuid: Optional[NonEmptyStr] = None
    occurred_at_ms: UnixMilliseconds
    recorded_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _stream_position_is_atomic(self) -> "JobLogRecord":
        if (self.stream_uuid is None) != (self.stream_sequence is None):
            raise ValueError("stream_uuid and stream_sequence must be set together")
        return self


class ErrorSnapshotRecord(ServerObject):
    error_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    gate_uuid: NonEmptyStr
    source_event_uuid: NonEmptyStr
    error_type: NonEmptyStr
    error_code: Optional[str] = None
    message: NonEmptyStr
    stack_payload_uuid: Optional[NonEmptyStr] = None
    device_state_payload_uuid: Optional[NonEmptyStr] = None
    action_context_payload_uuid: Optional[NonEmptyStr] = None
    observed_at_ms: UnixMilliseconds
    recorded_at_ms: UnixMilliseconds


class DecisionAuditRecord(ServerObject):
    sequence: Optional[int] = Field(default=None, ge=1)
    audit_uuid: NonEmptyStr
    decision_uuid: NonEmptyStr
    gate_uuid: NonEmptyStr
    job_uuid: NonEmptyStr
    actor_type: NonEmptyStr
    actor_uuid: Optional[NonEmptyStr] = None
    action: Literal["release_failed", "replace_result"]
    scheduler_revision: Optional[int] = Field(default=None, ge=0)
    request_fingerprint: NonEmptyStr
    replacement_result_uuid: Optional[NonEmptyStr] = None
    replacement_result_version: Optional[PositiveVersion] = None
    before_payload_uuid: Optional[NonEmptyStr] = None
    after_payload_uuid: Optional[NonEmptyStr] = None
    occurred_at_ms: UnixMilliseconds
    recorded_at_ms: UnixMilliseconds

    @model_validator(mode="after")
    def _replacement_matches_action(self) -> "DecisionAuditRecord":
        has_replacement = (
            self.replacement_result_uuid is not None
            and self.replacement_result_version is not None
        )
        if (self.action == "replace_result") != has_replacement:
            raise ValueError("replace_result requires replacement result identity")
        if (self.replacement_result_uuid is None) != (
            self.replacement_result_version is None
        ):
            raise ValueError("replacement result UUID and version must be set together")
        if self.action == "release_failed" and self.scheduler_revision is None:
            raise ValueError("release_failed requires scheduler_revision")
        return self


class HistoryMaintenanceRecord(ServerObject):
    dataset_key: NonEmptyStr
    retention_action: Literal["delete", "archive_then_delete"]
    keep_days: Optional[int] = Field(default=None, ge=1)
    max_size_bytes: Optional[int] = Field(default=None, ge=1)
    archive_uri_prefix: Optional[NonEmptyStr] = None
    watermark_occurred_at_ms: UnixMilliseconds = 0
    maintenance_state: Literal["idle", "archiving", "deleting", "failed"] = "idle"
    inflight_run_uuid: Optional[NonEmptyStr] = None
    inflight_cutoff_at_ms: Optional[UnixMilliseconds] = None
    inflight_archive_uri: Optional[NonEmptyStr] = None
    inflight_archive_sha256: Optional[Sha256Hex] = None
    last_completed_run_uuid: Optional[NonEmptyStr] = None
    last_completed_at_ms: Optional[UnixMilliseconds] = None
    last_error: Optional[NonEmptyStr] = None
    updated_at_ms: UnixMilliseconds
    version: PositiveVersion = 1

    @model_validator(mode="after")
    def _validate_retention_policy(self) -> "HistoryMaintenanceRecord":
        if self.keep_days is None and self.max_size_bytes is None:
            raise ValueError("retention policy requires a time or size limit")
        if (self.retention_action == "archive_then_delete") != (
            self.archive_uri_prefix is not None
        ):
            raise ValueError("archive_then_delete requires archive_uri_prefix only")
        inflight_identity = (
            self.inflight_run_uuid is not None
            and self.inflight_cutoff_at_ms is not None
        )
        if (self.maintenance_state != "idle") != inflight_identity:
            raise ValueError("active maintenance requires run UUID and cutoff")
        if self.maintenance_state == "idle" and (
            self.inflight_archive_uri is not None
            or self.inflight_archive_sha256 is not None
        ):
            raise ValueError("idle maintenance cannot retain in-flight archive data")
        if self.retention_action == "delete":
            if (
                self.inflight_archive_uri is not None
                or self.inflight_archive_sha256 is not None
            ):
                raise ValueError("delete retention cannot use archive data")
        elif self.maintenance_state != "idle" and self.inflight_archive_uri is None:
            raise ValueError("archive maintenance requires its deterministic URI")
        if (
            self.retention_action == "archive_then_delete"
            and self.maintenance_state == "deleting"
            and self.inflight_archive_sha256 is None
        ):
            raise ValueError("archive digest is required before deleting source rows")
        if (self.maintenance_state == "failed") != bool(self.last_error):
            raise ValueError("failed maintenance requires last_error only")
        return self


__all__ = [
    "ActionAvailabilityEventRecord",
    "DecisionAuditRecord",
    "ErrorSnapshotRecord",
    "HistoryMaintenanceRecord",
    "JobFeedbackRecord",
    "JobLogRecord",
    "JobResultRecord",
    "JobTransitionRecord",
    "PayloadObjectRecord",
]
