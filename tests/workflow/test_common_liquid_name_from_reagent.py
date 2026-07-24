"""P8 — Stage 3 (``workflow/common.py``) 写入 ``set_liquid_from_plate.param.liquid_names`` 时
优先取 ``reagent[key].liquid_name``，缺省时 fallback 到 reagent_key。

对应 ``product_designs/protocol_convert/08-liquid-name-from-reagent-block.md`` §3.4 + §5。

设计要点
--------
- ``reagent[key].liquid_name`` 是 P8 新增的**可选**字段，承载真实化学名（与 reagent_key
  解耦：reagent_key 仍是数据流引用名 / 业务别名，``liquid_name`` 是写入 PLR tracker /
  前端的 human-readable 名称）。
- ``liquid_name`` 来源优先级：Stage 0 mock ``Well.load_liquid(liquid=...)`` 实参 >
  README 语义词 > 不写（Stage 3 fallback 到 reagent_key）。
- ``liquid_name`` 保留空格 / 中文 / 括号等原字符，**不**做 snake_case / underscore 替换。
- 旧 JSON（无 ``liquid_name`` 字段）行为完全不变（设计点 §7.A）。

测试用例
--------
- ``test_per_plate_fallback_when_no_liquid_name`` —— 缺省 fallback：
  reagent 块无 ``liquid_name`` → liquid_names[i] == reagent_key（与 P8 前一致）。
- ``test_per_plate_uses_explicit_liquid_name`` —— 显式 liquid_name：
  liquid_names[i] == "EDTA Plasma"。
- ``test_per_plate_preserves_spaces_and_special_chars`` —— 含空格 / 括号：
  liquid_names[i] 不被 ``replace(" ", "_")`` 处理（不同于 reagent_key 用的 res_id）。
- ``test_merged_node_uses_explicit_liquid_name_per_dispense`` —— merged 节点
  每个 dispense 独立取 ``liquid_name or key``，部分有部分无能共存。
- ``test_liquid_name_independent_of_reagent_key_normalization`` —— 与 P4 共存：
  reagent_key 仍是 ``samples_2`` 等去重后缀，但 liquid_names 写的是真实化学名。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Dict, List


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _install_fake_optional_deps() -> None:
    """与 test_common_set_liquid_dedup.py 一致的可选依赖 stub。"""
    if "matplotlib" not in sys.modules:
        sys.modules["matplotlib"] = types.ModuleType("matplotlib")
    if "matplotlib.pyplot" not in sys.modules:
        sys.modules["matplotlib.pyplot"] = types.ModuleType("matplotlib.pyplot")
    try:
        from networkx.drawing import nx_agraph  # noqa: F401
    except Exception:
        nx_drawing = types.ModuleType("networkx.drawing")
        nx_agraph_mod = types.ModuleType("networkx.drawing.nx_agraph")
        nx_agraph_mod.to_agraph = lambda _g: None  # type: ignore[attr-defined]
        nx_drawing.nx_agraph = nx_agraph_mod  # type: ignore[attr-defined]
        sys.modules["networkx.drawing"] = nx_drawing
        sys.modules["networkx.drawing.nx_agraph"] = nx_agraph_mod


_install_fake_optional_deps()

import pytest  # noqa: E402

from unilabos.workflow.common import build_protocol_graph  # noqa: E402


# ==================== 辅助 ====================


def _set_liquid_nodes(graph) -> List[Dict[str, Any]]:
    return [
        {"id": nid, **node}
        for nid, node in graph.nodes.items()
        if node.get("template_name") == "set_liquid_from_plate"
    ]


def _per_plate_for(graph, reagent_key: str) -> Dict[str, Any]:
    """根据 ``description = "Set liquid: <reagent_key>"`` 反查 per-plate 节点。"""
    for n in _set_liquid_nodes(graph):
        if n.get("description") == f"Set liquid: {reagent_key}":
            return n
    raise AssertionError(f"未找到 per-plate set_liquid_from_plate(reagent_key={reagent_key!r})")


def _merged_nodes(graph) -> List[Dict[str, Any]]:
    return [
        n for n in _set_liquid_nodes(graph)
        if str(n.get("name", "")).startswith("_merged_targets_")
    ]


def _make_source_target_labware(
    *,
    source_key: str = "src_1",
    source_liquid_name: str | None = None,
    target_keys: List[str] | None = None,
    target_liquid_names: Dict[str, str] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """构造 1 个 source + N 个 target reagent + 1 个 tip rack。

    ``*_liquid_name`` 为 None / 缺省时**不**写入 ``liquid_name`` 字段，
    模拟旧 schema / mock 未给 liquid_name 的真实回归场景。
    """
    info: Dict[str, Dict[str, Any]] = {}
    source_entry: Dict[str, Any] = {
        "slot": 1,
        "well": ["A1"],
        "labware": "nest_12_reservoir_15ml",
        "object": "source",
    }
    if source_liquid_name is not None:
        source_entry["liquid_name"] = source_liquid_name
    info[source_key] = source_entry

    target_keys = target_keys or ["t_A"]
    target_liquid_names = target_liquid_names or {}
    for i, tk in enumerate(target_keys, start=1):
        entry: Dict[str, Any] = {
            "slot": 2 + i,
            "well": ["A1"],
            "labware": "nest_96_wellplate_2ml_deep",
            "object": "target",
        }
        if tk in target_liquid_names:
            entry["liquid_name"] = target_liquid_names[tk]
        info[tk] = entry

    info["tiprack_12"] = {
        "slot": 12,
        "well": [],
        "labware": "opentrons_96_tiprack_300ul",
        "object": "tiprack",
    }
    return info


# ==================== T1 缺省 fallback ====================


def test_per_plate_fallback_when_no_liquid_name():
    """reagent block 无 ``liquid_name`` 字段 → liquid_names[i] == reagent_key（P8 前行为）。"""
    labware = _make_source_target_labware(
        source_key="src_1",
        target_keys=["t_A"],
        # 都不给 liquid_name
    )
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": "t_A",
                "tip_racks": "tiprack_12",
                "asp_vols": [10.0],
                "dis_vols": [10.0],
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    src_node = _per_plate_for(g, "src_1")
    tgt_node = _per_plate_for(g, "t_A")
    assert src_node["param"]["liquid_names"] == ["src_1"], (
        f"无 liquid_name 时 source per-plate 应 fallback 到 reagent_key；"
        f" 实际 {src_node['param']['liquid_names']}"
    )
    assert tgt_node["param"]["liquid_names"] == ["t_A"], (
        f"无 liquid_name 时 target per-plate 应 fallback 到 reagent_key；"
        f" 实际 {tgt_node['param']['liquid_names']}"
    )


# ==================== T2 显式 liquid_name ====================


def test_per_plate_uses_explicit_liquid_name():
    """reagent block 含 ``liquid_name`` → liquid_names[i] 用该值（不是 reagent_key）。"""
    labware = _make_source_target_labware(
        source_key="src_1",
        source_liquid_name="EDTA Plasma",
        target_keys=["t_A"],
        target_liquid_names={"t_A": "PBS Diluent"},
    )
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": "t_A",
                "tip_racks": "tiprack_12",
                "asp_vols": [10.0],
                "dis_vols": [10.0],
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    src_node = _per_plate_for(g, "src_1")
    tgt_node = _per_plate_for(g, "t_A")
    assert src_node["param"]["liquid_names"] == ["EDTA Plasma"], (
        f"source per-plate 应使用 reagent.liquid_name；实际 {src_node['param']['liquid_names']}"
    )
    assert tgt_node["param"]["liquid_names"] == ["PBS Diluent"], (
        f"target per-plate 应使用 reagent.liquid_name；实际 {tgt_node['param']['liquid_names']}"
    )


# ==================== T3 空格 / 括号 ====================


def test_per_plate_preserves_spaces_and_special_chars():
    """``liquid_name`` 保留空格 / 括号 / 中文等原字符，不被 replace(' ', '_') 处理。

    这条与 reagent_key 走 ``res_id = str(labware_id).replace(' ', '_')`` 的语义不同。
    """
    labware = _make_source_target_labware(
        source_key="src_1",
        source_liquid_name="Tris HCl pH 8.0 (1×)",
        target_keys=["t_A"],
        target_liquid_names={"t_A": "稀释液 A"},
    )
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": "t_A",
                "tip_racks": "tiprack_12",
                "asp_vols": [10.0],
                "dis_vols": [10.0],
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    src_node = _per_plate_for(g, "src_1")
    tgt_node = _per_plate_for(g, "t_A")

    assert src_node["param"]["liquid_names"] == ["Tris HCl pH 8.0 (1×)"], (
        f"空格 / 括号应原样保留；实际 {src_node['param']['liquid_names']}"
    )
    assert tgt_node["param"]["liquid_names"] == ["稀释液 A"], (
        f"中文应原样保留；实际 {tgt_node['param']['liquid_names']}"
    )

    # reagent_key 自身仍受 ``res_id = replace(' ', '_')`` 影响，
    # 但本测试 reagent_key 不含空格，故 sl_node_title 仍以 reagent_key 为根。
    # 这里仅断言 liquid_names 字段独立于 reagent_key normalize。


# ==================== T4 merged 节点跨板部分有部分无 ====================


def test_merged_node_uses_explicit_liquid_name_per_dispense():
    """merged 节点 ``liquid_names`` 与 list-targets 同长，每个元素独立取
    ``reagent[key].liquid_name or key``：本例 3 个 target，2 个有显式名、1 个无。
    """
    labware = _make_source_target_labware(
        source_key="src_1",
        target_keys=["t_A", "t_B", "t_C"],
        target_liquid_names={
            "t_A": "Plasma",
            # t_B 无 liquid_name
            "t_C": "Buffer X",
        },
    )
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": ["t_A", "t_B", "t_C"],
                "tip_racks": "tiprack_12",
                "asp_vols": [5.0] * 3,
                "dis_vols": [5.0] * 3,
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    merged = _merged_nodes(g)
    assert len(merged) == 1, f"应有 1 个 merged 节点，实际 {len(merged)}"
    liquid_names = merged[0]["param"]["liquid_names"]
    assert liquid_names == ["Plasma", "t_B", "Buffer X"], (
        f"merged 每 dispense 独立取 liquid_name or key；实际 {liquid_names}"
    )


# ==================== T5 与 P4 reagent_key 后缀共存 ====================


def test_liquid_name_independent_of_reagent_key_normalization():
    """P4 命名链产生 ``samples_2`` 这种带后缀的 reagent_key（跨板去重）；
    P8 ``liquid_name`` 应保持原始化学名，**不**带 P4 的去重后缀。

    构造：2 个 target reagent_keys ``samples`` / ``samples_2``（不同 slot，
    模拟跨板同液体被 Stage 2 去重），都标 liquid_name="Bacterial Culture"。
    """
    labware = _make_source_target_labware(
        source_key="src_1",
        target_keys=["samples", "samples_2"],
        target_liquid_names={
            "samples": "Bacterial Culture",
            "samples_2": "Bacterial Culture",
        },
    )
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": ["samples", "samples_2"],
                "tip_racks": "tiprack_12",
                "asp_vols": [5.0, 5.0],
                "dis_vols": [5.0, 5.0],
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    merged = _merged_nodes(g)
    assert len(merged) == 1
    liquid_names = merged[0]["param"]["liquid_names"]
    assert liquid_names == ["Bacterial Culture", "Bacterial Culture"], (
        f"P8 liquid_name 应与 P4 reagent_key 后缀解耦：同液体的两个 reagent_key 应得相同"
        f" liquid_name；实际 {liquid_names}"
    )
    # 同时 reagent_key 仍是 samples / samples_2（不变）
    wells = merged[0]["param"]["wells"]
    parents = [w["parent"] for w in wells]
    assert parents == ["samples", "samples_2"], (
        f"merged wells.parent 应等于 list-targets reagent_keys；实际 {parents}"
    )


# ==================== T6 source per-plate / target per-plate 同步生效 ====================


def test_both_source_and_target_per_plate_use_liquid_name():
    """str-targets 路径（无 merged）下，source 和 target 都走 per-plate emit，
    各自独立取 ``liquid_name``。"""
    labware = _make_source_target_labware(
        source_key="src_1",
        source_liquid_name="Reagent A",
        target_keys=["t_A"],
        target_liquid_names={"t_A": "Reagent B"},
    )
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": "t_A",  # str-targets，不触发 merged
                "tip_racks": "tiprack_12",
                "asp_vols": [10.0],
                "dis_vols": [10.0],
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    assert _merged_nodes(g) == [], "str-targets 不应产生 merged 节点"
    src_node = _per_plate_for(g, "src_1")
    tgt_node = _per_plate_for(g, "t_A")
    assert src_node["param"]["liquid_names"] == ["Reagent A"]
    assert tgt_node["param"]["liquid_names"] == ["Reagent B"]


# ==================== T7 多孔同 reagent → 整列 liquid_names 一致 ====================


def test_multi_well_reagent_replicates_liquid_name():
    """1 个 reagent 含 8 wells（multi-channel 扩展场景）→ liquid_names 应是
    ``[liquid_name] * 8``，与 wells 长度一致。"""
    labware: Dict[str, Dict[str, Any]] = {
        "src_1": {
            "slot": 1,
            "well": ["A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"],
            "labware": "nest_96_wellplate_100ul_pcr_full_skirt",
            "object": "source",
            "liquid_name": "Mastermix",
        },
        "t_A": {
            "slot": 3,
            "well": ["A1"],
            "labware": "nest_96_wellplate_2ml_deep",
            "object": "target",
        },
        "tiprack_12": {
            "slot": 12,
            "well": [],
            "labware": "opentrons_96_tiprack_300ul",
            "object": "tiprack",
        },
    }
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": "t_A",
                "tip_racks": "tiprack_12",
                "asp_vols": [10.0],
                "dis_vols": [10.0],
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    src_node = _per_plate_for(g, "src_1")
    liquid_names = src_node["param"]["liquid_names"]
    assert liquid_names == ["Mastermix"] * 8, (
        f"per-plate 应把 liquid_name 复制 well_count 份；实际 {liquid_names}"
    )
    # 同时 wells / volumes 长度一致
    assert len(src_node["param"]["wells"]) == 8
    assert len(src_node["param"]["volumes"]) == 8
