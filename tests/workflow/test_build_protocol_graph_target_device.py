"""P6.1 / P6.1.1 `build_protocol_graph` 集成测试 —— 对应 06-labware-mapping-table.md §11.7.7 C / §11.8.7 C。

6 条用例：

- `test_build_graph_default_target_device_prcxi` —— 不传 target_device 时默认 "prcxi"，
  与 P6 等价（PRCXI_* class_name）。
- `test_build_graph_explicit_target_device_prcxi` —— 显式 "prcxi" 与默认完全等价。
- `test_build_graph_target_device_unknown_falls_back_to_default_section` —— 未声明的
  target_device 由 loader 自动 fallback 到 ``target_devices.default``；第一版 default
  段按 prcxi 拷贝，所以结果应与 "prcxi" 完全一致。
- `test_build_graph_per_device_tip_class` —— 临时 YAML 同时声明 prcxi 与 beckman tip
  量程档；同一 transfer_liquid 在 target_device="prcxi" / "beckman" 下命中不同 class。
- `test_field_renamed_target_class_name` —— `labware_info` 写入的字段是
  `target_class_name`，**旧字段 `prcxi_class_name` 不存在**。
- `test_build_graph_model_level_slot_remap` —— P6.1.1：``target_model`` 透传到
  ``_map_deck_slot`` 后改变 create_resource 的 slot（同厂商不同型号 deck 物理布局不同）。

本测试在导入 common.py 之前 mock 掉 matplotlib / networkx.drawing.nx_agraph，避免在
没有图形依赖的最小 Python 环境下也能跑（与 P6 批量回归脚本同样的策略）。
"""
from __future__ import annotations

import sys
import types
import warnings
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _install_fake_optional_deps() -> None:
    """安装 matplotlib / networkx.drawing.nx_agraph 的 fake 实现，避免本地环境硬依赖。

    common.py 在模块级 import 这些库做可视化辅助；build_protocol_graph 主路径不会真用到。
    fake 模块只需要满足 ``from X import Y`` 的查找即可。
    """
    if "matplotlib" not in sys.modules:
        fake_matplotlib = types.ModuleType("matplotlib")
        sys.modules["matplotlib"] = fake_matplotlib
    if "matplotlib.pyplot" not in sys.modules:
        fake_plt = types.ModuleType("matplotlib.pyplot")
        sys.modules["matplotlib.pyplot"] = fake_plt
    # networkx.drawing.nx_agraph.to_agraph 依赖 pygraphviz；不可用时给个空 stub
    try:
        from networkx.drawing import nx_agraph  # noqa: F401
    except Exception:
        nx_drawing = types.ModuleType("networkx.drawing")
        nx_agraph_mod = types.ModuleType("networkx.drawing.nx_agraph")

        def _to_agraph(_g):  # type: ignore[no-untyped-def]
            raise RuntimeError("nx_agraph fake — not used in build_protocol_graph main path")

        nx_agraph_mod.to_agraph = _to_agraph  # type: ignore[attr-defined]
        nx_drawing.nx_agraph = nx_agraph_mod  # type: ignore[attr-defined]
        sys.modules["networkx.drawing"] = nx_drawing
        sys.modules["networkx.drawing.nx_agraph"] = nx_agraph_mod


_install_fake_optional_deps()

from unilabos.workflow import labware_mapping as lm  # noqa: E402
from unilabos.workflow.common import build_protocol_graph  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_mapping_cache():
    """每个用例后清 lru_cache，避免跨用例污染。"""
    yield
    lm.reload_mapping()


# ==================== 公共 fixture：最小 transfer_liquid 协议 ====================


def _minimal_labware_info() -> dict:
    """返回最小可用的 labware_info（mutable，每个 case 独立 build 一份）。

    包含 tip rack + 24-tube rack + 96 wellplate（slot 1/2/3），覆盖 P6.1 主要 kind。
    tube rack / plate 显式声明 ``num_wells``，避免在无 labware_defs / 无 prcxi_labware 模板
    时通过 well-count 启发式（well_n=3）误判孔数；与真实协议中 labware_defs 提供 num_wells
    的行为对齐。
    """
    return {
        "tips": {
            "slot": 1,
            "well": [],
            "labware": "opentrons_96_tiprack_300ul",
            "object": "tiprack",
        },
        "samples": {
            "slot": 2,
            "well": ["A1", "A2", "A3"],
            "labware": "opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap",
            "object": "source",
            "num_wells": 24,
        },
        "plate_target": {
            "slot": 3,
            "well": ["A1", "A2", "A3"],
            "labware": "opentrons_96_wellplate_300ul_pcr",
            "object": "target",
            "num_wells": 96,
        },
    }


def _minimal_protocol_steps() -> list:
    """最小 transfer_liquid 协议步骤：asp_vols/dis_vols 最大 200 µL → PRCXI 300ul 档。"""
    return [
        {
            "action": "transfer_liquid",
            "parameters": {
                "sources": "samples",
                "targets": "plate_target",
                "tip_racks": "tips",
                "asp_vols": [200.0, 200.0, 200.0],
                "dis_vols": [200.0, 200.0, 200.0],
            },
            "step_number": 1,
        }
    ]


def _collect_create_resource_classes(graph) -> dict:
    """从工作流图中提取每个 create_resource 节点的 ``slot_on_deck → class_name``。"""
    out: dict = {}
    for _nid, node in graph.nodes.items():
        if node.get("template_name") != "create_resource":
            continue
        param = node.get("param") or {}
        slot = str(param.get("slot_on_deck") or "")
        cls = str(param.get("class_name") or "")
        if slot:
            out[slot] = cls
    return out


# ==================== 5 条核心用例 ====================


def test_build_graph_default_target_device_prcxi():
    """不传 target_device → 默认 "prcxi" → 与 P6 等价（PRCXI_* class_name）。"""
    labware_info = _minimal_labware_info()
    g = build_protocol_graph(
        labware_info=labware_info,
        protocol_steps=_minimal_protocol_steps(),
        workstation_name="PRCXI",
    )
    classes = _collect_create_resource_classes(g)
    assert classes["1"] == "PRCXI_300ul_Tips"           # 200 µL → 300 档
    assert classes["2"] == "PRCXI_EP_Adapter"            # 24-tube rack
    assert classes["3"] == "PRCXI_BioER_96_wellplate"    # 96 wellplate


def test_build_graph_explicit_target_device_prcxi():
    """显式传 target_device="prcxi" 应与默认完全等价。"""
    labware_info_a = _minimal_labware_info()
    labware_info_b = _minimal_labware_info()
    g_default = build_protocol_graph(
        labware_info=labware_info_a,
        protocol_steps=_minimal_protocol_steps(),
        workstation_name="PRCXI",
    )
    g_prcxi = build_protocol_graph(
        labware_info=labware_info_b,
        protocol_steps=_minimal_protocol_steps(),
        workstation_name="PRCXI",
        target_device="prcxi",
    )
    assert _collect_create_resource_classes(g_default) == _collect_create_resource_classes(g_prcxi)


def test_build_graph_target_device_unknown_falls_back_to_default_section():
    """未声明的 target_device → loader 自动 fallback 到固定段 target_devices.default + warning。

    第一版 default 段按 prcxi 拷贝填充 → 结果应与 target_device="prcxi" 完全等价（PRCXI_*）。
    """
    labware_info_a = _minimal_labware_info()
    labware_info_b = _minimal_labware_info()
    g_prcxi = build_protocol_graph(
        labware_info=labware_info_a,
        protocol_steps=_minimal_protocol_steps(),
        workstation_name="PRCXI",
        target_device="prcxi",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        g_unknown = build_protocol_graph(
            labware_info=labware_info_b,
            protocol_steps=_minimal_protocol_steps(),
            workstation_name="PRCXI",
            target_device="unknown_xxx",
        )
    assert _collect_create_resource_classes(g_unknown) == _collect_create_resource_classes(g_prcxi)
    # loader 至少打 1 次 warning 提示「未声明、已回退到 default」
    assert any(
        ("未在 labware_mapping.yaml" in str(w.message))
        or ("target_devices.default" in str(w.message))
        for w in caught
    )


def test_build_graph_per_device_tip_class(tmp_path, monkeypatch):
    """同一 protocol，target_device="prcxi" / "beckman" 在 200µL 下命中不同 tip 档（P6.1.1 schema）。"""
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds:\n'
        '  - {pattern: "trash", kind: trash}\n'
        '  - {pattern: "tiprack|tip[_ ]?rack|opentrons_\\\\d+_tiprack", kind: tip_rack}\n'
        '  - {pattern: "tuberack|tube[_ ]rack|eppendorf.*rack|safelock.*rack", kind: tube_rack}\n'
        '  - {pattern: ".*", kind: plate}\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {"4": "13", "8": "14"}, by_object: {trash: {"12": "16"}}}\n'
        '    rules:\n'
        '      - {kind: tip_rack,  hole_count: 96, volume_max: 10,    class_name: PRCXI_10uL_Tips}\n'
        '      - {kind: tip_rack,  hole_count: 96, volume_max: 299.9, class_name: PRCXI_300ul_Tips}\n'
        '      - {kind: tip_rack,  hole_count: 96,                    class_name: PRCXI_1000uL_Tips}\n'
        '      - {kind: tube_rack, hole_count: 24, class_name: PRCXI_EP_Adapter}\n'
        '      - {kind: plate,     hole_count: 96, class_name: PRCXI_BioER_96_wellplate}\n'
        '  prcxi:\n'
        '    slot_remap: {default: {"4": "13", "8": "14"}, by_object: {trash: {"12": "16"}}}\n'
        '    rules:\n'
        '      - {kind: tip_rack,  hole_count: 96, volume_max: 10,    class_name: PRCXI_10uL_Tips}\n'
        '      - {kind: tip_rack,  hole_count: 96, volume_max: 299.9, class_name: PRCXI_300ul_Tips}\n'
        '      - {kind: tip_rack,  hole_count: 96,                    class_name: PRCXI_1000uL_Tips}\n'
        '      - {kind: tube_rack, hole_count: 24, class_name: PRCXI_EP_Adapter}\n'
        '      - {kind: plate,     hole_count: 96, class_name: PRCXI_BioER_96_wellplate}\n'
        '  beckman:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {trash: {"12": "16"}}}\n'
        '    rules:\n'
        '      - {kind: tip_rack,  hole_count: 96, volume_max: 20,    class_name: Beckman_20uL_Tips}\n'
        '      - {kind: tip_rack,  hole_count: 96, volume_max: 199.9, class_name: Beckman_200uL_Tips}\n'
        '      - {kind: tip_rack,  hole_count: 96,                    class_name: Beckman_1000uL_Tips}\n'
        '      - {kind: tube_rack, hole_count: 24, class_name: Beckman_24_TubeRack}\n'
        '      - {kind: plate,     hole_count: 96, class_name: Beckman_BioMek_96_wellplate}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm.reload_mapping()

    g_prcxi = build_protocol_graph(
        labware_info=_minimal_labware_info(),
        protocol_steps=_minimal_protocol_steps(),
        workstation_name="PRCXI",
        target_device="prcxi",
    )
    g_beckman = build_protocol_graph(
        labware_info=_minimal_labware_info(),
        protocol_steps=_minimal_protocol_steps(),
        workstation_name="PRCXI",
        target_device="beckman",
    )

    classes_prcxi = _collect_create_resource_classes(g_prcxi)
    classes_beckman = _collect_create_resource_classes(g_beckman)

    # 200 µL：prcxi 走 300 档；beckman 200 档已超 → 1000 档
    assert classes_prcxi["1"] == "PRCXI_300ul_Tips"
    assert classes_beckman["1"] == "Beckman_1000uL_Tips"
    # plate / tube rack 也按 target_device 输出对应厂商类
    assert classes_prcxi["2"] == "PRCXI_EP_Adapter"
    assert classes_beckman["2"] == "Beckman_24_TubeRack"
    assert classes_prcxi["3"] == "PRCXI_BioER_96_wellplate"
    assert classes_beckman["3"] == "Beckman_BioMek_96_wellplate"


def test_field_renamed_target_class_name():
    """`labware_info` 写入的字段是 `target_class_name`；旧字段 `prcxi_class_name` 不存在。"""
    labware_info = _minimal_labware_info()
    build_protocol_graph(
        labware_info=labware_info,
        protocol_steps=_minimal_protocol_steps(),
        workstation_name="PRCXI",
    )
    for lid, item in labware_info.items():
        assert "target_class_name" in item, f"{lid!r} 缺少 target_class_name 字段"
        assert "prcxi_class_name" not in item, f"{lid!r} 残留了旧字段 prcxi_class_name"
        assert item["target_class_name"], f"{lid!r} target_class_name 为空"


# ==================== P6.1.1 新增集成测试 ====================


def _labware_info_slot4_plate() -> dict:
    """slot=4 的 96 板：用来验证 target_model 透传后 slot_remap 改变 create_resource 的槽位。"""
    return {
        "plate_slot4": {
            "slot": 4,
            "well": ["A1"],
            "labware": "opentrons_96_wellplate_300ul_pcr",
            "object": "target",
            "num_wells": 96,
        },
    }


def test_build_graph_model_level_slot_remap(tmp_path, monkeypatch):
    """P6.1.1：target_model 透传到 _map_deck_slot 后改变 create_resource 的 slot_on_deck。

    YAML 中 prcxi 厂商级 slot_remap 4→13；模型 "4040" 显式覆盖 4→16。
    同一份 labware_info（slot=4）build 出的两份图，slot_on_deck 应分别为 "13" 与 "16"。
    """
    yaml_path = tmp_path / "labware_mapping.yaml"
    yaml_path.write_text(
        'kinds: [{pattern: ".*", kind: plate}]\n'
        'target_devices:\n'
        '  default:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: [{kind: plate, hole_count: 96, class_name: PRCXI_BioER_96_wellplate}]\n'
        '  prcxi:\n'
        '    slot_remap: {default: {"4": "13"}, by_object: {}}\n'
        '    rules: [{kind: plate, hole_count: 96, class_name: PRCXI_BioER_96_wellplate}]\n'
        '    models:\n'
        '      "4040":\n'
        '        slot_remap: {default: {"4": "16"}, by_object: {}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lm, "_DEFAULT_PATH", yaml_path)
    lm.reload_mapping()

    g_default = build_protocol_graph(
        labware_info=_labware_info_slot4_plate(),
        protocol_steps=[],
        workstation_name="PRCXI",
        target_device="prcxi",
    )
    g_model_4040 = build_protocol_graph(
        labware_info=_labware_info_slot4_plate(),
        protocol_steps=[],
        workstation_name="PRCXI",
        target_device="prcxi",
        target_model="4040",
    )

    classes_default = _collect_create_resource_classes(g_default)
    classes_4040 = _collect_create_resource_classes(g_model_4040)

    # 厂商级（无 model）→ slot 4 → "13"
    assert "13" in classes_default, f"未找到 slot 13，实际生成的 slots: {list(classes_default)}"
    assert "16" not in classes_default
    # 模型 4040 → slot 4 → "16"
    assert "16" in classes_4040, f"未找到 slot 16，实际生成的 slots: {list(classes_4040)}"
    assert "13" not in classes_4040
    # class_name 不变（rules 继承厂商级）
    assert classes_default["13"] == "PRCXI_BioER_96_wellplate"
    assert classes_4040["16"] == "PRCXI_BioER_96_wellplate"
