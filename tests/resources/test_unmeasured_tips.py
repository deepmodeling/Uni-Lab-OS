"""自定义枪头占位几何和未实测标记的兼容回归。"""

import pytest
from pylabrobot.resources import Resource, Tip, TipSpot

from unilabos.resources.presets.tips import (
    TIP_GEOMETRY_ESTIMATES, make_unmeasured_tip, tip_geometry_metadata,
)


@pytest.mark.parametrize("model", TIP_GEOMETRY_ESTIMATES)
def test_unmeasured_tip_geometry_and_spot_roundtrip(model):
    """每个孔位及枪头都持久保存未实测标记。"""
    def make_tip(name):
        return make_unmeasured_tip(
            name, model=model, volume=200, length=60, fitting_depth=8
        )

    spot = TipSpot(
        name="spot", size_x=9, size_y=9, make_tip=make_tip,
        metadata=tip_geometry_metadata(model),
    )
    first, second = spot.make_tip(), spot.make_tip()
    assert first is not second
    assert first.name != second.name
    restored = TipSpot.deserialize(spot.serialize())
    mark = restored.metadata["geometry_validation"]
    assert mark["status"] == "unmeasured"
    assert mark["measured"] is False
    assert mark["estimated_fields"]["diameter"]["value"] == TIP_GEOMETRY_ESTIMATES[model][0]
    tip = restored.make_tip()
    assert tip.maximal_volume == 200
    assert tip.get_size_x() == TIP_GEOMETRY_ESTIMATES[model][0]
    assert tip.metadata == spot.metadata
    assert "total_tip_length" not in tip.serialize()


@pytest.mark.parametrize("model", [m for m in TIP_GEOMETRY_ESTIMATES if m.startswith("PRCXI")])
def test_prcxi_tip_templates_roundtrip(model):
    """已注册 PRCXI 枪头盒可创建并恢复，10 uL 不得再变成 1250 uL。"""
    from unilabos.devices.liquid_handling.prcxi import prcxi_labware

    rack = getattr(prcxi_labware, model)(name="audit_" + model)
    restored = Resource.deserialize(rack.serialize(), allow_marshal=True)
    restored.load_all_state(rack.serialize_all_state())
    spot = restored.get_item("A1")
    assert spot.metadata["geometry_validation"]["measured"] is False
    tip = spot.make_tip()
    expected = 10 if model in ("PRCXI_10uL_Tips", "PRCXI_10ul_eTips") else {
        "PRCXI_200uL_Tips": 200, "PRCXI_300ul_Tips": 300,
        "PRCXI_1000uL_Tips": 1000, "PRCXI_1250uL_Tips": 1250,
    }[model]
    assert tip.maximal_volume == expected


@pytest.mark.parametrize("model", [m for m in TIP_GEOMETRY_ESTIMATES if m.startswith("PRCXI")])
def test_two_tip_racks_have_unique_names_in_one_tree(model):
    from pylabrobot.resources import Coordinate
    from unilabos.devices.liquid_handling.prcxi import prcxi_labware

    first = getattr(prcxi_labware, model)(name="rack_a")
    second = getattr(prcxi_labware, model)(name="rack_b")
    assert first.get_item("A1").name == "rack_a_tipspot_A1"
    assert second.get_item("A1").name == "rack_b_tipspot_A1"
    root = Resource("deck", 500, 500, 100)
    root.assign_child_resource(first, location=Coordinate(0, 0, 0))
    root.assign_child_resource(second, location=Coordinate(150, 0, 0))
    names = [node.name for node in root.get_all_children()]
    assert len(names) == len(set(names))


def test_bioyond_tip_template_roundtrip():
    """BIOYOND 默认空枪头盒保留占位参数，不擅自补满枪头。"""
    from unilabos.resources.presets.bioyond.bottles import BIOYOND_PolymerStation_TipBox

    rack = BIOYOND_PolymerStation_TipBox("bioyond_tips")
    restored = Resource.deserialize(rack.serialize(), allow_marshal=True)
    restored.load_all_state(rack.serialize_all_state())
    assert restored.get_item("A1").metadata["geometry_validation"]["measured"] is False
    assert restored.get_item("A1").make_tip().maximal_volume == 1000
    assert not restored.get_item("A1").has_tip()


def test_metadata_is_independent():
    """不能通过修改一个实例的标记污染其他模板。"""
    first = tip_geometry_metadata("PRCXI_10uL_Tips")
    first["geometry_validation"]["measured"] = True
    assert tip_geometry_metadata("PRCXI_10uL_Tips")["geometry_validation"]["measured"] is False


@pytest.mark.parametrize("model", ["LaiYuTipRack200", "LaiYuTipRack1000"])
def test_laiyu_factories_create_distinct_named_tips(model, monkeypatch):
    """来与使用自己的容器，验证其孔位工厂而非假定它是 PLR TipRack。"""
    from unilabos.devices.liquid_handling.laiyu.core import laiyu_liquid_res

    monkeypatch.setattr(laiyu_liquid_res, "load_deck_config", lambda: {})
    rack = getattr(laiyu_liquid_res, model)("rack")
    assert len(rack.child_resources) == 96
    spot = next(iter(rack.child_resources.values()))["resource"]
    first, second = spot.make_tip(), spot.make_tip()
    assert first is not second
    assert first.name != second.name
    assert spot.metadata["geometry_validation"]["measured"] is False


def test_tipbox64_preserves_occupancy():
    """64 个占用孔位的枪头盒保持中间 32 孔为空。"""
    from unilabos.devices.workstation.coin_cell_assembly.YB_YH_materials import TipBox64

    rack = TipBox64("tips64")
    restored = Resource.deserialize(rack.serialize(), allow_marshal=True)
    restored.load_all_state(rack.serialize_all_state())
    occupied = [spot.has_tip() for spot in restored.get_all_items()]
    assert occupied == [True] * 32 + [False] * 32 + [True] * 32
    assert all(
        spot.metadata["geometry_validation"]["measured"] is False
        for spot in restored.get_all_items()
    )
