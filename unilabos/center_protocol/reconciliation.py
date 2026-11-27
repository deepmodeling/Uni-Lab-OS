"""单调用核实的纯协议校验；核实回执始终不授予新执行权限。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from unilabos.center_protocol.digest import canonical_json, digest, is_digest


class ReconciliationError(ValueError):
    """核实请求、快照或确认与冻结身份不符。"""


def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ReconciliationError("核实身份必须是规范 UUID") from None


def _revision(value: Any) -> None:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ReconciliationError("核实 revision 必须是安全非负整数")


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "expected_state_revision", "expected_result_revision", "expected_evidence_digest",
        "reason", "evidence_refs", "resolution",
    }:
        raise ReconciliationError("核实请求字段无效")
    for key in ("expected_state_revision", "expected_result_revision"):
        _revision(value[key])
    if not is_digest(value["expected_evidence_digest"]):
        raise ReconciliationError("核实证据摘要无效")
    if (not isinstance(value["reason"], str) or not value["reason"].strip()
            or len(value["reason"].encode("utf-8")) > 1000):
        raise ReconciliationError("核实原因必须非空且不超过 1000 字节")
    refs = value["evidence_refs"]
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 32
            or any(not isinstance(ref, str) or not ref.strip() or len(ref.encode("utf-8")) > 2048 for ref in refs)):
        raise ReconciliationError("核实证据引用必须为 1 至 32 个非空字符串")
    resolution = value["resolution"]
    if not isinstance(resolution, dict):
        raise ReconciliationError("核实 resolution 必须是对象")
    kinds = {
        "record_evidence": {"kind"},
        "confirm_recorded_result": {"kind", "local_task_uuid", "result_event_uuid", "result_digest"},
        "resume_original_task": {"kind", "local_task_uuid", "payload_digest", "event_epoch_uuid"},
    }
    if (not isinstance(resolution.get("kind"), str) or resolution["kind"] not in kinds
            or set(resolution) != kinds[resolution["kind"]]):
        raise ReconciliationError("核实 resolution 类型或字段无效")
    for key in ("local_task_uuid", "result_event_uuid", "event_epoch_uuid"):
        if key in resolution:
            _uuid(resolution[key])
    for key in ("payload_digest", "result_digest"):
        if key in resolution and not is_digest(resolution[key]):
            raise ReconciliationError("核实执行或结果摘要无效")
    return deepcopy(value)


def validate_snapshot(value: Any, invocation_uuid: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "station_invocation_uuid", "global_task_uuid", "station_uuid", "workflow_uuid", "payload_digest",
        "state", "state_revision", "result_revision", "recovery_blocked", "result_events", "event_receipts",
        "conflicts", "history", "evidence_digest",
    }:
        raise ReconciliationError("核实快照字段无效")
    for key in ("station_invocation_uuid", "global_task_uuid", "station_uuid", "workflow_uuid"):
        _uuid(value[key])
    if value["station_invocation_uuid"] != invocation_uuid or type(value["recovery_blocked"]) is not bool:
        raise ReconciliationError("核实快照调用身份或恢复保护无效")
    for key in ("payload_digest", "evidence_digest"):
        if not is_digest(value[key]):
            raise ReconciliationError("核实快照摘要无效")
    for key in ("state_revision", "result_revision"):
        _revision(value[key])
    state = value["state"]
    if not isinstance(state, dict) or set(state) != {
        "dispatch_status", "execution_status", "attention_state", "owner_instance_uuid",
        "local_task_uuid", "result_digest", "result_revision",
    }:
        raise ReconciliationError("核实调用状态字段无效")
    if (any(not isinstance(state[key], str) for key in ("dispatch_status", "execution_status", "attention_state"))
            or state["dispatch_status"] not in {"pending", "waiting_for_station", "ready", "offered", "claimed", "accepted", "canceled", "rejected", "skipped"}
            or state["execution_status"] not in {"not_started", "running", "succeeded", "failed", "canceled"}
            or state["attention_state"] not in {"clear", "required"}
            or state["result_revision"] != value["result_revision"]):
        raise ReconciliationError("核实调用状态值无效")
    _revision(state["result_revision"])
    for key in ("owner_instance_uuid", "local_task_uuid"):
        if state[key] is not None:
            _uuid(state[key])
    if state["result_digest"] is not None and not is_digest(state["result_digest"]):
        raise ReconciliationError("核实结果摘要无效")
    for key in ("result_events", "event_receipts", "conflicts", "history"):
        if not isinstance(value[key], list):
            raise ReconciliationError("核实证据必须是数组")
    if digest({key: item for key, item in value.items() if key != "evidence_digest"}) != value["evidence_digest"]:
        raise ReconciliationError("核实快照证据摘要不匹配")
    return deepcopy(value)


def validate_receipt(value: Any, invocation_uuid: str, request: dict[str, Any],
                     *, actor_instance_uuid: str) -> dict[str, Any]:
    request = validate_request(request)
    if not isinstance(value, dict) or set(value) != {"reconciliation_uuid", "resolution", "snapshot", "execution_authorized"}:
        raise ReconciliationError("核实回执字段无效")
    _uuid(value["reconciliation_uuid"])
    if value["execution_authorized"] is not False or canonical_json(value["resolution"]) != canonical_json(request["resolution"]):
        raise ReconciliationError("核实回执不得授予执行权或改写 resolution")
    snapshot = validate_snapshot(value["snapshot"], invocation_uuid)
    matches = [item for item in snapshot["history"] if isinstance(item, dict)
               and item.get("reconciliation_uuid") == value["reconciliation_uuid"]]
    if len(matches) != 1:
        raise ReconciliationError("核实回执必须对应唯一持久历史记录")
    history = matches[0]
    if (history.get("request_digest") != digest(request)
            or history.get("resolution") != request["resolution"]
            or history.get("reason") != request["reason"]
            or history.get("evidence_refs") != request["evidence_refs"]
            or history.get("actor_kind") != "machine" or history.get("actor_uuid") != actor_instance_uuid):
        raise ReconciliationError("核实回执历史没有精确绑定原请求与原实例")
    resolution = request["resolution"]
    if "local_task_uuid" in resolution and snapshot["state"]["local_task_uuid"] != resolution["local_task_uuid"]:
        raise ReconciliationError("核实回执本地 Task 身份不匹配")
    if resolution["kind"] == "resume_original_task" and snapshot["payload_digest"] != resolution["payload_digest"]:
        raise ReconciliationError("恢复回执冻结任务摘要不匹配")
    if resolution["kind"] == "confirm_recorded_result" and (
        snapshot["state"]["execution_status"] not in {"succeeded", "failed", "canceled"}
        or snapshot["state"]["result_digest"] != resolution["result_digest"]
    ):
        raise ReconciliationError("结果核实回执没有精确匹配已记录终态")
    return deepcopy(value)
