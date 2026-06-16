# -*- coding: utf-8 -*-
"""
功能:
    提供化学品查询结果追加入库所需的轻量工具函数.
    包括 ChemicalBook 结构化结果适配, 物态推断, 重复检查规则生成,
    以及全量 sidecar JSON 落盘.
参数:
    无.
返回:
    无.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import storage


logger = logging.getLogger("ChemicalAppendUtils")


def _first_non_empty(*values: Any) -> str:
    """
    功能:
        从多个候选值中返回第一个非空字符串.
    参数:
        values: 任意数量的候选值.
    返回:
        str, 第一个非空字符串. 若都为空则返回空字符串.
    """
    for value in values:
        value_text = str(value or "").strip()
        if value_text != "":
            return value_text
    return ""


def _first_non_none(*values: Any) -> Any:
    """
    功能:
        从多个候选值中返回第一个非 None 的值.
    参数:
        values: 任意数量的候选值.
    返回:
        Any, 第一个非 None 的值. 若都为空则返回 None.
    """
    for value in values:
        if value is not None:
            return value
    return None


def get_measurement_value(normalized: Optional[Dict[str, Any]], field_name: str) -> Optional[float]:
    """
    功能:
        从 ChemicalBook normalized 结果中提取数值型测量字段.
    参数:
        normalized: Optional[Dict[str, Any]], ChemicalBook normalized 字段.
        field_name: str, 目标字段名, 如 density 或 boiling_point.
    返回:
        Optional[float], 成功解析出的数值. 不存在时返回 None.
    """
    if isinstance(normalized, dict) is False:
        return None

    field_value = normalized.get(field_name)
    if isinstance(field_value, dict) is False:
        return None

    numeric_value = field_value.get("value")
    if numeric_value is None:
        return None

    try:
        return float(numeric_value)
    except (TypeError, ValueError):
        logger.warning("ChemicalBook 字段无法转换为浮点数: field=%s, value=%s", field_name, numeric_value)
        return None


def infer_physical_state(
    existing_state: Optional[str],
    melting_point: Optional[float],
    boiling_point: Optional[float],
) -> str:
    """
    功能:
        根据现有状态和温度数据推断 25 摄氏度下的物态.
    参数:
        existing_state: Optional[str], 已有物态值.
        melting_point: Optional[float], 熔点, 单位为摄氏度.
        boiling_point: Optional[float], 沸点, 单位为摄氏度.
    返回:
        str, solid, liquid, gas 或空字符串.
    """
    normalized_state = str(existing_state or "").strip().lower()
    if normalized_state != "":
        return normalized_state

    if melting_point is not None and melting_point > 25.0:
        return "solid"
    if boiling_point is not None and boiling_point <= 25.0:
        return "gas"
    if melting_point is not None or boiling_point is not None:
        return "liquid"
    return ""


def join_aliases(alias_values: Optional[Sequence[Any]]) -> str:
    """
    功能:
        将别名列表去重后拼接为适合写入 Excel 的字符串.
    参数:
        alias_values: Optional[Sequence[Any]], ChemicalBook 别名列表.
    返回:
        str, 以 ; 分隔的别名文本. 无有效别名时返回空字符串.
    """
    if isinstance(alias_values, Sequence) is False or isinstance(alias_values, (str, bytes)):
        return ""

    deduplicated_aliases: List[str] = []
    seen_aliases = set()
    for alias_value in alias_values:
        alias_text = str(alias_value or "").strip()
        if alias_text == "":
            continue
        if alias_text in seen_aliases:
            continue
        seen_aliases.add(alias_text)
        deduplicated_aliases.append(alias_text)

    return "; ".join(deduplicated_aliases)


def _format_numeric_text(value: Any) -> str:
    """
    功能:
        将数值格式化为适合写入展示名的紧凑文本.
    参数:
        value: Any, 原始数值.
    返回:
        str, 去除多余尾零后的文本.
    """
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return str(value or "").strip()
    return format(numeric_value, "g")


def build_prepared_display_name(
    base_row_data: Dict[str, Any],
    prepared_form: str,
    active_content: Any,
    *,
    solvent_name: str = "",
) -> str:
    """
    功能:
        根据母体化合物信息构造溶液或 beads 条目的展示名称.
    参数:
        base_row_data: Dict[str, Any], 母体化合物行数据.
        prepared_form: str, 派生形态, 支持 solution 或 beads.
        active_content: Any, solution 时表示 mol/L, beads 时表示 wt%.
        solvent_name: str, solution 使用的溶剂名称.
    返回:
        str, 派生条目展示名称.
    异常:
        ValueError: 形态非法, 或缺少母体名称, 或 solution 缺少溶剂名时抛出.
    """
    normalized_form = str(prepared_form or "").strip().lower()
    base_name = _first_non_empty(base_row_data.get("base_substance"))
    if base_name == "":
        substance_text = _first_non_empty(
            base_row_data.get("substance"),
            base_row_data.get("substance_chinese_name"),
        )
        if " (" in substance_text:
            substance_text = substance_text.split(" (", 1)[0].strip()
        base_name = _first_non_empty(
            substance_text,
            base_row_data.get("substance_english_name"),
        )
    if base_name == "":
        raise ValueError("母体化合物缺少名称, 无法生成派生条目名称")

    active_text = _format_numeric_text(active_content)
    if normalized_form == "solution":
        normalized_solvent_name = str(solvent_name or "").strip()
        if normalized_solvent_name == "":
            raise ValueError("溶液条目缺少溶剂名称")
        return f"{base_name} (溶液, {active_text} M in {normalized_solvent_name})"
    if normalized_form == "beads":
        return f"{base_name} (beads, {active_text}%)"

    raise ValueError(f"不支持的派生形态: {prepared_form}")


def build_prepared_chemical_row_data(
    base_row_data: Dict[str, Any],
    prepared_form: str,
    active_content: Any,
    *,
    solvent_name: str = "",
) -> Dict[str, Any]:
    """
    功能:
        基于母体化合物行数据构造溶液或 beads 的追加入库字段.
    参数:
        base_row_data: Dict[str, Any], 母体化合物行数据.
        prepared_form: str, 派生形态, 支持 solution 或 beads.
        active_content: Any, solution 时表示 mol/L, beads 时表示 wt%.
        solvent_name: str, solution 使用的溶剂名称.
    返回:
        Dict[str, Any], 可直接用于 Excel 追加的派生条目字段.
    异常:
        ValueError: 形态非法, active_content 无法转浮点, 或必要字段缺失时抛出.
    """
    normalized_form = str(prepared_form or "").strip().lower()
    if normalized_form not in {"solution", "beads"}:
        raise ValueError(f"不支持的派生形态: {prepared_form}")

    try:
        numeric_active_content = float(active_content)
    except (TypeError, ValueError) as exc:
        raise ValueError("active_content 必须为数值") from exc

    display_name = build_prepared_display_name(
        base_row_data=base_row_data,
        prepared_form=normalized_form,
        active_content=numeric_active_content,
        solvent_name=solvent_name,
    )

    derived_density = base_row_data.get("density (g/mL)")
    if normalized_form == "beads":
        derived_density = ""

    return {
        "cas_number": str(base_row_data.get("cas_number") or "").strip(),
        "chemical_id": "",
        "substance_english_name": str(base_row_data.get("substance_english_name") or "").strip(),
        "substance": display_name,
        "substance_chinese_name": display_name,
        "other_name": "",
        "brand": "",
        "package_size": "",
        "storage_location": "",
        "molecular_weight": base_row_data.get("molecular_weight"),
        "density (g/mL)": derived_density,
        "physical_state": "liquid" if normalized_form == "solution" else "solid",
        "physical_form": normalized_form,
        "active_content(mol/L or wt%)": numeric_active_content,
    }


def build_append_row_data(
    query: str,
    lookup_info: Optional[Any],
    chemicalbook_record: Optional[Dict[str, Any]],
    legacy_chemicalbook_info: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    功能:
        按约定优先级合并多源查询结果, 生成 Excel 追加行所需字段.
        Excel 主表仅保留核心字段, ChemicalBook 的全量结构化结果单独落盘 sidecar.
    参数:
        query: str, 用户输入的 CAS 或名称.
        lookup_info: Optional[Any], lookup_chemical 返回对象.
        chemicalbook_record: Optional[Dict[str, Any]], fetch_chemicalbook_by_cas 返回结果.
    返回:
        Dict[str, Any], 追加入库所需字段映射.
    """
    return _build_append_row_data(
        query=query,
        lookup_info=lookup_info,
        chemicalbook_record=chemicalbook_record,
        legacy_chemicalbook_info=legacy_chemicalbook_info,
        allow_query_as_cas_fallback=True,
    )


def build_append_row_data_for_smiles(
    lookup_info: Optional[Any],
    chemicalbook_record: Optional[Dict[str, Any]],
    legacy_chemicalbook_info: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    功能:
        为 SMILES 在线查询流程构建 Excel 追加行字段.
        与通用构造逻辑的区别是: 不允许将原始查询文本回填到 cas_number.
    参数:
        lookup_info: Optional[Any], lookup_chemical_by_smiles 返回对象.
        chemicalbook_record: Optional[Dict[str, Any]], fetch_chemicalbook_by_cas 返回结果.
    返回:
        Dict[str, Any], 追加入库所需字段映射.
    """
    return _build_append_row_data(
        query="",
        lookup_info=lookup_info,
        chemicalbook_record=chemicalbook_record,
        legacy_chemicalbook_info=legacy_chemicalbook_info,
        allow_query_as_cas_fallback=False,
    )


def _build_append_row_data(
    query: str,
    lookup_info: Optional[Any],
    chemicalbook_record: Optional[Dict[str, Any]],
    legacy_chemicalbook_info: Optional[Any],
    allow_query_as_cas_fallback: bool,
) -> Dict[str, Any]:
    """
    功能:
        合并多源查询结果并生成 Excel 追加行字段.
        可按调用场景决定是否允许将原始查询值作为 CAS 兜底写入.
    参数:
        query: str, 原始查询文本.
        lookup_info: Optional[Any], 查询结果对象.
        chemicalbook_record: Optional[Dict[str, Any]], ChemicalBook 结构化结果.
        allow_query_as_cas_fallback: bool, 是否允许用 query 回填 cas_number.
    返回:
        Dict[str, Any], 追加入库所需字段映射.
    """
    normalized = {}
    if isinstance(chemicalbook_record, dict) is True and isinstance(chemicalbook_record.get("normalized"), dict) is True:
        normalized = chemicalbook_record["normalized"]

    use_legacy_fallback = needs_legacy_chemicalbook_fallback(chemicalbook_record)
    legacy_cn_name = None
    legacy_en_name = None
    legacy_density = None
    legacy_melting_point = None
    if use_legacy_fallback is True and legacy_chemicalbook_info is not None:
        legacy_cn_name = getattr(legacy_chemicalbook_info, "substance", None)
        legacy_en_name = getattr(legacy_chemicalbook_info, "substance_english_name", None)
        legacy_density = getattr(legacy_chemicalbook_info, "density", None)
        legacy_melting_point = getattr(legacy_chemicalbook_info, "melting_point", None)

    lookup_cas = getattr(lookup_info, "cas_number", None)
    lookup_en_name = getattr(lookup_info, "substance_english_name", None)
    lookup_cn_name = getattr(lookup_info, "substance", None)
    lookup_molecular_weight = getattr(lookup_info, "molecular_weight", None)
    lookup_density = getattr(lookup_info, "density", None)
    lookup_melting_point = getattr(lookup_info, "melting_point", None)
    lookup_state = getattr(lookup_info, "physical_state", None)

    chemicalbook_cas = ""
    if isinstance(chemicalbook_record, dict) is True:
        chemicalbook_cas = str(chemicalbook_record.get("cas") or "").strip()

    cas_values = [lookup_cas, chemicalbook_cas]
    if allow_query_as_cas_fallback is True:
        cas_values.append(query)
    cas_number = _first_non_empty(*cas_values)
    substance_english_name = _first_non_empty(
        lookup_en_name,
        normalized.get("en_name"),
        legacy_en_name,
    )
    substance_chinese_name = _first_non_empty(
        lookup_cn_name,
        normalized.get("cn_name"),
        legacy_cn_name,
    )
    molecular_weight = _first_non_none(
        lookup_molecular_weight,
        normalized.get("molecular_weight"),
    )
    density_value = _first_non_none(
        lookup_density,
        get_measurement_value(normalized, "density"),
        legacy_density,
    )
    melting_point_value = _first_non_none(
        lookup_melting_point,
        get_measurement_value(normalized, "melting_point"),
        legacy_melting_point,
    )
    boiling_point_value = get_measurement_value(normalized, "boiling_point")
    physical_state = infer_physical_state(
        existing_state=lookup_state,
        melting_point=melting_point_value,
        boiling_point=boiling_point_value,
    )
    # 主表保持精简, 别名不直接回填到 other_name.
    other_name = ""

    return {
        "cas_number": cas_number,
        "chemical_id": "",
        "substance_english_name": substance_english_name,
        "substance": substance_chinese_name,
        "substance_chinese_name": substance_chinese_name,
        "other_name": other_name,
        "brand": "",
        "package_size": "",
        "storage_location": "",
        "molecular_weight": molecular_weight,
        "density (g/mL)": density_value,
        "physical_state": physical_state,
        "physical_form": "neat",
        "active_content(mol/L or wt%)": "",
    }


def needs_legacy_chemicalbook_fallback(record: Optional[Dict[str, Any]]) -> bool:
    """
    功能:
        判断当前 ChemicalBook 结构化结果是否缺少追加主表所需的核心字段.
        当前仅检查中文名, 密度和熔点, 缺任一项即认为需要旧兜底结果.
    参数:
        record: Optional[Dict[str, Any]], ChemicalBook 结构化结果.
    返回:
        bool, True 表示需要旧 ChemicalBook 兜底.
    """
    if isinstance(record, dict) is False:
        return True

    normalized = record.get("normalized")
    if isinstance(normalized, dict) is False:
        return True

    cn_name = str(normalized.get("cn_name") or "").strip()
    density_value = get_measurement_value(normalized, "density")
    melting_point_value = get_measurement_value(normalized, "melting_point")

    return any([
        cn_name == "",
        density_value is None,
        melting_point_value is None,
    ])


def collect_missing_append_headers(header_map: Dict[str, int]) -> List[str]:
    """
    功能:
        检查化学品追加流程依赖的表头是否存在.
        中文名列兼容 substance 与 substance_chinese_name 两种命名.
    参数:
        header_map: Dict[str, int], Excel 表头映射.
    返回:
        List[str], 缺失的字段名列表.
    """
    missing_headers: List[str] = []
    required_headers = [
        "cas_number",
        "substance_english_name",
        "molecular_weight",
        "density (g/mL)",
        "physical_state",
        "physical_form",
    ]

    for header_name in required_headers:
        if header_name not in header_map:
            missing_headers.append(header_name)

    if "substance" not in header_map and "substance_chinese_name" not in header_map:
        missing_headers.append("substance/substance_chinese_name")

    return missing_headers


def build_duplicate_check_specs(row_data: Dict[str, Any]) -> List[Tuple[Tuple[str, ...], str, str]]:
    """
    功能:
        生成按优先级排列的重复检查规则.
        neat 条目按 CAS > 英文名 > 中文名检查.
        solution 与 beads 仅按完整展示名检查, 允许与母体共享 CAS 与英文名.
    参数:
        row_data: Dict[str, Any], 准备写入 Excel 的行数据.
    返回:
        List[Tuple[Tuple[str, ...], str, str]], 每项包含候选列名集合, 目标值, 日志标签.
    """
    duplicate_specs: List[Tuple[Tuple[str, ...], str, str]] = []
    physical_form = str(row_data.get("physical_form") or "").strip().lower()
    is_prepared_form = physical_form in {"solution", "beads"}

    if is_prepared_form is False:
        cas_number = str(row_data.get("cas_number") or "").strip()
        if cas_number != "":
            duplicate_specs.append((("cas_number",), cas_number, "CAS"))

        substance_english_name = str(row_data.get("substance_english_name") or "").strip()
        if substance_english_name != "":
            duplicate_specs.append((("substance_english_name",), substance_english_name, "英文名"))

    substance_chinese_name = str(
        row_data.get("substance")
        or row_data.get("substance_chinese_name")
        or ""
    ).strip()
    if substance_chinese_name != "":
        duplicate_specs.append((("substance", "substance_chinese_name"), substance_chinese_name, "中文名"))

    return duplicate_specs


def get_excel_write_value(row_data: Dict[str, Any], column_name: str) -> Any:
    """
    功能:
        根据目标表头名称, 返回适合写入 Excel 的字段值.
        该函数负责 substance 与 substance_chinese_name 两种列名的兼容.
    参数:
        row_data: Dict[str, Any], 追加行字段映射.
        column_name: str, Excel 表头列名.
    返回:
        Any, 对应列应写入的值.
    """
    if column_name == "substance":
        return row_data.get("substance") or row_data.get("substance_chinese_name")
    if column_name == "substance_chinese_name":
        return row_data.get("substance_chinese_name") or row_data.get("substance")
    return row_data.get(column_name)


def save_chemicalbook_record(
    record: Optional[Dict[str, Any]],
    output_root: Optional[Path] = None,
) -> str:
    """
    功能:
        将 ChemicalBook 全量结构化结果保存为 sidecar JSON 文件.
    参数:
        record: Optional[Dict[str, Any]], fetch_chemicalbook_by_cas 返回结果.
        output_root: Optional[Path], 自定义输出目录. None 时使用默认目录.
    返回:
        str, 保存后的 JSON 文件路径. 无法保存时返回空字符串.
    """
    if isinstance(record, dict) is False:
        return ""

    cas_number = str(record.get("cas") or "").strip()
    if cas_number == "":
        return ""

    if output_root is None:
        storage.ensure_chemicalbook_data_layout()
    target_root = output_root or storage.CHEMICALBOOK_RECORD_ROOT
    target_root.mkdir(parents=True, exist_ok=True)
    output_path = target_root / f"{cas_number}.json"
    output_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(output_path)
