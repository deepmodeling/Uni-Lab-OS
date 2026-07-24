"""Opentrons → 目标仪器 物料映射表加载与查询（P6 / P6.1 / P6.1.1）。

YAML 文件位置（默认）：``Uni-Lab-OS/labware_mapping.yaml``（项目根，与
``pyproject.toml`` 同级，最显眼）。

模块对外暴露 4 个 API：

- :func:`remap_slot`             ← 替代 ``_map_deck_slot``
- :func:`infer_kind`             ← 替代 ``_infer_reagent_kind`` 的字符串匹配链
- :func:`resolve_target_class`   ← 替代 ``_tip_prcxi_class_for_max_ul`` +
                                   ``_apply_prcxi_labware_auto_match`` 的主路径
- :func:`reload_mapping`         ← 测试 / 脚本中改 YAML 后清缓存重读

P6.1.1 关键约定（与 P6.1 不同）：

- YAML 两段顶层 key：``kinds`` / ``target_devices``。
  顶层 ``slot_remap`` 段**已不支持**；检出 → warning + 整段 fallback 到 :data:`_BUILTIN_DEFAULT`。
- ``slot_remap`` 内嵌到 ``target_devices.<device>.slot_remap``，可由
  ``target_devices.<device>.models.<model>.slot_remap`` 进一步按型号覆盖。
- ``rules`` 同样支持型号级覆盖（``target_devices.<device>.models.<model>.rules``）。
- ``slot_remap`` 与 ``rules`` 共用同一条 4 段 fallback 链（model → device → default → builtin）。
- ``target_devices.default`` **不支持** ``models`` 子段；若声明则 loader warning + 忽略。
"""
from __future__ import annotations

import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# 项目根的查找：__file__ 在 Uni-Lab-OS/unilabos/workflow/labware_mapping.py；
# 上溯三级到 Uni-Lab-OS/（parents[0]=workflow, [1]=unilabos, [2]=Uni-Lab-OS）。
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "labware_mapping.yaml"

# P6.1：兜底段名硬编码为常量。caller 传入的 target_device 在 target_devices
# 段下未声明时，自动 fallback 到这个段。
_DEFAULT_SECTION = "default"

# 默认 slot_remap（与原 _map_deck_slot 硬编码一致）。default + prcxi 段共享同一份。
_BUILTIN_DEFAULT_SLOT_REMAP: Dict[str, Any] = {
    "default": {"4": "13", "8": "14"},
    "by_object": {"trash": {"12": "16"}},
}

# default 段 + prcxi 段的共享规则列表（两段在 YAML 中各自独立，但第一版字节一致）。
_BUILTIN_DEFAULT_RULES: List[Dict[str, Any]] = [
    {"kind": "tip_rack",  "hole_count": 96, "volume_max": 10,    "class_name": "PRCXI_10uL_Tips"},
    {"kind": "tip_rack",  "hole_count": 96, "volume_max": 299.9, "class_name": "PRCXI_300ul_Tips"},
    {"kind": "tip_rack",  "hole_count": 96,                     "class_name": "PRCXI_1000uL_Tips"},
    {"kind": "tube_rack", "hole_count": 2,  "class_name": "PRCXI_2_Reagent_Rack_50mL"},
    {"kind": "tube_rack", "hole_count": 8,  "class_name": "PRCXI_8_Reagent_Rack_10mL"},
    {"kind": "tube_rack", "hole_count": 24, "class_name": "PRCXI_EP_Adapter"},
    {"kind": "tube_rack", "hole_count": 10, "class_name": "PRCXI_EP_Adapter"},
    {"kind": "tube_rack", "hole_count": 96, "class_name": "PRCXI_BioER_96_wellplate"},
    {"kind": "tube_rack", "hole_count": 384, "class_name": "PRCXI_BioRad_384_wellplate"},
    {"kind": "plate",     "hole_count": 1,   "class_name": "PRCXI_nest_1_troughplate"},
    {"kind": "plate",     "hole_count": 4,   "class_name": "PRCXI_AGenBio_4_troughplate"},
    {"kind": "plate",     "hole_count": 12,  "class_name": "PRCXI_nest_12_troughplate"},
    {"kind": "plate",     "hole_count": 96,  "class_name": "PRCXI_BioER_96_wellplate"},
    {"kind": "plate",     "hole_count": 384, "class_name": "PRCXI_BioRad_384_wellplate"},
    {"kind": "trash",                        "class_name": "PRCXI_trash"},
]


def _builtin_device_section() -> Dict[str, Any]:
    """构造一个独立的 device 段（slot_remap + rules 都是深拷贝），避免段间共享引用。"""
    return {
        "slot_remap": {
            "default": dict(_BUILTIN_DEFAULT_SLOT_REMAP["default"]),
            "by_object": {k: dict(v) for k, v in _BUILTIN_DEFAULT_SLOT_REMAP["by_object"].items()},
        },
        "rules": [dict(r) for r in _BUILTIN_DEFAULT_RULES],
    }


# 内置兜底表：当 YAML 文件不存在 / 解析失败 / 检测到旧 schema 时退化使用。
# 与 YAML 文件保持同步。default 与 prcxi 段是两份独立的副本（语义独立、内容相同）。
_BUILTIN_DEFAULT: Dict[str, Any] = {
    "kinds": [
        {"pattern": "trash", "kind": "trash"},
        {"pattern": r"tiprack|tip[_ ]?rack|opentrons_\d+_tiprack", "kind": "tip_rack"},
        {"pattern": r"tuberack|tube[_ ]rack|eppendorf.*rack|safelock.*rack", "kind": "tube_rack"},
        {"pattern": r"(?:^|[^a-z])rack(?:[^a-z]|$)", "kind": "tube_rack"},
        {"pattern": r".*", "kind": "plate"},
    ],
    "target_devices": {
        _DEFAULT_SECTION: _builtin_device_section(),
        "prcxi": _builtin_device_section(),
    },
}


def _has_legacy_schema(data: Dict[str, Any]) -> bool:
    """检测旧 schema 痕迹：

    - P6.1 旧 schema：顶层 ``vendors`` 段，或任一 rule 含 ``prcxi_class``。
    - P6.1.1 旧 schema（**本期新增**）：顶层 ``slot_remap`` 段（应内嵌到 target_devices 下）。
    """
    if "vendors" in data:
        return True
    # P6.1.1：顶层 slot_remap 段被视为旧 schema
    if "slot_remap" in data:
        return True
    td = data.get("target_devices")
    if isinstance(td, dict):
        for sect in td.values():
            if not isinstance(sect, dict):
                continue
            for r in sect.get("rules") or []:
                if isinstance(r, dict) and "prcxi_class" in r:
                    return True
            # 也检查 models 内
            models = sect.get("models") or {}
            if isinstance(models, dict):
                for m in models.values():
                    if not isinstance(m, dict):
                        continue
                    for r in m.get("rules") or []:
                        if isinstance(r, dict) and "prcxi_class" in r:
                            return True
    return False


def _legacy_schema_reason(data: Dict[str, Any]) -> str:
    """生成具体的旧 schema 提示，便于用户定位升级点。"""
    reasons: List[str] = []
    if "vendors" in data:
        reasons.append("顶层 `vendors` 段（应改为 `target_devices`）")
    if "slot_remap" in data:
        reasons.append("顶层 `slot_remap` 段（应内嵌到 `target_devices.<device>.slot_remap`）")
    td = data.get("target_devices")
    if isinstance(td, dict):
        for sect_name, sect in td.items():
            if not isinstance(sect, dict):
                continue
            for r in sect.get("rules") or []:
                if isinstance(r, dict) and "prcxi_class" in r:
                    reasons.append(f"`target_devices.{sect_name}.rules` 中含旧字段 `prcxi_class`（应改为 `class_name`）")
                    break
            models = sect.get("models") or {}
            if isinstance(models, dict):
                for m_name, m in models.items():
                    if not isinstance(m, dict):
                        continue
                    for r in m.get("rules") or []:
                        if isinstance(r, dict) and "prcxi_class" in r:
                            reasons.append(
                                f"`target_devices.{sect_name}.models.{m_name}.rules` 中含旧字段 `prcxi_class`"
                            )
                            break
    return "；".join(reasons) if reasons else "未知"


@lru_cache(maxsize=4)
def _load_mapping(path: Optional[str] = None) -> Dict[str, Any]:
    """从 YAML 加载映射表；缺文件 / 解析失败 / 旧 schema 时退化到内置默认。

    ``path`` 缺省时取项目根 ``labware_mapping.yaml``。结果按路径缓存，
    重复调用零成本；测试 / 脚本改 YAML 后通过 :func:`reload_mapping` 失效缓存。

    P6.1.1 校验顺序：

    1. 文件存在 + 可 parse + 根 dict
    2. 旧 schema 检测（含 P6.1 `vendors` / `prcxi_class` + P6.1.1 顶层 `slot_remap`）
       → 整段 fallback 到 :data:`_BUILTIN_DEFAULT`
    3. 两段顶层 key 校验：``kinds`` / ``target_devices``
    4. ``target_devices`` 下必含 :data:`_DEFAULT_SECTION` 段；缺则该段使用 builtin default 段
    5. ``target_devices.default.models`` 不允许；若声明则 warning + 删除
    """
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        warnings.warn(f"labware_mapping.yaml 未找到：{p}，使用内置默认表")
        return _BUILTIN_DEFAULT
    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        warnings.warn(f"labware_mapping.yaml 解析失败：{e}，使用内置默认表")
        return _BUILTIN_DEFAULT
    if not isinstance(data, dict):
        warnings.warn(f"labware_mapping.yaml 根不是 dict：{type(data).__name__}，使用内置默认表")
        return _BUILTIN_DEFAULT

    if _has_legacy_schema(data):
        warnings.warn(
            "labware_mapping.yaml 检测到旧 schema："
            f"{_legacy_schema_reason(data)}。P6.1.1 不再支持；"
            "请参考 product_designs/protocol_convert/06-labware-mapping-table.md §11.8 升级 schema。"
            "本次加载整段使用内置默认表。"
        )
        return _BUILTIN_DEFAULT

    for key in ("kinds", "target_devices"):
        if key not in data or data[key] is None:
            warnings.warn(f"labware_mapping.yaml 缺少 `{key}` 段；该段将使用内置默认")
            data[key] = _BUILTIN_DEFAULT[key]

    td = data.get("target_devices")
    if not isinstance(td, dict) or _DEFAULT_SECTION not in td or td.get(_DEFAULT_SECTION) is None:
        warnings.warn(
            f"labware_mapping.yaml 缺少必需的 `target_devices.{_DEFAULT_SECTION}` 段；"
            f"该段将使用内置默认。default 段是 P6.1 的兜底物料集，未来未声明的 "
            f"target_device 都会回退到它。"
        )
        if not isinstance(td, dict):
            td = {}
            data["target_devices"] = td
        td[_DEFAULT_SECTION] = _BUILTIN_DEFAULT["target_devices"][_DEFAULT_SECTION]

    # P6.1.1：target_devices.default 不支持 models 子段
    default_sect = td.get(_DEFAULT_SECTION)
    if isinstance(default_sect, dict) and "models" in default_sect:
        warnings.warn(
            f"labware_mapping.yaml: `target_devices.{_DEFAULT_SECTION}.models` 不被支持 —— "
            "型号粒度差异必须落到具体仪器段。该子段将被忽略。"
        )
        # 副作用：从 cached data 中删除，避免后续解析误用
        default_sect.pop("models", None)

    return data


def reload_mapping(path: Optional[str] = None) -> None:
    """测试或脚本中修改 YAML 后重新加载（失效 lru_cache）。"""
    _load_mapping.cache_clear()
    if path is not None:
        _load_mapping(str(path))


# ============================================================================
# 4 段 fallback helper：model → device → default → builtin default
# ============================================================================

def _resolve_section(
    field_name: str,
    target_device: str,
    target_model: Optional[str],
) -> Any:
    """4 段 fallback 链解析指定字段（``slot_remap`` / ``rules`` / ...）。

    Args:
        field_name: ``target_devices.<device>`` 或 ``models.<model>`` 下的字段名，
            如 ``"slot_remap"`` / ``"rules"``。
        target_device: caller 传入的目标仪器名（厂商粒度）。
        target_model: caller 传入的目标型号名；``None`` 表示不区分型号、走厂商级。

    Returns:
        对应字段的值（保留原 dict / list 形态）；找不到任何兜底也只返回 ``None``。

    fallback 链：

    1. ``target_devices.<target_device>.models.<target_model>.<field_name>``
       —— 仅当 ``target_model`` 非空且 model 子段含该字段。
    2. ``target_devices.<target_device>.<field_name>`` —— 厂商级。
    3. ``target_devices.default.<field_name>`` —— 兜底段。
    4. ``_BUILTIN_DEFAULT.target_devices.default.<field_name>`` —— 最终硬编码兜底。

    warning 策略：
    - caller 传未声明的 ``target_device`` 段（步骤 2 没拿到值且 device 段整体不存在）→ 单次 warning。
    - caller 传未声明的 ``target_model``（model 名不存在或 model 内缺该字段）→ **静默** fallback
      （这是常见的"用厂商默认"用法，不应报噪音）。
    - YAML 误删 default 段（步骤 3 也拿不到值）→ 单次 warning。
    """
    td = _load_mapping().get("target_devices") or {}
    builtin_td = _BUILTIN_DEFAULT["target_devices"]

    device_sect = td.get(target_device) if isinstance(td, dict) else None
    device_sect = device_sect if isinstance(device_sect, dict) else None

    # Step 1: model 级
    if target_model and device_sect is not None:
        models = device_sect.get("models")
        if isinstance(models, dict):
            m = models.get(target_model)
            if isinstance(m, dict) and m.get(field_name) is not None:
                return m[field_name]
        # model 名整体未声明 / 该字段缺失 → 静默 fallback

    # Step 2: device 级
    if device_sect is not None and device_sect.get(field_name) is not None:
        return device_sect[field_name]

    # Step 3: default 段
    if target_device != _DEFAULT_SECTION and device_sect is None:
        warnings.warn(
            f"target_device {target_device!r} 未在 labware_mapping.yaml 的 target_devices 中声明，"
            f"已回退到固定段 target_devices.{_DEFAULT_SECTION}。"
            f"请在 YAML 中补 target_devices.{target_device}.{field_name}。"
        )
    default_sect = td.get(_DEFAULT_SECTION) if isinstance(td, dict) else None
    if isinstance(default_sect, dict) and default_sect.get(field_name) is not None:
        return default_sect[field_name]

    # Step 4: builtin default（YAML 误删 default 段时）
    warnings.warn(
        f"labware_mapping.yaml 缺少必需的 target_devices.{_DEFAULT_SECTION}.{field_name}；"
        f"本次解析整段使用内置默认表。"
    )
    builtin_default = builtin_td.get(_DEFAULT_SECTION) or {}
    return builtin_default.get(field_name)


# ============================================================================
# 公开 API
# ============================================================================


def remap_slot(
    raw_slot: Any,
    object_type: str = "",
    *,
    target_device: str = "prcxi",
    target_model: Optional[str] = None,
) -> str:
    """协议槽位 → 目标设备 deck 实际位置。等价于历史 ``_map_deck_slot``：

    1. 优先查 ``slot_remap.by_object[object_type][raw]``（如 ``trash`` 的 ``12 → 16``）。
    2. 否则查 ``slot_remap.default[raw]``（如 ``4 → 13``、``8 → 14``）。
    3. 否则原样返回。

    P6.1.1：``slot_remap`` 内嵌在 ``target_devices.<target_device>`` 下，
    可由 ``target_devices.<target_device>.models.<target_model>.slot_remap`` 进一步覆盖。
    走 :func:`_resolve_section` 的 4 段 fallback 链（model → device → default → builtin）。

    Args:
        raw_slot: 协议中的原始槽位标识；接受 ``int`` / ``str`` / ``None``。
        object_type: ``labware_info[id]['object']`` 的值（如 ``"trash"`` / ``"source"``）。
        target_device: 目标仪器名（厂商粒度）；默认 ``"prcxi"``。
        target_model: 目标型号名（型号粒度）；``None`` 表示不区分型号，走厂商级。
    """
    s = "" if raw_slot is None else str(raw_slot).strip()
    if not s:
        return ""
    cfg = _resolve_section("slot_remap", target_device, target_model) or {}
    if not isinstance(cfg, dict):
        return s
    ot = (object_type or "").strip().lower()
    by_obj = (cfg.get("by_object") or {}).get(ot) or {}
    if s in by_obj:
        return str(by_obj[s])
    return str((cfg.get("default") or {}).get(s, s))


def infer_kind(labware_hint: str, object_type: str = "") -> str:
    """labware 字符串 + object 字段 → ``plate / tip_rack / tube_rack / trash`` 之一。

    与历史 ``_infer_reagent_kind`` 行为对齐：

    - ``object_type == "trash"`` → 直接 ``trash``。
    - ``object_type == "tiprack"`` → 直接 ``tip_rack``。
    - 否则按 YAML ``kinds`` 段顺序，对 ``lower(labware_hint)`` 做 ``re.search``；
      首个命中胜出。
    - 全不命中 → ``plate``（YAML 默认 ``.*`` 兜底也回到 plate）。

    ``kinds`` 段是**全局**的（与 target_device 无关），P6.1.1 起依然保留在顶层。
    """
    ot = (object_type or "").strip().lower()
    if ot == "trash":
        return "trash"
    if ot == "tiprack":
        return "tip_rack"
    hint = (labware_hint or "").lower()
    for rule in _load_mapping().get("kinds") or []:
        pat = rule.get("pattern")
        kd = rule.get("kind")
        if not pat or not kd:
            continue
        try:
            if re.search(pat, hint):
                return str(kd)
        except re.error:
            warnings.warn(f"labware_mapping.yaml: kinds 规则正则不合法 {pat!r}，跳过")
            continue
    return "plate"


def _match_rules(
    rules: List[Dict[str, Any]],
    kind: str,
    hole_count: Optional[int],
    volume: Optional[float],
) -> Optional[str]:
    """在给定 rules 列表内按 kind + hole_count + volume 找首个命中规则的 ``class_name``。

    匹配规则（与 P6 完全相同的语义）：

    - ``rule.kind == kind``（严格相等）。
    - ``rule.hole_count`` 缺失 OR 严格等于传入 ``hole_count``。
      若传入 ``hole_count is None``，则只要 rule 也未约束 hole_count 即可视为不冲突。
    - ``volume`` 范围：rule 的 ``volume_min`` / ``volume_max`` 闭区间，二者均可省略。
      若传入 ``volume is None``，则只要 rule 也未约束 volume 即可视为不冲突。
    """
    for r in rules or []:
        if r.get("kind") != kind:
            continue
        if "hole_count" in r and r["hole_count"] is not None:
            if hole_count is None:
                continue
            try:
                if int(r["hole_count"]) != int(hole_count):
                    continue
            except (TypeError, ValueError):
                continue
        vmin = r.get("volume_min")
        vmax = r.get("volume_max")
        if vmin is not None or vmax is not None:
            if volume is None:
                continue
            try:
                vf = float(volume)
            except (TypeError, ValueError):
                continue
            if vmin is not None and vf < float(vmin):
                continue
            if vmax is not None and vf > float(vmax):
                continue
        cls = r.get("class_name")
        if cls:
            return str(cls)
    return None


def resolve_target_class(
    target_device: str,
    kind: str,
    hole_count: Optional[int] = None,
    volume: Optional[float] = None,
    *,
    target_model: Optional[str] = None,
) -> Optional[str]:
    """按 target_device (+ target_model) + kind + hole_count + volume 选首个命中的 ``class_name``。

    P6.1.1 4 段 fallback 链（走 :func:`_resolve_section`，``field_name="rules"``）：

    1. 查 ``target_devices.<target_device>.models.<target_model>.rules``，找到首个命中规则 → 返回 ``class_name``。
    2. 若步骤 1 缺字段 → 查 ``target_devices.<target_device>.rules``。
    3. 若 ``target_device`` 段不存在（caller 传 YAML 未声明的名字）→
       查 ``target_devices.default.rules`` + 单次 warning。
    4. 若 ``default`` 段也不存在 → 走 :data:`_BUILTIN_DEFAULT` 的 default 段 + warning。

    在最终命中的 rules 列表内仍未匹配到（孔数 / 体积超出覆盖范围）→ 返回 ``None``，
    交给上游 ``_apply_target_labware_class_auto_match`` 走 PRCXI 模板打分匹配 fallback。
    """
    rules = _resolve_section("rules", target_device, target_model)
    if not isinstance(rules, list):
        rules = []
    return _match_rules(rules, kind, hole_count, volume)
