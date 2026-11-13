"""原生资源族及特殊 state 边界的往返回归。"""

from types import SimpleNamespace

import pytest
from pylabrobot.resources import Coordinate, Resource

from unilabos.resources.plr_identity import material_children, material_serialization
from unilabos.resources.resource_tracker import ResourceTreeSet


def test_business_head_fields_are_not_tip_channels():
    class BusinessResource(Resource):
        def serialize_state(self):
            return {"head_state": {"status": "ready"}}

    root = BusinessResource("business", 10, 10, 10)
    root.head = {0: SimpleNamespace(_pending_tip=Resource("not_a_tip", 1, 1, 1))}
    assert material_children(root) == []
    data, states = material_serialization(root)
    assert data["children"] == []
    assert states["business"] == {"head_state": {"status": "ready"}}


@pytest.mark.parametrize("kind", ["holder", "stack", "petri", "tube", "well"])
def test_native_resource_families_roundtrip(kind, monkeypatch):
    from pylabrobot.resources import ResourceHolder, ResourceStack, PetriDish, Tube, Well

    if kind == "holder":
        child = ResourceHolder("holder", 50, 50, 20, child_location=Coordinate(2, 3, 4))
        child.assign_child_resource(Resource("payload", 10, 10, 10))
    elif kind == "stack":
        child = ResourceStack("stack", direction="z", resources=[Resource("payload", 10, 10, 10)])
    elif kind == "petri":
        child = PetriDish("petri", diameter=90, height=15)
    else:
        child = {"tube": Tube, "well": Well}[kind](kind, size_x=10, size_y=10, size_z=20, max_volume=1000)
    root = Resource("root", 200, 200, 200, category="resource")
    root.location = Coordinate(11, 22, 33)
    root.assign_child_resource(child, location=Coordinate(4, 5, 6))
    if hasattr(child, "tracker"):
        child.tracker.set_volume(12)
    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    tree = ResourceTreeSet.from_plr_resources([root], known_random_uuid=True)
    restored = ResourceTreeSet.load(tree.dump()).to_plr_resources(skip_devices=False)[0]
    assert restored.location == root.location
    assert restored.children[0].location == child.location
    assert type(restored.children[0]) is type(child)
    assert restored.children[0].unilabos_uuid == child.unilabos_uuid
    assert restored.children[0].get_size_x() == child.get_size_x()
    assert restored.children[0].get_size_z() == child.get_size_z()
    if hasattr(child, "tracker"):
        assert restored.children[0].tracker.get_used_volume() == 12
    if child.children:
        assert restored.children[0].children[0].location == child.children[0].location
        assert restored.children[0].children[0].unilabos_uuid == child.children[0].unilabos_uuid


def _bottle_carrier(name: str):
    """2x3 瓶架，每个位点放一只按载架名前缀命名的瓶子（厂商预设的通用形态）。"""
    from pylabrobot.resources import ResourceHolder
    from pylabrobot.resources.utils import create_ordered_items_2d

    from unilabos.resources.presets.itemized_carrier import Bottle, BottleCarrier

    sites = create_ordered_items_2d(
        name_prefix=name, klass=ResourceHolder, num_items_x=3, num_items_y=2,
        dx=10.0, dy=10.0, dz=5.0, item_dx=40.0, item_dy=35.0,
        size_x=20.0, size_y=20.0, size_z=50.0,
    )
    carrier = BottleCarrier(name=name, size_x=127.8, size_y=85.5, size_z=50.0, sites=sites)
    for index, label in enumerate(["A1", "B1", "A2", "B2", "A3", "B3"]):
        carrier[index] = Bottle(f"{name}_{label}", diameter=20.0, height=60.0, max_volume=30000.0)
    return carrier


@pytest.mark.parametrize("kind", ["bottle_carrier", "warehouse", "plate", "tip_rack"])
def test_two_presets_can_share_one_tree(kind):
    from tests.plr_fixtures import plate, tip_rack
    from unilabos.resources.presets.warehouse import warehouse_factory

    factories = {
        "bottle_carrier": _bottle_carrier,
        "warehouse": lambda name: warehouse_factory(name, num_items_x=2, num_items_y=2, num_items_z=1),
        "plate": plate,
        "tip_rack": tip_rack,
    }
    root = Resource("bench", 1000, 1000, 1000)
    for i, name in enumerate(("first", "second")):
        root.assign_child_resource(factories[kind](name), Coordinate(i * 200, 0, 0))
    names = [node.name for node in root.get_all_children()]
    assert len(names) == len(set(names))
