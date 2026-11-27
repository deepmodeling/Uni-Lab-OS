"""设备终态与库存物理结算之间的纯策略。

本模块不访问工作流库、库存库或执行器，只决定一个非成功设备终态能否释放
Claim/Fence。事务投影和桥接层共享同一稳定原因，避免失败、取消、超时分支产生
不同的物料安全语义。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MATERIAL_TRANSFER_RECONCILIATION_REQUIRED = (
    "material_transfer_inventory_reconciliation_required"
)
MATERIAL_CONTENT_RECONCILIATION_REQUIRED = (
    "material_content_inventory_reconciliation_required"
)
QUANTITY_RECONCILIATION_REQUIRED = "workflow_quantity_reconciliation_required"
_NON_SUCCESS_OUTCOMES = frozenset({"failed", "canceled", "timeout"})


class PhysicalSettlementPolicyError(ValueError):
    """物理结算输入或既有停止证明互相冲突。"""


@dataclass(frozen=True, slots=True)
class TerminalSettlementPlan:
    """一个明确设备终态对应的 Claim 释放或冻结决定。

    参数：``control_data`` 是要原样持久化的作业控制证据；
    ``uncertainty_reason`` 非空表示仍需人工/设备提交实际物料位置。返回值由事务
    投影消费；本对象不执行数据库写入，也不改变传入字典。
    """

    control_data: dict[str, Any]
    uncertainty_reason: str | None

    @property
    def hold_claim(self) -> bool:
        """返回当前终态是否必须继续冻结完整 Claim/Fence。"""

        return self.uncertainty_reason is not None


def execution_stopped_evidence(
    *,
    outcome: str,
    return_info: Mapping[str, Any],
    error_info: Sequence[Any],
) -> dict[str, Any]:
    """生成设备已停止但物料位置可能未知的不可变证据形状。

    参数：``outcome`` 只接受失败、取消或超时；其余两项是同一设备结果的返回与
    错误证据。返回：可写入 ``control_data.physical_settlement`` 的新字典。
    异常：成功、未知终态或错误的容器类型抛 ``PhysicalSettlementPolicyError``。
    """

    if outcome not in _NON_SUCCESS_OUTCOMES:
        raise PhysicalSettlementPolicyError("物理停止证明不能声明成功或未知终态")
    if not isinstance(return_info, Mapping):
        raise PhysicalSettlementPolicyError("物理停止返回信息必须是对象")
    if not isinstance(error_info, Sequence) or isinstance(
        error_info,
        (str, bytes, bytearray),
    ):
        raise PhysicalSettlementPolicyError("物理停止错误信息必须是序列")
    return {
        "execution_stopped": True,
        "outcome": outcome,
        "return_info": dict(return_info),
        "error_info": list(error_info),
    }


def plan_terminal_settlement(
    *,
    outcome: str,
    return_info: Mapping[str, Any],
    error_info: Sequence[Any],
    expected_change_set: Mapping[str, Any],
    control_data: Mapping[str, Any],
    proven_not_started: bool,
) -> TerminalSettlementPlan:
    """决定一个非成功终态是否需要物料位置对账。

    参数：设备终态证据、冻结预期 ChangeSet、既有控制证据，以及命令是否可证明
    从未发送。返回：应持久化的控制证据和不确定原因；无库存变化或未发送时原因
    为空，可立即释放 Claim。异常：未知 ChangeSet、终态或相互冲突的停止证明抛
    ``PhysicalSettlementPolicyError``，调用方必须关闭式拒绝状态推进。
    """

    evidence = execution_stopped_evidence(
        outcome=outcome,
        return_info=return_info,
        error_info=error_info,
    )
    change_kind = expected_change_set.get("kind")
    if change_kind not in {
        None,
        "no_inventory_change",
        "material_io",
        "material_transfer",
        "material_content_aliquot",
    }:
        raise PhysicalSettlementPolicyError(
            f"不支持的预期物料变化类型：{change_kind}"
        )
    updated_control = dict(control_data)
    if proven_not_started:
        return TerminalSettlementPlan(updated_control, None)
    existing = control_data.get("physical_settlement")
    if existing is not None and existing != evidence:
        raise PhysicalSettlementPolicyError("作业已有另一份物理停止证明")
    updated_control["physical_settlement"] = evidence
    if change_kind in {None, "no_inventory_change", "material_io"}:
        return TerminalSettlementPlan(updated_control, None)
    return TerminalSettlementPlan(
        updated_control,
        (
            MATERIAL_CONTENT_RECONCILIATION_REQUIRED
            if change_kind == "material_content_aliquot"
            else MATERIAL_TRANSFER_RECONCILIATION_REQUIRED
        ),
    )


__all__ = [
    "MATERIAL_CONTENT_RECONCILIATION_REQUIRED",
    "MATERIAL_TRANSFER_RECONCILIATION_REQUIRED",
    "QUANTITY_RECONCILIATION_REQUIRED",
    "PhysicalSettlementPolicyError",
    "TerminalSettlementPlan",
    "execution_stopped_evidence",
    "plan_terminal_settlement",
]
