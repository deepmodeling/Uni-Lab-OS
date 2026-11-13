"""自定义枪头占位几何和未实测标记的兼容回归。"""

import pytest
from pylabrobot.resources import Resource, Tip, TipRack, TipSpot
from pylabrobot.resources.utils import create_ordered_items_2d

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


def _unmeasured_tip_rack(name: str, model: str, volume: float, *, with_tips: bool = True) -> TipRack:
    """驱动包定义枪头盒的通用形态：孔位带未实测标记，枪头按型号占位几何创建。"""
    spots = create_ordered_items_2d(
        TipSpot, name_prefix=name, num_items_x=12, num_items_y=8,
        dx=8.0, dy=5.0, dz=2.0, item_dx=9.0, item_dy=9.0,
        size_x=7.0, size_y=7.0, size_z=0,
        metadata=tip_geometry_metadata(model),
        make_tip=lambda tip_name: make_unmeasured_tip(
            tip_name, model=model, volume=volume, length=60, fitting_depth=8,
        ),
    )
    return TipRack(
        name=name, size_x=122.0, size_y=85.5, size_z=58.0,
        ordered_items=spots, model=model, with_tips=with_tips,
    )


@pytest.mark.parametrize("model", TIP_GEOMETRY_ESTIMATES)
def test_unmeasured_tip_rack_roundtrip(model):
    """枪头盒序列化往返后保留未实测标记与枪头容量。"""
    rack = _unmeasured_tip_rack("audit_" + model, model, 300)
    restored = Resource.deserialize(rack.serialize(), allow_marshal=True)
    restored.load_all_state(rack.serialize_all_state())
    spot = restored.get_item("A1")
    assert spot.metadata["geometry_validation"]["measured"] is False
    assert spot.has_tip()
    assert spot.make_tip().maximal_volume == 300


def test_two_tip_racks_have_unique_names_in_one_tree():
    from pylabrobot.resources import Coordinate

    model = next(iter(TIP_GEOMETRY_ESTIMATES))
    first = _unmeasured_tip_rack("rack_a", model, 300)
    second = _unmeasured_tip_rack("rack_b", model, 300)
    assert first.get_item("A1").name == "rack_a_tipspot_A1"
    assert second.get_item("A1").name == "rack_b_tipspot_A1"
    root = Resource("deck", 500, 500, 100)
    root.assign_child_resource(first, location=Coordinate(0, 0, 0))
    root.assign_child_resource(second, location=Coordinate(150, 0, 0))
    names = [node.name for node in root.get_all_children()]
    assert len(names) == len(set(names))


def test_empty_tip_rack_is_not_filled_on_roundtrip():
    """默认空的枪头盒保留占位参数，不擅自补满枪头。"""
    model = next(iter(TIP_GEOMETRY_ESTIMATES))
    rack = _unmeasured_tip_rack("empty_tips", model, 1000, with_tips=False)
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


def test_partially_filled_tip_rack_preserves_occupancy():
    """部分孔位有枪头的枪头盒往返后保持占用分布（中间 32 孔为空）。"""
    model = next(iter(TIP_GEOMETRY_ESTIMATES))
    rack = _unmeasured_tip_rack("tips64", model, 1000, with_tips=False)
    rack.set_tip_state([True] * 32 + [False] * 32 + [True] * 32)
    restored = Resource.deserialize(rack.serialize(), allow_marshal=True)
    restored.load_all_state(rack.serialize_all_state())
    occupied = [spot.has_tip() for spot in restored.get_all_items()]
    assert occupied == [True] * 32 + [False] * 32 + [True] * 32
    assert all(
        spot.metadata["geometry_validation"]["measured"] is False
        for spot in restored.get_all_items()
    )
