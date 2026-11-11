"""规范动作合同的注册表投影与执行值校验；旧动作保持现有语义。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


class TypedActionValueError(ValueError):
    """参数或结果违反动作合同。"""


def material_lock_parameters(schema: Mapping[str, Any]) -> list[str]:
    """从规范字段取最终物料锁集合，包括数组和可空字段。"""
    def locked(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        return value.get("x-unilabos-material-lock") is True or locked(value.get("items")) or any(
            locked(member) for member in value.get("anyOf", ())
        )

    fields = schema.get("properties", {}).get("goal", {}).get("properties", {})
    return [name for name, field in fields.items() if locked(field)]


def validate_action_values(
    schema: Mapping[str, Any] | None,
    values: Mapping[str, Any],
    *,
    section: str = "goal",
    deferred: Sequence[str] = (),
    managed_resources: bool = False,
) -> dict[str, Any]:
    """校验实际值，补合同声明的默认值；预检只推迟尚未产生的上游值。"""
    if not schema or "x-unilabos-action-contract" not in schema:
        return dict(values)
    if section == "result" and values is None and not schema["properties"][section].get("properties"):
        values = {}
    if not isinstance(values, Mapping):
        raise TypedActionValueError(f"动作{section}必须是具名字段对象")
    result = dict(values)
    from unilabos.protocol.utils.json_codec import encode_json

    try:
        encode_json(result)
    except (ValueError, TypeError) as error:
        raise TypedActionValueError(f"动作{section}包含无效 JSON 值") from error
    if section == "goal":
        # 物理结算和跨动作设备托管需要持久开始记录及资源租约，不能仅凭 Schema 宣称已执行。
        resource_contract = schema["x-unilabos-action-contract"].get("resource_contract") or {}
        unsupported = [name for name in ("device_tenancy", "operate_in_place", "transfer", "transfer_step", "aliquot", "order_sensitive") if resource_contract.get(name) and not (managed_resources and name in {"device_tenancy", "operate_in_place", "transfer", "transfer_step", "aliquot"})]
        if unsupported:
            raise TypedActionValueError("当前 dev 尚未启用资源执行合同：" + "、".join(unsupported))
    selected = deepcopy(schema["properties"][section])
    for name, field in selected.get("properties", {}).items():
        if name not in result and "default" in field and name not in deferred:
            result[name] = deepcopy(field["default"])
    for name in deferred:
        result.pop(name, None)
    selected["required"] = [key for key in selected.get("required", []) if key not in deferred]
    errors = sorted(
        Draft202012Validator(selected, format_checker=FormatChecker()).iter_errors(result),
        key=lambda error: str(list(error.path)),
    )
    if errors:
        error = errors[0]
        path = "/".join(str(part) for part in error.path)
        raise TypedActionValueError(f"动作{section}合同校验失败 /{path}: {error.message}")
    return result


def require_frozen_resource_contract(schema: Mapping[str, Any] | None, payload: Mapping[str, Any]) -> None:
    """开始许可必须封存当前驱动的资源语义，防止更新驱动后沿用旧计划绕过托管。"""
    contract = ((schema or {}).get("x-unilabos-action-contract") or {}).get("resource_contract") or {}
    frozen = payload.get("resource_contract") or {}
    if any(contract.get(key) for key in ("transfer", "transfer_step", "aliquot")) and not payload.get("material_execution"):
        raise TypedActionValueError("物理动作缺少材料权威准备记录")
    if (contract or frozen) and contract != frozen:
        raise TypedActionValueError("当前驱动资源合同与冻结执行计划不一致，请重新创建任务")
