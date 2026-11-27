"""中心边界事件的唯一线格式及 RFC 8785 摘要合同。"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from unilabos.center_protocol.digest import DigestError, digest, is_digest

MAX_EVENT_SEQUENCE = 2**53 - 1
EVENT_FIELDS = frozenset({
    "protocol_version", "event_uuid", "event_epoch_uuid", "event_sequence",
    "station_uuid", "instance_uuid", "station_invocation_uuid", "local_task_uuid",
    "event_type", "payload", "occurred_at", "event_digest",
})
ACK_FIELDS = frozenset({
    "event_uuid", "event_epoch_uuid", "event_sequence", "event_digest",
    "application", "last_contiguous_event_sequence",
})
IDENTITY_FIELDS = ("event_uuid", "event_epoch_uuid", "event_sequence", "event_digest")
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
OUTCOME_CORE_FIELDS = frozenset({"task_payload_digest", "status", "output"})
OUTCOME_EVIDENCE_FIELDS = frozenset({"material_refs", "container_refs", "artifact_refs", "started_at", "finished_at"})


class EventEnvelopeError(ValueError):
    """信封或确认不满足持久身份合同。"""


def validate_utc_timestamp(value: Any) -> str:
    """接受秒或毫秒精度的 UTC 文本，保留原文作为摘要覆盖的事实。"""
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise EventEnvelopeError("事件时间必须为 UTC RFC3339 字符串，精度仅限秒或毫秒")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed < _EPOCH:
            raise ValueError
    except ValueError:
        raise EventEnvelopeError("事件时间无效或早于 Unix 纪元") from None
    return value


def utc_timestamp_from_ms(value: int) -> str:
    """旧毫秒事实精确转换为固定三位小数，不经过浮点时间戳。"""
    if type(value) is not int or value < 0:
        raise EventEnvelopeError("旧事件时间必须为非负整数毫秒")
    try:
        parsed = _EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        raise EventEnvelopeError("事件时间超出 RFC3339 日期范围") from None
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise EventEnvelopeError("事件身份必须为规范 UUID") from None


def validate_outcome_refs(value: Any, *, station_uuid: str) -> dict[str, Any]:
    """闭合引用 DTO；新源物料没有交接时 handoff_item_uuid 必须显式为 null。"""
    if not isinstance(value, dict) or set(value) != {"material_refs", "container_refs", "artifact_refs"}:
        raise EventEnvelopeError("结果引用字段缺失或包含未知字段")
    for collection in value.values():
        if not isinstance(collection, list) or len(collection) > 100:
            raise EventEnvelopeError("结果引用必须为最多 100 项的数组")
    materials, lineages, containers, artifacts = set(), set(), set(), set()
    for ref in value["material_refs"]:
        if not isinstance(ref, dict) or set(ref) != {
            "station_uuid", "local_material_uuid", "local_revision", "material_lineage_uuid", "handoff_item_uuid",
        }:
            raise EventEnvelopeError("物料引用字段无效")
        for key in ("station_uuid", "local_material_uuid", "material_lineage_uuid"):
            _uuid(ref[key])
        if ref["handoff_item_uuid"] is not None:
            _uuid(ref["handoff_item_uuid"])
        if (ref["station_uuid"] != station_uuid or type(ref["local_revision"]) is not int
                or not 1 <= ref["local_revision"] <= MAX_EVENT_SEQUENCE
                or ref["local_material_uuid"] in materials or ref["material_lineage_uuid"] in lineages):
            raise EventEnvelopeError("物料引用归属、版本或唯一性无效")
        materials.add(ref["local_material_uuid"])
        lineages.add(ref["material_lineage_uuid"])
    for ref in value["container_refs"]:
        if not isinstance(ref, dict) or set(ref) != {"container_uuid", "state_version"}:
            raise EventEnvelopeError("容器引用字段无效")
        _uuid(ref["container_uuid"])
        if (type(ref["state_version"]) is not int or not 1 <= ref["state_version"] <= MAX_EVENT_SEQUENCE
                or ref["container_uuid"] in containers):
            raise EventEnvelopeError("容器引用版本或唯一性无效")
        containers.add(ref["container_uuid"])
    for ref in value["artifact_refs"]:
        if (not isinstance(ref, dict) or set(ref) != {"uri", "sha256", "size", "media_type"}
                or not isinstance(ref["uri"], str) or not ref["uri"].strip() or len(ref["uri"].encode()) > 2048
                or not isinstance(ref["media_type"], str) or not ref["media_type"].strip() or len(ref["media_type"].encode()) > 255
                or not isinstance(ref["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) is None
                or type(ref["size"]) is not int or not 0 <= ref["size"] <= MAX_EVENT_SEQUENCE
                or ref["uri"] in artifacts):
            raise EventEnvelopeError("文件引用身份、大小或唯一性无效")
        artifacts.add(ref["uri"])
    return deepcopy(value)


def validate_event(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """校验并复制完整信封；禁止额外字段、布尔序号及未覆盖正文的摘要。"""
    if not isinstance(envelope, Mapping) or set(envelope) != EVENT_FIELDS:
        raise EventEnvelopeError("事件信封字段缺失或包含未知字段")
    for name in ("event_uuid", "event_epoch_uuid", "station_uuid", "instance_uuid",
                 "station_invocation_uuid"):
        _uuid(envelope[name])
    if envelope["protocol_version"] != "1.0":
        raise EventEnvelopeError("事件协议版本不兼容")
    if type(envelope["event_sequence"]) is not int or not 1 <= envelope["event_sequence"] <= MAX_EVENT_SEQUENCE:
        raise EventEnvelopeError("事件序号超出安全整数范围")
    validate_utc_timestamp(envelope["occurred_at"])
    payload = envelope["payload"]
    if not isinstance(payload, dict) or not is_digest(payload.get("task_payload_digest")):
        raise EventEnvelopeError("事件 payload 或冻结任务包摘要无效")
    kind = envelope["event_type"]
    if kind == "admission_failed":
        errors = payload.get("output", {}).get("error_info") if isinstance(payload.get("output"), dict) else None
        valid = (envelope["local_task_uuid"] is None and set(payload) == OUTCOME_CORE_FIELDS
                 and payload.get("status") == "failed" and set(payload.get("output", {})) == {"error_info"}
                 and isinstance(errors, list) and len(errors) == 1
                 and isinstance(errors[0], dict) and set(errors[0]) == {"code", "message"}
                 and isinstance(errors[0]["code"], str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", errors[0]["code"]) is not None
                 and isinstance(errors[0]["message"], str) and bool(errors[0]["message"].strip())
                 and len(errors[0]["message"].encode("utf-8")) <= 500)
    elif kind == "accepted":
        valid = (set(payload) == {"task_payload_digest", "accepted_at", "local_task_uuid"}
                 and payload["local_task_uuid"] == envelope["local_task_uuid"])
        if valid:
            validate_utc_timestamp(payload["accepted_at"])
    elif kind == "running":
        valid = set(payload) == {"task_payload_digest"}
    elif kind == "transport_progress":
        valid = set(payload) == {"task_payload_digest", "transport_progress"}
        if valid:
            from unilabos.center_protocol.transport import validate_transport_progress

            try:
                validate_transport_progress(payload["transport_progress"])
            except ValueError as error:
                raise EventEnvelopeError(str(error)) from error
    elif kind == "outcome":
        valid = (set(payload) in (OUTCOME_CORE_FIELDS, OUTCOME_CORE_FIELDS | OUTCOME_EVIDENCE_FIELDS)
                 and payload["status"] in ("succeeded", "failed", "canceled")
                 and isinstance(payload["output"], dict))
        if valid and "finished_at" in payload:
            validate_outcome_refs({key: payload[key] for key in ("material_refs", "container_refs", "artifact_refs")},
                                  station_uuid=envelope["station_uuid"])
            finished = validate_utc_timestamp(payload["finished_at"])
            if payload["started_at"] is not None:
                started = validate_utc_timestamp(payload["started_at"])
                if datetime.fromisoformat(started.replace("Z", "+00:00")) > datetime.fromisoformat(finished.replace("Z", "+00:00")):
                    raise EventEnvelopeError("结果开始时间晚于结束时间")
            if finished != envelope["occurred_at"]:
                raise EventEnvelopeError("结果结束时间必须等于事件发生时间")
    elif kind == "attention_changed":
        valid = (set(payload) == {"task_payload_digest", "required", "reason"}
                 and type(payload["required"]) is bool and isinstance(payload["reason"], str)
                 and bool(payload["reason"].strip()) and len(payload["reason"].encode("utf-8")) <= 500)
    else:
        valid = False
    if not valid:
        raise EventEnvelopeError("事件类型与 payload 不匹配")
    if kind != "admission_failed":
        _uuid(envelope["local_task_uuid"])
    body = deepcopy(dict(envelope))
    supplied = body.pop("event_digest")
    try:
        if not is_digest(supplied) or digest(body) != supplied:
            raise EventEnvelopeError("事件信封摘要不匹配")
    except DigestError as error:
        raise EventEnvelopeError("事件正文不能规范化") from error
    body["event_digest"] = supplied
    return body


def make_event(*, event_uuid: str, event_epoch_uuid: str, event_sequence: int,
               station_uuid: str, instance_uuid: str, station_invocation_uuid: str,
               local_task_uuid: str | None, event_type: str, payload: dict[str, Any],
               occurred_at: str) -> dict[str, Any]:
    """调用者先确定持久序号；摘要覆盖除 event_digest 外的完整信封。"""
    body = {
        "protocol_version": "1.0", "event_uuid": event_uuid, "event_epoch_uuid": event_epoch_uuid,
        "event_sequence": event_sequence, "station_uuid": station_uuid, "instance_uuid": instance_uuid,
        "station_invocation_uuid": station_invocation_uuid, "local_task_uuid": local_task_uuid,
        "event_type": event_type, "payload": deepcopy(payload), "occurred_at": occurred_at,
    }
    try:
        body["event_digest"] = digest(body)
    except DigestError as error:
        raise EventEnvelopeError("事件正文不能规范化") from error
    return validate_event(body)


def validate_ack(response: Mapping[str, Any], event: Mapping[str, Any]) -> str:
    """精确确认事件身份；连续前缀仅作观测，不能跳过本地其他事件。"""
    if not isinstance(response, Mapping) or set(response) != ACK_FIELDS:
        raise EventEnvelopeError("事件确认字段缺失或包含未知字段")
    if any(type(response[name]) is not type(event[name]) or response[name] != event[name]
           for name in IDENTITY_FIELDS):
        raise EventEnvelopeError("事件确认身份不匹配，保留原事件等待重传")
    contiguous = response["last_contiguous_event_sequence"]
    if type(contiguous) is not int or not 0 <= contiguous <= MAX_EVENT_SEQUENCE:
        raise EventEnvelopeError("事件确认连续前缀无效")
    application = response["application"]
    if application not in ("applied", "duplicate", "conflict"):
        raise EventEnvelopeError("事件确认入账结果未知")
    return application
