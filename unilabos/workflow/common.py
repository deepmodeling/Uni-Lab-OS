"""
工作流转换模块 - JSON 到 WorkflowGraph 的转换流程

==================== 输入格式 (JSON) ====================

{
    "workflow": [
        {"action": "transfer_liquid", "action_args": {"sources": "cell_lines", "targets": "Liquid_1", "asp_vol": 100.0, "dis_vol": 74.75, ...}},
        ...
    ],
    "reagent": {
        "cell_lines": {"slot": 4, "well": ["A1", "A3", "A5"], "labware": "DRUG + YOYO-MEDIA"},
        "Liquid_1": {"slot": 1, "well": ["A4", "A7", "A10"], "labware": "rep 1"},
        ...
    }
}

==================== 转换步骤 ====================

第一步: 按 slot 去重创建 create_resource 节点（创建板子）
--------------------------------------------------------------------------------
- 首先创建一个 Group 节点（type="Group", minimized=true），用于包含所有 create_resource 节点
- 遍历所有 reagent，按 slot 去重，为每个唯一的 slot 创建一个板子
- 所有 create_resource 节点的 parent_uuid 指向 Group 节点，minimized=true
- 生成参数:
    res_id / 节点 name / display_name: {匹配后的 target 类名}_slot_{槽位}
    device_id: /PRCXI
    class_name: 与 res_id 中类型一致（如 PRCXI 384/96 孔板注册类）
    parent: /PRCXI/PRCXI_Deck
    slot_on_deck: "{slot}"
- 输出端口: labware（用于连接 set_liquid_from_plate）
- 控制流: create_resource 之间通过 ready 端口串联

示例: slot=1, slot=4 -> 创建 1 个 Group + 2 个 create_resource 节点

第二步: 为每个 reagent 创建 set_liquid_from_plate 节点（设置液体）
--------------------------------------------------------------------------------
- 首先创建一个 Group 节点（type="Group", minimized=true），用于包含所有 set_liquid_from_plate 节点
- 遍历所有 reagent，为每个试剂创建 set_liquid_from_plate 节点
- 所有 set_liquid_from_plate 节点的 parent_uuid 指向 Group 节点，minimized=true
- 生成参数（P3 框选化，新主路径）:
    wells: [
        {id, name, parent: labware_id, type: "well"},
        ...
    ]（list[dict]，每孔一个资源引用；前端通过 placeholder 框选 well 时回填 uuid）
    liquid_names: ["cell_lines", "cell_lines", "cell_lines"]（与 wells 数量一致）
    volumes: [1e5, 1e5, 1e5]（与 wells 数量一致，默认体积）
    # 兼容字段（旧 runtime / 旧 schema fallback）:
    plate: []（通过连接传递，来自 create_resource 的 labware）
    well_names: ["A1", "A3", "A5"]（来自 reagent 的 well 数组）
- 输入连接: create_resource (labware) -> set_liquid_from_plate (wells_identifier)
    （P3 §3.4.3 简化方案：source_port 仍为 labware；placeholder 内部把 labware.wells.@flatten 映射到 wells 字段）
- 输出端口: output_wells（用于连接 transfer_liquid）
- 控制流: set_liquid_from_plate 连接在所有 create_resource 之后，通过 ready 端口串联

第三步: 解析 workflow，创建 transfer_liquid 等动作节点
--------------------------------------------------------------------------------
- 遍历 workflow 数组，为每个动作创建步骤节点
- 参数重命名: asp_vol -> asp_vols, dis_vol -> dis_vols, asp_flow_rate -> asp_flow_rates, dis_flow_rate -> dis_flow_rates
- 参数输入转换: liquid_height（按 wells 扩展）；mix_stage/mix_times/mix_vol/mix_rate/mix_liquid_height 保持标量
- 参数扩展: 根据 targets 的 wells 数量，将单值扩展为数组
    例: asp_vol=100.0, targets 有 3 个 wells -> asp_vols=[100.0, 100.0, 100.0]
- 连接处理: 如果 sources/targets 已通过 set_liquid_from_plate 连接，参数值改为 []
- 输入连接: set_liquid_from_plate (output_wells) -> transfer_liquid (sources_identifier / targets_identifier)
- 输出端口: sources_out, targets_out（用于连接下一个 transfer_liquid）

==================== 连接关系图 ====================

控制流 (ready 端口串联):
    - create_resource 之间: 无 ready 连接
    - set_liquid_from_plate 之间: 无 ready 连接
    - create_resource 与 set_liquid_from_plate 之间: 无 ready 连接
    - 第一个 transfer_liquid: 通过 ready 等待所有 create_resource 与所有 set_liquid_from_plate
      （含跨板 merged 节点）完成，确保所有孔位在第一次移液开始前已全部初始化
      （set_liquid_from_plate 是绝对覆盖语义，必须在任何移液前完成，否则后续 stage 的
      初始化会把已移入的液体重置归零）
    - transfer_liquid 之间: 通过 ready 端口串联
        transfer_liquid_1 -> transfer_liquid_2 -> transfer_liquid_3 -> ...

物料流:
    [create_resource] --labware--> [set_liquid_from_plate] --output_wells--> [transfer_liquid] --sources_out/targets_out--> [下一个 transfer_liquid]
          (slot=1)                    (cell_lines)         (wells_identifier)  (sources_identifier)                          (sources_identifier)
          (slot=4)                    (Liquid_1)                               (targets_identifier)                          (targets_identifier)

==================== 端口映射 ====================

create_resource:
    输出: labware

set_liquid_from_plate:
    输入: wells -> wells_identifier（P3 主路径；input_plate 作旧 schema fallback 仍存在）
    输出: output_plate, output_wells, output_volumes

transfer_liquid:
    输入: sources -> sources_identifier, targets -> targets_identifier
    输出: sources -> sources_out, targets -> targets_out

==================== 设备名配置 (device_name) ====================

每个节点都有 device_name 字段，指定在哪个设备上执行:
- create_resource: device_name = "host_node"（固定）
- set_liquid_from_plate: device_name = "PRCXI"（可配置，见 DEVICE_NAME_DEFAULT）
- transfer_liquid 等动作: device_name = "PRCXI"（可配置，见 DEVICE_NAME_DEFAULT）

==================== 校验规则 ====================

- 检查 sources/targets 是否在 reagent 中定义
- 检查 sources 和 targets 的 wells 数量是否匹配
- 检查参数数组长度是否与 wells 数量一致
- 如有问题，在 footer 中添加 [WARN: ...] 标记
"""

import json
import os
import re
import uuid
import warnings

import networkx as nx
from networkx.drawing.nx_agraph import to_agraph
import matplotlib.pyplot as plt
from typing import Dict, List, Any, Set, Tuple, Optional

from unilabos.workflow.labware_mapping import (
    infer_kind as _yaml_infer_kind,
    remap_slot as _yaml_remap_slot,
    resolve_target_class as _yaml_resolve_target_class,
)

# P6.1 默认目标仪器；caller 不显式传 target_device 时使用。
# 注意：这里写 "prcxi" 是 P6 历史兜底（与原版 _tip_prcxi_class_for_max_ul、
# _apply_prcxi_labware_auto_match 走 PRCXI 模板的语义一致），与 YAML
# 顶层是否声明 prcxi 段无关。
DEFAULT_TARGET_DEVICE = "prcxi"

Json = Dict[str, Any]


# ==================== 默认配置 ====================

# 设备名配置
DEVICE_NAME_HOST = "host_node"  # create_resource 固定在 host_node 上执行
DEVICE_NAME_DEFAULT = "PRCXI"  # transfer_liquid, set_liquid_from_plate 等动作的默认设备名

# 节点类型
NODE_TYPE_DEFAULT = "ILab"  # 所有节点的默认类型

CLASS_NAMES_MAPPING = {
    "plate": "PRCXI_BioER_96_wellplate",
    "tip_rack": "PRCXI_300ul_Tips",
}
# create_resource 节点默认参数
CREATE_RESOURCE_DEFAULTS = {
    "device_id": "/PRCXI",
    "parent_template": "/PRCXI/PRCXI_Deck",
}

# 默认液体体积 (uL)
DEFAULT_LIQUID_VOLUME = 1e5

# 参数重命名映射：单数 -> 复数（用于 transfer_liquid 等动作）
PARAM_RENAME_MAPPING = {
    "asp_vol": "asp_vols",
    "dis_vol": "dis_vols",
    "asp_flow_rate": "asp_flow_rates",
    "dis_flow_rate": "dis_flow_rates",
}


def _map_deck_slot(
    raw_slot: str,
    object_type: str = "",
    *,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> str:
    """协议槽位 -> 实际 deck：默认 4→13，8→14，12+trash→16，其余不变。

    P6.1.1：``slot_remap`` 内嵌在 ``target_devices.<target_device>`` 下，
    可由 ``target_devices.<target_device>.models.<target_model>.slot_remap`` 进一步覆盖。
    转调 :func:`labware_mapping.remap_slot`，走 4 段 fallback 链（model → device → default → builtin）。
    """
    return _yaml_remap_slot(
        raw_slot,
        object_type,
        target_device=target_device,
        target_model=target_model,
    )


def _labware_def_index(labware_defs: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    m: Dict[str, Dict[str, Any]] = {}
    for d in labware_defs or []:
        for k in ("id", "name", "reagent_id", "reagent"):
            key = d.get(k)
            if key is not None and str(key):
                m[str(key)] = d
    return m


def _labware_hint_text(labware_id: str, item: Dict[str, Any]) -> str:
    """合并 id 与协议里的 labware 描述（OpenTrons 全名常在 labware 字段）。"""
    parts = [str(labware_id), str(item.get("labware", "") or "")]
    return " ".join(parts).lower()


def _infer_reagent_kind(labware_id: str, item: Dict[str, Any]) -> str:
    """labware → ``plate / tip_rack / tube_rack / trash``。

    P6：转调 ``labware_mapping.infer_kind``，匹配规则由
    ``Uni-Lab-OS/labware_mapping.yaml`` 的 ``kinds`` 段声明（顺序敏感、首个命中胜出）。
    object 字段（``trash`` / ``tiprack``）优先级保留在 YAML loader 内部。

    KIND 判定**只依据真实 labware 名**（``item["labware"]``），不混入 reagent 业务键
    ``labware_id``：键里偶尔含 ``tuberack`` / ``rack`` / ``well`` 等子串（如 384b23 的
    ``using_tuberack`` / ``tuberack_a_tubes`` 键，labware 其实是 ``6x5_half_inch`` 普通板），
    会把板误判成 tube_rack，再经同槽归并把整槽拖到 4×6 适配器 → 行 E-H 越界。
    仅当 labware 字段为空时才回退到合并 hint。
    """
    lw = str(item.get("labware", "") or "").strip() if isinstance(item, dict) else ""
    hint = lw.lower() if lw else _labware_hint_text(labware_id, item)
    return _yaml_infer_kind(
        hint,
        (item.get("object") or "") if isinstance(item, dict) else "",
    )


# 源 Opentrons labware 定义孔数缓存：key=归一后的 labware 名，value=孔数(int) 或 None（查不到）。
_SOURCE_HOLE_COUNT_CACHE: Dict[str, Optional[int]] = {}
_OT_CUSTOM_DEFS_CACHE: Optional[Dict[str, Any]] = None


def _load_ot_custom_defs() -> Dict[str, Any]:
    """加载本地 opentrons_custom_labware_defs.json（含 ordering/wells），失败返回空 dict。

    与 :mod:`unilabos.resources.lab_resources` 用的是同一份定义文件；这里独立加载，
    避免为了取孔数而 import lab_resources（后者会拉起 pylabrobot）。
    """
    global _OT_CUSTOM_DEFS_CACHE
    if _OT_CUSTOM_DEFS_CACHE is None:
        try:
            path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "resources",
                "opentrons_custom_labware_defs.json",
            )
            with open(path, "r", encoding="utf-8") as f:
                _OT_CUSTOM_DEFS_CACHE = json.load(f) or {}
        except Exception:
            _OT_CUSTOM_DEFS_CACHE = {}
    return _OT_CUSTOM_DEFS_CACHE


def _hole_count_from_ot_definition(defn: Any) -> Optional[int]:
    """从一个 Opentrons labware 定义 dict 里取真实孔数：优先 wells，其次 ordering。"""
    if not isinstance(defn, dict):
        return None
    wells = defn.get("wells")
    if isinstance(wells, dict) and wells:
        return len(wells)
    ordering = defn.get("ordering")
    if isinstance(ordering, list) and ordering:
        try:
            return sum(len(col) for col in ordering)
        except TypeError:
            return None
    return None


def _source_labware_hole_count(labware_name: str) -> Optional[int]:
    """按源 Opentrons labware 名解析其真实物理孔数（权威真值）；解析不到返回 None。

    数据源优先级：
      1. ``opentrons_shared_data.labware.load_definition``（硬依赖，无需 pylabrobot）；
      2. 本地 ``opentrons_custom_labware_defs.json``（非标准 labware 兜底）。
    名字归一：先按原名（带点，如 ``1.5ml``）查；查不到再把 ``point`` 还原为 ``.`` 后重试，
    兼容清洗过的 ``1point5ml`` 命名。所有失败一律吞掉并返回 None（最小环境 / 未知 labware
    优雅回退到调用方的启发式），绝不因解析失败中断上传。
    """
    if not labware_name or not str(labware_name).strip():
        return None
    name = str(labware_name).strip()
    if name in _SOURCE_HOLE_COUNT_CACHE:
        return _SOURCE_HOLE_COUNT_CACHE[name]

    # 候选名：原名 + point→. 还原名（去重、保序）
    candidates = [name]
    depointed = re.sub(r"(\d+)point(\d+)", r"\1.\2", name)
    if depointed != name:
        candidates.append(depointed)

    result: Optional[int] = None

    # 1) opentrons_shared_data 标准定义
    try:
        from opentrons_shared_data.labware import load_definition as _ot_load_definition

        for cand in candidates:
            try:
                defn = _ot_load_definition(cand, 1)
            except Exception:
                continue
            hc = _hole_count_from_ot_definition(defn)
            if hc:
                result = hc
                break
    except Exception:
        result = result  # ImportError 等：跳过标准库路径

    # 2) 本地 custom 定义兜底
    if result is None:
        custom = _load_ot_custom_defs()
        if custom:
            for cand in candidates:
                hc = _hole_count_from_ot_definition(custom.get(cand))
                if hc:
                    result = hc
                    break

    _SOURCE_HOLE_COUNT_CACHE[name] = result
    return result


def _tube_rack_positions_from_name(labware_id: str, item: Dict[str, Any]) -> Optional[int]:
    """从 ``24_tuberack`` 等命名中解析孔位数；**无显式数字时返回 None**（不臆造默认值）。"""
    hint = _labware_hint_text(labware_id, item)
    for pat in (r"(\d+)_tuberack", r"tuberack[_\s]*(\d+)", r"(\d+)\s*[-_]?\s*pos(?:ition)?s?"):
        m = re.search(pat, hint)
        if m:
            return int(m.group(1))
    return None


def _infer_tube_rack_num_positions(labware_id: str, item: Dict[str, Any]) -> int:
    """从 ``24_tuberack`` 等命名中解析孔位数；解析不到则默认 96（历史兜底行为）。"""
    n = _tube_rack_positions_from_name(labware_id, item)
    return n if n is not None else 96


def _infer_plate_num_children_from_wells(wells: Any) -> Optional[int]:
    """根据 well 名推断孔板总孔数档位：列>12 或 行>H(8) 视为 384，否则 96。"""
    if not isinstance(wells, list) or not wells:
        return None
    max_row = 0
    max_col = 0
    for w in wells:
        m = re.match(r"^([A-Za-z]+)(\d+)$", str(w).strip())
        if not m:
            continue
        row_s, col_s = m.group(1).upper(), m.group(2)
        ri = 0
        for ch in row_s:
            ri = ri * 26 + (ord(ch) - ord("A") + 1)
        max_row = max(max_row, ri)
        max_col = max(max_col, int(col_s))
    if max_col <= 0:
        return None
    if max_col > 12 or max_row > 8:
        return 384
    return 96


def _well_grid_span(wells: Any) -> tuple[int, int]:
    """返回已用 well 名的 (最大行序号, 最大列序号)；解析不出时返回 (0, 0)。

    行号按字母进制：A=1 … Z=26、AA=27 …；列号取数字部分。用于判断一批 well 是否
    超出某 labware 的物理行列布局（如 24 位 4×6 适配器只有 A-D 行、1-6 列）。
    """
    max_row = 0
    max_col = 0
    if isinstance(wells, list):
        for w in wells:
            m = re.match(r"^([A-Za-z]+)(\d+)$", str(w).strip())
            if not m:
                continue
            row_s, col_s = m.group(1).upper(), m.group(2)
            ri = 0
            for ch in row_s:
                ri = ri * 26 + (ord(ch) - ord("A") + 1)
            max_row = max(max_row, ri)
            max_col = max(max_col, int(col_s))
    return max_row, max_col


def _infer_plate_num_children_from_labware_hint(labware_id: str, item: Dict[str, Any]) -> Optional[int]:
    """从 labware 命名（如 custom_384_wellplate、nest_96_wellplate）解析孔数，供模板匹配。

    P6 hint bug 修复（2026-05-22）：hint 只用 ``item["labware"]``，**不**拼上
    ``labware_id``（reagent_key 业务名，如 ``samples_6``、``samples_24`` 末尾数字
    会被宽松正则 ``[_\\s](\\d+)[_\\s]`` 误识别为孔板规格，进而触发
    ``_apply_target_labware_class_auto_match`` fallback 到 PRCXI 4-孔 trough 模板，
    最终把同 deck 槽位上所有 reagent 的 ``target_class_name`` unify 成错误的 trough class）。
    """
    hint = str(item.get("labware", "") or "").lower()
    m = re.search(
        r"\b(1536|384|96|48|24|12|6)(\s*[-_]?\s*well|wellplate|_well_)",
        hint,
    )
    if m:
        return int(m.group(1))
    # reservoir / trough：从命名中提取通道数（如 reagent_1_reservoir_130000ul、nest_12_reservoir_15ml）。
    # 这类载体在协议里经常只写 1 个 well（A1），若误推为 96 会把其映射到 96 孔板，
    # 进而把 source 初始体积 clamp 到 2200uL，触发 TooLittleLiquid。
    if "reservoir" in hint or "trough" in hint:
        m = re.search(
            r"(?:^|[_\s-])(\d+)(?=[_\s-]*(?:reservoir|trough))",
            hint,
        )
        if not m:
            m = re.search(
                r"(?:reservoir|trough)[_\s-]*(\d+)(?:[_\s-]|$)",
                hint,
            )
        if m:
            try:
                n = int(m.group(1))
                if n > 0:
                    return n
            except (TypeError, ValueError):
                pass
        wells = item.get("well")
        if isinstance(wells, list) and wells:
            return len(wells)
    m = re.search(r"[_\s](1536|384|96|48|24|12|6)[_\s]", hint)
    if m and ("well" in hint or "plate" in hint):
        return int(m.group(1))
    return None


def _infer_plate_num_children(
    labware_id: str,
    item: Dict[str, Any],
    wells: Any,
    num_from_def: int,
) -> int:
    """孔板用于 PRCXI 匹配的孔数：优先定义表，其次命名，再 well 地址，最后默认 96。"""
    if num_from_def > 0:
        return num_from_def
    hinted = _infer_plate_num_children_from_labware_hint(labware_id, item)
    if hinted is not None:
        return hinted
    from_wells = _infer_plate_num_children_from_wells(wells)
    if from_wells is not None:
        return from_wells
    return 96


def _tip_volume_hint(item: Dict[str, Any], labware_id: str) -> Optional[float]:
    s = _labware_hint_text(labware_id, item)
    for v in (1250, 1000, 300, 200, 10):
        if f"{v}ul" in s or f"{v}μl" in s or f"{v}u" in s:
            return float(v)
        if f" {v} " in f" {s} ":
            return float(v)
    return None


def _flatten_transfer_vols(value: Any) -> List[float]:
    """将 asp_vols/dis_vols 标量或列表展平为 float 列表，无法转换的项跳过。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: List[float] = []
        for v in value:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out
    try:
        return [float(value)]
    except (TypeError, ValueError):
        return []


def _apply_tip_rack_class_from_transfer_volumes(
    labware_info: Dict[str, Dict[str, Any]],
    protocol_steps_refactored: List[Dict[str, Any]],
    *,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> None:
    """根据各 ``transfer_liquid`` 的 asp_vols/dis_vols 为对应 ``tip_racks`` 写入 ``target_class_name``。

    P6.1：tip 量程档不再硬编码 PRCXI 三档，改为查
    ``labware_mapping.yaml`` 的 ``target_devices.<target_device>.rules``（tip_rack
    + hole_count=96 + volume_max 闭区间）。YAML 未命中时 fallback 到
    ``CLASS_NAMES_MAPPING['tip_rack']``（保守默认 PRCXI_300ul_Tips）。

    P6.1.1：``target_model`` 透传给 :func:`_yaml_resolve_target_class`，
    允许同厂商不同型号声明不同 tip 量程档（如 PRCXI 9320 与 4040 用同档，
    Beckman i7 与 i5 可能用不同档）。
    """
    tip_to_max_ul: Dict[str, float] = {}

    for step in protocol_steps_refactored:
        if step.get("template_name") != "transfer_liquid":
            continue
        p = step.get("param") or {}
        tip_key_raw = p.get("tip_racks")
        if tip_key_raw is None or str(tip_key_raw).strip() == "":
            continue
        tip_key = str(tip_key_raw).strip()
        if tip_key not in labware_info:
            continue

        nums = _flatten_transfer_vols(p.get("asp_vols", p.get("asp_vol"))) + _flatten_transfer_vols(
            p.get("dis_vols", p.get("dis_vol"))
        )
        if not nums:
            continue
        step_max = max(nums)
        tip_to_max_ul[tip_key] = max(tip_to_max_ul.get(tip_key, 0.0), step_max)

    default_tip_cls = CLASS_NAMES_MAPPING.get("tip_rack", "PRCXI_300ul_Tips")
    for tip_key, max_ul in tip_to_max_ul.items():
        item = labware_info.get(tip_key)
        if item is None:
            continue
        if _infer_reagent_kind(tip_key, item) != "tip_rack":
            continue
        cls = _yaml_resolve_target_class(
            target_device, "tip_rack", hole_count=96, volume=max_ul, target_model=target_model
        )
        item["target_class_name"] = cls if cls else default_tip_cls


def _volume_template_covers_requirement(template: Dict[str, Any], req: Optional[float], kind: str) -> bool:
    """有明确需求体积时，模板标称 Volume 必须 >= 需求；无 Volume 的模板不参与（trash 除外）。"""
    if kind == "trash":
        return True
    if req is None or req <= 0:
        return True
    mv = float(template.get("Volume") or 0)
    if mv <= 0:
        return False
    return mv >= req


def _direct_labware_class_name(item: Dict[str, Any]) -> str:
    """仅用于 tip_rack 且 ``preserve_tip_rack_incoming_class=True``：``class_name``/``class`` 原样；否则 ``labware`` → ``lab_*``。"""
    explicit = item.get("class_name") or item.get("class")
    if explicit is not None and str(explicit).strip() != "":
        return str(explicit).strip()
    lw = str(item.get("labware", "") or "").strip()
    if lw:
        return f"lab_{lw.lower().replace('.', 'point').replace(' ', '_')}"
    return ""


def _match_score_prcxi_template(
    template: Dict[str, Any],
    num_children: int,
    child_max_volume: Optional[float],
) -> float:
    """孔数差主导；有需求体积且模板已满足 >= 时，余量比例 (模板-需求)/需求 越小越好（优先选刚好够的）。"""
    hole_count = int(template.get("hole_count") or 0)
    hole_diff = abs(num_children - hole_count)
    material_volume = float(template.get("Volume") or 0)
    req = child_max_volume
    if req is not None and req > 0 and material_volume > 0:
        vol_diff = (material_volume - req) / max(req, 1e-9)
    elif material_volume > 0 and req is not None:
        vol_diff = abs(float(req) - material_volume) / material_volume
    else:
        vol_diff = 0.0
    return hole_diff * 1000 + vol_diff


def _apply_target_labware_class_auto_match(
    labware_info: Dict[str, Dict[str, Any]],
    labware_defs: Optional[List[Dict[str, Any]]] = None,
    *,
    preserve_tip_rack_incoming_class: bool = True,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> None:
    """上传构建图前：按孔数 + 容量将 reagent 条目匹配到目标仪器物料注册类名，写入 ``target_class_name``。

    P6.1 流程：

    1. 先查 ``labware_mapping.yaml`` 的 ``target_devices.<target_device>.rules``
       （未声明的 target_device 由 :func:`_yaml_resolve_target_class` 自动 fallback
       到固定段 ``target_devices.default``）；命中直接采用。
    2. YAML 未命中（孔数 / 体积超出表内规则覆盖范围）→ 走 ``prcxi_labware``
       注册模板打分匹配 fallback，并打 warning 提示「请补到映射表」。

    若给出需求体积，仅选用模板标称 Volume >= 该值的物料，并在满足条件的模板中选余量最小者。

    ``preserve_tip_rack_incoming_class=True``（默认）时：**仅 tip_rack** 不做模板匹配，类名由 ``class_name``/``class`` 或
    ``labware``（``lab_*``）直接给出；**plate / tube_rack / trash 等**仍按注册模板匹配。
    ``False`` 时 **全部**（含 tip_rack）走模板匹配。"""
    if not labware_info:
        return

    default_prcxi_tip_class = CLASS_NAMES_MAPPING.get("tip_rack", "PRCXI_300ul_Tips")

    # P6.1：模板 fallback 只在 prcxi_labware 可导入且非空时启用；YAML 查表路径**始终**生效。
    # 这样在最小 Python 环境（无 pylabrobot）下，YAML 命中也能写入 target_class_name。
    templates: List[Dict[str, Any]] = []
    try:
        from unilabos.devices.liquid_handling.prcxi.prcxi_labware import get_prcxi_labware_template_specs
        templates = list(get_prcxi_labware_template_specs() or [])
    except Exception:
        templates = []

    def_map = _labware_def_index(labware_defs)

    for labware_id, item in labware_info.items():
        if item.get("target_class_name"):
            continue

        kind = _infer_reagent_kind(labware_id, item)

        if preserve_tip_rack_incoming_class and kind == "tip_rack":
            inc_s = _direct_labware_class_name(item)
            if inc_s == default_prcxi_tip_class:
                inc_s = ""
            if inc_s:
                item["target_class_name"] = inc_s
            continue

        explicit = item.get("class_name") or item.get("class")
        if explicit and str(explicit).startswith("PRCXI_"):
            item["target_class_name"] = str(explicit)
            continue

        extra = def_map.get(str(labware_id), {})

        wells = item.get("well") or []
        well_n = len(wells) if isinstance(wells, list) else 0
        num_from_def = int(extra.get("num_wells") or extra.get("well_count") or item.get("num_wells") or 0)

        # 权威信号：源 Opentrons labware 定义里的真实孔数（wells/ordering）。
        # 优先于名字正则 / 已用孔数，避免 reagent-key 拆分把已用孔少计（如 4→2）导致选型偏小。
        src_holes = _source_labware_hole_count(item.get("labware", ""))

        if kind == "trash":
            num_children = 0
        elif kind == "tip_rack":
            num_children = num_from_def if num_from_def > 0 else 96
        elif kind == "tube_rack":
            if src_holes:
                num_children = src_holes
            else:
                # 定义查不到（未知 / 自定义 labware）：取多信号最大值，宁大勿小，防「4→2」少计。
                # 候选：定义表孔数、名字显式孔数、已用孔经行列跨度抬档。
                _cands = []
                if num_from_def > 0:
                    _cands.append(num_from_def)
                _name_n = _tube_rack_positions_from_name(labware_id, item)
                if _name_n is not None:
                    _cands.append(_name_n)
                if well_n > 0:
                    _used_n = well_n
                    # 若已用 well 的行/列跨度超出 24 位适配器 (4 行 A-D × 6 列) 的几何，
                    # 按真实网格档位 (96=8×12 / 384) 取容量。否则 96/384 管架会被按"已用孔数"
                    # 误配到 4×6 适配器，行 E-H 或列 7-12 越界 → pylabrobot get_item 抛
                    # "'E1' is not in list"（孔位映射类失败的根因）。
                    _mr, _mc = _well_grid_span(wells)
                    if _mr > 4 or _mc > 6:
                        _used_n = 384 if (_mr > 8 or _mc > 12) else 96
                    _cands.append(_used_n)
                num_children = max(_cands) if _cands else _infer_tube_rack_num_positions(labware_id, item)
        else:
            # plate：定义孔数优先；查不到再走名字→well→96 的既有启发式
            # （勿在无 labware_defs 时默认 96，否则 384 板会被错配成 96 模板）。
            if src_holes:
                num_children = src_holes
            else:
                num_children = _infer_plate_num_children(labware_id, item, wells, num_from_def)

        child_max_volume = item.get("max_volume")
        if child_max_volume is None:
            child_max_volume = extra.get("max_volume")
        try:
            child_max_volume_f = float(child_max_volume) if child_max_volume is not None else None
        except (TypeError, ValueError):
            child_max_volume_f = None

        if kind == "tip_rack" and child_max_volume_f is None:
            child_max_volume_f = _tip_volume_hint(item, labware_id) or 300.0

        # P6.1: 先查 labware_mapping.yaml；命中直接采用，跳过 PRCXI 模板打分匹配
        # P6.1.1: target_model 透传，允许型号级 rules 覆盖
        yaml_cls = _yaml_resolve_target_class(
            target_device,
            kind,
            hole_count=num_children if kind != "trash" else None,
            volume=child_max_volume_f,
            target_model=target_model,
        )
        if yaml_cls:
            item["target_class_name"] = yaml_cls
            continue

        # YAML 未命中：fallback 到 PRCXI 模板打分匹配（保留历史行为）+ warning 提示补表
        candidates = [t for t in templates if t["kind"] == kind]
        if not candidates:
            continue

        best = None
        best_score = float("inf")
        for t in candidates:
            if kind != "trash" and int(t.get("hole_count") or 0) <= 0:
                continue
            if not _volume_template_covers_requirement(t, child_max_volume_f, kind):
                continue
            sc = _match_score_prcxi_template(t, num_children, child_max_volume_f)
            if sc < best_score:
                best_score = sc
                best = t

        if best:
            item["target_class_name"] = best["class_name"]
            warnings.warn(
                f"labware {labware_id!r} (kind={kind}, holes={num_children}, vol={child_max_volume_f}) "
                f"未在 labware_mapping.yaml 的 target_devices.{target_device}.rules / "
                f"target_devices.default.rules 中命中，已用 PRCXI 模板兜底 {best['class_name']}；"
                f"建议在 labware_mapping.yaml 中补一条对应规则。"
            )


def _prcxi_class_capacity(class_name: Optional[str]) -> int:
    """估算 PRCXI labware class 的孔位容量（用于同槽位归并时挑"能装下所有 well"的最大网格）。

    EP_Adapter（无数字）按 24（4×6）；其余从类名里的数字段取（96/384/12/4/1）；解析不出按 0。
    """
    if not class_name:
        return -1
    if "EP_Adapter" in class_name:
        return 24
    m = re.search(r"_(\d+)(?:_|$)", str(class_name))
    return int(m.group(1)) if m else 0


def _reconcile_slot_carrier_target_class(
    labware_info: Dict[str, Dict[str, Any]],
    *,
    preserve_tip_rack_incoming_class: bool = False,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> None:
    """同一 deck 槽位上多条 reagent 时，按载体类型优先级统一 ``target_class_name``，避免先遍历到 96 板后槽位被错误绑定。

    ``preserve_tip_rack_incoming_class=True`` 时：tip_rack 条目不参与同槽类名合并（不被覆盖、也不把 tip 类名扩散到同槽其它条目）。

    P6.1.1：``target_device`` / ``target_model`` 透传给 :func:`_map_deck_slot`，
    保证「同槽位归并」按目标仪器型号的实际 deck 物理布局进行。
    """
    by_slot: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for lid, item in labware_info.items():
        ot = item.get("object", "") or ""
        slot = _map_deck_slot(
            str(item.get("slot", "")),
            ot,
            target_device=target_device,
            target_model=target_model,
        )
        if not slot:
            continue
        by_slot.setdefault(str(slot), []).append((lid, item))

    priority = {"trash": 0, "tube_rack": 1, "tip_rack": 2, "plate": 3}

    for _slot, pairs in by_slot.items():
        if len(pairs) < 2:
            continue

        def _rank(p: Tuple[str, Dict[str, Any]]) -> int:
            return priority.get(_infer_reagent_kind(p[0], p[1]), 9)

        pairs_sorted = sorted(pairs, key=_rank)
        # 候选 = 有 class 的非 tip 条目（tip 在 preserve 模式下不参与归并）
        cand = [
            (lid, it)
            for lid, it in pairs_sorted
            if it.get("target_class_name")
            and not (preserve_tip_rack_incoming_class and _infer_reagent_kind(lid, it) == "tip_rack")
        ]
        if not cand:
            continue
        # 取优先级最高的 kind（trash<tube_rack<tip_rack<plate），在该 kind 内挑"容量最大"的 class。
        # 原因：同一物理 labware 常被拆成多个 reagent key（well 子集不同），各自按子集几何
        # 解析出不同 class（如 samples=A-D→EP_Adapter、samples_2=A-H→BioER_96）。必须统一到
        # 能容纳所有 well 的最大网格，否则小网格放不下 E-H 行 → 运行时 get_item 越界报错。
        top_kind = _infer_reagent_kind(cand[0][0], cand[0][1])
        same_kind_classes = [
            it.get("target_class_name")
            for lid, it in cand
            if _infer_reagent_kind(lid, it) == top_kind
        ]
        best_cls = max(same_kind_classes, key=_prcxi_class_capacity)
        if not best_cls:
            continue
        for lid, it in pairs:
            if preserve_tip_rack_incoming_class and _infer_reagent_kind(lid, it) == "tip_rack":
                continue
            it["target_class_name"] = best_cls


# ---------------- Graph ----------------


class WorkflowGraph:
    """简单的有向图实现：使用 params 单层参数；inputs 内含连线；支持 node-link 导出"""

    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Dict[str, Any]] = []

    def add_node(self, node_id: str, **attrs):
        self.nodes[node_id] = attrs

    def add_edge(self, source: str, target: str, **attrs):
        # 将 source_port/target_port 映射为服务端期望的 source_handle_key/target_handle_key
        source_handle_key = attrs.pop("source_port", "") or attrs.pop("source_handle_key", "")
        target_handle_key = attrs.pop("target_port", "") or attrs.pop("target_handle_key", "")

        edge = {
            "source": source,
            "target": target,
            "source_node_uuid": source,
            "target_node_uuid": target,
            "source_handle_key": source_handle_key,
            "source_handle_io": attrs.pop("source_handle_io", "source"),
            "target_handle_key": target_handle_key,
            "target_handle_io": attrs.pop("target_handle_io", "target"),
            **attrs,
        }
        self.edges.append(edge)

    def _materialize_wiring_into_inputs(
        self,
        obj: Any,
        inputs: Dict[str, Any],
        variable_sources: Dict[str, Dict[str, Any]],
        target_node_id: str,
        base_path: List[str],
    ):
        has_var = False

        def walk(node: Any, path: List[str]):
            nonlocal has_var
            if isinstance(node, dict):
                if "__var__" in node:
                    has_var = True
                    varname = node["__var__"]
                    placeholder = f"${{{varname}}}"
                    src = variable_sources.get(varname)
                    if src:
                        key = ".".join(path)  # e.g. "params.foo.bar.0"
                        inputs[key] = {"node": src["node_id"], "output": src.get("output_name", "result")}
                        self.add_edge(
                            str(src["node_id"]),
                            target_node_id,
                            source_handle_io=src.get("output_name", "result"),
                            target_handle_io=key,
                        )
                    return placeholder
                return {k: walk(v, path + [k]) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v, path + [str(i)]) for i, v in enumerate(node)]
            return node

        replaced = walk(obj, base_path[:])
        return replaced, has_var

    def add_workflow_node(
        self,
        node_id: int,
        *,
        device_key: Optional[str] = None,  # 实例名，如 "ser"
        resource_name: Optional[str] = None,  # registry key（原 device_class）
        module: Optional[str] = None,
        template_name: Optional[str] = None,  # 动作/模板名（原 action_key）
        params: Dict[str, Any],
        variable_sources: Dict[str, Dict[str, Any]],
        add_ready_if_no_vars: bool = True,
        prev_node_id: Optional[int] = None,
        **extra_attrs,
    ) -> None:
        """添加工作流节点：params 单层；自动变量连线与 ready 串联；支持附加属性"""
        node_id_str = str(node_id)
        inputs: Dict[str, Any] = {}

        params, has_var = self._materialize_wiring_into_inputs(
            params, inputs, variable_sources, node_id_str, base_path=["params"]
        )

        if add_ready_if_no_vars and not has_var:
            last_id = str(prev_node_id) if prev_node_id is not None else "-1"
            inputs["ready"] = {"node": int(last_id), "output": "ready"}
            self.add_edge(last_id, node_id_str, source_handle_io="ready", target_handle_io="ready")

        node_obj = {
            "device_key": device_key,
            "resource_name": resource_name,  # ✅ 新名字
            "module": module,
            "template_name": template_name,  # ✅ 新名字
            "params": params,
            "inputs": inputs,
        }
        node_obj.update(extra_attrs or {})
        self.add_node(node_id_str, parameters=node_obj)

    # 顺序工作流导出（连线在 inputs，不返回 edges）
    def to_dict(self) -> List[Dict[str, Any]]:
        result = []
        for node_id, attrs in self.nodes.items():
            node = {"uuid": node_id}
            params = dict(attrs.get("parameters", {}) or {})
            flat = {k: v for k, v in attrs.items() if k != "parameters"}
            flat.update(params)
            node.update(flat)
            result.append(node)
        return sorted(result, key=lambda n: int(n["uuid"]) if str(n["uuid"]).isdigit() else n["uuid"])

    # node-link 导出（含 edges）
    def to_node_link_dict(self) -> Dict[str, Any]:
        nodes_list = []
        for node_id, attrs in self.nodes.items():
            node_attrs = attrs.copy()
            params = node_attrs.pop("parameters", {}) or {}
            node_attrs.update(params)
            nodes_list.append({"uuid": node_id, **node_attrs})
        return {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": nodes_list,
            "edges": self.edges,
            "links": self.edges,
        }


def refactor_data(
    data: List[Dict[str, Any]],
    action_resource_mapping: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """统一的数据重构函数，根据操作类型自动选择模板

    Args:
        data: 原始步骤数据列表
        action_resource_mapping: action 到 resource_name 的映射字典，可选
    """
    refactored_data = []

    # 定义操作映射，包含生物实验和有机化学的所有操作
    OPERATION_MAPPING = {
        # 生物实验操作
        "transfer_liquid": "transfer_liquid",
        "transfer": "transfer",
        "incubation": "incubation",
        "move_labware": "move_labware",
        "oscillation": "oscillation",
        # 有机化学操作
        "HeatChillToTemp": "HeatChillProtocol",
        "StopHeatChill": "HeatChillStopProtocol",
        "StartHeatChill": "HeatChillStartProtocol",
        "HeatChill": "HeatChillProtocol",
        "Dissolve": "DissolveProtocol",
        "Transfer": "TransferProtocol",
        "Evaporate": "EvaporateProtocol",
        "Recrystallize": "RecrystallizeProtocol",
        "Filter": "FilterProtocol",
        "Dry": "DryProtocol",
        "Add": "AddProtocol",
    }

    UNSUPPORTED_OPERATIONS = ["Purge", "Wait", "Stir", "ResetHandling"]

    for step in data:
        operation = step.get("action")
        if not operation or operation in UNSUPPORTED_OPERATIONS:
            continue

        # 处理重复操作
        if operation == "Repeat":
            times = step.get("times", step.get("parameters", {}).get("times", 1))
            sub_steps = step.get("steps", step.get("parameters", {}).get("steps", []))
            for i in range(int(times)):
                sub_data = refactor_data(sub_steps, action_resource_mapping)
                refactored_data.extend(sub_data)
            continue

        # 获取模板名称
        template_name = OPERATION_MAPPING.get(operation)
        if not template_name:
            # 自动推断模板类型
            if operation.lower() in ["transfer", "incubation", "move_labware", "oscillation"]:
                template_name = f"biomek-{operation}"
            else:
                template_name = f"{operation}Protocol"

        # 获取 resource_name
        resource_name = f"device.{operation.lower()}"
        if action_resource_mapping:
            resource_name = action_resource_mapping.get(operation, resource_name)

        # 获取步骤编号，生成 name 字段
        step_number = step.get("step_number")
        name = f"Step {step_number}" if step_number is not None else None

        # 创建步骤数据
        step_data = {
            "template_name": template_name,
            "resource_name": resource_name,
            "description": step.get("description", step.get("purpose", f"{operation} operation")),
            "lab_node_type": "Transport" if "transfer" in template_name.lower() else "Device",
            "param": step.get("parameters", step.get("action_args", {})),
            "footer": f"{template_name}-{resource_name}",
        }
        if name:
            step_data["name"] = name
        refactored_data.append(step_data)

    return refactored_data


MERGED_TARGETS_SYNTHETIC_PREFIX = "_merged_targets_"
# P2 v3：sources 端跨板/多 key 聚合的 synthetic key 前缀（与 targets 对称）。
# 转换器的 N:N 合并会把 transfer.sources 产出成 list[str]（多个 reagent_key），而图构建的
# 单边路径只认 str（``resource_name in dict`` 对 list 会 ``unhashable type: 'list'`` 崩溃）。
# 这里用与 merged-targets 完全对称的 merged set_liquid_from_plate 节点把 list[str] sources
# 收敛成一个 synthetic str source key，从而支持「合并后仍可上传」。
MERGED_SOURCES_SYNTHETIC_PREFIX = "_merged_sources_"


def _collect_set_liquid_coverage(
    protocol_steps: List[Dict[str, Any]],
) -> Tuple[Set[str], Set[str]]:
    """P2 v2 §14：预扫描 transfer_liquid 的 ``params.targets``，统计 reagent_key 覆盖关系。

    输入要求：``protocol_steps`` 已经过 :func:`refactor_data` 标准化，每个 step 形如
    ``{"template_name": "transfer_liquid", "param": {"targets": ...}, ...}``。

    Returns
    -------
    (covered_by_merged, referenced_by_str)
        ``covered_by_merged`` —— 所有出现在某个 ``list[str] targets`` 中的 reagent_keys。
        ``referenced_by_str`` —— 所有以 ``str`` 形态出现在 ``targets`` 中的 reagent_keys。

    用途
    ----
    第二步循环（``for labware_id, item in labware_info.items()``）根据这两个集合
    判断某 target reagent_key 是否完全被 ``_emit_merged_set_liquid`` 接管：
    若 ``key ∈ covered_by_merged ∧ key ∉ referenced_by_str``，则跳过 per-plate
    ``set_liquid_from_plate`` 节点（避免冗余）。

    详见 ``product_designs/protocol_convert/02-cross-slot-merge.md`` §14。
    """
    covered_by_merged: Set[str] = set()
    referenced_by_str: Set[str] = set()
    for step in protocol_steps:
        if step.get("template_name") != "transfer_liquid":
            continue
        tgt = (step.get("param") or {}).get("targets")
        if isinstance(tgt, list):
            for t in tgt:
                if isinstance(t, str) and t:
                    covered_by_merged.add(t)
        elif isinstance(tgt, str) and tgt:
            referenced_by_str.add(tgt)
    return covered_by_merged, referenced_by_str


def _emit_merged_set_liquid(
    G: "WorkflowGraph",
    target_reagent_keys: List[str],
    labware_info: Dict[str, Dict[str, Any]],
    slot_to_create_resource: Dict[str, str],
    *,
    set_liquid_group_id: str,
    merged_index: int,
    target_device: str,
    target_model: Optional[str],
    synthetic_prefix: str = MERGED_TARGETS_SYNTHETIC_PREFIX,
    well_volume: float = 0,
    kind_label: str = "Targets",
) -> Tuple[str, str]:
    """P2 v2：为含 ``list[str] targets`` 的 transfer_liquid 节点插入一个 merged
    ``set_liquid_from_plate`` 跨板聚合节点。

    详见 ``product_designs/protocol_convert/02-cross-slot-merge.md`` §9.2 / §9.5。

    构造逻辑：

    1. 按 ``target_reagent_keys`` 顺序遍历，逐 key 使用独立 cursor 取
       ``labware_info[key].well[cursor % len(wells)]`` 作为该 dispense 对应的 well 名；
       wells 列表为空时退化为 ``key`` 本身（不带 ``/<well>`` 后缀）。
    2. 把每个 dispense 的 ``{id, name, parent: key, type: "well"}`` 顺序压入
       merged 节点的 ``param.wells``——这是 v2 的「顺序权威」载体（构造期固化）。
    3. 多入边：对每个 distinct reagent_key 涉及的 plate，从对应 ``create_resource``
       节点连一条 ``labware → wells_identifier`` 入边（同 plate 不重复连接）。
    4. 注册一个 synthetic str ``_merged_targets_<idx>``，供 caller 改写
       ``params.targets`` 与 ``resource_last_writer`` 映射。

    Returns
    -------
    (synthetic_key, merged_node_id)
        ``synthetic_key`` —— 写入到 ``transfer_liquid.params.targets``（str 形态），
        以及 ``resource_last_writer[synthetic_key] = f"{merged_node_id}:output_wells"``。
        ``merged_node_id`` —— 新插入节点的 UUID。
    """
    # 每个 reagent_key 一个 cursor，按 dispense 顺序推进；mod 处理同 well 重复 dispense
    cursor: Dict[str, int] = {}
    merged_wells: List[Dict[str, Any]] = []
    liquid_names: List[str] = []
    # P2 v2 §14 fix（2026-05-22）：merged 节点的 well_names 用 "<plate_plr_name>/<well>" 形态
    # 编码每个 dispense 对应的 PLR Plate 实例名，让 abstract 层 fallback 能定位跨板 plate
    # （否则 ROS placeholder 的 wells_identifier 多入边只保留最后一个 plate，导致跨板信息丢失）。
    # plate_plr_name 复用 create_resource 节点的命名约定: f"{target_class_name}_slot_{mapped_slot}".
    well_names_prefixed: List[str] = []
    for key in target_reagent_keys:
        info = labware_info.get(key) or {}
        wells = info.get("well") or []
        idx = cursor.get(key, 0)
        if wells:
            well_name = wells[idx % len(wells)]
            ref_id = f"{key}/{well_name}"
        else:
            well_name = None
            ref_id = key
        cursor[key] = idx + 1
        merged_wells.append({
            "id": ref_id,
            "name": ref_id,
            "parent": key,
            "type": "well",
        })
        # P8（2026-05-24）：reagent block 显式 ``liquid_name`` 字段优先，作为写入 PLR
        # tracker / 前端的真实化学名；缺省时 fallback 到 reagent_key（行为不变）。
        # 详见 ``product_designs/protocol_convert/08-liquid-name-from-reagent-block.md`` §3.4。
        ln_value = info.get("liquid_name") or str(key)
        liquid_names.append(ln_value)

        # 计算 PLR Plate name 给 well_names prefix（跨板 fallback 用）
        object_type = info.get("object", "") or ""
        mapped_slot = _map_deck_slot(
            str(info.get("slot", "")),
            object_type,
            target_device=target_device,
            target_model=target_model,
        )
        target_class = info.get("target_class_name") or ""
        if target_class and mapped_slot and well_name:
            plate_plr_name = f"{target_class}_slot_{mapped_slot}".replace(" ", "_")
            well_names_prefixed.append(f"{plate_plr_name}/{well_name}")
        elif well_name:
            # target_class 未知时仅写 well 名（abstract 层会走单 plate fallback；
            # 跨板信息丢失，但至少不破坏单 slot 协议）
            well_names_prefixed.append(well_name)
        else:
            well_names_prefixed.append("")

    merged_node_id = str(uuid.uuid4())
    synthetic_key = f"{synthetic_prefix}{merged_index}"

    G.add_node(
        merged_node_id,
        template_name="set_liquid_from_plate",
        resource_name="liquid_handler.prcxi",
        name=synthetic_key,
        display_name=f"Merged{kind_label}({len(set(target_reagent_keys))}p×{len(merged_wells)}w)",
        description=f"Merged set_liquid_from_plate: {kind_label.lower()}={target_reagent_keys}",
        lab_node_type="Reagent",
        footer="set_liquid_from_plate-liquid_handler.prcxi",
        device_name=DEVICE_NAME_DEFAULT,
        type=NODE_TYPE_DEFAULT,
        parent_uuid=set_liquid_group_id,
        minimized=True,
        param={
            "wells": merged_wells,
            "liquid_names": liquid_names,
            # targets：well_volume=0（仅占位，不预注液）；sources：well_volume=DEFAULT_LIQUID_VOLUME
            # （set_liquid 是绝对覆盖语义，源孔必须保留液体，否则后续 aspirate 取到空孔）。
            "volumes": [well_volume] * len(merged_wells),
            # 兼容字段：保留 plate/well_names 让旧 runtime / 旧前端可继续解析
            "plate": [],
            # 升级：well_names 元素为 "<plate_plr_name>/<well>" 形态（含跨板 plate 定位信息），
            # abstract 层 set_liquid_from_plate 的 schema_fallback 会按 "/" 拆解逐个查 plate。
            "well_names": well_names_prefixed,
        },
    )

    # 多入边：对每个 distinct plate 接一条 create_resource.labware → wells_identifier
    seen_keys: set = set()
    for key in target_reagent_keys:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        info = labware_info.get(key) or {}
        object_type = info.get("object", "") or ""
        mapped_slot = _map_deck_slot(
            str(info.get("slot", "")),
            object_type,
            target_device=target_device,
            target_model=target_model,
        )
        cr_node = slot_to_create_resource.get(mapped_slot)
        if cr_node:
            G.add_edge(
                cr_node,
                merged_node_id,
                source_port="labware",
                target_port="wells_identifier",
            )

    return synthetic_key, merged_node_id


def build_protocol_graph(
    labware_info: Dict[str, Dict[str, Any]],
    protocol_steps: List[Dict[str, Any]],
    workstation_name: str,
    action_resource_mapping: Optional[Dict[str, str]] = None,
    labware_defs: Optional[List[Dict[str, Any]]] = None,
    preserve_tip_rack_incoming_class: bool = False,
    target_device: str = DEFAULT_TARGET_DEVICE,
    target_model: Optional[str] = None,
) -> WorkflowGraph:
    """统一的协议图构建函数，根据设备类型自动选择构建逻辑

    Args:
        labware_info: labware 信息字典，格式为 {name: {slot, well, labware, ...}, ...}
        protocol_steps: 协议步骤列表
        workstation_name: 工作站名称
        action_resource_mapping: action 到 resource_name 的映射字典，可选
        labware_defs: 可选，``[{"id": "...", "num_wells": 96, "max_volume": 2200}, ...]`` 等，辅助 PRCXI 模板匹配
        preserve_tip_rack_incoming_class: 默认 True 时**仅 tip_rack** 不跑模板匹配（类名由传入的 class/labware 决定）；
            **其它载体**仍按 PRCXI 模板匹配。False 时 **全部**（含 tip_rack）都走模板匹配。
        target_device: P6.1 新增。目标仪器名（厂商粒度，如 ``prcxi`` / ``beckman`` / ``tecan``）。
            决定查 ``labware_mapping.yaml`` 中 ``target_devices.<target_device>.rules`` 段；未声明的
            名字由 :func:`labware_mapping.resolve_target_class` 自动 fallback 到固定段
            ``target_devices.default``。默认 ``"prcxi"``（与历史 P6 完全等价）。
        target_model: P6.1.1 新增。同厂商内的目标型号名（如 ``"9320"`` / ``"4040"``）；
            决定查 ``target_devices.<target_device>.models.<target_model>`` 下的 ``slot_remap`` /
            ``rules`` 覆盖。``None`` 表示不区分型号，走厂商级配置。

    会先 ``refactor_data`` 规范化步骤，再根据 ``transfer_liquid`` 的 ``asp_vols``/``dis_vols`` 为对应
    ``tip_racks`` 写入 ``target_class_name``（最大体积 ``≤10`` → ``PRCXI_10uL_Tips``，``<300`` → ``PRCXI_300ul_Tips``，
    否则 ``PRCXI_1000uL_Tips``）；无有效体积的步骤不覆盖。
    """
    G = WorkflowGraph()
    resource_last_writer = {}  # reagent_name -> "node_id:port"
    slot_to_create_resource = {}  # slot -> create_resource node_id

    protocol_steps = refactor_data(protocol_steps, action_resource_mapping)
    _apply_tip_rack_class_from_transfer_volumes(
        labware_info,
        protocol_steps,
        target_device=target_device,
        target_model=target_model,
    )

    _apply_target_labware_class_auto_match(
        labware_info,
        labware_defs,
        preserve_tip_rack_incoming_class=preserve_tip_rack_incoming_class,
        target_device=target_device,
        target_model=target_model,
    )
    _reconcile_slot_carrier_target_class(
        labware_info,
        preserve_tip_rack_incoming_class=preserve_tip_rack_incoming_class,
        target_device=target_device,
        target_model=target_model,
    )

    # ==================== 第一步：按 slot 去重创建 create_resource 节点 ====================
    # 按槽聚合：同一 slot 多条 reagent 时不能只取遍历顺序第一条，否则 tip 的 target_class_name / object 会被其它条目盖住
    by_slot: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    for labware_id, item in labware_info.items():
        object_type = item.get("object", "") or ""
        slot = _map_deck_slot(
            str(item.get("slot", "")),
            object_type,
            target_device=target_device,
            target_model=target_model,
        )
        if not slot:
            continue
        by_slot.setdefault(slot, []).append((labware_id, item))

    slots_info: Dict[str, Dict[str, Any]] = {}
    for slot, pairs in by_slot.items():
        def _ot_tip(it: Dict[str, Any]) -> bool:
            return str(it.get("object", "") or "").strip().lower() == "tiprack"

        tip_pairs = [(lid, it) for lid, it in pairs if _ot_tip(it)]
        chosen_lid = ""
        chosen_item: Dict[str, Any] = {}
        target_class_val: Optional[str] = None

        scan = tip_pairs if tip_pairs else pairs
        for lid, it in scan:
            c = it.get("target_class_name")
            if c:
                chosen_lid, chosen_item, target_class_val = lid, it, str(c)
                break
        if not chosen_lid and scan:
            chosen_lid, chosen_item = scan[0]
            pv = chosen_item.get("target_class_name")
            target_class_val = str(pv) if pv else None

        labware = str(chosen_item.get("labware", "") or "")
        slots_info[slot] = {
            "labware": labware,
            "labware_id": chosen_lid,
            "object": chosen_item.get("object", "") or "",
            "target_class_name": target_class_val,
        }

    # 创建 Group 节点，包含所有 create_resource 节点
    group_node_id = str(uuid.uuid4())
    G.add_node(
        group_node_id,
        name="Resources Group",
        type="Group",
        parent_uuid="",
        lab_node_type="Device",
        template_name="",
        resource_name="",
        footer="",
        minimized=True,
        param=None,
    )

    trash_create_node_id = None  # 记录 trash 的 create_resource 节点

    # 为每个唯一的 slot 创建 create_resource 节点
    for slot, info in slots_info.items():
        node_id = str(uuid.uuid4())
        object_type = info.get("object", "") or ""
        ot_lo = str(object_type).strip().lower()
        matched = info.get("target_class_name")
        if ot_lo == "trash":
            res_type_name = "PRCXI_trash"
        elif matched:
            res_type_name = matched
        elif ot_lo == "tiprack":
            if preserve_tip_rack_incoming_class:
                lid = str(info.get("labware_id") or "").strip() or "tip_rack"
                res_type_name = f"lab_{lid.lower().replace('.', 'point').replace(' ', '_')}"
            else:
                res_type_name = CLASS_NAMES_MAPPING.get("tip_rack", "PRCXI_300ul_Tips")
        else:
            res_type_name = f"lab_{info['labware'].lower().replace('.', 'point')}"
        # 上传物料：匹配后的类型名 + _slot_ + 槽位（name / display_name / res_id 一致）
        res_id = f"{res_type_name}_slot_{slot}".replace(" ", "_")
        G.add_node(
            node_id,
            template_name="create_resource",
            resource_name="host_node",
            name=res_id,
            display_name=res_id,
            description=f"Create plate on slot {slot}",
            lab_node_type="Labware",
            footer="create_resource-host_node",
            device_name=DEVICE_NAME_HOST,
            type=NODE_TYPE_DEFAULT,
            parent_uuid=group_node_id,  # 指向 Group 节点
            minimized=True,  # 折叠显示
            param={
                "res_id": res_id,
                "device_id": CREATE_RESOURCE_DEFAULTS["device_id"],
                "class_name": res_type_name,
                "parent": CREATE_RESOURCE_DEFAULTS["parent_template"].format(slot=slot),
                "bind_locations": {"x": 0.0, "y": 0.0, "z": 0.0},
                "slot_on_deck": slot,
            },
        )
        slot_to_create_resource[slot] = node_id
        if ot_lo == "tiprack":
            resource_last_writer[info["labware_id"]] = f"{node_id}:labware"
        if ot_lo == "trash":
            trash_create_node_id = node_id
        # create_resource 之间不需要 ready 连接

    # ==================== 第二步：为每个 reagent 创建 set_liquid_from_plate 节点 ====================
    # 创建 Group 节点，包含所有 set_liquid_from_plate 节点
    set_liquid_group_id = str(uuid.uuid4())
    G.add_node(
        set_liquid_group_id,
        name="SetLiquid Group",
        type="Group",
        parent_uuid="",
        lab_node_type="Device",
        template_name="",
        resource_name="",
        footer="",
        minimized=True,
        param=None,
    )

    # P2 v2 §14：预扫描 list-targets / str-targets 覆盖关系，
    # 第二步循环将跳过被 merged 节点完全接管的 target reagent_keys（避免冗余 per-plate 节点）。
    # 详见 product_designs/protocol_convert/02-cross-slot-merge.md §14。
    set_liquid_covered_by_merged, set_liquid_referenced_by_str = _collect_set_liquid_coverage(
        protocol_steps
    )

    set_liquid_index = 0
    # 收集所有 set_liquid_from_plate 节点（per-plate + 下方预创建的 merged），
    # 用于让第一个 transfer_liquid 通过 ready 等待全部初始化完成（见「初始化前置」说明）。
    all_set_liquid_node_ids: List[str] = []

    for labware_id, item in labware_info.items():
        # 跳过 Tip/Rack 类型
        if "Rack" in str(labware_id) or "Tip" in str(labware_id):
            continue
        if item.get("type") == "hardware":
            continue

        object_type = item.get("object", "") or ""

        # P2 v2 §14：被 merged 节点完全接管的 target reagent_key 跳过 per-plate 创建。
        # 仅当 object="target" ∧ key ∈ covered_by_merged ∧ key ∉ referenced_by_str 时才跳过；
        # 共用 key（被 list 与 str 双重引用）必须保留 per-plate，否则 str transfer 失去 output_wells 来源（R1 缓解）。
        if (
            object_type == "target"
            and labware_id in set_liquid_covered_by_merged
            and labware_id not in set_liquid_referenced_by_str
        ):
            continue

        slot = _map_deck_slot(
            str(item.get("slot", "")),
            object_type,
            target_device=target_device,
            target_model=target_model,
        )
        wells = item.get("well", [])
        if not wells or not slot:
            continue

        # res_id 不能有空格（液体名仍用协议中的 reagent key）
        res_id = str(labware_id).replace(" ", "_")
        well_count = len(wells)
        liquid_volume = DEFAULT_LIQUID_VOLUME if object_type == "source" else 0

        # P8（2026-05-24）：reagent block 显式 ``liquid_name`` 字段优先于 reagent_key，
        # 用于写入 PLR tracker / 前端显示的真实化学名（保留空格 / 中文 / 括号等，
        # **不** 经过 ``replace(" ", "_")``）。缺省时 fallback 到 ``res_id``（行为不变）。
        # 详见 ``product_designs/protocol_convert/08-liquid-name-from-reagent-block.md`` §3.4。
        liquid_name_value = str(item.get("liquid_name") or res_id)

        node_id = str(uuid.uuid4())
        set_liquid_index += 1
        target_class = item.get("target_class_name")
        if target_class:
            sl_node_title = f"{target_class}_slot_{slot}_{res_id}"
        else:
            sl_node_title = f"lab_{res_id.lower()}_slot_{slot}_{set_liquid_index}"

        # P3 框选化：新主路径 = param.wells（list[dict]，每孔一个资源引用），
        # 端口 target_port="wells_identifier"。
        # 旧字段（plate / well_names）仍写入 param 作 fallback，便于旧 runtime / 旧 schema 解析。
        #
        # well 引用 parent 必须用**物理板名**（``{target_class}_slot_{slot}``，与 create_resource
        # 的 res_id 对齐），而非逻辑 reagent_key（如 ``Liquid_2`` / ``samples_3``）。否则运行时
        # wells_identifier 边未覆盖 wells 时，set_liquid 收到的逻辑 parent 在 resource_tracker
        # 中根本不存在（只注册了 ``{class}_slot_{N}``），``_coerce_well`` 无法解析而报错。
        # 与 merged 路径（well_names 用 ``<plate_plr_name>/<well>`` prefix）保持一致。
        # 实证：源容器从未以 reagent_key 命名注册，resource_tracker 中只有 ``{class}_slot_{N}``。
        sl_plate_plr_name = (
            f"{target_class}_slot_{slot}".replace(" ", "_") if target_class else str(labware_id)
        )
        well_resource_refs = [
            {
                "id": f"{sl_plate_plr_name}/{w}",
                "name": f"{sl_plate_plr_name}/{w}",
                "parent": sl_plate_plr_name,
                "type": "well",
            }
            for w in wells
        ]

        G.add_node(
            node_id,
            template_name="set_liquid_from_plate",
            resource_name="liquid_handler.prcxi",
            name=sl_node_title,
            display_name=sl_node_title,
            description=f"Set liquid: {labware_id}",
            lab_node_type="Reagent",
            footer="set_liquid_from_plate-liquid_handler.prcxi",
            device_name=DEVICE_NAME_DEFAULT,
            type=NODE_TYPE_DEFAULT,
            parent_uuid=set_liquid_group_id,  # 指向 Group 节点
            minimized=True,  # 折叠显示
            param={
                # P3 新主路径：wells 框选化（list[well_resource_ref]）
                "wells": well_resource_refs,
                "liquid_names": [liquid_name_value] * well_count,
                "volumes": [liquid_volume] * well_count,
                # 兼容字段：保留 plate / well_names 以便旧 runtime / 旧前端继续工作；
                # 新 yaml schema 已将 required 改为 [liquid_names, volumes]
                "plate": [],
                "well_names": wells,
            },
        )
        all_set_liquid_node_ids.append(node_id)

        # set_liquid_from_plate 之间不需要 ready 连接

        # 物料流：create_resource 的 labware -> set_liquid_from_plate 的 wells_identifier
        # （P3 §3.4.3 简化方案：source_port 仍为 labware；目标端口换为 wells_identifier，
        # placeholder 内部把 labware.wells.@flatten 映射到 wells 字段）
        create_res_node_id = slot_to_create_resource.get(slot)
        if create_res_node_id:
            G.add_edge(
                create_res_node_id,
                node_id,
                source_port="labware",
                target_port="wells_identifier",
            )

        # set_liquid_from_plate 的输出 output_wells 用于连接 transfer_liquid
        resource_last_writer[labware_id] = f"{node_id}:output_wells"

    # 收集所有 create_resource 节点 ID，用于让第一个 transfer_liquid 等待所有资源创建完成
    all_create_resource_node_ids = list(slot_to_create_resource.values())

    # ============================================================
    # 初始化前置：预创建所有跨板 merged set_liquid_from_plate 节点
    # ------------------------------------------------------------
    # set_liquid_from_plate 的 runtime 语义是「绝对设定」（well.set_liquids 覆盖孔位 tracker
    # 的液体状态），而非累加。若像旧逻辑那样在主循环里按需创建 merged 节点，多 stage 协议中
    # 后一个 stage 的 merged 节点只通过数据边连到它对应的 transfer，可能被调度到前一个 stage
    # 的 transfer 之后才执行，从而把已经移入的液体重置归零，导致后续 stage 统计的总液体量
    # 丢失前序移液量。
    # 这里把全部 merged 节点提前创建好，并在下方让第一个 transfer_liquid 通过 ready 等待所有
    # set_liquid_from_plate（per-plate + merged）完成，确保「先全部初始化、再开始移液」。
    # ============================================================
    merged_set_liquid_counter = 0
    step_to_merged: Dict[int, Tuple[str, str]] = {}  # step 索引 -> (synthetic_key, merged_node_id)
    # P2 v3：sources 端同样支持 list[str] —— 转换器 N:N 合并产出的 list sources 需收敛成
    # synthetic str，否则图构建的单边 ``resource_name in dict`` 会 ``unhashable type: 'list'`` 崩溃。
    merged_source_counter = 0
    step_to_merged_src: Dict[int, Tuple[str, str]] = {}
    for step_idx, step in enumerate(protocol_steps):
        if step.get("template_name") != "transfer_liquid":
            continue
        raw_targets = (step.get("param") or {}).get("targets")
        if (
            isinstance(raw_targets, list)
            and len(raw_targets) > 0
            and all(isinstance(t, str) and t for t in raw_targets)
        ):
            synth_key, merged_node_id = _emit_merged_set_liquid(
                G,
                raw_targets,
                labware_info,
                slot_to_create_resource,
                set_liquid_group_id=set_liquid_group_id,
                merged_index=merged_set_liquid_counter,
                target_device=target_device,
                target_model=target_model,
            )
            merged_set_liquid_counter += 1
            step_to_merged[step_idx] = (synth_key, merged_node_id)
            all_set_liquid_node_ids.append(merged_node_id)
            resource_last_writer[synth_key] = f"{merged_node_id}:output_wells"

        raw_sources = (step.get("param") or {}).get("sources")
        if (
            isinstance(raw_sources, list)
            and len(raw_sources) > 0
            and all(isinstance(s, str) and s for s in raw_sources)
        ):
            synth_src_key, merged_src_node_id = _emit_merged_set_liquid(
                G,
                raw_sources,
                labware_info,
                slot_to_create_resource,
                set_liquid_group_id=set_liquid_group_id,
                merged_index=merged_source_counter,
                target_device=target_device,
                target_model=target_model,
                synthetic_prefix=MERGED_SOURCES_SYNTHETIC_PREFIX,
                well_volume=DEFAULT_LIQUID_VOLUME,
                kind_label="Sources",
            )
            merged_source_counter += 1
            step_to_merged_src[step_idx] = (synth_src_key, merged_src_node_id)
            all_set_liquid_node_ids.append(merged_src_node_id)
            resource_last_writer[synth_src_key] = f"{merged_src_node_id}:output_wells"

    # transfer_liquid 之间通过 ready 串联；第一个 transfer_liquid 需要等待所有 create_resource 完成
    last_control_node_id = trash_create_node_id
    is_first_action_node = True

    # 端口名称映射：JSON 字段名 -> 实际 handle key
    INPUT_PORT_MAPPING = {
        "sources": "sources_identifier",
        "targets": "targets_identifier",
        "vessel": "vessel",
        "to_vessel": "to_vessel",
        "from_vessel": "from_vessel",
        "reagent": "reagent",
        "solvent": "solvent",
        "compound": "compound",
        "tip_racks": "tip_rack_identifier",
    }

    OUTPUT_PORT_MAPPING = {
        "sources": "sources_out",  # 输出端口是 xxx_out
        "targets": "targets_out",  # 输出端口是 xxx_out
        "vessel": "vessel_out",
        "to_vessel": "to_vessel_out",
        "from_vessel": "from_vessel_out",
        "filtrate_vessel": "filtrate_out",
        "reagent": "reagent",
        "solvent": "solvent",
        "compound": "compound",
    }

    # 需要根据 wells 数量扩展的参数列表：
    # - 复数参数（asp_vols 等）支持单值自动扩展
    # - liquid_height 按 wells 扩展为数组
    # - mix_* 参数保持标量，避免被转换为 list
    EXPAND_BY_WELLS_PARAMS = [
        "asp_vols",
        "dis_vols",
        "asp_flow_rates",
        "dis_flow_rates",
        "liquid_height",
    ]

    # 处理协议步骤
    for step_idx, step in enumerate(protocol_steps):
        node_id = str(uuid.uuid4())
        params = step.get("param", {}).copy()  # 复制一份，避免修改原数据
        connected_params = set()  # 记录被连接的参数
        warnings = []  # 收集警告信息

        # 参数重命名：单数 -> 复数
        for old_name, new_name in PARAM_RENAME_MAPPING.items():
            if old_name in params:
                params[new_name] = params.pop(old_name)

        # touch_tip 输入归一化：
        # - 支持 bool / 0/1 / "true"/"false" / 单元素 list
        # - 最终统一为 bool 标量，避免被下游误当作序列处理
        if "touch_tip" in params:
            touch_tip_value = params.get("touch_tip")
            if isinstance(touch_tip_value, list):
                if len(touch_tip_value) == 1:
                    touch_tip_value = touch_tip_value[0]
                elif len(touch_tip_value) == 0:
                    touch_tip_value = False
                else:
                    warnings.append(f"touch_tip 期望标量，但收到长度为 {len(touch_tip_value)} 的列表，使用首个值")
                    touch_tip_value = touch_tip_value[0]
            if isinstance(touch_tip_value, str):
                norm = touch_tip_value.strip().lower()
                if norm in {"true", "1", "yes", "y", "on"}:
                    touch_tip_value = True
                elif norm in {"false", "0", "no", "n", "off", ""}:
                    touch_tip_value = False
                else:
                    warnings.append(f"touch_tip 字符串值无法识别: {touch_tip_value}，按 True 处理")
                    touch_tip_value = True
            elif isinstance(touch_tip_value, (int, float)):
                touch_tip_value = bool(touch_tip_value)
            elif touch_tip_value is None:
                touch_tip_value = False
            else:
                touch_tip_value = bool(touch_tip_value)
            params["touch_tip"] = touch_tip_value

        # delays 输入归一化：
        # - 支持标量（int/float/字符串数字）与 list
        # - 最终统一为数字列表，供下游按 delays[0]/delays[1] 使用
        if "delays" in params:
            delays_value = params.get("delays")
            if delays_value is None or delays_value == "":
                params["delays"] = []
            else:
                raw_list = delays_value if isinstance(delays_value, list) else [delays_value]
                normalized_delays = []
                for delay_item in raw_list:
                    if isinstance(delay_item, str):
                        delay_item = delay_item.strip()
                        if delay_item == "":
                            continue
                    try:
                        normalized_delays.append(float(delay_item))
                    except (TypeError, ValueError):
                        warnings.append(f"delays 包含无法转换为数字的值: {delay_item}，已忽略")
                params["delays"] = normalized_delays

        # use_channels 输入归一化（P1 多通道意图透传）：
        # - 与 LiquidHandler.transfer_liquid 的 use_channels: Optional[List[int]] 入参对齐
        # - None / 缺失 / 非 list 一律删除该 key，让 runtime 走自动选头默认逻辑
        # - 不参与 EXPAND_BY_WELLS_PARAMS：use_channels 是「这条 transfer 用哪些通道」的常量，
        #   长度由通道数决定（单通道 [0]/[1]、8 通道 [0..7]），与 targets 的 wells 数无关。
        if "use_channels" in params:
            uc_value = params["use_channels"]
            if uc_value is None:
                params.pop("use_channels")
            elif isinstance(uc_value, list):
                try:
                    params["use_channels"] = [int(x) for x in uc_value]
                except (TypeError, ValueError):
                    warnings.append(f"use_channels 列表中存在无法转换为 int 的值: {uc_value}，已忽略")
                    params.pop("use_channels")
            else:
                warnings.append(
                    f"use_channels 期望 list[int]，实际 {type(uc_value).__name__}，已忽略"
                )
                params.pop("use_channels")

        # ============================================================
        # P2 v2 跨板聚合：当 params.targets 是 list[str] 时，对应的 merged
        # set_liquid_from_plate 节点已在主循环前预创建（见「初始化前置」），其
        # resource_last_writer[synth_key] 也已注册。这里直接把 params.targets 改写为
        # synthetic str，让 INPUT_PORT_MAPPING 走 P3 既有的单边路径。
        # 详见 product_designs/protocol_convert/02-cross-slot-merge.md §9.2。
        # ============================================================
        if step_idx in step_to_merged:
            synth_key, _merged_node_id = step_to_merged[step_idx]
            params["targets"] = synth_key

        # P2 v3：list[str] sources 同样改写为 synthetic str（merged set_liquid 已预创建）。
        if step_idx in step_to_merged_src:
            synth_src_key, _merged_src_node_id = step_to_merged_src[step_idx]
            params["sources"] = synth_src_key

        # 处理输入连接
        for param_key, target_port in INPUT_PORT_MAPPING.items():
            resource_name = params.get(param_key)
            if resource_name and resource_name in resource_last_writer:
                source_node, source_port = resource_last_writer[resource_name].split(":")
                G.add_edge(source_node, node_id, source_port=source_port, target_port=target_port)
                connected_params.add(param_key)
            elif resource_name and resource_name not in resource_last_writer:
                # 资源名在 labware_info 中不存在
                warnings.append(f"{param_key}={resource_name} 未找到")

        # 获取 targets 对应的 wells 数量，用于扩展参数
        targets_name = params.get("targets")
        sources_name = params.get("sources")
        targets_wells_count = 1
        sources_wells_count = 1

        # P2 v2：synthetic merged targets key（_merged_targets_<idx>）不在 labware_info 中，
        # wells 数量从 dis_vols 长度推断，且不打「未在 reagent 中定义」warning。
        targets_is_synthetic = (
            isinstance(targets_name, str)
            and targets_name.startswith(MERGED_TARGETS_SYNTHETIC_PREFIX)
        )
        # P2 v3：synthetic merged sources key（_merged_sources_<idx>）同样不在 labware_info 中，
        # wells 数量从 asp_vols 长度推断，且不打「未在 reagent 中定义」warning。
        sources_is_synthetic = (
            isinstance(sources_name, str)
            and sources_name.startswith(MERGED_SOURCES_SYNTHETIC_PREFIX)
        )

        if targets_name and targets_name in labware_info:
            target_wells = labware_info[targets_name].get("well", [])
            targets_wells_count = len(target_wells) if target_wells else 1
        elif targets_is_synthetic:
            # merged set_liquid 的 wells 长度 == dis_vols 长度（顺序权威由 Stage 3 构造期固化）
            dis_vols_val = params.get("dis_vols")
            if isinstance(dis_vols_val, list) and dis_vols_val:
                targets_wells_count = len(dis_vols_val)
        elif targets_name:
            warnings.append(f"targets={targets_name} 未在 reagent 中定义")

        if sources_name and sources_name in labware_info:
            source_wells = labware_info[sources_name].get("well", [])
            sources_wells_count = len(source_wells) if source_wells else 1
        elif sources_is_synthetic:
            asp_vols_val = params.get("asp_vols")
            if isinstance(asp_vols_val, list) and asp_vols_val:
                sources_wells_count = len(asp_vols_val)
        elif sources_name:
            warnings.append(f"sources={sources_name} 未在 reagent 中定义")

        # 检查 sources 和 targets 的 wells 数量是否匹配（v2 跨板：1:N 是合法的，跳过 warning）
        if (
            targets_wells_count != sources_wells_count
            and targets_name
            and sources_name
            and not targets_is_synthetic
            and not sources_is_synthetic
            and sources_wells_count not in (0, 1)
        ):
            warnings.append(f"wells 数量不匹配: sources={sources_wells_count}, targets={targets_wells_count}")

        # 使用 targets 的 wells 数量来扩展参数
        wells_count = targets_wells_count

        # P1 多通道：use_channels 存在且 len > 1（multi 协议）时，
        # asp_vols / dis_vols 等数组的长度已是 8 × M（Stage 2 复制完毕），
        # 与 reagent.well 长度（plate=8 / reservoir=1）不一定相等——跳过 wells 长度对齐警告，
        # 让长度由 use_channels × 列锚条目决定。
        is_multi_channel = (
            isinstance(params.get("use_channels"), list)
            and len(params.get("use_channels", [])) > 1
        )

        # 扩展单值参数为数组（根据 targets 的 wells 数量）
        for expand_param in EXPAND_BY_WELLS_PARAMS:
            if expand_param in params:
                value = params[expand_param]
                # 如果是单个值，扩展为数组
                if not isinstance(value, list):
                    params[expand_param] = [value] * wells_count
                # 如果已经是数组但长度不对，记录警告（multi 通道场景下跳过）
                elif len(value) != wells_count and not is_multi_channel:
                    warnings.append(f"{expand_param} 数量({len(value)})与 wells({wells_count})不匹配")

        # 如果 sources/targets 已通过连接传递，将参数值改为空数组
        for param_key in connected_params:
            if param_key in params:
                params[param_key] = []

        # 更新 step 的 param、footer、device_name 和 type
        step_copy = step.copy()
        step_copy["param"] = params
        step_copy["device_name"] = DEVICE_NAME_DEFAULT  # 动作节点使用默认设备名
        step_copy["type"] = NODE_TYPE_DEFAULT  # 节点类型

        # 如果有警告，修改 footer 添加警告标记（警告放前面）
        if warnings:
            original_footer = step.get("footer", "")
            step_copy["footer"] = f"[WARN: {'; '.join(warnings)}] {original_footer}"

        G.add_node(node_id, **step_copy)

        # 控制流
        if is_first_action_node:
            # 第一个 transfer_liquid 需要等待所有 create_resource 完成
            for cr_node_id in all_create_resource_node_ids:
                G.add_edge(cr_node_id, node_id, source_port="ready", target_port="ready")
            # 同时等待所有 set_liquid_from_plate（per-plate + merged）完成初始化，
            # 保证「先全部初始化、再开始移液」：set_liquid_from_plate 是绝对覆盖语义，
            # 若晚于某次移液执行会把已移入的液体重置归零。
            for sl_node_id in all_set_liquid_node_ids:
                G.add_edge(sl_node_id, node_id, source_port="ready", target_port="ready")
            is_first_action_node = False
        elif last_control_node_id is not None:
            G.add_edge(last_control_node_id, node_id, source_port="ready", target_port="ready")
        last_control_node_id = node_id

        # 处理输出：更新 resource_last_writer
        # P2 v2：``step.param[param_key]`` 可能是 list[str]（跨板 reagent_keys），
        # 此时为每个 reagent_key 注册 transfer_liquid 的下游 writer，保留多 reagent
        # 链式 transfer 的能力。
        for param_key, output_port in OUTPUT_PORT_MAPPING.items():
            raw_value = step.get("param", {}).get(param_key)  # 使用原始参数值
            if isinstance(raw_value, list):
                for name in raw_value:
                    if isinstance(name, str) and name:
                        resource_last_writer[name] = f"{node_id}:{output_port}"
            elif raw_value:
                resource_last_writer[raw_value] = f"{node_id}:{output_port}"

    return G


def draw_protocol_graph(protocol_graph: WorkflowGraph, output_path: str):
    """
    (辅助功能) 使用 networkx 和 matplotlib 绘制协议工作流图，用于可视化。
    """
    if not protocol_graph:
        print("Cannot draw graph: Graph object is empty.")
        return

    G = nx.DiGraph()

    for node_id, attrs in protocol_graph.nodes.items():
        label = attrs.get("description", attrs.get("template_name", node_id[:8]))
        G.add_node(node_id, label=label, **attrs)

    for edge in protocol_graph.edges:
        G.add_edge(edge["source"], edge["target"])

    plt.figure(figsize=(20, 15))
    try:
        pos = nx.nx_agraph.graphviz_layout(G, prog="dot")
    except Exception:
        pos = nx.shell_layout(G)  # Fallback layout

    node_labels = {node: data["label"] for node, data in G.nodes(data=True)}
    nx.draw(
        G,
        pos,
        with_labels=False,
        node_size=2500,
        node_color="skyblue",
        node_shape="o",
        edge_color="gray",
        width=1.5,
        arrowsize=15,
    )
    nx.draw_networkx_labels(G, pos, labels=node_labels, font_size=8, font_weight="bold")

    plt.title("Chemical Protocol Workflow Graph", size=15)
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close()
    print(f"  - Visualization saved to '{output_path}'")


COMPASS = {"n", "e", "s", "w", "ne", "nw", "se", "sw", "c"}


def _is_compass(port: str) -> bool:
    return isinstance(port, str) and port.lower() in COMPASS


def draw_protocol_graph_with_ports(protocol_graph, output_path: str, rankdir: str = "LR"):
    """
    使用 Graphviz 端口语法绘制协议工作流图。
    - 若边上的 source_port/target_port 是 compass（n/e/s/w/...），直接用 compass。
    - 否则自动为节点创建 record 形状并定义命名端口 <portname>。
    最终由 PyGraphviz 渲染并输出到 output_path（后缀决定格式，如 .png/.svg/.pdf）。
    """
    if not protocol_graph:
        print("Cannot draw graph: Graph object is empty.")
        return

    # 1) 先用 networkx 搭建有向图，保留端口属性
    G = nx.DiGraph()
    for node_id, attrs in protocol_graph.nodes.items():
        label = attrs.get("description", attrs.get("template_name", node_id[:8]))
        # 保留一个干净的“中心标签”，用于放在 record 的中间槽
        G.add_node(node_id, _core_label=str(label), **{k: v for k, v in attrs.items() if k not in ("label",)})

    edges_data = []
    in_ports_by_node = {}  # 收集命名输入端口
    out_ports_by_node = {}  # 收集命名输出端口

    for edge in protocol_graph.edges:
        u = edge["source"]
        v = edge["target"]
        sp = edge.get("source_handle_key") or edge.get("source_port")
        tp = edge.get("target_handle_key") or edge.get("target_port")

        # 记录到图里（保留原始端口信息）
        G.add_edge(u, v, source_handle_key=sp, target_handle_key=tp)
        edges_data.append((u, v, sp, tp))

        # 如果不是 compass，就按“命名端口”先归类，等会儿给节点造 record
        if sp and not _is_compass(sp):
            out_ports_by_node.setdefault(u, set()).add(str(sp))
        if tp and not _is_compass(tp):
            in_ports_by_node.setdefault(v, set()).add(str(tp))

    # 2) 转为 AGraph，使用 Graphviz 渲染
    A = to_agraph(G)
    A.graph_attr.update(rankdir=rankdir, splines="true", concentrate="false", fontsize="10")
    A.node_attr.update(
        shape="box", style="rounded,filled", fillcolor="lightyellow", color="#999999", fontname="Helvetica"
    )
    A.edge_attr.update(arrowsize="0.8", color="#666666")

    # 3) 为需要命名端口的节点设置 record 形状与 label
    #    左列 = 输入端口；中间 = 核心标签；右列 = 输出端口
    for n in A.nodes():
        node = A.get_node(n)
        core = G.nodes[n].get("_core_label", n)

        in_ports = sorted(in_ports_by_node.get(n, []))
        out_ports = sorted(out_ports_by_node.get(n, []))

        # 如果该节点涉及命名端口，则用 record；否则保留原 box
        if in_ports or out_ports:

            def port_fields(ports):
                if not ports:
                    return " "  # 必须留一个空槽占位
                # 每个端口一个小格子，<p> name
                return "|".join(f"<{re.sub(r'[^A-Za-z0-9_:.|-]', '_', p)}> {p}" for p in ports)

            left = port_fields(in_ports)
            right = port_fields(out_ports)

            # 三栏：左(入) | 中(节点名) | 右(出)
            record_label = f"{{ {left} | {core} | {right} }}"
            node.attr.update(shape="record", label=record_label)
        else:
            # 没有命名端口：普通盒子，显示核心标签
            node.attr.update(label=str(core))

    # 4) 给边设置 headport / tailport
    #    - 若端口为 compass：直接用 compass（e.g., headport="e"）
    #    - 若端口为命名端口：使用在 record 中定义的 <port> 名（同名即可）
    for u, v, sp, tp in edges_data:
        e = A.get_edge(u, v)

        # Graphviz 属性：tail 是源，head 是目标
        if sp:
            if _is_compass(sp):
                e.attr["tailport"] = sp.lower()
            else:
                # 与 record label 中 <port> 名一致；特殊字符已在 label 中做了清洗
                e.attr["tailport"] = re.sub(r"[^A-Za-z0-9_:.|-]", "_", str(sp))

        if tp:
            if _is_compass(tp):
                e.attr["headport"] = tp.lower()
            else:
                e.attr["headport"] = re.sub(r"[^A-Za-z0-9_:.|-]", "_", str(tp))

        # 可选：若想让边更贴边缘，可设置 constraint/spline 等
        # e.attr["arrowhead"] = "vee"

    # 5) 输出
    A.draw(output_path, prog="dot")
    print(f"  - Port-aware workflow rendered to '{output_path}'")


# ---------------- Registry Adapter ----------------


class RegistryAdapter:
    """根据 module 的类名（冒号右侧）反查 registry 的 resource_name（原 device_class），并抽取参数顺序"""

    def __init__(self, device_registry: Dict[str, Any]):
        self.device_registry = device_registry or {}
        self.module_class_to_resource = self._build_module_class_index()

    def _build_module_class_index(self) -> Dict[str, str]:
        idx = {}
        for resource_name, info in self.device_registry.items():
            module = info.get("module")
            if isinstance(module, str) and ":" in module:
                cls = module.split(":")[-1]
                idx[cls] = resource_name
                idx[cls.lower()] = resource_name
        return idx

    def resolve_resource_by_classname(self, class_name: str) -> Optional[str]:
        if not class_name:
            return None
        return self.module_class_to_resource.get(class_name) or self.module_class_to_resource.get(class_name.lower())

    def get_device_module(self, resource_name: Optional[str]) -> Optional[str]:
        if not resource_name:
            return None
        return self.device_registry.get(resource_name, {}).get("module")

    def get_actions(self, resource_name: Optional[str]) -> Dict[str, Any]:
        if not resource_name:
            return {}
        return (self.device_registry.get(resource_name, {}).get("class", {}).get("action_value_mappings", {})) or {}

    def get_action_schema(self, resource_name: Optional[str], template_name: str) -> Optional[Json]:
        return (self.get_actions(resource_name).get(template_name) or {}).get("schema")

    def get_action_goal_default(self, resource_name: Optional[str], template_name: str) -> Json:
        return (self.get_actions(resource_name).get(template_name) or {}).get("goal_default", {}) or {}

    def get_action_input_keys(self, resource_name: Optional[str], template_name: str) -> List[str]:
        schema = self.get_action_schema(resource_name, template_name) or {}
        goal = (schema.get("properties") or {}).get("goal") or {}
        props = goal.get("properties") or {}
        required = goal.get("required") or []
        return list(dict.fromkeys(required + list(props.keys())))
