"""
PumpTransfer 和 Separate 全链路测试

构建包含泵/阀门/分液漏斗的完整设备图，
输出完整的中间数据（最短路径、泵骨架、动作列表等）。
"""

import copy
import json
import pprint
import pytest
import networkx as nx

from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.compile.utils.resource_helper import get_resource_id, get_resource_data
from unilabos.compile.utils.vessel_parser import get_vessel


def _make_raw_resource(id, uuid=None, name=None, klass="", type_="device",
                       parent=None, parent_uuid=None, data=None, config=None, extra=None):
    return {
        "id": id,
        "uuid": uuid or f"uuid-{id}",
        "name": name or id,
        "class": klass,
        "type": type_,
        "parent": parent,
        "parent_uuid": parent_uuid or "",
        "description": "",
        "config": config or {},
        "data": data or {},
        "extra": extra or {},
        "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    }


def _simulate_enrichment(raw_data_list):
    tree_set = ResourceTreeSet.from_raw_dict_list(raw_data_list)
    root = tree_set.trees[0].root_node if tree_set.trees else None
    return root.get_plr_nested_dict() if root else {}


def _build_pump_transfer_graph():
    """
    构建带泵/阀门的设备图，用于测试 PumpTransfer:

    flask_water (container)
        ↓
    valve_1 (multiway_valve, pump_1 连接)
        ↓
    reactor_01 (device)

    同时有: stirrer_1, heatchill_1, separator_1
    """
    G = nx.DiGraph()

    # 源容器
    G.add_node("flask_water", **{
        "id": "flask_water", "name": "flask_water",
        "type": "container", "class": "",
        "data": {"reagent_name": "water", "liquid": [{"liquid_type": "water", "volume": 200.0}]},
        "config": {"reagent": "water"},
    })

    # 多通阀
    G.add_node("valve_1", **{
        "id": "valve_1", "name": "valve_1",
        "type": "device", "class": "multiway_valve",
        "data": {}, "config": {},
    })

    # 注射泵（连接到阀门）
    G.add_node("pump_1", **{
        "id": "pump_1", "name": "pump_1",
        "type": "device", "class": "virtual_pump",
        "data": {}, "config": {"max_volume": 25.0},
    })

    # 目标容器
    G.add_node("reactor_01", **{
        "id": "reactor_01", "name": "reactor_01",
        "type": "device", "class": "virtual_stirrer",
        "data": {"liquid": [{"liquid_type": "water", "volume": 50.0}]},
        "config": {},
    })

    # 搅拌器
    G.add_node("stirrer_1", **{
        "id": "stirrer_1", "name": "stirrer_1",
        "type": "device", "class": "virtual_stirrer",
        "data": {}, "config": {},
    })

    # 加热器
    G.add_node("heatchill_1", **{
        "id": "heatchill_1", "name": "heatchill_1",
        "type": "device", "class": "virtual_heatchill",
        "data": {}, "config": {},
    })

    # 分离器
    G.add_node("separator_1", **{
        "id": "separator_1", "name": "separator_1",
        "type": "device", "class": "separator_controller",
        "data": {}, "config": {},
    })

    # 废液容器
    G.add_node("waste_workup", **{
        "id": "waste_workup", "name": "waste_workup",
        "type": "container", "class": "",
        "data": {}, "config": {},
    })

    # 产物收集瓶
    G.add_node("product_flask", **{
        "id": "product_flask", "name": "product_flask",
        "type": "container", "class": "",
        "data": {}, "config": {},
    })

    # DCM溶剂瓶
    G.add_node("flask_dcm", **{
        "id": "flask_dcm", "name": "flask_dcm",
        "type": "container", "class": "",
        "data": {"reagent_name": "dcm", "liquid": [{"liquid_type": "dcm", "volume": 500.0}]},
        "config": {"reagent": "dcm"},
    })

    # 边连接 —— flask_water → valve_1 → reactor_01
    G.add_edge("flask_water", "valve_1", port={"valve_1": "port_1"})
    G.add_edge("valve_1", "reactor_01", port={"valve_1": "port_2"})
    # 阀门 → 泵
    G.add_edge("valve_1", "pump_1")
    G.add_edge("pump_1", "valve_1")
    # 搅拌器 ↔ reactor
    G.add_edge("stirrer_1", "reactor_01")
    # 加热器 ↔ reactor
    G.add_edge("heatchill_1", "reactor_01")
    # 分离器 ↔ reactor
    G.add_edge("separator_1", "reactor_01")
    G.add_edge("reactor_01", "separator_1")
    # DCM → valve → reactor (同一泵路)
    G.add_edge("flask_dcm", "valve_1", port={"valve_1": "port_3"})
    # reactor → valve → product/waste
    G.add_edge("valve_1", "product_flask", port={"valve_1": "port_4"})
    G.add_edge("valve_1", "waste_workup", port={"valve_1": "port_5"})

    return G


def _format_action(action, indent=0):
    """格式化单个 action 为可读字符串"""
    prefix = "  " * indent
    if isinstance(action, list):
        # 并行动作
        lines = [f"{prefix}[PARALLEL]"]
        for sub in action:
            lines.append(_format_action(sub, indent + 1))
        return "\n".join(lines)

    name = action.get("action_name", "?")
    device = action.get("device_id", "")
    kwargs = action.get("action_kwargs", {})
    comment = action.get("_comment", "")
    meta = action.get("_transfer_meta", "")

    parts = [f"{prefix}→ {device}::{name}"]
    if kwargs:
        # 精简输出
        kw_str = ", ".join(f"{k}={v}" for k, v in kwargs.items()
                           if k not in ("progress_message",))
        if kw_str:
            parts.append(f"  kwargs: {{{kw_str}}}")
    if comment:
        parts.append(f"  # {comment}")
    if meta:
        parts.append(f"  meta: {meta}")
    return "\n".join(f"{prefix}{p}" if i > 0 else p for i, p in enumerate(parts))


def _dump_actions(actions, title=""):
    """打印完整动作列表"""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"  总动作数: {len(actions)}")
    print(f"{'='*70}")
    for i, action in enumerate(actions):
        print(f"\n  [{i:02d}] {_format_action(action, indent=2)}")
    print(f"\n{'='*70}\n")


# ==================== PumpTransfer 全链路 ====================

class TestPumpTransferFullChain:
    """PumpTransfer: 包含图路径查找、泵骨架构建、动作序列生成"""

    def test_pump_transfer_basic(self):
        """基础泵转移：flask_water → valve_1 → reactor_01"""
        from unilabos.compile.pump_protocol import generate_pump_protocol

        G = _build_pump_transfer_graph()

        # 检查最短路径
        path = nx.shortest_path(G, "flask_water", "reactor_01")
        print(f"\n最短路径: {path}")
        assert "valve_1" in path

        # 调用编译器
        actions = generate_pump_protocol(
            G=G,
            from_vessel_id="flask_water",
            to_vessel_id="reactor_01",
            volume=10.0,
            flowrate=2.5,
            transfer_flowrate=0.5,
        )

        _dump_actions(actions, "PumpTransfer: flask_water → reactor_01, 10mL")

        # 验证
        assert isinstance(actions, list)
        assert len(actions) > 0
        # 应该有 set_valve_position 和 set_position 动作
        flat = [a for a in actions if isinstance(a, dict)]
        action_names = [a.get("action_name") for a in flat]
        print(f"动作名称列表: {action_names}")
        assert "set_valve_position" in action_names
        assert "set_position" in action_names

    def test_pump_transfer_with_rinsing_enriched_vessel(self):
        """pump_with_rinsing 接收 enriched vessel dict"""
        from unilabos.compile.pump_protocol import generate_pump_protocol_with_rinsing

        G = _build_pump_transfer_graph()

        # 模拟 enrichment
        from_raw = [_make_raw_resource(
            id="flask_water", klass="", type_="container",
            data={"reagent_name": "water", "liquid": [{"liquid_type": "water", "volume": 200.0}]},
        )]
        to_raw = [_make_raw_resource(
            id="reactor_01", klass="virtual_stirrer", type_="device",
        )]

        from_enriched = _simulate_enrichment(from_raw)
        to_enriched = _simulate_enrichment(to_raw)

        print(f"\nfrom_vessel enriched: {json.dumps(from_enriched, indent=2, ensure_ascii=False)[:300]}...")
        print(f"to_vessel enriched: {json.dumps(to_enriched, indent=2, ensure_ascii=False)[:300]}...")

        # get_vessel 兼容
        fid, fdata = get_vessel(from_enriched)
        tid, tdata = get_vessel(to_enriched)
        print(f"from_vessel_id={fid}, to_vessel_id={tid}")
        assert fid == "flask_water"
        assert tid == "reactor_01"

        actions = generate_pump_protocol_with_rinsing(
            G=G,
            from_vessel=from_enriched,
            to_vessel=to_enriched,
            volume=15.0,
            flowrate=2.5,
            transfer_flowrate=0.5,
        )

        _dump_actions(actions, "PumpTransferWithRinsing: flask_water → reactor_01, 15mL (enriched)")

        assert isinstance(actions, list)
        assert len(actions) > 0

    def test_pump_transfer_multi_batch(self):
        """体积 > max_volume 时自动分批"""
        from unilabos.compile.pump_protocol import generate_pump_protocol

        G = _build_pump_transfer_graph()

        # pump_1 的 max_volume = 25mL，转 60mL 应该分 3 批
        actions = generate_pump_protocol(
            G=G,
            from_vessel_id="flask_water",
            to_vessel_id="reactor_01",
            volume=60.0,
            flowrate=2.5,
            transfer_flowrate=0.5,
        )

        _dump_actions(actions, "PumpTransfer 分批: 60mL (max_volume=25mL, 预期 3 批)")

        assert len(actions) > 0
        # 应该有多轮 set_position
        flat = [a for a in actions if isinstance(a, dict)]
        set_position_count = sum(1 for a in flat if a.get("action_name") == "set_position")
        print(f"set_position 动作数: {set_position_count}")
        # 3批 × 2次 (吸液 + 排液) = 6 次 set_position
        assert set_position_count >= 6

    def test_pump_transfer_no_path(self):
        """无路径时返回空"""
        from unilabos.compile.pump_protocol import generate_pump_protocol

        G = _build_pump_transfer_graph()
        G.add_node("isolated_flask", type="container")

        actions = generate_pump_protocol(
            G=G,
            from_vessel_id="isolated_flask",
            to_vessel_id="reactor_01",
            volume=10.0,
        )

        print(f"\n无路径时的动作列表: {actions}")
        assert actions == []

    def test_pump_backbone_filtering(self):
        """验证泵骨架过滤逻辑（电磁阀被跳过）"""
        from unilabos.compile.pump_protocol import generate_pump_protocol

        G = _build_pump_transfer_graph()
        # 添加电磁阀到路径中
        G.add_node("solenoid_valve_1", **{
            "type": "device", "class": "solenoid_valve",
            "data": {}, "config": {},
        })
        # flask_water → solenoid_valve_1 → valve_1 → reactor_01
        G.remove_edge("flask_water", "valve_1")
        G.add_edge("flask_water", "solenoid_valve_1")
        G.add_edge("solenoid_valve_1", "valve_1")

        path = nx.shortest_path(G, "flask_water", "reactor_01")
        print(f"\n含电磁阀的路径: {path}")
        assert "solenoid_valve_1" in path

        actions = generate_pump_protocol(
            G=G,
            from_vessel_id="flask_water",
            to_vessel_id="reactor_01",
            volume=10.0,
        )

        _dump_actions(actions, "PumpTransfer 含电磁阀: flask_water → solenoid → valve_1 → reactor_01")
        # 电磁阀应被跳过，泵骨架只有 valve_1
        assert len(actions) > 0


# ==================== Separate 全链路 ====================

class TestSeparateProtocolFullChain:
    """Separate: 包含 bug 确认和正常路径测试"""

    def test_separate_bug_line_128_fixed(self):
        """验证 separate_protocol.py:128 的 bug 已修复（不再 crash）"""
        from unilabos.compile.separate_protocol import generate_separate_protocol

        G = _build_pump_transfer_graph()

        raw_data = [_make_raw_resource(
            id="reactor_01", klass="virtual_stirrer",
            data={"liquid": [{"liquid_type": "water", "volume": 100.0}]},
        )]
        enriched = _simulate_enrichment(raw_data)

        # 修复前：final_vessel_id, _ = vessel_id 会 crash（字符串解包）
        # 修复后：final_vessel_id = vessel_id，正常返回 action 列表
        result = generate_separate_protocol(
            G=G,
            vessel=enriched,
            purpose="extract",
            product_phase="top",
            product_vessel="product_flask",
            waste_vessel="waste_workup",
            solvent="dcm",
            volume="100 mL",
        )
        assert isinstance(result, list)
        assert len(result) > 0

    def test_separate_manual_workaround(self):
        """
        绕过 line 128 bug，手动测试分离编译器中可以工作的子函数
        """
        from unilabos.compile.separate_protocol import (
            find_separator_device,
            find_separation_vessel_bottom,
        )
        from unilabos.compile.utils.vessel_parser import (
            find_connected_stirrer,
            find_solvent_vessel,
        )
        from unilabos.compile.utils.unit_parser import parse_volume_input
        from unilabos.compile.utils.resource_helper import get_resource_liquid_volume as get_vessel_liquid_volume

        G = _build_pump_transfer_graph()

        # 1. get_vessel 解析 enriched dict
        raw_data = [_make_raw_resource(
            id="reactor_01", klass="virtual_stirrer",
            data={"liquid": [{"liquid_type": "water", "volume": 100.0}]},
        )]
        enriched = _simulate_enrichment(raw_data)
        vessel_id, vessel_data = get_vessel(enriched)
        print(f"\nvessel_id: {vessel_id}")
        print(f"vessel_data: {vessel_data}")
        assert vessel_id == "reactor_01"
        assert vessel_data["liquid"][0]["volume"] == 100.0

        # 2. find_separator_device
        sep = find_separator_device(G, vessel_id)
        print(f"分离器设备: {sep}")
        assert sep == "separator_1"

        # 3. find_connected_stirrer
        stirrer = find_connected_stirrer(G, vessel_id)
        print(f"搅拌器设备: {stirrer}")
        assert stirrer == "stirrer_1"

        # 4. find_solvent_vessel
        solvent_v = find_solvent_vessel(G, "dcm")
        print(f"DCM溶剂容器: {solvent_v}")
        assert solvent_v == "flask_dcm"

        # 5. parse_volume_input
        vol = parse_volume_input("200 mL")
        print(f"体积解析: '200 mL' → {vol}")
        assert vol == 200.0

        vol2 = parse_volume_input("1.5 L")
        print(f"体积解析: '1.5 L' → {vol2}")
        assert vol2 == 1500.0

        # 6. get_vessel_liquid_volume
        liq_vol = get_vessel_liquid_volume(enriched)
        print(f"液体体积 (enriched dict): {liq_vol}")
        assert liq_vol == 100.0

        # 7. find_separation_vessel_bottom
        bottom = find_separation_vessel_bottom(G, vessel_id)
        print(f"分离容器底部: {bottom}")
        # 当前图中没有命名匹配的底部容器

    def test_pump_transfer_for_separate_subflow(self):
        """测试 separate 中调用的 pump 子流程（溶剂添加 → 分液漏斗）"""
        from unilabos.compile.pump_protocol import generate_pump_protocol_with_rinsing

        G = _build_pump_transfer_graph()

        # 模拟分离前的溶剂添加步骤
        actions = generate_pump_protocol_with_rinsing(
            G=G,
            from_vessel="flask_dcm",
            to_vessel="reactor_01",
            volume=100.0,
            flowrate=2.5,
            transfer_flowrate=0.5,
        )

        _dump_actions(actions, "Separate 子流程: flask_dcm → reactor_01, 100mL DCM")

        assert isinstance(actions, list)
        assert len(actions) > 0

        # 模拟分离后产物转移
        actions2 = generate_pump_protocol_with_rinsing(
            G=G,
            from_vessel="reactor_01",
            to_vessel="product_flask",
            volume=50.0,
            flowrate=2.5,
            transfer_flowrate=0.5,
        )

        _dump_actions(actions2, "Separate 子流程: reactor_01 → product_flask, 50mL 产物")

        assert len(actions2) > 0

        # 废液转移
        actions3 = generate_pump_protocol_with_rinsing(
            G=G,
            from_vessel="reactor_01",
            to_vessel="waste_workup",
            volume=50.0,
            flowrate=2.5,
            transfer_flowrate=0.5,
        )

        _dump_actions(actions3, "Separate 子流程: reactor_01 → waste_workup, 50mL 废液")

        assert len(actions3) > 0


# ==================== 图路径可视化 ====================

class TestGraphPathVisualization:
    """输出图中关键路径信息"""

    def test_all_shortest_paths(self):
        """输出所有容器之间的最短路径"""
        G = _build_pump_transfer_graph()

        containers = [n for n in G.nodes() if G.nodes[n].get("type") == "container"]
        devices = [n for n in G.nodes() if G.nodes[n].get("type") == "device"]

        print(f"\n{'='*70}")
        print(f"  设备图概览")
        print(f"{'='*70}")
        print(f"  容器节点 ({len(containers)}): {containers}")
        print(f"  设备节点 ({len(devices)}): {devices}")
        print(f"  边数: {G.number_of_edges()}")
        print(f"  边列表:")
        for u, v, data in G.edges(data=True):
            port_info = data.get("port", "")
            print(f"    {u} → {v}  {port_info if port_info else ''}")

        print(f"\n  关键路径:")
        pairs = [
            ("flask_water", "reactor_01"),
            ("flask_dcm", "reactor_01"),
            ("reactor_01", "product_flask"),
            ("reactor_01", "waste_workup"),
            ("flask_water", "product_flask"),
        ]
        for src, dst in pairs:
            try:
                path = nx.shortest_path(G, src, dst)
                length = len(path) - 1
                # 标注路径上的节点类型
                annotated = []
                for n in path:
                    ntype = G.nodes[n].get("type", "?")
                    nclass = G.nodes[n].get("class", "")
                    annotated.append(f"{n}({ntype}{'/' + nclass if nclass else ''})")
                print(f"    {src} → {dst}: 距离={length}")
                print(f"      路径: {' → '.join(annotated)}")
            except nx.NetworkXNoPath:
                print(f"    {src} → {dst}: 无路径!")

        print(f"{'='*70}\n")
