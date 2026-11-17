"""设备归属取权威树上的最近设备身份，不要求给设备重复写绑定标记。"""

from types import SimpleNamespace

import pytest

from unilabos.resources.materials import EXTRA_BOUND_DEVICE, owner_device_of


def record(uuid, *, parent=None, resource_type="resource", resource_id=None, owner=None):
    return SimpleNamespace(material=SimpleNamespace(
        material_uuid=uuid, parent_material_uuid=parent, resource_type=resource_type,
        resource_id=resource_id or uuid, name=uuid, display_name=resource_id or uuid,
        extra={EXTRA_BOUND_DEVICE: owner} if owner else {},
    ))


def gateway(*records):
    lookup = {value.material.material_uuid: value for value in records}
    return SimpleNamespace(get_material=lookup.__getitem__)


def test_graph_device_needs_no_extra_owner_marker():
    gw = gateway(record("deck", parent="device", owner="stale"),
                 record("device", resource_type="device", resource_id="material_bench"))
    assert owner_device_of({"uuid": "deck"}, gateway=gw) == "material_bench"


def test_closest_subdevice_owns_material_not_workstation_root():
    gw = gateway(record("well", parent="plate"), record("plate", parent="pump"),
                 record("pump", parent="station", resource_type="device", resource_id="pump-2"),
                 record("station", resource_type="device", resource_id="workstation"))
    assert owner_device_of({"uuid": "well"}, gateway=gw) == "pump-2"


def test_transfer_follows_new_parent_even_if_old_marker_remains():
    gw = gateway(record("plate", parent="device-b", owner="device-a"),
                 record("device-b", resource_type="device", resource_id="new-device"))
    assert owner_device_of({"uuid": "plate"}, gateway=gw) == "new-device"


def test_explicitly_bound_standalone_root_still_resolves():
    gw = gateway(record("plate", parent="deck"), record("deck", owner="remote-bench"))
    assert owner_device_of({"uuid": "plate"}, gateway=gw) == "remote-bench"


def test_unowned_root_and_cycles_are_rejected():
    with pytest.raises(ValueError, match="未登记所属设备"):
        owner_device_of({"uuid": "plate"}, gateway=gateway(record("plate")))
    with pytest.raises(ValueError, match="成环"):
        owner_device_of({"uuid": "plate"}, gateway=gateway(record("plate", parent="deck"), record("deck", parent="plate")))
