"""派发凭据跨库 Saga 的保守收敛合同。"""

from __future__ import annotations

import pytest

from unilabos.workflow.dispatch_permit_saga import (
    DispatchPermitSagaError,
    freeze_projected_dispatch_permit,
)


def test_freeze_attempts_workflow_attention_when_inventory_transition_fails() -> None:
    """库存冻结失败也必须尝试冻结工作流 Claim。

    参数：无。返回：无。异常：预期聚合为
    ``DispatchPermitSagaError``，同时断言工作流投影仍被调用，
    防止一侧 SQLite 故障让另一侧继续运行。
    """

    events: list[tuple[str, str]] = []

    class _Inventory:
        """注入库存 Claim 冻结失败的窄测试替身。"""

        def transition_dispatch_permit(
            self,
            claim_uuid: str,
            *,
            target_state: str,
        ) -> None:
            """记录转换并抛存储错误；参数是 Claim 与目标状态。"""

            events.append(("inventory", f"{claim_uuid}:{target_state}"))
            raise RuntimeError("库存库不可写")

    class _Projection:
        """记录工作流物理对账投影的窄测试替身。"""

        def project_execution_attention(
            self,
            job_uuid: str,
            *,
            reason: str,
        ) -> None:
            """记录作业与不确定原因；参数原样保存，返回无。"""

            events.append(("workflow", f"{job_uuid}:{reason}"))

    with pytest.raises(DispatchPermitSagaError) as captured_error:
        freeze_projected_dispatch_permit(
            inventory=_Inventory(),
            projection=_Projection(),
            job_uuid="20000000-0000-4000-8000-000000000001",
            claim_uuid="75000000-0000-4000-8000-000000000001",
        )

    assert isinstance(captured_error.value.__cause__, RuntimeError)
    assert events == [
        (
            "inventory",
            "75000000-0000-4000-8000-000000000001:uncertain",
        ),
        (
            "workflow",
            "20000000-0000-4000-8000-000000000001:"
            "inventory_permit_commit_unknown",
        ),
    ]
