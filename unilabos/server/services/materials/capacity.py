"""容器容量投影和内容物总量校验；额定上限不能由实例配置放宽。"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .quantity import convert_quantity, quantity_tolerance

CAPACITY_UNITS = {"max_volume_ul": "ul", "max_mass_g": "g", "max_count": "count"}


def normalize_capacity(value: Any) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) - CAPACITY_UNITS.keys():
        raise ValueError("容量仅支持 max_volume_ul、max_mass_g、max_count")
    result = {}
    for key, raw in value.items():
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (float, int)) or not math.isfinite(raw) or raw <= 0:
            raise ValueError(f"{key} 必须是正有限数值")
        result[key] = float(raw)
    return result


def capacity_projection(config: Mapping[str, Any], definition: Mapping[str, Any]) -> dict[str, float]:
    rated = normalize_capacity(definition.get("capacity"))
    local = normalize_capacity(config.get("capacity"))
    if config.get("max_volume") is not None:
        legacy = normalize_capacity({"max_volume_ul": config["max_volume"]})
        rated["max_volume_ul"] = min(rated.get("max_volume_ul", float("inf")), legacy["max_volume_ul"])
    for name, maximum in local.items():
        if name in rated and maximum > rated[name] + quantity_tolerance(maximum, rated[name]):
            raise ValueError(f"{name} 超过容器额定规格 {rated[name]:g}")
    return {**rated, **local}


def validate_contents(capacity: Mapping[str, float], substances: Sequence[Any], chemicals: Mapping[str, Any]) -> None:
    """同一容器内各份内容物合计；跨维度只采用明确液体密度，不猜测溶液浓度。"""
    for dimension, maximum in capacity.items():
        total = 0.0
        for substance in substances:
            if substance.quantity == 0:
                continue
            chemical = chemicals.get(substance.chemical_uuid)
            density = (chemical or {}).get("density_g_per_ml")
            concentration = substance.meta_data_json.get("concentration_value")
            if concentration is None and substance.composition:
                concentration = 1
            converted = convert_quantity(
                substance.quantity, substance.quantity_unit, CAPACITY_UNITS[dimension],
                inventory_type="reagent", physical_state=substance.physical_state,
                density_g_per_ml=density,
                concentration_value=concentration,
            )
            if converted is None:
                raise ValueError(f"内容物 {substance.name} 无法按 {dimension} 验证容量，请补充适用的物性或容量维度")
            total += converted[0]
            if not math.isfinite(total):
                raise ValueError("内容物总量超出有限数值范围")
        if total > maximum + quantity_tolerance(total, maximum):
            raise ValueError(f"内容物总量 {total:g} {CAPACITY_UNITS[dimension]} 超过上限 {maximum:g}")
