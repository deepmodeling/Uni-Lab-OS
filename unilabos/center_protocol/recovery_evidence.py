"""灾备保管回执独立于业务 ACK；不得据此删除原 outbox 或恢复执行。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from unilabos.center_protocol.events import MAX_EVENT_SEQUENCE, EventEnvelopeError, validate_event
from unilabos.center_protocol.handoff import validate_handoff_event
from unilabos.center_protocol.material import canonical_uuid
from unilabos.center_protocol.transport_preparation import validate_event as validate_preparation_event


def validate_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EventEnvelopeError("保管证据须为原事件对象")
    return (validate_handoff_event(value) if "handoff_uuid" in value else
            validate_preparation_event(value) if "request_uuid" in value else validate_event(value))


def validate_receipt(value: Any, event: dict[str, Any]) -> dict[str, Any]:
    event = validate_envelope(event)
    identity = {"event_uuid", "event_digest", "event_epoch_uuid", "event_sequence", "station_uuid", "instance_uuid"}
    if not isinstance(value, dict) or set(value) != identity | {
        "receipt_uuid", "recovery_revision", "owner_verification", "disposition",
    }:
        raise EventEnvelopeError("灾备保管回执字段无效")
    canonical_uuid(value["receipt_uuid"])
    if (any(value[key] != event[key] for key in identity)
            or type(value["event_sequence"]) is not int
            or type(value["recovery_revision"]) is not int
            or not 1 <= value["recovery_revision"] <= MAX_EVENT_SEQUENCE
            or value["owner_verification"] not in {"original_owner", "unproven"}
            or value["disposition"] != "retained_needs_review"):
        raise EventEnvelopeError("保管回执身份不一致，或冒充执行确认")
    return deepcopy(value)
