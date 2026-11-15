"""资源条件失败分成「等待」与「失败」（V1 ``app/scheduler/resource_wait_policy.py``）。

目标库位被占、夹爪忙、来源暂时不在库位、库位被入口预留或另一作业未结算的物理动作占着……
这些条件会随其他作业、运输或物料移动而改变：attempt 保持排队，下一次资源变化时重试，不记失败。
合同错误（库位不属于目标容器、夹爪角色缺失或重复、参数 Schema 不符）照常失败。未知或空的码
一律按不可恢复处理，避免永久等待掩盖配置错误。
"""

from __future__ import annotations

from typing import Optional

#: 会随其他作业 / 物料移动而解除的资源条件（码与 V1 相同）
TEMPORARY_RESOURCE_CONDITIONS = frozenset(
    {
        "gripper_site_occupied",
        "operate_in_place_site_changed",
        "operate_in_place_site_missing",
        "site_claimed",
        "site_group_unavailable",
        "site_ingress_reserved",
        "site_occupied",
        "transfer_source_site_missing",
    }
)

#: 物料权威冲突类别 → 资源条件码（``active_claim``：物料或库位仍被另一作业未结算的物理动作占着）
_CONFLICT_CONDITIONS = {"active_claim": "site_claimed"}
#: 经 HTTP 物料网关时冲突类别不过线，只剩信封业务码：6007 库位被占；6011 活动 Claim 或入口预留
#: （两者在信封里同码，都会随其他作业结算或入库完成而解除）
_BUSINESS_CODE_CONDITIONS = {6007: "site_occupied", 6011: "site_claimed"}


class ResourceConditionWait(ValueError):
    """派发前解析资源合同时遇到的临时条件；``code`` 取 :data:`TEMPORARY_RESOURCE_CONDITIONS`。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.wait_code = code


def temporary_resource_condition(error: BaseException) -> Optional[str]:
    """异常对应的临时资源条件码；不是临时条件（应当失败）时返回 ``None``。"""

    code = getattr(error, "wait_code", None) or getattr(error, "conflict", None)
    if not code:
        business_code = getattr(error, "business_code", None)
        if isinstance(business_code, int) and not isinstance(business_code, bool):
            code = _BUSINESS_CODE_CONDITIONS.get(business_code)
    code = _CONFLICT_CONDITIONS.get(str(code or ""), str(code or "")).strip()
    return code if code in TEMPORARY_RESOURCE_CONDITIONS else None


__all__ = ["ResourceConditionWait", "TEMPORARY_RESOURCE_CONDITIONS", "temporary_resource_condition"]
