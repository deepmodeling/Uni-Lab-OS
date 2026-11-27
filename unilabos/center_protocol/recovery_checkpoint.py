"""经原 mTLS 会话读取的中心恢复基线；绑定当前本地灾备事件身份。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from unilabos.center_protocol.events import MAX_EVENT_SEQUENCE, EventEnvelopeError
from unilabos.center_protocol.material import canonical_uuid


def validate_checkpoint(value: Any, *, checkpoint_uuid: str, station_uuid: str, instance_uuid: str,
                        event_epoch_uuid: str, recovery_uuid: str) -> dict[str, Any]:
    fields = {"checkpoint_uuid", "recovery_uuid", "station_uuid", "instance_uuid", "event_epoch_uuid",
              "command_high_watermark", "event_high_watermark", "invocations", "reason", "created_at_ms"}
    if not isinstance(value, dict) or set(value) != fields:
        raise EventEnvelopeError("中心恢复基线字段不完整")
    expected = {"checkpoint_uuid": checkpoint_uuid, "recovery_uuid": recovery_uuid, "station_uuid": station_uuid,
                "instance_uuid": instance_uuid, "event_epoch_uuid": event_epoch_uuid}
    for key, identity in expected.items():
        canonical_uuid(identity)
        if value[key] != identity:
            raise EventEnvelopeError("恢复基线不属于当前实例、纪元或本次灾备")
    if (any(type(value[key]) is not int or not 0 <= value[key] <= MAX_EVENT_SEQUENCE
            for key in ("command_high_watermark", "event_high_watermark", "created_at_ms"))
            or not isinstance(value["reason"], str) or not value["reason"].strip()
            or not isinstance(value["invocations"], list)):
        raise EventEnvelopeError("恢复基线的高水位或核实说明无效")
    invocations, submissions = set(), set()
    for item in value["invocations"]:
        if not isinstance(item, dict) or set(item) != {"invocation_uuid", "global_task_uuid", "invocation_key"}:
            raise EventEnvelopeError("原调用绑定字段无效")
        canonical_uuid(item["invocation_uuid"])
        canonical_uuid(item["global_task_uuid"])
        if not isinstance(item["invocation_key"], str) or not item["invocation_key"]:
            raise EventEnvelopeError("原调用键无效")
        key = (item["global_task_uuid"], item["invocation_key"])
        if item["invocation_uuid"] in invocations or key in submissions:
            raise EventEnvelopeError("恢复基线中的原调用身份不唯一")
        invocations.add(item["invocation_uuid"])
        submissions.add(key)
    return deepcopy(value)
