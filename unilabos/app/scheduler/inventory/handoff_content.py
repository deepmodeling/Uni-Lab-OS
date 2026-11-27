"""交接内容的可携带历史：工站限定的组分引用不等同于本地可调度试剂瓶。"""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime
from typing import Any, NoReturn
from uuid import UUID

SAMPLE_HISTORY_FIELDS = ("sample_type", "source", "collected_at", "expires_at")
SUBSTANCE_FIELDS = ("name", "quantity", "quantity_unit", "physical_state", "observed_at", "description", "meta_data")
CHEMICAL_FIELDS = ("cas", "inchi_key", "molecular_formula")
_COMPONENT_FIELDS = {"reagent_uuid", "reagent_info_uuid", "name", "quantity", "quantity_unit", "source_station_uuid", "chemical_identity"}
_COMPONENT_OPTIONAL = {"concentration_value", "concentration_unit"}


def _reject(message: str) -> NoReturn:
    from .handoff import HandoffInventoryError

    raise HandoffInventoryError("invalid_content_history", message)


def _identity(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _number(value: Any) -> bool:
    return (type(value) is int and 0 <= value <= 2**53 - 1
            or type(value) is float and math.isfinite(value) and value >= 0)


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_sample_history(value: Any) -> None:
    """保留旧字段的原字符串与 null；不为旧无时区记录猜测时区或来源身份。"""
    if not isinstance(value, dict) or set(value) != set(SAMPLE_HISTORY_FIELDS):
        _reject("历史样品字段必须完整且不含未知字段")
    for key, item in value.items():
        if item is not None and (not isinstance(item, str) or key in {"collected_at", "expires_at"} and not _timestamp(item)):
            _reject("历史样品类型/来源必须是字符串，采集/过期时间必须是 ISO 日期时间")


def _chemical_identity(c, identity: str) -> dict[str, Any]:
    row = c.execute("SELECT * FROM reagent_info WHERE uuid=? AND deleted_at IS NULL", (identity,)).fetchone()
    if row is None:
        _reject("历史组分目录不存在，必须显式恢复化学身份后再转运")
    return {key: row[key] for key in CHEMICAL_FIELDS}


def export_composition(c, composition: str, station_uuid: str) -> list[dict[str, Any]]:
    """读取当前目录核验冻结组分；已消耗/软删除的历史试剂瓶无需重新变为活动库存。"""
    try:
        original = json.loads(composition)
    except (ValueError, TypeError):
        _reject("历史混合物组分 JSON 已损坏")
    if not isinstance(original, list):
        _reject("历史混合物组分必须是列表")
    result = []
    for raw in original:
        if not isinstance(raw, dict) or not _identity(raw.get("reagent_info_uuid")):
            _reject("历史混合物组分目录引用无效")
        component = copy.deepcopy(raw)
        chemistry = _chemical_identity(c, component["reagent_info_uuid"])
        if "chemical_identity" not in component:
            _reject("历史组分缺少形成时的化学身份凭证，需先核实，不能使用当前目录补造历史")
        if component["chemical_identity"] != chemistry:
            _reject("组分冻结化学身份与当前目录冲突，不能重新解释历史组成")
        component.setdefault("source_station_uuid", station_uuid)
        result.append(component)
    validate_composition(result)
    return result


def validate_composition(components: Any) -> set[str]:
    """严格核验历史引用和值；只核验已知计量维度，不推断组分合计或化学反应。"""
    if not isinstance(components, list) or len(components) > 1024:
        _reject("历史混合物组分必须是有限列表")
    identities, infos = set(), set()
    for component in components:
        if (not isinstance(component, dict) or not _COMPONENT_FIELDS <= set(component)
                or set(component) - _COMPONENT_FIELDS - _COMPONENT_OPTIONAL):
            _reject("历史组分字段不完整或包含未知字段")
        if any(not _identity(component[key]) for key in ("reagent_uuid", "reagent_info_uuid", "source_station_uuid")):
            _reject("历史组分必须有规范工站、试剂与化学目录 UUID")
        key = (component["source_station_uuid"], component["reagent_uuid"])
        if key in identities:
            _reject("历史组分工站与试剂身份重复")
        identities.add(key)
        infos.add(component["reagent_info_uuid"])
        if not isinstance(component["name"], str) or not component["name"].strip() or not _number(component["quantity"]):
            _reject("历史组分名称或数量无效")
        validate_unit(component["quantity_unit"])
        chemistry = component["chemical_identity"]
        if (not isinstance(chemistry, dict) or set(chemistry) != set(CHEMICAL_FIELDS)
                or any(value is not None and not isinstance(value, str) for value in chemistry.values())
                or not (chemistry["cas"] or chemistry["inchi_key"])):
            _reject("历史组分需要可核验 CAS 或 InChIKey，不能按名称猜测身份")
        has_value, has_unit = "concentration_value" in component, "concentration_unit" in component
        if has_value != has_unit:
            _reject("历史组分浓度值与单位必须同时提供")
        if has_value:
            if not _number(component["concentration_value"]):
                _reject("历史组分浓度必须为非负有限值")
            # 保留原字符串，不推断百分比基准，不做浓度换算；只接受已显式定义的表示。
            unit = component["concentration_unit"]
            if not isinstance(unit, str) or unit not in {"%", "%w/w", "%w/v", "%v/v", "mol/L", "mmol/L", "umol/L", "µmol/L", "μmol/L", "nmol/L", "mol/l", "mmol/l", "g/L", "mg/mL", "mg/L", "ug/mL", "µg/mL", "μg/mL", "M", "mM", "uM", "µM", "μM", "nM"}:
                _reject("历史组分浓度单位尚未显式支持")
    return infos


def validate_unit(unit: Any) -> None:
    if not isinstance(unit, str) or unit.strip().lower() not in {"ul", "µl", "μl", "ml", "l", "mg", "g", "kg", "粒"}:
        _reject("跨站混合物只支持明确的体积、质量或计数单位，未知单位需先显式建模")


def validate_substance(content: dict[str, Any]) -> set[str]:
    if set(content) != {"kind", "source_content_uuid", "source_revision", "values", "components"}:
        _reject("混合物快照字段不完整")
    if (not _identity(content["source_content_uuid"]) or type(content["source_revision"]) is not int
            or not 1 <= content["source_revision"] <= 2**53 - 1):
        _reject("混合物身份或修订无效")
    values = content["values"]
    if not isinstance(values, dict) or set(values) != set(SUBSTANCE_FIELDS):
        _reject("混合物值字段不完整")
    if not _number(values["quantity"]) or not isinstance(values["meta_data"], dict) or not _timestamp(values["observed_at"]):
        _reject("混合物数量、元数据或观测时间无效")
    validate_unit(values["quantity_unit"])
    if not isinstance(values["physical_state"], str) or values["physical_state"] not in {"solid", "liquid", "gas", "other", "unknown"}:
        _reject("混合物物态无效")
    if any(values[key] is not None and not isinstance(values[key], str) for key in ("name", "description")):
        _reject("混合物名称/描述必须是字符串或 null")
    return validate_composition(content["components"])


def validate_local_composition(c, components: list[dict[str, Any]]) -> None:
    """恢复事务内确认本地显式目录映射仍然对应原始化学事实。"""
    validate_composition(components)
    for component in components:
        if _chemical_identity(c, component["reagent_info_uuid"]) != component["chemical_identity"]:
            _reject("本地目录与历史组分化学身份不一致")
