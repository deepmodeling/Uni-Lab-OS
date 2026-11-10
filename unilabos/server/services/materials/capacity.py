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


def validate_contents(
    capacity: Mapping[str, float],
    substances: Sequence[Any],
    chemicals: Mapping[str, Any],
    *,
    allow_unquantified_volume: bool = False,
) -> None:
    """合计内容物；只有旧 max_volume 允许保存无法可靠换算体积的事实。

    显式 capacity 默认要求全部内容物可验算。旧规格仅检查已知体积的合计，
    通过检查不代表未知部分没有体积，也不构成设备动作的容量许可。
    """
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
            conversion = {
                "inventory_type": "reagent",
                "physical_state": substance.physical_state,
                "density_g_per_ml": density,
                "concentration_value": concentration,
            }
            target_unit = CAPACITY_UNITS[dimension]
            converted = convert_quantity(
                substance.quantity, substance.quantity_unit, target_unit, **conversion,
            )
            if converted is None:
                if allow_unquantified_volume and dimension == "max_volume_ul":
                    # 零量只探测单位和物性是否足以换算，不能把数值溢出当成缺少物性。
                    if convert_quantity(0, substance.quantity_unit, target_unit, **conversion) is None:
                        continue
                raise ValueError(f"内容物 {substance.name} 无法按 {dimension} 验证容量，请补充适用的物性或容量维度")
            total += converted[0]
            if not math.isfinite(total):
                raise ValueError("内容物总量超出有限数值范围")
        if total > maximum + quantity_tolerance(total, maximum):
            raise ValueError(f"内容物总量 {total:g} {CAPACITY_UNITS[dimension]} 超过上限 {maximum:g}")
