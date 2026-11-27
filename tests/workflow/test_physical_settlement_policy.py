"""设备终态到物料物理结算策略的纯合同测试。"""

from __future__ import annotations

import pytest

from unilabos.workflow.physical_settlement_policy import (
    MATERIAL_TRANSFER_RECONCILIATION_REQUIRED,
    PhysicalSettlementPolicyError,
    plan_terminal_settlement,
)


@pytest.mark.parametrize("outcome", ["failed", "canceled", "timeout"])
def test_unsuccessful_transfer_holds_claim_until_material_position_is_known(
    outcome: str,
) -> None:
    """失败、取消和超时转运都必须生成停止证明并冻结完整 Claim。

    参数：``outcome`` 覆盖三个非成功设备终态。返回无；断言策略给出统一物料
    对账原因和原始结果证据。异常：任何终态提前释放都会使断言失败。
    """

    plan = plan_terminal_settlement(
        outcome=outcome,
        return_info={"last_step": "place"},
        error_info=[{"code": "device_stopped"}],
        expected_change_set={
            "kind": "material_transfer",
            "material_uuid": "material-a",
        },
        control_data={"operator": "edge"},
        proven_not_started=False,
    )

    assert plan.hold_claim is True
    assert plan.uncertainty_reason == MATERIAL_TRANSFER_RECONCILIATION_REQUIRED
    assert plan.control_data["operator"] == "edge"
    assert plan.control_data["physical_settlement"] == {
        "execution_stopped": True,
        "outcome": outcome,
        "return_info": {"last_step": "place"},
        "error_info": [{"code": "device_stopped"}],
    }


def test_proven_not_started_transfer_can_release_without_fake_stop_evidence() -> None:
    """可证明未发送的转运可以释放，且不得伪造设备停止回执。

    参数与返回均为空。异常：策略仍要求物料对账或写入物理停止证据时断言失败。
    """

    plan = plan_terminal_settlement(
        outcome="canceled",
        return_info={"cancel_reason": "local_no_send_proof"},
        error_info=[],
        expected_change_set={"kind": "material_transfer"},
        control_data={},
        proven_not_started=True,
    )

    assert plan.hold_claim is False
    assert plan.control_data == {}


def test_unknown_change_kind_fails_closed() -> None:
    """未定义的库存变化不能被当作无变化而释放 Claim。

    参数与返回均为空。异常：预期并断言 ``PhysicalSettlementPolicyError``。
    """

    with pytest.raises(PhysicalSettlementPolicyError, match="不支持"):
        plan_terminal_settlement(
            outcome="failed",
            return_info={},
            error_info=[],
            expected_change_set={"kind": "implicit_device_side_move"},
            control_data={},
            proven_not_started=False,
        )
