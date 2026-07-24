"""P2 v2 跨 slot transfer_liquid 合并 —— Stage 3 (`workflow/common.py`) 集成测试。

对应 ``product_designs/protocol_convert/02-cross-slot-merge.md`` §9.5 step 6.2。

v2 设计要点（与本测试用例的映射）
-----------------------------------
当 transfer_liquid 节点 ``params.targets`` 是 ``list[str]`` 时，``build_protocol_graph``
在该 transfer_liquid 之前**插入一个 merged ``set_liquid_from_plate`` 节点**：

- merged 节点的 ``param.wells`` 是按 ``params.targets`` 顺序通过 cursor 拼出来的有序跨板
  well refs（每个元素是 ``{id, name, parent: reagent_key, type: "well"}``）。
- merged 节点接收来自每个涉及 plate 的 ``create_resource`` 节点的多入边
  (``labware`` → ``wells_identifier``)。
- merged 节点的 ``output_wells`` 通过**单条边**连到 transfer_liquid 的 ``targets_identifier``。
- transfer_liquid 节点的 ``params.targets`` 被改写为 synthetic key
  ``_merged_targets_<idx>``（runtime 不消费 list 形态），保证 INPUT_PORT_MAPPING 走单边路径。

用例
----
- ``test_emit_merged_set_liquid_basic`` — 4 个 distinct reagent_key（51b9a5 主场景）。
- ``test_emit_merged_set_liquid_repeat_key`` — 同 reagent_key 重复（同板多孔）。
- ``test_emit_merged_set_liquid_mixed`` — 跨板混合 + 同板重复（cursor 推进）。
- ``test_emit_merged_set_liquid_8ch`` — 与 P1 multi-channel 复合（8 通道 cross-slot）。
- ``test_transfer_liquid_targets_rewrite`` — transfer_liquid 节点改写后只剩 1 条
  ``targets_identifier`` 入边；params.targets 不再是 list。
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
    """与 test_build_protocol_graph_target_device.py 一致的可选依赖 stub。"""
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


# ==================== 测试辅助：从工作流图中提取节点/边 ====================


def _nodes_by_template(graph, template_name: str) -> List[Dict[str, Any]]:
    return [
        {"id": nid, **node}
        for nid, node in graph.nodes.items()
        if node.get("template_name") == template_name
    ]


def _create_resource_by_slot(graph) -> Dict[str, str]:
    """slot_on_deck (str) -> create_resource 节点 ID。"""
    out: Dict[str, str] = {}
    for nid, node in graph.nodes.items():
        if node.get("template_name") == "create_resource":
            slot = str(node.get("param", {}).get("slot_on_deck") or "")
            if slot:
                out[slot] = nid
    return out


def _edges_to(graph, target_id: str) -> List[Dict[str, Any]]:
    return [e for e in graph.edges if e["target"] == target_id]


def _edges_from(graph, source_id: str) -> List[Dict[str, Any]]:
    return [e for e in graph.edges if e["source"] == source_id]


# ==================== fixture：构造跨板 labware + steps ====================


def _cross_slot_labware_info() -> Dict[str, Dict[str, Any]]:
    """51b9a5 简化：slot1 source + slot2/3/5/6 target plates + slot12 tip。"""
    return {
        "l1": {
            "slot": 1,
            "well": ["A1"],
            "labware": "nest_12_reservoir_15ml",
            "object": "source",
        },
        "plate_slot2": {
            "slot": 2,
            "well": ["A1"],
            "labware": "nest_96_wellplate_2ml_deep",
            "object": "target",
        },
        "plate_slot3": {
            "slot": 3,
            "well": ["A1"],
            "labware": "nest_96_wellplate_2ml_deep",
            "object": "target",
        },
        "plate_slot5": {
            "slot": 5,
            "well": ["A1"],
            "labware": "nest_96_wellplate_2ml_deep",
            "object": "target",
        },
        "plate_slot6": {
            "slot": 6,
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


def _cross_slot_protocol_steps(targets: List[str], dis_vols: List[float]) -> List[Dict[str, Any]]:
    return [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "l1",
                "targets": targets,
                "tip_racks": "tiprack_12",
                "asp_vols": dis_vols.copy(),
                "dis_vols": dis_vols.copy(),
            },
            "step_number": 1,
        }
    ]


# ==================== 用例 ====================


def test_emit_merged_set_liquid_basic():
    """51b9a5 主场景：targets=[A,B,C,D] → 1 merged set_liquid 节点
    + 4 条入边（来自 4 个 distinct create_resource）+ 1 条出边（去 transfer_liquid）。
    """
    targets = ["plate_slot2", "plate_slot3", "plate_slot5", "plate_slot6"]
    dis_vols = [8.3, 8.3, 8.3, 8.3]
    g = build_protocol_graph(
        labware_info=_cross_slot_labware_info(),
        protocol_steps=_cross_slot_protocol_steps(targets, dis_vols),
        workstation_name="PRCXI",
    )

    set_liquid_nodes = _nodes_by_template(g, "set_liquid_from_plate")
    merged_nodes = [n for n in set_liquid_nodes if str(n.get("name", "")).startswith("_merged_targets_")]
    assert len(merged_nodes) == 1, (
        f"应有且仅有 1 个 merged set_liquid_from_plate 节点（v2 跨板聚合器）;"
        f" 实际找到 {len(merged_nodes)}: {[n.get('name') for n in merged_nodes]}"
    )
    merged = merged_nodes[0]
    merged_id = merged["id"]

    # param.wells：长度 4，每元素的 parent 是对应 reagent_key
    wells = merged.get("param", {}).get("wells") or []
    assert len(wells) == 4
    assert [w["parent"] for w in wells] == targets, "merged.wells 顺序必须严格按 targets 列表"
    # well 字段映射到 reagent.well[0]（都是 "A1"）
    for w, key in zip(wells, targets):
        assert w["id"].endswith("/A1"), f"well id 应包含 well 名: {w}"
        assert w["parent"] == key

    # 入边：4 条来自 distinct create_resource 节点（slot 2/3/5/6），target_port=wells_identifier
    cr_by_slot = _create_resource_by_slot(g)
    in_edges = _edges_to(g, merged_id)
    in_sources = {e["source"] for e in in_edges if e.get("target_handle_key") == "wells_identifier"}
    expected_sources = {cr_by_slot[s] for s in ("2", "3", "5", "6")}
    assert in_sources == expected_sources, (
        f"merged 节点应接收 4 个 distinct create_resource 的 wells_identifier 边;"
        f" 实际 {in_sources} vs 期望 {expected_sources}"
    )

    # 出边：1 条到 transfer_liquid（targets_identifier）
    transfer_nodes = _nodes_by_template(g, "transfer_liquid")
    assert len(transfer_nodes) == 1
    transfer_id = transfer_nodes[0]["id"]
    out_to_transfer = [
        e for e in _edges_from(g, merged_id)
        if e["target"] == transfer_id and e.get("target_handle_key") == "targets_identifier"
    ]
    assert len(out_to_transfer) == 1, (
        f"merged 节点应向 transfer_liquid.targets_identifier 发出唯一 1 条边;"
        f" 实际 {len(out_to_transfer)}"
    )


def test_emit_merged_set_liquid_repeat_key():
    """同 reagent_key 重复（同板多孔）：targets=[A,A,A] + reagent.A.well=[A1,A2,A3]
    → merged.wells 顺序 = [A/A1, A/A2, A/A3]（cursor 推进取每个 well）。
    """
    labware = _cross_slot_labware_info()
    labware["plate_slot2"]["well"] = ["A1", "A2", "A3"]

    targets = ["plate_slot2", "plate_slot2", "plate_slot2"]
    dis_vols = [10.0, 20.0, 30.0]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=_cross_slot_protocol_steps(targets, dis_vols),
        workstation_name="PRCXI",
    )

    merged_nodes = [
        n for n in _nodes_by_template(g, "set_liquid_from_plate")
        if str(n.get("name", "")).startswith("_merged_targets_")
    ]
    assert len(merged_nodes) == 1
    wells = merged_nodes[0]["param"]["wells"]
    assert [w["id"].rsplit("/", 1)[-1] for w in wells] == ["A1", "A2", "A3"], (
        "cursor 应依次取 reagent.A.well[0/1/2]"
    )
    assert all(w["parent"] == "plate_slot2" for w in wells)


def test_emit_merged_set_liquid_mixed():
    """跨板 + 同板重复：targets=[A,B,A,C] + reagent.A.well=[A1,A2]
    → merged.wells = [A/A1, B/A1, A/A2, C/A1]。
    """
    labware = _cross_slot_labware_info()
    labware["plate_slot2"]["well"] = ["A1", "A2"]

    targets = ["plate_slot2", "plate_slot3", "plate_slot2", "plate_slot5"]
    dis_vols = [10.0, 20.0, 30.0, 40.0]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=_cross_slot_protocol_steps(targets, dis_vols),
        workstation_name="PRCXI",
    )

    merged_nodes = [
        n for n in _nodes_by_template(g, "set_liquid_from_plate")
        if str(n.get("name", "")).startswith("_merged_targets_")
    ]
    assert len(merged_nodes) == 1
    wells = merged_nodes[0]["param"]["wells"]
    ids = [(w["parent"], w["id"].rsplit("/", 1)[-1]) for w in wells]
    assert ids == [
        ("plate_slot2", "A1"),
        ("plate_slot3", "A1"),
        ("plate_slot2", "A2"),
        ("plate_slot5", "A1"),
    ]


def test_emit_merged_set_liquid_8ch():
    """与 P1 multi-channel 复合：targets=[A]*8+[B]*8（每列 8 通道）。

    merged.wells 长度 16，前 8 全 plate_slot2 的 8 个 well，后 8 全 plate_slot3 的 8 个 well。
    """
    labware = _cross_slot_labware_info()
    # 8 通道场景 reagent.well 已被 P1 multi 展开为长度 8
    labware["plate_slot2"]["well"] = [f"{r}1" for r in "ABCDEFGH"]
    labware["plate_slot3"]["well"] = [f"{r}1" for r in "ABCDEFGH"]

    targets = ["plate_slot2"] * 8 + ["plate_slot3"] * 8
    dis_vols = [5.0] * 16
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=_cross_slot_protocol_steps(targets, dis_vols),
        workstation_name="PRCXI",
    )

    merged_nodes = [
        n for n in _nodes_by_template(g, "set_liquid_from_plate")
        if str(n.get("name", "")).startswith("_merged_targets_")
    ]
    assert len(merged_nodes) == 1
    wells = merged_nodes[0]["param"]["wells"]
    assert len(wells) == 16
    # 前 8 全 plate_slot2，后 8 全 plate_slot3（满足 cross-slot × 8ch 列对齐约束）
    assert all(w["parent"] == "plate_slot2" for w in wells[:8])
    assert all(w["parent"] == "plate_slot3" for w in wells[8:])
    # well 名顺序：A1..H1 重复两遍
    assert [w["id"].rsplit("/", 1)[-1] for w in wells[:8]] == [f"{r}1" for r in "ABCDEFGH"]
    assert [w["id"].rsplit("/", 1)[-1] for w in wells[8:]] == [f"{r}1" for r in "ABCDEFGH"]


def test_transfer_liquid_targets_rewrite():
    """transfer_liquid 节点改写后只剩 1 条 targets_identifier 入边；params.targets 不再是 list。"""
    targets = ["plate_slot2", "plate_slot3", "plate_slot5", "plate_slot6"]
    dis_vols = [8.3, 8.3, 8.3, 8.3]
    g = build_protocol_graph(
        labware_info=_cross_slot_labware_info(),
        protocol_steps=_cross_slot_protocol_steps(targets, dis_vols),
        workstation_name="PRCXI",
    )

    transfer_nodes = _nodes_by_template(g, "transfer_liquid")
    assert len(transfer_nodes) == 1
    tnode = transfer_nodes[0]
    transfer_id = tnode["id"]

    # params.targets：v2 中 list 形态在 INPUT_PORT_MAPPING 处理后被清空（[]）或为单字符串
    # （不再是原始 list[str]——避免下游 runtime 对其再做无序聚合）
    tparams = tnode.get("param", {}) or {}
    assert not isinstance(tparams.get("targets"), list) or tparams.get("targets") == [], (
        f"v2：params.targets 不再是非空 list；实际 {tparams.get('targets')!r}"
    )

    # targets_identifier 端口：只有 1 条入边
    in_targets_edges = [
        e for e in _edges_to(g, transfer_id)
        if e.get("target_handle_key") == "targets_identifier"
    ]
    assert len(in_targets_edges) == 1, (
        f"v2：transfer_liquid.targets_identifier 必须是单入边（来自 merged set_liquid）;"
        f" 实际 {len(in_targets_edges)}"
    )

    # 这条入边的源端口必须是 output_wells
    edge = in_targets_edges[0]
    assert edge.get("source_handle_key") == "output_wells"


def test_str_targets_no_merged_node_emitted():
    """对照组：targets 为 str（单 reagent） → 不插入 merged set_liquid_from_plate 节点。

    保证 v2 改造**只**对 list 形态触发，单 reagent 走 P3 原有 per-plate set_liquid 路径。
    """
    labware = _cross_slot_labware_info()
    labware["plate_slot2"]["well"] = ["A1", "A2", "A3"]

    steps = [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "l1",
                "targets": "plate_slot2",         # ← 单 str，非 list
                "tip_racks": "tiprack_12",
                "asp_vols": [8.3, 8.3, 8.3],
                "dis_vols": [8.3, 8.3, 8.3],
            },
            "step_number": 1,
        }
    ]
    g = build_protocol_graph(
        labware_info=labware,
        protocol_steps=steps,
        workstation_name="PRCXI",
    )
    merged_nodes = [
        n for n in _nodes_by_template(g, "set_liquid_from_plate")
        if str(n.get("name", "")).startswith("_merged_targets_")
    ]
    assert merged_nodes == [], "str 形态 targets 不应触发 v2 merged 聚合节点"
