from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pylabrobot.resources import Coordinate, Resource, Rotation
from pylabrobot.resources.barcode import Barcode
from pylabrobot import serializer as plr_serializer

from unilabos.resources.presets.container import RegularContainer
from unilabos.resources.objects.joint_state import ResourceJointState
from unilabos.resources.objects.resource import RESOURCE_ROOT_FIELDS
from unilabos.resources.objects.pose import (
    ResourceDictPosition,
    ResourceDictPositionObject,
    ResourceDictPositionSize,
)
from unilabos.resources.resource_tracker import (
    EXTRA_RESOURCE_CLASS,
    EXTRA_RESOURCE_JOINT_STATE,
    EXTRA_RESOURCE_META_DATA,
    EXTRA_RESOURCE_POSE,
    TRACKER_STATE_KEYS,
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeSet,
    assemble_tracker_state,
)


def _resource_payload(**overrides):
    payload = {
        "id": "beaker",
        "uuid": str(uuid4()),
        "name": "beaker",
        "type": "container",
        "class": "",
        "config": {"type": "RegularContainer"},
        "data": {},
        "extra": {},
    }
    payload.update(overrides)
    return payload


def test_from_plr_resources_only_generates_missing_uuids_for_tests():
    root = Resource(name="test_root", size_x=10, size_y=20, size_z=30)
    child = Resource(name="test_child", size_x=1, size_y=2, size_z=3)
    root.assign_child_resource(child, location=Coordinate.zero())

    with pytest.raises(ValueError, match="缺少微后端分配的 UUID"):
        ResourceTreeSet.from_plr_resources([root])

    assert not getattr(root, "unilabos_uuid", "")
    assert not getattr(child, "unilabos_uuid", "")

    tree = ResourceTreeSet.from_plr_resources([root], known_random_uuid=True)

    assert UUID(root.unilabos_uuid)
    assert UUID(child.unilabos_uuid)
    assert tree.root_nodes[0].res_content.uuid == root.unilabos_uuid
    assert tree.root_nodes[0].children[0].res_content.uuid == child.unilabos_uuid


def test_graphio_accepts_sparse_plr_serialization(monkeypatch):
    graphio = pytest.importorskip(
        "unilabos.resources.graphio",
        reason="GraphIO 依赖 ROS Jazzy 生成的 unilabos_msgs",
        exc_type=ImportError,
    )
    resource = RegularContainer(
        name="sparse_root",
        size_x=10,
        size_y=20,
        size_z=30,
        max_volume=100.0,
    )
    resource.unilabos_uuid = str(uuid4())
    original_serialize = resource.serialize

    def serialize_without_default_geometry():
        serialized = original_serialize()
        for key in ("location", "rotation", "children", "parent_name"):
            serialized.pop(key, None)
        return serialized

    monkeypatch.setattr(resource, "serialize", serialize_without_default_geometry)

    graph_payload = graphio.resource_plr_to_ulab(resource)
    assert graph_payload["children"] == []
    graph_resource = ResourceDict.model_validate(graph_payload)
    assert graph_resource.pose.position is None
    assert graph_resource.pose.rotation.model_dump() == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert graph_resource.parent is None


def test_tracker_state_is_promoted_from_data_to_root():
    resource = ResourceDict.model_validate(
        _resource_payload(
            data={
                "thing": "beaker_volume_tracker",
                "max_volume": 100.0,
                "liquids": [["water", 30.0, "ul"]],
                "liquid_history": [["water", 30.0, "ul"]],
                "unknown_counter": 0,
            }
        )
    )

    assert resource.liquids == [("water", 30.0, "ul")]
    assert resource.liquid_history == [("water", 30.0, "ul")]
    assert resource.unknown_counter == 0
    assert resource.data == {"thing": "beaker_volume_tracker", "max_volume": 100.0}
    assert set(TRACKER_STATE_KEYS) <= set(RESOURCE_ROOT_FIELDS)


def test_root_tracker_state_wins_and_roundtrips_to_plr_shape():
    resource = ResourceDict.model_validate(
        _resource_payload(
            data={
                "max_volume": 100.0,
                "liquids": [["stale", 1.0, "ul"]],
                "liquid_history": [["stale", 1.0, "ul"]],
                "unknown_counter": 9,
            },
            liquids=[],
            liquid_history=[],
            unknown_counter=0,
        )
    )

    assert resource.liquids == []
    assert resource.liquid_history == []
    assert resource.unknown_counter == 0
    assert assemble_tracker_state(resource) == {
        "max_volume": 100.0,
        "substances": [],
        "liquid_history": [],
        "unknown_counter": 0,
    }

    nested = ResourceDictInstance(resource).get_plr_nested_dict()
    assert nested["data"] == assemble_tracker_state(resource)
    assert all(state_key not in nested for state_key in TRACKER_STATE_KEYS)


def test_non_container_keeps_tracker_roots_none():
    resource = ResourceDict.model_validate(
        _resource_payload(type="tip_spot", data={"tip_state": {"has_tip": True}})
    )

    assert resource.liquids is None
    assert resource.liquid_history is None
    assert resource.unknown_counter is None
    assert assemble_tracker_state(resource) == {"tip_state": {"has_tip": True}}


def test_plr_container_tracker_state_survives_resource_tree_roundtrip(monkeypatch):
    container = RegularContainer(
        name="beaker",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100.0,
    )
    container.barcode = Barcode(
        data="BC-001",
        symbology="code128",
        position_on_resource="front",
    )
    container.tracker.add_liquid("water", 50.0)
    container.tracker.add_liquid(None, 10.0)
    container.tracker.remove_liquid(20.0)
    container.unilabos_uuid = str(uuid4())
    container.unilabos_extra = {
        EXTRA_RESOURCE_CLASS: "BeakerTemplate",
        EXTRA_RESOURCE_META_DATA: {"vendor": {"lot": "A-1"}},
    }

    tree = ResourceTreeSet.from_plr_resources([container])
    root = tree.root_nodes[0].res_content
    original_state = container.serialize_state()

    expected_substances = [
        (item[0], item[1], item[2] if len(item) >= 3 else "ul")
        for item in original_state["substances"]
    ]
    expected_history = [
        (item[0], item[1], item[2] if len(item) >= 3 else "ul")
        for item in original_state["liquid_history"]
    ]
    assert root.substances == expected_substances
    assert root.liquids == [
        item for item in expected_substances if item[2] == "ul"
    ]
    assert root.liquid_history == expected_history
    assert root.unknown_counter == original_state["unknown_counter"]
    assert all(state_key not in root.data for state_key in TRACKER_STATE_KEYS)
    assert root.barcode == "BC-001"
    assert root.barcode_symbology == "code128"
    assert "barcode" not in root.config
    assert root.template_name == "BeakerTemplate"
    assert EXTRA_RESOURCE_CLASS not in root.extra
    assert root.meta_data == {"vendor": {"lot": "A-1"}}
    assert EXTRA_RESOURCE_META_DATA not in root.extra

    # pose.position 是当前实际位置的唯一真相；其余几何字段通过 sidecar 往返。
    root.pose = ResourceDictPosition(
        position=ResourceDictPositionObject(x=40, y=50, z=60),
        position3d=ResourceDictPositionObject(x=10, y=20, z=30),
        size=ResourceDictPositionSize(width=10, height=10, depth=20),
    )

    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    original_deserialize = plr_serializer.deserialize

    def deserialize_geometry_without_backend_scan(value, allow_marshal=False):
        if isinstance(value, dict) and value.get("type") == "Coordinate":
            return Coordinate(value["x"], value["y"], value["z"])
        if isinstance(value, dict) and value.get("type") == "Rotation":
            return Rotation(value["x"], value["y"], value["z"])
        return original_deserialize(value, allow_marshal=allow_marshal)

    monkeypatch.setattr(plr_serializer, "deserialize", deserialize_geometry_without_backend_scan)
    restored = tree.to_plr_resources(skip_devices=False)[0]
    assert restored.serialize_state() == original_state
    assert restored.barcode is not None
    assert restored.barcode.serialize() == {
        "data": "BC-001",
        "symbology": "code128",
        "position_on_resource": "front",
    }
    assert restored.unilabos_extra[EXTRA_RESOURCE_CLASS] == "BeakerTemplate"
    assert restored.unilabos_extra[EXTRA_RESOURCE_META_DATA] == {
        "vendor": {"lot": "A-1"}
    }
    assert restored.location == Coordinate(40, 50, 60)
    assert "position" not in restored.unilabos_extra[EXTRA_RESOURCE_POSE]

    roundtripped = ResourceTreeSet.from_plr_resources(
        [restored]
    ).root_nodes[0].res_content
    assert not hasattr(roundtripped, "position")
    assert roundtripped.pose.position.model_dump() == {"x": 40.0, "y": 50.0, "z": 60.0}
    assert roundtripped.pose.position3d.model_dump() == {"x": 10.0, "y": 20.0, "z": 30.0}
    assert roundtripped.barcode == "BC-001"
    assert roundtripped.barcode_symbology == "code128"
    assert roundtripped.meta_data == {"vendor": {"lot": "A-1"}}


def test_barcode_config_is_promoted_once_and_conflicts_are_rejected():
    resource = ResourceDict.model_validate(
        _resource_payload(
            config={
                "type": "RegularContainer",
                "barcode": {
                    "data": "BC-002",
                    "symbology": "qr",
                    "position_on_resource": "left",
                },
            }
        )
    )

    assert resource.barcode == "BC-002"
    assert resource.barcode_symbology == "qr"
    assert "barcode" not in resource.config

    with pytest.raises(ValueError, match="barcode.*冲突"):
        ResourceDict.model_validate(
            _resource_payload(
                barcode="ROOT",
                config={
                    "type": "RegularContainer",
                    "barcode": {
                        "data": "CONFIG",
                        "symbology": "code128",
                    },
                },
            )
        )


def test_plr_resource_without_location_preserves_unknown_pose_position(monkeypatch):
    container = RegularContainer(
        name="unlocated_beaker",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100.0,
    )
    container.unilabos_uuid = str(uuid4())
    container.unilabos_extra = {EXTRA_RESOURCE_CLASS: "BeakerTemplate"}
    assert container.location is None

    tree = ResourceTreeSet.from_plr_resources([container])
    assert tree.root_nodes[0].res_content.pose.position is None
    assert tree.root_nodes[0].get_plr_nested_dict()["location"] is None

    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    original_deserialize = plr_serializer.deserialize

    def deserialize_geometry_without_backend_scan(value, allow_marshal=False):
        if isinstance(value, dict) and value.get("type") == "Coordinate":
            return Coordinate(value["x"], value["y"], value["z"])
        if isinstance(value, dict) and value.get("type") == "Rotation":
            return Rotation(value["x"], value["y"], value["z"])
        return original_deserialize(value, allow_marshal=allow_marshal)

    monkeypatch.setattr(plr_serializer, "deserialize", deserialize_geometry_without_backend_scan)
    restored = tree.to_plr_resources(skip_devices=False)[0]
    assert restored.location is None

    roundtripped = ResourceTreeSet.from_plr_resources(
        [restored]
    ).root_nodes[0].res_content
    assert roundtripped.pose.position is None


def test_joint_state_uses_plr_runtime_sidecar_without_entering_serialize(monkeypatch):
    container = RegularContainer(
        name="jointed_beaker",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100.0,
    )
    container.unilabos_uuid = str(uuid4())
    container.unilabos_extra = {EXTRA_RESOURCE_CLASS: "BeakerTemplate"}
    tree = ResourceTreeSet.from_plr_resources([container])
    root = tree.root_nodes[0].res_content
    root.joint_state = ResourceJointState(
        name=["joint_1", "joint_2"],
        position=[1.25, -0.5],
        velocity=[0.1, 0.2],
        effort=[],
    )

    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    restored = tree.to_plr_resources(skip_devices=False)[0]

    assert "joint_state" not in restored.serialize()
    assert EXTRA_RESOURCE_JOINT_STATE not in restored.serialize()
    assert restored.unilabos_extra[EXTRA_RESOURCE_JOINT_STATE] == {
        "name": ["joint_1", "joint_2"],
        "position": [1.25, -0.5],
        "velocity": [0.1, 0.2],
        "effort": [],
    }

    roundtripped = ResourceTreeSet.from_plr_resources(
        [restored]
    ).root_nodes[0].res_content
    assert roundtripped.joint_state is not None
    assert roundtripped.joint_state.position == [1.25, -0.5]
    assert EXTRA_RESOURCE_JOINT_STATE not in roundtripped.extra


def test_joint_state_rejects_mismatched_arrays():
    with pytest.raises(ValueError, match="velocity.*长度"):
        ResourceDict.model_validate(
            _resource_payload(
                joint_state={
                    "name": ["joint_1", "joint_2"],
                    "position": [1.0, 2.0],
                    "velocity": [0.1],
                    "effort": [],
                }
            )
        )


@pytest.mark.parametrize("legacy_source", ["config", "data"])
def test_resource_tree_set_missing_metadata_sidecar_allows_legacy_promotion(
    monkeypatch, legacy_source
):
    container = RegularContainer(
        name=f"legacy_{legacy_source}_beaker",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100.0,
    )
    container.unilabos_uuid = str(uuid4())
    container.unilabos_extra = {EXTRA_RESOURCE_CLASS: "BeakerTemplate"}
    legacy_meta_data = {"vendor": {"lot": f"legacy-{legacy_source}"}}

    if legacy_source == "config":
        original_serialize = container.serialize

        def serialize_with_legacy_meta_data():
            serialized = original_serialize()
            serialized["meta_data"] = legacy_meta_data
            return serialized

        monkeypatch.setattr(container, "serialize", serialize_with_legacy_meta_data)
    else:
        original_serialize_state = container.serialize_state

        def serialize_state_with_legacy_meta_data():
            return {
                **original_serialize_state(),
                "meta_data": legacy_meta_data,
            }

        monkeypatch.setattr(
            container,
            "serialize_state",
            serialize_state_with_legacy_meta_data,
        )

    tree_resource = ResourceTreeSet.from_plr_resources(
        [container]
    ).root_nodes[0].res_content
    assert tree_resource.meta_data == legacy_meta_data
    assert "meta_data" not in tree_resource.config
    assert "meta_data" not in tree_resource.data


@pytest.mark.parametrize("legacy_source", ["config", "data"])
def test_graphio_plr_missing_metadata_sidecar_allows_legacy_promotion(
    monkeypatch, legacy_source
):
    graphio = pytest.importorskip(
        "unilabos.resources.graphio",
        reason="GraphIO 依赖 ROS Jazzy 生成的 unilabos_msgs",
        exc_type=ImportError,
    )
    container = RegularContainer(
        name=f"legacy_graphio_{legacy_source}_beaker",
        size_x=10,
        size_y=10,
        size_z=20,
        max_volume=100.0,
    )
    container.unilabos_uuid = str(uuid4())
    container.unilabos_extra = {EXTRA_RESOURCE_CLASS: "BeakerTemplate"}
    legacy_meta_data = {"vendor": {"lot": f"legacy-{legacy_source}"}}

    if legacy_source == "config":
        original_serialize = container.serialize

        def serialize_with_legacy_meta_data():
            serialized = original_serialize()
            serialized["meta_data"] = legacy_meta_data
            return serialized

        monkeypatch.setattr(container, "serialize", serialize_with_legacy_meta_data)
    else:
        original_serialize_state = container.serialize_state

        def serialize_state_with_legacy_meta_data():
            return {
                **original_serialize_state(),
                "meta_data": legacy_meta_data,
            }

        monkeypatch.setattr(
            container,
            "serialize_state",
            serialize_state_with_legacy_meta_data,
        )

    graph_payload = graphio.resource_plr_to_ulab(container)
    assert "meta_data" not in graph_payload
    graph_resource = ResourceDict.model_validate(graph_payload)
    assert graph_resource.meta_data == legacy_meta_data
    assert "meta_data" not in graph_resource.config
    assert "meta_data" not in graph_resource.data
