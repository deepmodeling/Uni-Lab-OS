"""历史收件查询：精确确认与连续前缀独立，缺口不能猜测身份。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from unilabos.center_protocol.digest import is_digest
from unilabos.center_protocol.events import MAX_EVENT_SEQUENCE, EventEnvelopeError, validate_ack, validate_event
from unilabos.center_protocol.handoff import validate_handoff_ack, validate_handoff_event
from unilabos.center_protocol.material import canonical_uuid
from unilabos.center_protocol.transport_preparation import validate_event as validate_preparation_event


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"event_epoch_uuid", "event_sequence", "identity"}:
        raise EventEnvelopeError("历史查询字段无效")
    canonical_uuid(value["event_epoch_uuid"])
    sequence = value["event_sequence"]
    if sequence is not None and (type(sequence) is not int or not 1 <= sequence <= MAX_EVENT_SEQUENCE):
        raise EventEnvelopeError("历史序号无效")
    identity = value["identity"]
    if identity is not None:
        if sequence is None or not isinstance(identity, dict) or set(identity) != {"event_uuid", "event_digest"}:
            raise EventEnvelopeError("精确历史身份缺失序号或字段无效")
        canonical_uuid(identity["event_uuid"])
        if not is_digest(identity["event_digest"]):
            raise EventEnvelopeError("历史摘要无效")
    return deepcopy(value)


def validate_response(value: Any, request: dict[str, Any], *, station_uuid: str, instance_uuid: str) -> dict[str, Any]:
    request = validate_request(request)
    if not isinstance(value, dict) or set(value) != {
        "station_uuid", "instance_uuid", "event_epoch_uuid", "event_sequence", "status",
        "last_contiguous_event_sequence", "highest_received_event_sequence", "record",
    }:
        raise EventEnvelopeError("历史确认字段无效")
    if (value["station_uuid"] != station_uuid or value["instance_uuid"] != instance_uuid
            or value["event_epoch_uuid"] != request["event_epoch_uuid"]
            or type(value["event_sequence"]) is not type(request["event_sequence"])
            or value["event_sequence"] != request["event_sequence"]):
        raise EventEnvelopeError("历史确认不属于原工站、实例、纪元或序号")
    prefix, highest = value["last_contiguous_event_sequence"], value["highest_received_event_sequence"]
    if any(type(n) is not int or not 0 <= n <= MAX_EVENT_SEQUENCE for n in (prefix, highest)) or prefix > highest:
        raise EventEnvelopeError("历史确认连续前缀无效")
    status = value["status"]
    if status not in ("prefix", "missing", "recorded", "ambiguous") or ((status == "prefix") != (request["event_sequence"] is None)):
        raise EventEnvelopeError("历史确认状态与查询不匹配")
    record = value["record"]
    if status != "recorded":
        if record is not None:
            raise EventEnvelopeError("未证明历史事件不能附带成功确认")
    else:
        if not isinstance(record, dict) or set(record) != {"envelope", "acknowledgement", "handoff_response"}:
            raise EventEnvelopeError("历史事件证明字段无效")
        raw = record["envelope"]
        handoff = isinstance(raw, dict) and "handoff_uuid" in raw
        event = (validate_handoff_event(raw) if handoff else
                 validate_preparation_event(raw) if "request_uuid" in raw else validate_event(raw))
        if (any(event[key] != value[key] for key in ("station_uuid", "instance_uuid", "event_epoch_uuid", "event_sequence"))
                or event["event_sequence"] > highest
                or (request["identity"] is not None and any(event[key] != item for key, item in request["identity"].items()))):
            raise EventEnvelopeError("历史信封身份不匹配")
        validate_ack(record["acknowledgement"], event)
        if record["acknowledgement"]["last_contiguous_event_sequence"] != prefix:
            raise EventEnvelopeError("历史确认的当前前缀不一致")
        if record["handoff_response"] is not None:
            if not handoff:
                raise EventEnvelopeError("调用事件不能携带交接响应")
            validate_handoff_ack(record["handoff_response"], event)
    return deepcopy(value)
