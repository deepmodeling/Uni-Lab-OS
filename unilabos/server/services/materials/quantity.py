"""数量换算：同维度换算及带明确物性证据的质量／体积换算。"""
from __future__ import annotations
import math
from typing import Any


def quantity_tolerance(*values: float) -> float:
    """随数量级缩放的舍入容差，不能抹去微量库存。"""
    return max((max(8 * math.ulp(value), abs(value) * 1e-12) for value in values), default=0.0)


def _quantity_unit(unit: Any) -> tuple[str, float] | None:
    """识别 Backend 支持的质量或体积单位；未知单位返回 ``None``。"""

    definitions = {
        "count": ("count", 1.0),
        "piece": ("count", 1.0),
        "粒": ("count", 1.0),
        "mg": ("mass", 0.001),
        "g": ("mass", 1.0),
        "kg": ("mass", 1000.0),
        "μl": ("volume", 0.001),
        "ul": ("volume", 0.001),
        "ml": ("volume", 1.0),
        "l": ("volume", 1000.0),
    }
    return definitions.get(str(unit or "").strip().casefold())


def convert_quantity(
    quantity: float,
    source_unit: str,
    target_unit: str,
    *,
    inventory_type: str = "lot",
    physical_state: Any = None,
    concentration_value: Any = None,
    density_g_per_ml: Any = None,
) -> tuple[float, dict[str, Any]] | None:
    """按 Backend 规则把上报数量换算到库存单位。

    参数：数量、源/目标单位及试剂物性。返回：换算数量和审计信息；未知单位或
    不安全的质量/体积跨维度换算返回 ``None``，调用方按单位不匹配
    记录事实且不得扣减库存。非法数量抛出 ValueError。
    """

    if isinstance(quantity, bool) or not math.isfinite(quantity) or quantity < 0:
        raise ValueError("数量必须是非负有限数值")
    normalized_source = str(source_unit or "").strip()
    normalized_target = str(target_unit or "").strip()
    # 其他旧单位仍允许同单位使用；预检和消费必须采用同一规则。
    if (
        normalized_source
        and normalized_source.casefold() == normalized_target.casefold()
    ):
        return quantity, {
            "source_quantity": quantity,
            "source_unit": source_unit,
            "target_unit": target_unit,
            "result_quantity": quantity,
            "kind": "same_unit",
        }
    source = _quantity_unit(source_unit)
    target = _quantity_unit(target_unit)
    if source is None or target is None:
        return None
    source_dimension, source_rate = source
    target_dimension, target_rate = target
    if source_dimension == target_dimension:
        converted = quantity * source_rate / target_rate
        if not math.isfinite(converted):
            return None
        return converted, {
            "source_quantity": quantity,
            "source_unit": source_unit,
            "target_unit": target_unit,
            "result_quantity": converted,
            "kind": "same_dimension",
        }
    if {source_dimension, target_dimension} != {"mass", "volume"}:
        return None
    try:
        density = float(density_g_per_ml)
    except (TypeError, ValueError):
        return None
    if (
        inventory_type != "reagent"
        or str(physical_state or "").strip().casefold() != "liquid"
        or concentration_value is not None
        or not math.isfinite(density)
        or density <= 0
    ):
        return None
    source_base = quantity * source_rate
    converted = (
        source_base / density / target_rate
        if source_dimension == "mass"
        else source_base * density / target_rate
    )
    if not math.isfinite(converted):
        return None
    return converted, {
        "source_quantity": quantity,
        "source_unit": source_unit,
        "target_unit": target_unit,
        "result_quantity": converted,
        "kind": "cross_dimension",
        "density_g_per_ml": density,
    }
