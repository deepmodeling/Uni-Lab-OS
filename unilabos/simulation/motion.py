"""规划回执与证据等级的运行时合同。

OS 只校验领域 planner 交回的轨迹回执，不接管 MoveIt/FCL，也不保存第二份规划结果。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReceiptRejected(ValueError):
    """回执不满足运行时合同；调用方不得据此结算或升级证据。"""


class EvidenceLevel(str, Enum):
    """证据等级；数值用于拒绝跨级升级，不代表可自动提升。"""

    PLANNER_ONLY = "planner_only"
    COLLISION_CHECKED = "collision_checked"
    KINEMATIC_REPLAY = "kinematic_replay"
    PHYSICS_OBSERVED = "physics_observed"
    PHYSICAL_QUALIFIED = "physical_qualified"

    @property
    def rank(self) -> int:
        return list(type(self)).index(self)


class CollisionStatus(str, Enum):
    """碰撞检查结果；not_checked 不得被当作 clear。"""

    CLEAR = "clear"
    COLLISION = "collision"
    INCOMPLETE = "incomplete"
    TIMEOUT = "timeout"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True)
class TrajectoryReceipt:
    """领域 planner 产出、OS 校验的不可变回执。"""

    action_id: str
    trajectory_digest: str
    scene_digest: str
    units: str
    frames: tuple[str, ...]
    topology: str
    planned: str
    executed: str | None = None
    observed: str | None = None
    settled: str | None = None
    evidence_level: EvidenceLevel = EvidenceLevel.PLANNER_ONLY
    collision_status: CollisionStatus = CollisionStatus.NOT_CHECKED
    replay: bool = False


def verify_trajectory_receipt(receipt: TrajectoryReceipt) -> None:
    """校验回执；通过返回 None，不通过抛 ReceiptRejected。"""
    if not receipt.planned.strip():
        raise ReceiptRejected("planned_stage_required")
    if not _DIGEST.match(receipt.trajectory_digest) or not _DIGEST.match(receipt.scene_digest):
        raise ReceiptRejected("digest_malformed")
    if (
        receipt.evidence_level.rank >= EvidenceLevel.PHYSICAL_QUALIFIED.rank
        and receipt.collision_status is not CollisionStatus.CLEAR
    ):
        raise ReceiptRejected("physical_qualification_requires_clear_collision")
    if receipt.observed and not receipt.executed:
        raise ReceiptRejected("stage_order_invalid")
    if receipt.settled and not receipt.observed:
        raise ReceiptRejected("stage_order_invalid")
    if receipt.replay and receipt.evidence_level.rank > EvidenceLevel.KINEMATIC_REPLAY.rank:
        raise ReceiptRejected("replay_cannot_claim_runtime_evidence")
    if (
        receipt.evidence_level.rank >= EvidenceLevel.COLLISION_CHECKED.rank
        and receipt.collision_status is CollisionStatus.NOT_CHECKED
    ):
        raise ReceiptRejected("evidence_requires_collision_check")
    if not receipt.units.strip() or not receipt.topology.strip():
        raise ReceiptRejected("units_and_topology_required")
    if not receipt.frames or len(set(receipt.frames)) != len(receipt.frames):
        raise ReceiptRejected("frames_invalid")
