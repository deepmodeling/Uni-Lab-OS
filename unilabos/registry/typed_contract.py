"""JSON 动作的参数与返回值校验：所有 JSON 动作按注册表的统一 schema 校验。

注册表给每个 JSON 动作按签名生成统一 schema（``x-unilabos-action-contract`` 第 2 版信封，见
``unilabos.registry.action_schema``）；调度端（建任务、预检、派发）与 Host 调用前都按它校验参数，
调用后按返回注解校验返回值。没有这个信封的 schema 是 ROS 动作类型生成的、或还没补全的手写 YAML
（``--complete_registry`` 会按同一标准重新生成），不在这里校验。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

#: 由框架注入、不属于动作合同的参数：出现在参数里也不算多传字段。
_FRAMEWORK_PARAMETERS = frozenset({"sample_uuids", "action_context"})
#: Host 剥离到结果外层的保留返回字段。
_RESERVED_RESULT_FIELDS = frozenset({"unilabos_samples"})


#: 物料引用在校验时接受的线上形态：``{"uuid": ...}`` 引用（可带 id / name 等）、只给名字的引用（派发前
#: 由调度器向物料权威解析出 uuid）、上游端口传来的扁平资源树（列表）、uuid / JSON 字符串。物料必须能
#: 解析出权威 uuid 这一条由派发前的物料锁检查（``scheduler/materials.py``）和 Host 解析物料时保证。
_MATERIAL_WIRE_SCHEMA = {"anyOf": [{"type": "object"}, {"type": "array"}, {"type": "string"}]}


def _is_material_reference(schema: Mapping[str, Any]) -> bool:
    if isinstance(schema.get("x-unilabos-material-lock"), bool):
        return True
    properties = schema.get("properties")
    return (
        isinstance(properties, Mapping)
        and set(properties) == {"uuid"}
        and isinstance(properties["uuid"], Mapping)
        and properties["uuid"].get("format") == "uuid"
        and "uuid" in (schema.get("required") or [])
    )


def _validation_schema(schema: Any) -> Any:
    """校验用的 schema：物料引用字段换成接受全部线上形态的宽松 schema（展示用的 schema 不变）。"""

    if isinstance(schema, list):
        return [_validation_schema(item) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    if _is_material_reference(schema):
        kinds = schema.get("type")
        nullable = isinstance(kinds, list) and "null" in kinds
        return {"anyOf": [*_MATERIAL_WIRE_SCHEMA["anyOf"], {"type": "null"}]} if nullable else deepcopy(_MATERIAL_WIRE_SCHEMA)
    return {key: _validation_schema(value) for key, value in schema.items()}


def is_action_contract(schema: Any) -> bool:
    """schema 是注册表按统一标准生成的 JSON 动作 schema（带 ``x-unilabos-action-contract``）。"""

    return isinstance(schema, Mapping) and isinstance(schema.get("x-unilabos-action-contract"), Mapping)


class TypedActionValueError(ValueError):
    """参数或结果违反动作合同。"""


def material_lock_parameters(schema: Mapping[str, Any]) -> list[str]:
    """从规范字段取最终物料锁集合，包括数组和可空字段。

    带物料标记的字段一律加锁：旧合同里 ``MaterialLock(free=True)`` 留下的 ``false`` 标记同样
    忽略（物料锁不再按参数豁免）。
    """
    def locked(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        return isinstance(value.get("x-unilabos-material-lock"), bool) or locked(value.get("items")) or any(
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
    """校验实际值，补 schema 声明的默认值；预检只推迟尚未产生的上游值。

    ``section="goal"`` 校验参数（必须是具名字段对象，缺必填参数报错，不补 0 / 空串），返回补齐默认值的
    参数；``section="result"`` 按返回注解校验返回值（没写返回注解的 schema 是 ``{}``，不校验），原样返回。
    """
    if not is_action_contract(schema):
        return dict(values) if section == "goal" else values
    if section == "result":
        return _validate_result(schema, values)
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
        # ``order_sensitive`` 只约束计划（同一资源的兄弟动作必须显式排序，见 interval_plan），执行时无需额外处理。
        resource_contract = schema["x-unilabos-action-contract"].get("resource_contract") or {}
        managed = ("device_tenancy", "operate_in_place", "transfer", "transfer_step", "aliquot")
        unsupported = [name for name in managed if resource_contract.get(name) and not managed_resources]
        if unsupported:
            raise TypedActionValueError("当前 dev 尚未启用资源执行合同：" + "、".join(unsupported))
    selected = deepcopy(schema["properties"][section])
    for name, field in selected.get("properties", {}).items():
        if name not in result and "default" in field and name not in deferred:
            result[name] = deepcopy(field["default"])
    checked = {key: value for key, value in result.items() if key not in deferred}
    for name in _FRAMEWORK_PARAMETERS:
        if name not in selected.get("properties", {}):
            checked.pop(name, None)
    selected["required"] = [key for key in selected.get("required", []) if key not in deferred]
    missing = [key for key in selected["required"] if key not in checked]
    if missing:
        raise TypedActionValueError(f"动作参数缺少必填字段：{'、'.join(missing)}")
    for name, field in selected.get("properties", {}).items():
        # 不可空的单个物料参数给了空值（{} / "" / []，编辑器的空模板）：等于没给物料，不能放行成 None
        if (
            isinstance(field, Mapping)
            and _is_material_reference(field)
            and not (isinstance(field.get("type"), list) and "null" in field["type"])
            and name in checked
            and checked[name] in ({}, "", [])
        ):
            raise TypedActionValueError(f"物料参数 {name} 没有给物料：需要带 uuid 的物料引用")
    _raise_first_error(_validation_schema(selected), checked, "动作goal合同校验失败")
    for name in deferred:
        result.pop(name, None)
    return result


def _validate_result(schema: Mapping[str, Any], values: Any) -> Any:
    """按返回注解校验返回值；``test_mode`` 下模拟出来的返回值不来自驱动，不校验。"""

    selected = schema.get("properties", {}).get("result")
    if not isinstance(selected, Mapping) or not selected:
        return values
    from unilabos.client.host.runtime.test_mode import is_simulated_return
    from unilabos.protocol.utils.json_codec import encode_json

    if is_simulated_return(values):
        return values
    checked = values
    if isinstance(values, Mapping):
        checked = {key: value for key, value in values.items() if key not in _RESERVED_RESULT_FIELDS}
    try:
        encode_json(checked)
    except (ValueError, TypeError) as error:
        raise TypedActionValueError("动作result包含无效 JSON 值") from error
    _raise_first_error(_validation_schema(selected), checked, "动作返回值不符合返回注解")
    return values


def _raise_first_error(schema: Mapping[str, Any], value: Any, prefix: str) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda error: str(list(error.path)),
    )
    if errors:
        error = errors[0]
        path = "/".join(str(part) for part in error.path)
        raise TypedActionValueError(f"{prefix} /{path}: {error.message}")


def require_frozen_resource_contract(schema: Mapping[str, Any] | None, payload: Mapping[str, Any]) -> None:
    """开始许可必须封存当前驱动的资源语义，防止更新驱动后沿用旧计划绕过托管。"""
    contract = ((schema or {}).get("x-unilabos-action-contract") or {}).get("resource_contract") or {}
    frozen = payload.get("resource_contract") or {}
    if any(contract.get(key) for key in ("transfer", "transfer_step", "aliquot")) and not payload.get("material_execution"):
        raise TypedActionValueError("物理动作缺少材料权威准备记录")
    if (contract or frozen) and contract != frozen:
        raise TypedActionValueError("当前驱动资源合同与冻结执行计划不一致，请重新创建任务")
