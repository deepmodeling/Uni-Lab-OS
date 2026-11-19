from __future__ import annotations

import pytest
from pylabrobot.resources import Coordinate, Resource, ResourceHolder

from unilabos.resources.presets.itemized_carrier import Bottle, ItemizedCarrier
from unilabos.resources.presets.warehouse import warehouse_factory


def _holder(name: str, x: float, y: float, z: float = 0) -> ResourceHolder:
    holder = ResourceHolder(
        name=name,
        size_x=10,
        size_y=10,
        size_z=10,
        child_location=Coordinate.zero(),
    )
    holder.location = Coordinate(x, y, z)
    return holder


def test_explicit_layout_is_not_overwritten_by_dimensions():
    carrier = ItemizedCarrier(
        name="layout_carrier",
        size_x=10,
        size_y=20,
        size_z=30,
        num_items_x=1,
        num_items_y=2,
        num_items_z=3,
        layout="row-major",
    )

    assert carrier.layout == "row-major"
    assert carrier.serialize()["layout"] == "row-major"


def test_layout_is_inferred_only_when_omitted():
    carrier = ItemizedCarrier(
        name="layout_carrier",
        size_x=10,
        size_y=20,
        size_z=30,
        num_items_x=1,
        num_items_y=2,
        num_items_z=3,
    )

    assert carrier.layout == "z-y"


def test_item_returns_holder_and_resource_is_the_occupant():
    holders = {
        "A1": _holder("holder_A1", 10, 20),
        "B1": _holder("holder_B1", 10, 40),
        "A2": _holder("holder_A2", 50, 20),
        "B2": _holder("holder_B2", 50, 40),
    }
    carrier = ItemizedCarrier(
        name="test_carrier",
        size_x=100,
        size_y=80,
        size_z=30,
        num_items_x=2,
        num_items_y=2,
        num_items_z=1,
        sites=holders,
    )
    bottle = Bottle("bottle", diameter=25, height=50, max_volume=15)

    carrier["B2"] = bottle

    assert carrier["B2"] is holders["B2"]
    assert carrier["B2"].resource is bottle
    assert bottle.parent is holders["B2"]
    assert carrier.get_resources() == [bottle]
    assert carrier.get_child_identifier(bottle) == {
        "identifier": "B2",
        "idx": 3,
        "x": 1,
        "y": 1,
        "z": 0,
    }
    assert carrier.get_child_identifier(holders["B2"])["identifier"] == "B2"

    carrier["B2"] = None

    assert carrier["B2"].resource is None
    assert carrier.get_resources() == []
    assert carrier["B2"] in carrier.get_free_sites()


def test_plr_roundtrip_keeps_holder_resource_and_site_coordinates():
    carrier = ItemizedCarrier(
        name="carrier",
        size_x=100,
        size_y=80,
        size_z=30,
        num_items_x=2,
        num_items_y=1,
        num_items_z=1,
        sites={
            "A1": _holder("holder_A1", 10, 20),
            "A2": _holder("holder_A2", 50, 20),
        },
    )
    bottle = Bottle("bottle", diameter=25, height=50, max_volume=15)
    carrier["A2"] = bottle

    restored = Resource.deserialize(carrier.serialize())

    assert isinstance(restored, ItemizedCarrier)
    assert isinstance(restored["A2"], ResourceHolder)
    assert restored["A2"].resource is not None
    assert restored["A2"].resource.name == "bottle"
    assert restored.get_child_identifier(restored["A2"].resource) == {
        "identifier": "A2",
        "idx": 1,
        "x": 1,
        "y": 0,
        "z": 0,
    }


@pytest.mark.parametrize(
    ("ordering_layout", "expected_labels"),
    [
        ("row-major", ["A01", "A02", "A03", "B01", "B02", "B03"]),
        ("col-major", ["A01", "B01", "A02", "B02", "A03", "B03"]),
    ],
)
def test_warehouse_xy_generation_matches_label_and_order(
    ordering_layout: str, expected_labels: list[str]
):
    warehouse = warehouse_factory(
        "warehouse",
        num_items_x=3,
        num_items_y=2,
        num_items_z=1,
        dx=10,
        dy=20,
        dz=30,
        item_dx=100,
        item_dy=10,
        item_dz=5,
        layout=ordering_layout,
    )

    assert list(warehouse._ordering) == expected_labels
    for label, holder in warehouse._ordering.items():
        identifier = warehouse.get_child_identifier(holder)
        assert identifier["identifier"] == label
        assert holder.location.x == 10 + identifier["x"] * 100
        expected_y = (
            20 + identifier["y"] * 10
            if ordering_layout == "row-major"
            else 20 + (1 - identifier["y"]) * 10
        )
        assert holder.location.y == expected_y
        assert identifier["z"] == 0


def test_warehouse_xyz_generation_and_removed_position_keep_grid_indices():
    warehouse = warehouse_factory(
        "warehouse",
        num_items_x=1,
        num_items_y=2,
        num_items_z=2,
        dx=10,
        dy=20,
        dz=30,
        item_dx=100,
        item_dy=10,
        item_dz=5,
        removed_positions=[1],
        layout="row-major",
    )

    assert list(warehouse._ordering) == ["A01", "A02", "B02"]
    assert warehouse.get_child_identifier(warehouse["A01"]) == {
        "identifier": "A01",
        "idx": 0,
        "x": 0,
        "y": 0,
        "z": 0,
    }
    assert warehouse.get_child_identifier(warehouse["B02"]) == {
        "identifier": "B02",
        "idx": 2,
        "x": 0,
        "y": 1,
        "z": 1,
    }
    assert warehouse.get_site_by_layer_position(row=1, col=0, layer=1) is warehouse[
        "B02"
    ]
    with pytest.raises(ValueError, match="已被移除"):
        warehouse.get_site_by_layer_position(row=1, col=0, layer=0)


def test_get_child_identifier_rejects_unassigned_resource():
    carrier = ItemizedCarrier(
        name="carrier",
        size_x=10,
        size_y=10,
        size_z=10,
        sites={"A1": _holder("holder_A1", 0, 0)},
    )
    bottle = Bottle("unassigned", diameter=1, height=1, max_volume=1)

    with pytest.raises(ValueError, match="is not assigned to this carrier"):
        carrier.get_child_identifier(bottle)
