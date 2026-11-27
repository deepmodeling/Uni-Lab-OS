"""按物态与有效最大装料量校验库存；已知额定规格始终约束手工上限。"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, NoReturn

from .workflow_quantity import convert_quantity

CAPACITY_KEY = "capacity"
LOADING_LIMITS_KEY = "loading_limits"
CAPACITY_FIELDS = ("max_volume_ul", "max_mass_g", "max_count")
_UNITS = {
    "ul": ("max_volume_ul", 1.0), "µl": ("max_volume_ul", 1.0),
    "μl": ("max_volume_ul", 1.0), "ml": ("max_volume_ul", 1000.0),
    "l": ("max_volume_ul", 1_000_000.0),
    "mg": ("max_mass_g", 0.001), "g": ("max_mass_g", 1.0),
    "kg": ("max_mass_g", 1000.0),
    "粒": ("max_count", 1.0),
}


def _invalid(message: str) -> NoReturn:
    # 延迟导入，资源与试剂服务可共同复用此模块。
    from .backend_contract import INVALID_PARAMETER, BackendContractError

    raise BackendContractError(INVALID_PARAMETER, message)


def json_object(value: Any) -> dict[str, Any]:
    """解码存储层 JSON 对象；空字段视为未配置。"""
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_capacity(value: Any) -> dict[str, float]:
    """规范化正有限上限；缺失、null 或空对象表示未配置。"""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(CAPACITY_FIELDS):
        _invalid("上限须为对象，仅支持 max_volume_ul、max_mass_g 和 max_count")
    result = {}
    for key, raw in value.items():
        if raw is None:
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            _invalid(f"{key} 须为正有限数值")
        if isinstance(raw, bool) or not math.isfinite(number) or number <= 0:
            _invalid(f"{key} 须为正有限数值")
        result[key] = number
    return result


def stricter_capacity(*values: dict[str, float]) -> dict[str, float]:
    """同一维度取最小值，体积与质量互不换算。"""
    return {
        key: min(value[key] for value in values if key in value)
        for key in CAPACITY_FIELDS if any(key in value for value in values)
    }


def capacity_projection(config: Any, data: Any, template_meta: Any) -> dict[str, Any]:
    """合并领域包额定规格、旧 PLR 显式容量与 OS 实例配置。"""
    config, data, template_meta = map(json_object, (config, data, template_meta))
    legacy = []
    for source in (config, data):
        if source.get("max_volume") is not None:
            legacy.append(normalize_capacity({"max_volume_ul": source["max_volume"]}))
    rated = stricter_capacity(normalize_capacity(template_meta.get(CAPACITY_KEY)), *legacy)
    local = normalize_capacity(config.get(CAPACITY_KEY))
    return {"capacity": stricter_capacity(rated, local), "rated_capacity": rated}


def material_capacity(conn: sqlite3.Connection, material_uuid: str) -> dict[str, Any]:
    """在调用方事务内读取最新规格，避免数量与上限并发写入穿透。"""
    row = conn.execute(
        "SELECT m.config,m.data,t.meta_data,m.aggregate_version FROM material m "
        "LEFT JOIN resource_template t ON t.uuid=m.resource_template_uuid "
        "AND t.deleted_at IS NULL WHERE m.uuid=? AND m.deleted_at IS NULL",
        (material_uuid,),
    ).fetchone()
    if row is None:
        return {"capacity": {}, "rated_capacity": {}, "configured_capacity": {}, "material_revision": None}
    return {**capacity_projection(row["config"], row["data"], row["meta_data"]),
            "configured_capacity": normalize_capacity(json_object(row["config"]).get(CAPACITY_KEY)),
            "material_revision": row["aggregate_version"]}


def maximum_capacity(capacity: dict[str, float], metadata: Any) -> dict[str, float]:
    """读取实际生效的最大装料量，尚未编辑的旧记录继续应用旧限制。"""
    return stricter_capacity(capacity, normalize_capacity(json_object(metadata).get(LOADING_LIMITS_KEY)))


def validate_limits(limits: dict[str, float], capacity: dict[str, float]) -> None:
    """有适用额定规格时，手工上限只能收紧；缺失维度允许手工配置。"""
    for key, limit in limits.items():
        label = {"max_volume_ul": "体积（µL）", "max_mass_g": "质量（g）", "max_count": "数量（粒）"}.get(key, key)
        if key in capacity and limit > capacity[key] + max(1e-9, capacity[key] * 1e-12):
            _invalid(f"{label}上限 {limit:g} 超过容器规格 {capacity[key]:g}")


def _conversion_context(context: dict[str, Any]) -> dict[str, Any]:
    return {"inventory_type": "reagent", "physical_state": context.get("physical_state"),
            "density_g_per_ml": context.get("density_g_per_ml"),
            "concentration_value": context.get("concentration_value")}


def _converted_capacity(capacity: dict[str, float], context: dict[str, Any]) -> dict[str, float]:
    """把液体所有已知约束投影到两维；双方约束同时取更严者。"""
    converted = []
    for key, value in capacity.items():
        if key == "max_count":
            continue
        target = "max_mass_g" if key == "max_volume_ul" else "max_volume_ul"
        source_unit, target_unit = ("µL", "g") if key == "max_volume_ul" else ("g", "µL")
        result = convert_quantity(value, source_unit, target_unit, **_conversion_context(context))
        if result is None or not math.isfinite(result[0]) or result[0] <= 0:
            _invalid("液体质量与体积换算需要适用的正有限密度，且不能声明浓度；请使用可核验的容量单位")
        converted.append({target: result[0]})
    return stricter_capacity(capacity, *converted)


def rated_capacity_for_reagent(rated: dict[str, float], context: dict[str, Any]) -> dict[str, float]:
    """投影已有额定规格；固体不推质量，液体只按适用密度换算。"""
    state = str(context.get("physical_state") or "").strip().lower()
    if state not in {"solid", "liquid"}:
        _invalid("请先确认试剂物态为固体或液体，再录入库存或修改容量")
    if state == "solid":
        return {key: rated[key] for key in ("max_mass_g", "max_count") if key in rated}
    eligible = convert_quantity(1, "g", "mL", **_conversion_context(context)) is not None
    if eligible:
        return _converted_capacity(rated, context)
    if "max_mass_g" in rated:
        _invalid("液体质量规格需要适用的正有限密度，且不能声明浓度，才能校验容积")
    return dict(rated)


def validate_quantity(quantity: float, unit: str, loading_limits: dict[str, float], *, rated_capacity: dict[str, float],
                      configured_capacity: dict[str, float], context: dict[str, Any]) -> None:
    """校验新库存、加量和容量变更；缺少适用额定值时须有正有限手工上限。"""
    rated = rated_capacity_for_reagent(rated_capacity, context)
    dimension = _UNITS.get(str(unit).strip().lower())
    if dimension is None:
        _invalid("请使用 µL、mL、L、mg、g、kg 或粒计量")
    key, factor = dimension
    state = str(context.get("physical_state") or "").strip().lower()
    if state == "solid" and key not in {"max_mass_g", "max_count"}:
        _invalid("固体只支持 mg、g、kg 或粒")
    eligible = state == "liquid" and convert_quantity(1, "g", "mL", **_conversion_context(context)) is not None
    if state == "liquid" and key == "max_mass_g" and not eligible:
        _invalid("液体按质量计量需要适用的正有限密度，且不能声明浓度")
    manual = stricter_capacity(configured_capacity, loading_limits)
    if state == "solid" and "max_volume_ul" in manual:
        _invalid("固体手工体积上限无法用于质量校验，请显式重新设置质量最大装料量")
    if state == "liquid" and not eligible and "max_mass_g" in manual:
        _invalid("现有质量上限需要适用的正有限密度，且不能声明浓度，无法跳过此约束")
    validate_limits(configured_capacity, rated)
    validate_limits(loading_limits, rated)
    effective = stricter_capacity(rated, manual)
    if eligible:
        effective = _converted_capacity(effective, context)
    if key not in effective:
        _invalid("缺少适用的最大装料量，请手动设置正有限的最大装料量后再录入或增加库存")
    actual = quantity * factor
    if not math.isfinite(actual) or actual > effective[key] + max(1e-9, effective[key] * 1e-12):
        _invalid(f"数量 {quantity:g} {unit} 超过上限 {effective[key] / factor:g} {unit}")


def reagent_metadata(values: dict[str, Any], previous: Any = None) -> dict[str, Any]:
    """合并元数据，旧客户端省略字段不会清除装料上限或分装血缘。"""
    result = {**json_object(previous), **json_object(values.get("meta_data"))}
    if LOADING_LIMITS_KEY in result:
        limits = normalize_capacity(result[LOADING_LIMITS_KEY])
        if limits:
            result[LOADING_LIMITS_KEY] = limits
        else:
            result.pop(LOADING_LIMITS_KEY)
    return result


def validate_material_config(conn: sqlite3.Connection, material_uuid: str,
                             config: dict[str, Any], *, replace_loading_limits: bool = False,
                             updating_reagent_uuid: str | None = None,
                             reagent_context: dict[str, Any] | None = None,
                             validate_stock: bool = True) -> dict[str, Any]:
    """校验新的实例配置与已有装料，在物料更新的同一事务中调用。"""
    row = conn.execute(
        "SELECT m.data,t.meta_data FROM material m LEFT JOIN resource_template t "
        "ON t.uuid=m.resource_template_uuid AND t.deleted_at IS NULL WHERE m.uuid=?",
        (material_uuid,),
    ).fetchone()
    projection = capacity_projection(config, row["data"], row["meta_data"])
    if not validate_stock:
        return projection
    configured = normalize_capacity(config.get(CAPACITY_KEY))
    reagents = conn.execute(
        "SELECT uuid,quantity,quantity_unit,meta_data,physical_state,density_g_per_ml,concentration_value FROM reagent "
        "WHERE material_uuid=? AND deleted_at IS NULL", (material_uuid,),
    ).fetchall()
    if reagent_context is not None:
        validate_limits(configured, rated_capacity_for_reagent(projection["rated_capacity"], reagent_context))
    elif not reagents:
        validate_limits(configured, projection["rated_capacity"])
    for reagent in reagents:
        # 合并编辑由调用方用新余量校验，不能以修改前余量阻止一次性下调。
        if reagent["uuid"] == updating_reagent_uuid:
            continue
        limits = {} if replace_loading_limits else normalize_capacity(
            json_object(reagent["meta_data"]).get(LOADING_LIMITS_KEY))
        validate_quantity(reagent["quantity"], reagent["quantity_unit"], limits,
                          rated_capacity=projection["rated_capacity"], configured_capacity=configured,
                          context=dict(reagent))
    return projection
