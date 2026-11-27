"""执行计划中的中心来源上下文；只检查结构，不改写冻结协议或读取库存。"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict

from unilabos.center_protocol.digest import canonical_json


class CenterMaterialSourceContext(BaseModel):
    """内部投影允许空列表；成员身份仍由原任务包及库存准入规则验证。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    bindings: list[dict[str, Any]]
    material_refs: list[dict[str, Any]]


def source_context_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """从已验证的原任务包提取副本，保留 null、数值类型及协议字段的有无。"""

    context = {
        "bindings": deepcopy(payload.get("material_source_bindings", [])),
        "material_refs": deepcopy(payload["material_refs"]),
    }
    CenterMaterialSourceContext.model_validate(context)
    return context


def validate_source_context(
    plan: Mapping[str, Any], payload: Mapping[str, Any] | None,
) -> None:
    """按持久调用身份核对投影，禁止中心任务降级为本地来源准入。"""

    field = "center_material_source_context"
    if payload is None:
        if field in plan:
            raise ValueError("本地任务不得携带中心物料来源上下文")
        return
    value = plan.get(field)
    CenterMaterialSourceContext.model_validate(value)
    if canonical_json(value) != canonical_json(source_context_from_payload(payload)):
        raise ValueError("中心物料来源上下文与原冻结任务包不一致")
