"""工作流库与库存库之间的派发凭据 Saga 收敛策略。"""

from __future__ import annotations

from typing import Any, Protocol

INVENTORY_PERMIT_COMMIT_UNKNOWN = "inventory_permit_commit_unknown"


class DispatchPermitInventory(Protocol):
    """派发凭据 Saga 所需的最小库存生命周期接口。"""

    def transition_dispatch_permit(
        self,
        claim_uuid: str,
        *,
        target_state: str,
    ) -> None:
        """推进指定 Claim；存储与状态冲突异常由实现抛出。"""


class DispatchAttentionProjection(Protocol):
    """派发凭据 Saga 所需的最小工作流投影接口。"""

    def project_execution_attention(
        self,
        job_uuid: str,
        *,
        reason: str,
    ) -> Any:
        """把作业及执行锁冻结为待物理对账；返回形状由实现决定。"""


class DispatchPermitSagaError(RuntimeError):
    """已投影派发凭据无法在两个权威中完整冻结。"""


def freeze_projected_dispatch_permit(
    *,
    inventory: DispatchPermitInventory,
    projection: DispatchAttentionProjection,
    job_uuid: str,
    claim_uuid: str,
) -> None:
    """冻结已投影但库存确认结果不明的派发凭据。

    参数：``inventory`` 与 ``projection`` 是两个 SQLite 权威的
    窄接口；``job_uuid`` 是已持久化派发意图的作业；
    ``claim_uuid`` 是库存签发的同一 Claim。返回：两侧都完成
    ``uncertain`` 冻结时无返回值。异常：任一侧失败时仍尝试
    另一侧，最后聚合为 ``DispatchPermitSagaError``；调用方不得
    释放资源或继续物理派发。
    """

    failures: list[BaseException] = []
    try:
        inventory.transition_dispatch_permit(
            claim_uuid,
            target_state="uncertain",
        )
    except BaseException as error:  # noqa: BLE001 - 另一侧仍必须冻结
        failures.append(error)
    try:
        projection.project_execution_attention(
            job_uuid,
            reason=INVENTORY_PERMIT_COMMIT_UNKNOWN,
        )
    except BaseException as error:  # noqa: BLE001 - 两侧都尝试后统一报错
        failures.append(error)
    if failures:
        raise DispatchPermitSagaError(
            f"派发凭据无法完整冻结：{job_uuid}"
        ) from failures[0]


__all__ = [
    "DispatchAttentionProjection",
    "DispatchPermitInventory",
    "DispatchPermitSagaError",
    "INVENTORY_PERMIT_COMMIT_UNKNOWN",
    "freeze_projected_dispatch_permit",
]
