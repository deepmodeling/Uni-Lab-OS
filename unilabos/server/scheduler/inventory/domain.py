"""仓储领域模型：状态机、不变量、领域错误.

本模块零外部依赖（纯 stdlib），不 import HTTP/ROS/sqlite。
Edge 是仓储/物料实例/物理层级/内容物/预留的唯一事实源（云端只做投影）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, TypeAlias, Union


JsonPrimitive: TypeAlias = Union[str, int, float, bool, None]
JsonValue: TypeAlias = Union[
    JsonPrimitive,
    List["JsonValue"],
    Dict[str, "JsonValue"],
]
JsonObject: TypeAlias = Dict[str, JsonValue]


# ---------------------------------------------------------------------------
# 领域错误
# ---------------------------------------------------------------------------


class InventoryError(Exception):
    """仓储领域错误基类."""

    code = "inventory_error"


class InsufficientStock(InventoryError):
    """可用数量不足（预留/消费时）."""

    code = "insufficient_stock"


class VersionConflict(InventoryError):
    """expected_version 与当前 aggregate version 不一致（禁止 Last-Write-Wins）."""

    code = "version_conflict"


class InvariantViolation(InventoryError):
    """数量不变量被破坏（非负 / available+reserved<=total）."""

    code = "invariant_violation"


class DuplicateBarcode(InventoryError):
    """barcode 在 active 实例中必须唯一."""

    code = "duplicate_barcode"


class NotFound(InventoryError):
    """目标聚合不存在."""

    code = "not_found"


class CommandRejected(InventoryError):
    """云端 command 被拒绝（版本过期/参数非法/状态机不允许）."""

    code = "command_rejected"


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class LotState(str, Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"      # 全部数量都被预留
    DEPLETED = "depleted"
    QUARANTINED = "quarantined"


class InstanceState(str, Enum):
    WAREHOUSE = "warehouse"
    RESERVED = "reserved"
    BENCH = "bench"            # 已上台（deploy）
    IN_USE = "in_use"
    CONSUMED = "consumed"
    DISCARDED = "discarded"
    QUARANTINED = "quarantined"


class ReservationState(str, Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"      # 预留已转为实际消费
    RELEASED = "released"
    QUARANTINED = "quarantined"  # 节点失败但物理已使用，转人工复核


#: instance 状态机允许的迁移（from -> {to}）
INSTANCE_TRANSITIONS: Dict[InstanceState, set] = {
    InstanceState.WAREHOUSE: {InstanceState.RESERVED, InstanceState.BENCH, InstanceState.DISCARDED,
                              InstanceState.QUARANTINED},
    InstanceState.RESERVED: {InstanceState.WAREHOUSE, InstanceState.BENCH, InstanceState.QUARANTINED},
    InstanceState.BENCH: {InstanceState.IN_USE, InstanceState.WAREHOUSE, InstanceState.CONSUMED,
                          InstanceState.DISCARDED, InstanceState.QUARANTINED},
    InstanceState.IN_USE: {InstanceState.BENCH, InstanceState.CONSUMED, InstanceState.DISCARDED,
                           InstanceState.QUARANTINED},
    InstanceState.CONSUMED: set(),
    InstanceState.DISCARDED: set(),
    InstanceState.QUARANTINED: {InstanceState.WAREHOUSE, InstanceState.DISCARDED},  # 人工复核后放行/报废
}

#: active（占用 barcode / 占用库存）的实例状态
ACTIVE_INSTANCE_STATES = {
    InstanceState.WAREHOUSE,
    InstanceState.RESERVED,
    InstanceState.BENCH,
    InstanceState.IN_USE,
    InstanceState.QUARANTINED,
}


def check_instance_transition(current: InstanceState, target: InstanceState) -> None:
    if target not in INSTANCE_TRANSITIONS[current]:
        raise CommandRejected(f"instance transition {current.value} -> {target.value} not allowed")


def lot_state_for(total: float, available: float, reserved: float, quarantined: bool) -> LotState:
    """根据数量推导 lot 状态（状态是数量的函数，不单独维护）."""
    if quarantined:
        return LotState.QUARANTINED
    if total <= 0:
        return LotState.DEPLETED
    if available <= 0 and reserved > 0:
        return LotState.RESERVED
    return LotState.AVAILABLE


def check_lot_invariants(total: float, available: float, reserved: float) -> None:
    """数量非负，available + reserved <= total."""
    if total < 0 or available < 0 or reserved < 0:
        raise InvariantViolation(
            f"negative quantity: total={total} available={available} reserved={reserved}"
        )
    # 浮点容差
    if available + reserved > total + 1e-9:
        raise InvariantViolation(
            f"available({available}) + reserved({reserved}) > total({total})"
        )


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


@dataclass
class MaterialRequirement:
    """一个节点对物料的需求（挂在 WorkflowNode 上，可选字段）.

    - lot 需求：template_id/lot_id + quantity（数量型，FIFO 扣 lot）
    - instance 需求：instance_uuid 或 barcode（实体型，deploy 具体实例）
    """

    template_id: str = ""
    lot_id: str = ""
    quantity: float = 0.0
    unit: str = ""
    instance_uuid: str = ""
    barcode: str = ""

    def is_instance_requirement(self) -> bool:
        return bool(self.instance_uuid or self.barcode)

    def to_dict(self) -> JsonObject:
        return {
            "template_id": self.template_id,
            "lot_id": self.lot_id,
            "quantity": self.quantity,
            "unit": self.unit,
            "instance_uuid": self.instance_uuid,
            "barcode": self.barcode,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MaterialRequirement":
        return cls(
            template_id=str(data.get("template_id") or data.get("templateId") or ""),
            lot_id=str(data.get("lot_id") or data.get("lotId") or ""),
            quantity=float(data.get("quantity") or 0.0),
            unit=str(data.get("unit") or ""),
            instance_uuid=str(data.get("instance_uuid") or data.get("instanceUuid") or ""),
            barcode=str(data.get("barcode") or ""),
        )


@dataclass
class OutboxEvent:
    """同步事件 envelope（sequence 由 store 落库时分配）."""

    event_id: str
    edge_id: str
    lab_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    occurred_at: int  # 毫秒
    causation_id: str
    payload: JsonObject = field(default_factory=dict)
    sequence: int = 0

    def to_dict(self) -> JsonObject:
        return {
            "event_id": self.event_id,
            "edge_id": self.edge_id,
            "lab_id": self.lab_id,
            "sequence": self.sequence,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": self.aggregate_version,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "causation_id": self.causation_id,
            "payload": self.payload,
        }


def new_event_id(occurred_at_ms: int) -> str:
    """可排序 event id：毫秒时间戳(12hex) + uuid4 尾部（UUIDv7 风格）."""
    ts_hex = format(occurred_at_ms & 0xFFFFFFFFFFFF, "012x")
    tail = uuid.uuid4().hex[12:]
    return f"{ts_hex[:8]}-{ts_hex[8:12]}-7{tail[:3]}-{tail[3:7]}-{tail[7:19]}"


def idempotency_key(workflow_id: str, node_id: str, attempt: int) -> str:
    return f"{workflow_id}:{node_id}:{attempt}"


# 需求聚合 --------------------------------------------------------------------


def aggregate_lot_requirements(requirements: List[MaterialRequirement]) -> Dict[str, float]:
    """按 (lot_id 或 template:xxx) 汇总数量型需求，用于整 DAG 预留."""
    totals: Dict[str, float] = {}
    for req in requirements:
        if req.is_instance_requirement() or req.quantity <= 0:
            continue
        key = req.lot_id if req.lot_id else f"template:{req.template_id}"
        totals[key] = totals.get(key, 0.0) + req.quantity
    return totals
