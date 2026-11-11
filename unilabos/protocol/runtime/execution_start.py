"""执行开始的闭合协议；一次许可只允许一个不可重复的调用边界。"""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any
from pydantic import BaseModel, ConfigDict, Field
from unilabos.protocol.utils.json_codec import encode_json
import hashlib

Name = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]


class ExecutionPermit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    claim_uuid: Name
    authority_epoch: Name
    attempt_no: PositiveInt
    payload_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    fences: dict[str, PositiveInt]


class ExecutionBeginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    permit: ExecutionPermit
    invocation_uuid: Name
    executor_epoch: Name
    identity: dict[str, Any]


class ExecutionStartReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    permit: ExecutionPermit
    invocation_uuid: Name
    executor_epoch: Name
    receipt_uuid: Name
    started_at_ms: PositiveInt


def execution_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """只冻结作用于设备的身份与参数，传输附加字段不参与摘要。"""
    from unilabos.protocol.materials import InventoryRequirement
    required = ("job_id", "task_id", "node_id", "device_id", "action")
    value = {key: payload[key] for key in required}
    value.update({
        "node_run_uuid": payload.get("node_run_uuid", ""),
        "attempt_no": payload.get("attempt_no", 1),
        "action_type": payload.get("action_type") or "",
        "action_args": payload.get("action_args") or {},
        "sample_material": payload.get("sample_material") or {},
        "server_info": payload.get("server_info"),
        "inventory_reservation_uuid": payload.get("inventory_reservation_uuid"),
        "inventory_requirements": [InventoryRequirement.model_validate(item).model_dump(mode="json", exclude_none=False)
                                   for item in (payload.get("inventory_requirements") or [])],
    })
    if payload.get("resource_contract") is not None:
        value["resource_contract"] = deepcopy(payload["resource_contract"])
    if payload.get("material_execution") is not None:
        value["material_execution"] = deepcopy(payload["material_execution"])
    encode_json(value, sort_keys=True)
    return deepcopy(value)


def identity_hash(identity: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(encode_json(identity, sort_keys=True)).hexdigest()
