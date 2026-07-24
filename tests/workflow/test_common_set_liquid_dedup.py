"""P2 v2 §14 set_liquid_from_plate 去重 —— Stage 3 (`workflow/common.py`) 集成测试。

对应 ``product_designs/protocol_convert/02-cross-slot-merge.md`` §14（2026-05-22 plan）。

§14 设计要点
-----------------
当 ``transfer_liquid.params.targets`` 是 ``list[str]`` 时，``_emit_merged_set_liquid``
已经为该 transfer 插入一个 merged ``set_liquid_from_plate`` 节点，
其 ``param.wells`` 聚合了 list 中所有 reagent_keys 的跨板 wells。

§14 之前：第二步循环（``for labware_id, item in labware_info.items()``）仍然为
list-targets 中出现的每个 reagent_key 创建一个 per-plate ``set_liquid_from_plate`` 节点，
导致**节点冗余**（per-plate 节点的 ``output_wells`` 对 transfer_liquid 的
``targets_identifier`` 边毫无贡献 —— transfer_liquid 单边只接 merged 节点）。

§14 改造：在第二步循环**之前**预扫描 protocol_steps，收集
``set_liquid_covered_by_merged: Set[str]``（出现在某个 list[str] targets 中的所有 keys）
与 ``set_liquid_referenced_by_str: Set[str]``（出现在 str targets 中的所有 keys）。
循环内对 ``object="target"`` 且 ``key ∈ covered ∧ key ∉ referenced_by_str`` 的 reagent_key
**跳过** per-plate 节点创建。

测试用例
----
- ``test_per_plate_skipped_when_covered_by_merged`` —— list-targets 覆盖的
  target reagent_keys 不再产生 per-plate set_liquid_from_plate。
- ``test_per_plate_kept_when_also_referenced_by_str_targets`` —— R1 缓解：
  同时被 list-targets 和 str-targets 引用的 reagent_key 仍保留 per-plate。
- ``test_str_targets_protocol_unaffected`` —— 单 slot 协议（仅 str-targets）
  节点数完全不变（回归防护）。
- ``test_51b9a5_style_node_count`` —— 12 list-targets × len=9 大规模场景：
  set_liquid_from_plate 总节点数 = source per-plate + merged + 0 target per-plate。
- ``test_source_per_plate_always_kept`` —— source 端不受 §14 影响：source
  reagent_keys 不出现在 targets 字段中，per-plate 节点恒在。
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
    """与 test_common_cross_slot_v2.py 一致的可选依赖 stub。"""
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


def _nodes_by_template(graph, template_name: str) -> List[Dict[str, Any]]:
    return [
        {"id": nid, **node}
        for nid, node in graph.nodes.items()
        if node.get("template_name") == template_name
    ]


def _set_liquid_nodes_split(graph):
    """返回 (per_plate_nodes, merged_nodes)。merged 节点 name 以 `_merged_targets_` 开头。"""
    all_sl = _nodes_by_template(graph, "set_liquid_from_plate")
    merged = [n for n in all_sl if str(n.get("name", "")).startswith("_merged_targets_")]
    per_plate = [n for n in all_sl if not str(n.get("name", "")).startswith("_merged_targets_")]
    return per_plate, merged


def _labware_with_targets(target_keys: List[str], source_keys: List[str] | None = None) -> Dict[str, Dict[str, Any]]:
    """构造 labware_info：source 端 1 个 + 任意数量 target plates + tip rack。"""
    info: Dict[str, Dict[str, Any]] = {}
    source_keys = source_keys or ["src_1"]
    for i, sk in enumerate(source_keys, start=1):
        info[sk] = {
            "slot": 1 + i - 1,  # slot 1 占位（实际可能映射）
            "well": ["A1"],
            "labware": "nest_12_reservoir_15ml",
            "object": "source",
        }
    for i, tk in enumerate(target_keys, start=1):
        info[tk] = {
            "slot": 2 + i,  # 错开 source 使用的 slot
            "well": ["A1"],
            "labware": "nest_96_wellplate_2ml_deep",
            "object": "target",
        }
    info["tiprack_12"] = {
        "slot": 12,
        "well": [],
        "labware": "opentrons_96_tiprack_300ul",
        "object": "tiprack",
    }
    return info


# ==================== 用例 ====================


def test_per_plate_skipped_when_covered_by_merged():
    """单 list-targets transfer 覆盖 4 个 target reagent_keys → per-plate 不再出现。"""
    targets = ["t_A", "t_B", "t_C", "t_D"]
    labware = _labware_with_targets(targets, source_keys=["src_1"])
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": targets,
                "tip_racks": "tiprack_12",
                "asp_vols": [8.0] * 4,
                "dis_vols": [8.0] * 4,
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    per_plate, merged = _set_liquid_nodes_split(g)

    # merged 节点：1 个
    assert len(merged) == 1, f"应有 1 个 merged 节点；实际 {len(merged)}"

    # per-plate 节点：仅 source 1 个（src_1）；target 端被全部跳过
    per_plate_names = {n.get("description", "") for n in per_plate}
    per_plate_keys = {
        n.get("description", "").replace("Set liquid: ", "")
        for n in per_plate
    }
    assert "src_1" in per_plate_keys, "source 端 per-plate 必须保留"
    for tk in targets:
        assert tk not in per_plate_keys, (
            f"§14：target reagent_key '{tk}' 已被 merged 覆盖，不应再有 per-plate 节点；"
            f" 实际 per_plate_keys={per_plate_keys}"
        )


def test_per_plate_kept_when_also_referenced_by_str_targets():
    """R1 缓解：t_A 既被 list-targets 引用，又被 str-targets 引用 → per-plate 必须保留。"""
    targets_list = ["t_A", "t_B", "t_C"]
    labware = _labware_with_targets(targets_list, source_keys=["src_1"])
    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": targets_list,
                "tip_racks": "tiprack_12",
                "asp_vols": [5.0] * 3,
                "dis_vols": [5.0] * 3,
            },
            "step_number": 1,
        },
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": "t_A",
                "tip_racks": "tiprack_12",
                "asp_vols": [10.0],
                "dis_vols": [10.0],
            },
            "step_number": 2,
        },
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    per_plate, merged = _set_liquid_nodes_split(g)
    per_plate_keys = {
        n.get("description", "").replace("Set liquid: ", "")
        for n in per_plate
    }

    assert "t_A" in per_plate_keys, (
        f"R1：t_A 被 str transfer #2 引用，必须保留 per-plate 节点；"
        f" 实际 per_plate_keys={per_plate_keys}"
    )
    assert "t_B" not in per_plate_keys, "t_B 仅出现在 list-targets，应跳过"
    assert "t_C" not in per_plate_keys, "t_C 仅出现在 list-targets，应跳过"

    # merged 节点数：1（仅 list-targets transfer #1 生成）
    assert len(merged) == 1


def test_str_targets_protocol_unaffected():
    """单 slot 协议（全 str-targets）→ 每个 target reagent_key 仍有 per-plate（零回归）。"""
    labware = _labware_with_targets(["t_A", "t_B"], source_keys=["src_1"])
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
        },
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_1",
                "targets": "t_B",
                "tip_racks": "tiprack_12",
                "asp_vols": [20.0],
                "dis_vols": [20.0],
            },
            "step_number": 2,
        },
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    per_plate, merged = _set_liquid_nodes_split(g)
    per_plate_keys = {
        n.get("description", "").replace("Set liquid: ", "")
        for n in per_plate
    }

    assert merged == [], "全 str-targets 协议不应触发 merged 节点"
    assert {"src_1", "t_A", "t_B"}.issubset(per_plate_keys), (
        f"单 slot 协议每个 reagent_key（含 source/target）都应保留 per-plate；"
        f" 实际 {per_plate_keys}"
    )


def test_51b9a5_style_node_count():
    """大规模场景：N 个 list-targets transfers，每个长度 M（同 source 不同跨板）。

    构造：2 个 source（src_A1、src_A2）+ 9 个 target plates × 2 个 well = 18 target reagent_keys。
    2 个 transfer：
      - transfer #1: targets = [t_A1_1, t_A1_2, ..., t_A1_9]（同 source src_A1，跨 9 plate）
      - transfer #2: targets = [t_A2_1, t_A2_2, ..., t_A2_9]（同 source src_A2，跨 9 plate）

    期望 set_liquid_from_plate 总节点数 = 2 source per-plate + 2 merged + 0 target per-plate = 4。
    """
    target_keys_a1 = [f"t_A1_{i}" for i in range(1, 10)]
    target_keys_a2 = [f"t_A2_{i}" for i in range(1, 10)]
    all_target_keys = target_keys_a1 + target_keys_a2

    labware = _labware_with_targets(
        all_target_keys,
        source_keys=["src_A1", "src_A2"],
    )

    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_A1",
                "targets": target_keys_a1,
                "tip_racks": "tiprack_12",
                "asp_vols": [8.3] * 9,
                "dis_vols": [8.3] * 9,
            },
            "step_number": 1,
        },
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_A2",
                "targets": target_keys_a2,
                "tip_racks": "tiprack_12",
                "asp_vols": [8.3] * 9,
                "dis_vols": [8.3] * 9,
            },
            "step_number": 2,
        },
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    per_plate, merged = _set_liquid_nodes_split(g)

    assert len(merged) == 2, f"应有 2 个 merged 节点；实际 {len(merged)}"

    per_plate_keys = {
        n.get("description", "").replace("Set liquid: ", "")
        for n in per_plate
    }

    # source 端：2 个 per-plate
    assert "src_A1" in per_plate_keys and "src_A2" in per_plate_keys, (
        f"source 端必须有 src_A1 + src_A2 per-plate；实际 {per_plate_keys}"
    )

    # target 端：18 个全部被跳过
    for tk in all_target_keys:
        assert tk not in per_plate_keys, (
            f"§14：target reagent_key '{tk}' 应被 merged 覆盖并跳过；"
            f" 实际 per_plate_keys 包含 {tk}"
        )

    # 总节点数 == 2 + 2
    assert len(per_plate) + len(merged) == 4, (
        f"set_liquid_from_plate 总节点数应为 4 (2 source + 2 merged + 0 target per-plate);"
        f" 实际 per_plate={len(per_plate)} merged={len(merged)}"
    )


def test_source_per_plate_always_kept():
    """source reagent_keys 不出现在任何 targets 字段中 → per-plate 节点恒保留（与 §14 无关）。"""
    target_keys = ["t_A", "t_B", "t_C"]
    labware = _labware_with_targets(target_keys, source_keys=["src_X", "src_Y"])

    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_X",
                "targets": target_keys,
                "tip_racks": "tiprack_12",
                "asp_vols": [5.0] * 3,
                "dis_vols": [5.0] * 3,
            },
            "step_number": 1,
        },
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "src_Y",
                "targets": "t_A",
                "tip_racks": "tiprack_12",
                "asp_vols": [10.0],
                "dis_vols": [10.0],
            },
            "step_number": 2,
        },
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )

    per_plate, _ = _set_liquid_nodes_split(g)
    per_plate_keys = {
        n.get("description", "").replace("Set liquid: ", "")
        for n in per_plate
    }

    assert "src_X" in per_plate_keys, "source src_X 必须有 per-plate（source 不会被 §14 跳过）"
    assert "src_Y" in per_plate_keys, "source src_Y 必须有 per-plate"
