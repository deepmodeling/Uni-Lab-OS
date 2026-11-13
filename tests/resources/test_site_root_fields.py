from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pylabrobot.resources import Carrier, Coordinate, Resource, ResourceHolder

from unilabos.resources.presets.itemized_carrier import ItemizedCarrier
from unilabos.resources.objects.pose import ResourceDictPositionObject
from unilabos.resources.objects.resource import ResourceDict
from unilabos.resources.objects.site import ResourceSite
from unilabos.resources.objects.site import normalize_available_sites
from unilabos.resources.resource_tracker import (
    EXTRA_RESOURCE_CLASS,
    EXTRA_SITES,
    ResourceTreeSet,
    apply_plr_site_metadata,
    extract_plr_sites,
    merge_resource_sites,
    prepare_resource_creation_payloads,
    prepare_resource_tree_for_creation,
    replace_resource_uuids_in_tree,
    set_plr_template_name,
    sites_for_plr_deserialization,
)


def _resource_payload(**overrides):
    payload = {
        "id": "carrier",
        "uuid": str(uuid4()),
        "name": "carrier",
        "type": "carrier",
        "class": "",
        "config": {"type": "StrictCarrier"},
        "data": {},
        "extra": {},
        "pose": {"position": {"x": 1, "y": 2, "z": 3}},
    }
    payload.update(overrides)
    return payload


def _site_payload(owner_uuid: str, template_name: str = "StrictCarrier", **overrides):
    payload = {
        "schema_version": 1,
        "uuid": str(uuid4()),
        "template_name": template_name,
        "material_uuid": owner_uuid,
        "index": "A1",
        "label": "A1",
        "occupied_material_uuid": None,
        "pose": {
            "position": {"x": 10, "y": 20, "z": 0},
            "position3d": {"x": 10, "y": 20, "z": 30},
        },
    }
    payload.update(overrides)
    return payload


def test_runtime_create_response_preserves_canonical_pose_and_site_identity():
    owner_uuid = str(uuid4())
    site = _site_payload(owner_uuid)
    raw = _resource_payload(
        uuid=owner_uuid,
        template_name="StrictCarrier",
        pose={"position": {"x": 40, "y": 50, "z": 60}},
        sites=[site],
        sites_initialized=True,
        children=[],
    )

    _, prepared = prepare_resource_creation_payloads([raw])
    created = prepared[0]
    assert "position" not in created
    assert created["pose"]["position"] == {"x": 40.0, "y": 50.0, "z": 60.0}
    assert created["sites"][0]["material_uuid"] == created["uuid"]
    assert created["sites_initialized"] is True
    site_uuid = created["sites"][0]["uuid"]
    assert site_uuid == site["uuid"]

    _, prepared_again = prepare_resource_creation_payloads(prepared)
    assert prepared_again[0]["uuid"] == created["uuid"]
    assert prepared_again[0]["sites"][0]["uuid"] == site_uuid


def test_instance_payload_discards_available_sites_without_instantiating_them():
    raw = _resource_payload(
        template_name="StrictCarrier",
        available_sites=[
            {"index": "A1", "label": "A1", "allowed_resource_categories": ["tube"]}
        ],
        sites=[],
        sites_initialized=True,
        children=[],
    )

    tree, prepared = prepare_resource_creation_payloads([raw])

    resource = tree.root_nodes[0].res_content
    assert resource.sites_initialized is True
    assert resource.sites == []
    assert "available_sites" not in resource.model_dump()
    assert "available_sites" not in prepared[0]


def test_startup_material_tree_must_already_have_authoritative_site_snapshot():
    tree = ResourceTreeSet.from_raw_dict_list(
        [
            _resource_payload(
                available_sites=[{"index": 0, "label": "slot-0"}],
                sites=[],
                sites_initialized=True,
            )
        ]
    )

    assert prepare_resource_tree_for_creation(tree) == 1

    startup_resource = tree.dump()[0][0]
    assert startup_resource["sites_initialized"] is True
    assert startup_resource["sites"] == []
    assert "available_sites" not in startup_resource

    pending = ResourceTreeSet.from_raw_dict_list(
        [_resource_payload(sites=None, sites_initialized=False)]
    )
    with pytest.raises(ValueError, match="尚未由微后端"):
        prepare_resource_tree_for_creation(pending)


def test_offline_uuid_migration_updates_tree_and_site_references_atomically():
    owner_uuid = str(uuid4())
    occupant_uuid = str(uuid4())
    site_uuid = str(uuid4())
    tree = ResourceTreeSet.from_raw_dict_list(
        [
            _resource_payload(
                uuid=owner_uuid,
                template_name="StrictCarrier",
                sites_initialized=True,
                sites=[
                    {
                        "schema_version": 1,
                        "uuid": site_uuid,
                        "template_name": "StrictCarrier",
                        "material_uuid": owner_uuid,
                        "index": "A1",
                        "label": "A1",
                        "occupied_material_uuid": occupant_uuid,
                    }
                ],
            ),
            _resource_payload(
                id="tube-1",
                uuid=occupant_uuid,
                name="tube-1",
                type="tube",
                config={"type": "Tube"},
                parent_uuid=owner_uuid,
            ),
        ]
    )
    migrated_owner_uuid = str(uuid4())
    migrated_occupant_uuid = str(uuid4())

    assert (
        tree.replace_resource_uuids(
            {owner_uuid: migrated_owner_uuid, occupant_uuid: migrated_occupant_uuid}
        )
        == 2
    )

    owner = tree.find_by_uuid(migrated_owner_uuid)
    occupant = tree.find_by_uuid(migrated_occupant_uuid)
    assert owner is not None and occupant is not None
    assert occupant.res_content.parent_uuid == migrated_owner_uuid
    assert occupant.res_content.parent is owner.res_content
    assert owner.res_content.sites is not None
    site = owner.res_content.sites[0]
    assert site.uuid == site_uuid
    assert site.material_uuid == migrated_owner_uuid
    assert site.occupied_material_uuid == migrated_occupant_uuid

    before = tree.dump()
    with pytest.raises(ValueError, match="重复资源 UUID"):
        tree.replace_resource_uuids({migrated_owner_uuid: migrated_occupant_uuid})
    assert tree.dump() == before


def test_allowed_resource_categories_are_frontend_metadata_not_tree_enforcement():
    owner_uuid = str(uuid4())
    occupant_uuid = str(uuid4())

    tree = ResourceTreeSet.from_raw_dict_list(
        [
            _resource_payload(
                uuid=owner_uuid,
                template_name="StrictCarrier",
                sites_initialized=True,
                sites=[
                    _site_payload(
                        owner_uuid,
                        occupied_material_uuid=occupant_uuid,
                        allowed_resource_categories=["plate"],
                    )
                ],
            ),
            _resource_payload(
                id="tube-1",
                uuid=occupant_uuid,
                name="tube-1",
                type="tube",
                config={"type": "Tube"},
                template_name="TubeTemplate",
                parent_uuid=owner_uuid,
            ),
        ]
    )

    owner = tree.find_by_uuid(owner_uuid)
    assert owner is not None
    assert owner.res_content.sites is not None
    assert owner.res_content.sites[0].allowed_resource_categories == ["plate"]


def test_plr_offline_uuid_migration_updates_site_sidecar_references():
    owner_uuid = str(uuid4())
    occupant_uuid = str(uuid4())
    migrated_owner_uuid = str(uuid4())
    migrated_occupant_uuid = str(uuid4())
    site_payload = {
        "material_uuid": owner_uuid,
        "occupied_material_uuid": occupant_uuid,
    }
    resource = {
        "data": {"unilabos_uuid": owner_uuid},
        "sites": [deepcopy(site_payload)],
        "extra": {EXTRA_SITES: {"A1": deepcopy(site_payload)}},
        "children": [
            {
                "data": {"unilabos_uuid": occupant_uuid},
                "parent_uuid": owner_uuid,
                "extra": {},
                "children": [],
            }
        ],
    }

    assert (
        replace_resource_uuids_in_tree(
            resource,
            {owner_uuid: migrated_owner_uuid, occupant_uuid: migrated_occupant_uuid},
        )
        == 2
    )

    assert resource["uuid"] == migrated_owner_uuid
    assert resource["data"]["unilabos_uuid"] == migrated_owner_uuid
    assert resource["children"][0]["uuid"] == migrated_occupant_uuid
    assert resource["children"][0]["data"]["unilabos_uuid"] == migrated_occupant_uuid
    assert resource["children"][0]["parent_uuid"] == migrated_owner_uuid
    assert resource["sites"][0] == {
        "material_uuid": migrated_owner_uuid,
        "occupied_material_uuid": migrated_occupant_uuid,
    }
    assert resource["extra"][EXTRA_SITES]["A1"] == resource["sites"][0]


def test_flat_site_geometry_is_promoted_to_shared_pose():
    owner_uuid = str(uuid4())
    occupant_uuid = str(uuid4())
    resource = ResourceDict.model_validate(
        _resource_payload(
            uuid=owner_uuid,
            config={
                "type": "StrictCarrier",
                "sites": [
                    {
                        "uuid": str(uuid4()),
                        "template_name": "StrictCarrier",
                        "material_uuid": owner_uuid,
                        "index": "A1",
                        "label": "A1",
                        "occupied_material_uuid": occupant_uuid,
                        "position": {"x": 10, "y": 20, "z": 30},
                        "size": {"width": 40, "height": 50, "depth": 60},
                        "rotation": {"x": 1, "y": 2, "z": 3},
                        "allowed_resource_categories": ["plate", "plate"],
                        "vendor_extension": {"rack": 7},
                    }
                ],
            },
        )
    )

    dumped = resource.model_dump(by_alias=True)
    assert "sites" not in dumped["config"]
    assert dumped["config"]["type"] == "StrictCarrier"
    assert "position" not in dumped
    assert dumped["pose"]["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    site = dumped["sites"][0]
    assert site["template_name"] == "StrictCarrier"
    assert site["material_uuid"] == owner_uuid
    assert site["occupied_material_uuid"] == occupant_uuid
    assert site["pose"]["position"] == {"x": 10.0, "y": 20.0, "z": 30.0}
    assert site["pose"]["position3d"] == {"x": 10.0, "y": 20.0, "z": 30.0}
    assert site["pose"]["size"] == {"width": 40.0, "height": 50.0, "depth": 60.0}
    assert site["pose"]["rotation"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert site["allowed_resource_categories"] == ["plate"]
    assert site["meta_data"]["legacy_fields"]["vendor_extension"] == {"rack": 7}
    assert {
        "position_x",
        "position_y",
        "position_z",
        "width",
        "length",
        "depth",
    }.isdisjoint(site)
    assert "occupied_by" not in site
    assert dumped["sites_initialized"] is True


def test_bundled_experiment_without_strict_identity_is_rejected_at_edge_import():
    # 内置图已升级为新契约（全节点带 UUID）；此处剥离 uuid 构造非法变体，
    # 验证 strict import 仍拒绝缺身份的图。
    experiment_path = (
        Path(__file__).resolve().parents[2]
        / "unilabos"
        / "test"
        / "experiments"
        / "plr_test_converted_slim.json"
    )
    raw = json.loads(experiment_path.read_text(encoding="utf-8"))
    nodes = deepcopy(raw["nodes"])
    for node in nodes:
        node.pop("uuid", None)
        (node.get("data") or {}).pop("unilabos_uuid", None)

    with pytest.raises((ValueError, ValidationError), match="UUID|Field required"):
        ResourceTreeSet.from_raw_dict_list(nodes)


def test_registry_available_sites_normalizes_flat_geometry_without_entering_instance():
    definitions = normalize_available_sites(
        [
            {
                "index": "A1",
                "label": "A1",
                "position_x": 1,
                "width": 10,
                "length": 20,
            }
        ]
    )
    assert definitions[0]["pose"]["position3d"]["x"] == 1
    assert definitions[0]["pose"]["size"]["height"] == 20

    resource = ResourceDict.model_validate(
        _resource_payload(available_sites=definitions, sites=[], sites_initialized=True)
    )
    assert "available_sites" not in resource.model_dump()


def test_sites_initialized_state_machine_preserves_authoritative_empty_snapshot():
    owner_uuid = str(uuid4())
    authoritative_empty = ResourceDict.model_validate(
        _resource_payload(
            uuid=owner_uuid,
            template_name="StrictCarrier",
            available_sites=[{"index": "A1", "label": "A1"}],
            sites=[],
            sites_initialized=True,
        )
    )
    assert authoritative_empty.sites_initialized is True
    assert authoritative_empty.sites == []
    assert "available_sites" not in authoritative_empty.model_dump()

    pending = ResourceDict.model_validate(
        _resource_payload(uuid=owner_uuid, sites=[], sites_initialized=False)
    )
    assert pending.sites_initialized is False
    assert pending.sites == []

    populated = ResourceDict.model_validate(
        _resource_payload(
            uuid=owner_uuid,
            template_name="StrictCarrier",
            sites=[_site_payload(owner_uuid)],
            sites_initialized=False,
        )
    )
    assert populated.sites_initialized is True


def test_canonical_site_rejects_unknown_or_conflicting_fields():
    owner_uuid = str(uuid4())
    canonical = {
        "schema_version": 1,
        "uuid": str(uuid4()),
        "template_name": "StrictCarrier",
        "material_uuid": owner_uuid,
        "index": 0,
        "label": "A1",
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ResourceSite.model_validate({**canonical, "unexpected": True})

    with pytest.raises(ValidationError, match="material_uuid"):
        ResourceDict.model_validate(
            _resource_payload(
                uuid=owner_uuid,
                template_name="StrictCarrier",
                sites=[{**canonical, "material_uuid": str(uuid4())}],
            )
        )

    resource = ResourceDict.model_validate(
        _resource_payload(pose={"position": {"x": 9, "y": 8, "z": 7}})
    )
    assert not hasattr(resource, "position")
    assert resource.pose.position.model_dump() == {"x": 9.0, "y": 8.0, "z": 7.0}

    with pytest.raises(ValidationError, match="根字段 position 不受支持"):
        ResourceDict.model_validate(
            _resource_payload(position={"x": 1, "y": 2, "z": 3})
        )


def test_pose_position_can_be_unknown_without_zero_default():
    resource = ResourceDict.model_validate(_resource_payload(pose={"position": None}))

    assert resource.pose.position is None
    dumped = resource.model_dump(by_alias=True)
    assert "position" not in dumped
    assert ResourceDict.model_validate(dumped).pose.position is None


def test_full_root_position_shape_is_rejected():
    with pytest.raises(ValidationError, match="根字段 position 不受支持"):
        ResourceDict.model_validate(
            _resource_payload(
                position={
                    "position": {"x": 1, "y": 2, "z": 3},
                    "position3d": {"x": 4, "y": 5, "z": 6},
                    "size": {"width": 7, "height": 8, "depth": 9},
                },
            )
        )


def test_occupied_by_name_is_rejected():
    owner_uuid = str(uuid4())
    raw = [
        _resource_payload(
            uuid=owner_uuid,
            sites=[
                {
                    "uuid": str(uuid4()),
                    "template_name": "StrictCarrier",
                    "material_uuid": owner_uuid,
                    "index": "A1",
                    "label": "A1",
                    "occupied_by": "tube-1",
                }
            ],
        )
    ]
    with pytest.raises(ValidationError, match="occupied_by 不受支持"):
        ResourceTreeSet.from_raw_dict_list(raw)


@pytest.mark.parametrize("capacity", [0, 1, 111, 384, 1024])
def test_site_model_preserves_large_root_lists(capacity):
    owner_uuid = str(uuid4())
    sites = [
        {
            "schema_version": 1,
            "uuid": str(uuid4()),
            "template_name": "LargeCarrier",
            "material_uuid": owner_uuid,
            "index": index,
            "label": f"S{index}",
            "pose": {
                "position": {"x": float(index), "y": 0, "z": 0},
                "position3d": {"x": float(index), "y": 0, "z": 0},
            },
            "allowed_resource_categories": ["plate"],
            "meta_data": {"ordinal": index},
        }
        for index in range(capacity)
    ]
    resource = ResourceDict.model_validate(
        _resource_payload(
            uuid=owner_uuid,
            config={"type": "LargeCarrier"},
            template_name="LargeCarrier",
            sites=sites,
        )
    )
    restored = ResourceDict.model_validate(resource.model_dump(by_alias=True))
    assert restored.sites is not None
    assert len(restored.sites) == capacity
    if capacity:
        assert restored.sites[-1].pose.position3d.x == float(capacity - 1)
        assert restored.sites[-1].meta_data == {"ordinal": capacity - 1}


def test_itemized_carrier_uses_native_sites_and_injected_extra_metadata():
    carrier = ItemizedCarrier(
        name="carrier",
        size_x=100,
        size_y=100,
        size_z=20,
        sites={"A1": None, "A2": None},
    )
    carrier.unilabos_uuid = str(uuid4())
    set_plr_template_name(carrier, "StrictCarrier")
    assert carrier.unilabos_extra[EXTRA_RESOURCE_CLASS] == "StrictCarrier"

    # 与带 Site 的 Deck 一致：PLR payload 不承载 canonical Site。
    assert "sites" not in carrier.serialize()

    backend_sites = [
        ResourceSite(
            uuid=str(uuid4()),
            template_name="StrictCarrier",
            material_uuid=carrier.unilabos_uuid,
            index=index,
            label=label,
            pose={
                "position": {
                    "x": carrier.child_locations[label].x,
                    "y": carrier.child_locations[label].y,
                    "z": carrier.child_locations[label].z,
                },
                "position3d": {
                    "x": carrier.child_locations[label].x,
                    "y": carrier.child_locations[label].y,
                    "z": carrier.child_locations[label].z,
                },
                "size": carrier.child_size[label],
            },
        )
        for index, label in enumerate(carrier.child_locations)
    ]
    apply_plr_site_metadata(carrier, {carrier.name: backend_sites})

    serialized = carrier.serialize()
    assert "sites" not in serialized

    extracted = extract_plr_sites(carrier, serialized)
    assert extracted is not None
    assert [site.model_dump() for site in extracted] == [
        site.model_dump() for site in backend_sites
    ]
    assert set(extracted[0].model_dump()) == set(ResourceSite.model_fields)
    injected_site = extracted[0].model_copy(
        deep=True,
        update={
            "allowed_resource_categories": ["plate"],
            "parent_link": "deck/main",
            "meta_data": {"vendor": {"slot": "A1"}},
        },
    )
    injected_site.pose.position.x = 101
    injected_site.pose.position.y = 102
    apply_plr_site_metadata(carrier, {carrier.name: [injected_site, extracted[1]]})

    assert carrier.unilabos_extra[EXTRA_SITES]["A1"]["uuid"] == injected_site.uuid
    assert carrier.resource_sites is not None
    assert carrier.resource_sites[0].uuid == injected_site.uuid
    restored = extract_plr_sites(carrier, carrier.serialize())
    assert restored is not None
    assert restored[0].allowed_resource_categories == ["plate"]
    assert restored[0].parent_link == "deck/main"
    assert restored[0].meta_data == {"vendor": {"slot": "A1"}}
    assert restored[0].pose.position.model_dump() == {"x": 101.0, "y": 102.0, "z": 0.0}


def test_known_random_uuid_seeds_itemized_carrier_draft_sites():
    carrier = ItemizedCarrier(
        name="carrier",
        size_x=100,
        size_y=100,
        size_z=20,
        sites={0: None, "A2": None},
        model="StrictCarrier",
    )

    tree = ResourceTreeSet.from_plr_resources([carrier], known_random_uuid=True)

    sites = tree.root_nodes[0].res_content.sites
    assert sites is not None
    assert [site.label for site in sites] == ["0", "A2"]
    assert all(site.template_name == "StrictCarrier" for site in sites)
    assert all(site.material_uuid == carrier.unilabos_uuid for site in sites)
    assert all(UUID(site.uuid) for site in sites)
    assert carrier.resource_sites == sites


def test_itemized_carrier_rejects_canonical_site_not_in_native_layout():
    carrier = ItemizedCarrier(
        name="carrier",
        size_x=100,
        size_y=100,
        size_z=20,
        sites={"A1": None},
    )
    carrier.unilabos_uuid = str(uuid4())
    set_plr_template_name(carrier, "StrictCarrier")
    wrong_site = ResourceSite(
        uuid=str(uuid4()),
        template_name="StrictCarrier",
        material_uuid=carrier.unilabos_uuid,
        index=1,
        label="A2",
    )

    with pytest.raises(ValueError, match="槽位与 canonical sites 不一致"):
        apply_plr_site_metadata(carrier, {carrier.name: [wrong_site]})


def test_itemized_carrier_deserialization_uses_canonical_shape():
    owner_uuid = str(uuid4())
    occupant_uuid = str(uuid4())
    site = ResourceSite(
        uuid=str(uuid4()),
        template_name="StrictCarrier",
        material_uuid=owner_uuid,
        index=0,
        label="A1",
        occupied_material_uuid=occupant_uuid,
        pose={
            "position": {"x": 101, "y": 102, "z": 0},
            "position3d": {"x": 1, "y": 2, "z": 3},
            "size": {"width": 4, "height": 5, "depth": 6},
        },
        allowed_resource_categories=["tube"],
    )

    native = sites_for_plr_deserialization([site])
    assert native == [site.model_dump()]

    carrier = ItemizedCarrier(
        name="carrier",
        size_x=100,
        size_y=100,
        size_z=20,
        sites=native,
    )
    assert "sites" not in carrier.serialize()
    extracted = extract_plr_sites(carrier, carrier.serialize())
    assert extracted is not None
    assert [restored.model_dump() for restored in extracted] == native


def test_itemized_carrier_resource_tree_roundtrip_preserves_sites():
    owner_uuid = str(uuid4())
    site = ResourceSite(
        uuid=str(uuid4()),
        template_name="StrictCarrier",
        material_uuid=owner_uuid,
        index=0,
        label="A1",
        pose={
            "position": {"x": 1, "y": 2, "z": 3},
            "position3d": {"x": 1, "y": 2, "z": 3},
            "size": {"width": 4, "height": 5, "depth": 6},
        },
    )
    carrier = ItemizedCarrier(
        name="carrier",
        size_x=100,
        size_y=100,
        size_z=20,
        sites=[site],
    )
    carrier.unilabos_uuid = owner_uuid
    set_plr_template_name(carrier, "StrictCarrier")

    tree = ResourceTreeSet.from_plr_resources([carrier])
    restored_tree = ResourceTreeSet.load(tree.dump())
    restored_carrier = restored_tree.to_plr_resources()[0]

    assert "sites" not in restored_carrier.serialize()
    restored_sites = extract_plr_sites(restored_carrier, restored_carrier.serialize())
    assert restored_sites == [site]


def test_itemized_carrier_site_boundary_has_only_canonical_fields():
    site = ResourceSite(
        uuid=str(uuid4()),
        template_name="StrictCarrier",
        material_uuid=str(uuid4()),
        index=7,
        label="T8",
        visible=False,
        occupied_material_uuid=str(uuid4()),
        pose={
            "position": {"x": 101, "y": 102, "z": 0},
            "position3d": {"x": 1, "y": 2, "z": 3},
            "size": {"width": 4, "height": 5, "depth": 6},
        },
        allowed_resource_categories=["plate", "plate"],
        meta_data={"vendor": "kept-in-ResourceSite"},
    )

    native = sites_for_plr_deserialization([site])[0]

    assert native == site.model_dump()
    assert set(native) == set(ResourceSite.model_fields)
    assert "occupied_by" not in native
    assert native["occupied_material_uuid"] == site.occupied_material_uuid
    assert site.pose.position.model_dump() == {"x": 101.0, "y": 102.0, "z": 0.0}
    with pytest.raises(ValidationError, match="occupied_by 不受支持"):
        ResourceSite.model_validate({**native, "occupied_by": "plate-1"})


def test_resource_models_are_reusable_and_nested_xyz_is_required():
    from unilabos.resources.objects import ResourceDict as ExportedResourceDict
    from unilabos.resources.resource_tracker import ResourceDict as TrackerResourceDict

    assert ExportedResourceDict is ResourceDict
    assert TrackerResourceDict is ResourceDict
    with pytest.raises(ValidationError, match="Field required"):
        ResourceDictPositionObject.model_validate({"x": 1})


def test_single_site_model_preserves_explicit_extra_round_trip():
    site = ResourceSite(
        uuid=str(uuid4()),
        template_name="StrictCarrier",
        material_uuid=str(uuid4()),
        index=0,
        label="A1",
        pose={
            "position3d": {"x": 1, "y": 2, "z": 3},
            "size": {"width": 4, "height": 5, "depth": 6},
        },
        extra={"vendor_slot": 7},
    )

    native = sites_for_plr_deserialization([site])[0]
    assert native["extra"] == {"vendor_slot": 7}
    assert ResourceSite.model_validate(native) == site
    assert site.extra == {"vendor_slot": 7}


def test_resource_site_storage_is_structural_and_requires_exact_match():
    owner_uuid = str(uuid4())
    occupant_uuid = str(uuid4())
    site = ResourceSite(
        uuid=str(uuid4()),
        template_name="FakeDeck",
        material_uuid=owner_uuid,
        index=0,
        label="T1",
        occupied_material_uuid=occupant_uuid,
        pose={
            "position": {"x": 101, "y": 102, "z": 0},
            "position3d": {"x": 1, "y": 2, "z": 3},
            "size": {"width": 4, "height": 5, "depth": 6},
        },
        allowed_resource_categories=["plate"],
        extra={"vendor_slot": 7},
    )
    plate = Resource("plate-1", 1, 1, 1)
    plate.unilabos_uuid = occupant_uuid

    class FakeDeck:
        def __init__(self, stored_site):
            self.name = "deck"
            self.unilabos_uuid = owner_uuid
            self.unilabos_extra = {EXTRA_RESOURCE_CLASS: "FakeDeck"}
            self.children = [plate]
            self.sites = [stored_site]

    assert sites_for_plr_deserialization([site]) == [site.model_dump()]
    deck = FakeDeck(site.model_copy(deep=True))
    apply_plr_site_metadata(deck, {"deck": [site]})

    assert deck.sites == [site]
    extracted = extract_plr_sites(deck, {})
    assert extracted is not None
    assert extracted[0].extra == {"vendor_slot": 7}

    mismatched = site.model_copy(deep=True)
    mismatched.pose.position3d.x = 99
    with pytest.raises(ValueError, match="与根字段不一致"):
        apply_plr_site_metadata(FakeDeck(mismatched), {"deck": [site]})


def test_site_deck_uses_resource_sites_without_extending_plr_serialize():
    from tests.plr_fixtures import SiteDeck

    owner_uuid = str(uuid4())
    site = ResourceSite(
        uuid=str(uuid4()),
        template_name="SiteDeck",
        material_uuid=owner_uuid,
        index=0,
        label="T1",
        pose={
            "position": {"x": 10, "y": 20, "z": 0},
            "position3d": {"x": 10, "y": 20, "z": 30},
            "size": {"width": 40, "height": 50, "depth": 60},
        },
        allowed_resource_categories=["plate"],
    )
    deck = SiteDeck(
        name="deck",
        size_x=100,
        size_y=100,
        size_z=20,
        sites=[site],
    )

    assert len(deck.sites) == 1
    assert isinstance(deck.sites[0], ResourceSite)
    assert deck.sites[0].pose.position3d.x == 10
    assert deck.sites[0].pose.size.height == 50
    assert "sites" not in deck.serialize()
    extracted = extract_plr_sites(deck, deck.serialize())
    assert extracted == [site]

    plate = Resource("plate-1", 10, 10, 10)
    plate.unilabos_uuid = str(uuid4())
    deck.assign_child_resource(plate, spot=0)
    assert "sites" not in deck.serialize()
    assert deck.sites[0].occupied_material_uuid == plate.unilabos_uuid
    extracted = extract_plr_sites(deck, deck.serialize())
    assert extracted is not None
    assert extracted[0].occupied_material_uuid == plate.unilabos_uuid

    deck.unassign_child_resource(plate)
    assert "sites" not in deck.serialize()
    assert deck.sites[0].occupied_material_uuid is None


def test_deck_resource_sites_remain_canonical_for_constructor():
    owner_uuid = str(uuid4())
    occupant_uuid = str(uuid4())
    site = ResourceSite(
        uuid=str(uuid4()),
        template_name="SiteDeck",
        material_uuid=owner_uuid,
        index=0,
        label="T1",
        occupied_material_uuid=occupant_uuid,
        pose={
            "position": {"x": 101, "y": 102, "z": 0},
            "position3d": {"x": 1, "y": 2, "z": 3},
            "size": {"width": 4, "height": 5, "depth": 6},
        },
        allowed_resource_categories=["plate"],
    )

    native = sites_for_plr_deserialization([site])

    assert native == [site.model_dump()]


def test_standard_plr_carrier_site_roundtrip_preserves_identity_and_metadata():
    holder = ResourceHolder(
        "slot-A",
        size_x=10,
        size_y=20,
        size_z=5,
        child_location=Coordinate.zero(),
    )
    holder.location = Coordinate(1, 2, 3)
    carrier = Carrier("carrier", 100, 100, 20, sites={7: holder}, model="carrier-model")
    occupant = Resource("tube", 1, 1, 1)
    holder.assign_child_resource(occupant, location=Coordinate.zero())
    carrier.unilabos_uuid = str(uuid4())
    holder.unilabos_uuid = str(uuid4())
    occupant.unilabos_uuid = str(uuid4())
    set_plr_template_name(carrier, "carrier-model")
    backend_site = ResourceSite(
        uuid=str(uuid4()),
        template_name="carrier-model",
        material_uuid=carrier.unilabos_uuid,
        index=7,
        label="slot-A",
        occupied_material_uuid=occupant.unilabos_uuid,
        pose={
            "position": {"x": 1, "y": 2, "z": 3},
            "position3d": {"x": 1, "y": 2, "z": 3},
            "size": {"width": 10, "height": 20, "depth": 5},
        },
    )
    apply_plr_site_metadata(carrier, {carrier.name: [backend_site]})

    tree = ResourceTreeSet.from_plr_resources([carrier])
    site = tree.root_nodes[0].res_content.sites[0]
    assert site.template_name == "carrier-model"
    assert site.index == 7
    assert site.label == "slot-A"
    assert site.material_uuid == carrier.unilabos_uuid
    assert site.occupied_material_uuid == occupant.unilabos_uuid
    assert site.pose.position.model_dump() == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert site.pose.position3d.model_dump() == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert site.pose.size.model_dump() == {"depth": 5.0, "width": 10.0, "height": 20.0}
    loaded = ResourceTreeSet.load(tree.dump())
    assert loaded.root_nodes[0].res_content.sites[0] == site

    payload = site.model_copy(
        update={
            "allowed_resource_categories": ["tube"],
            "meta_data": {"vendor": {"slot": 7}},
        }
    )
    apply_plr_site_metadata(carrier, {carrier.name: [payload]})
    assert not hasattr(holder, "content_type")
    extracted = extract_plr_sites(carrier, carrier.serialize())
    assert extracted is not None
    assert extracted[0].uuid == site.uuid
    assert extracted[0].allowed_resource_categories == ["tube"]
    assert extracted[0].meta_data == {"vendor": {"slot": 7}}


def test_site_snapshot_merge_is_uuid_based_and_never_deletes_missing_sites():
    owner_uuid = str(uuid4())
    first_uuid = str(uuid4())
    second_uuid = str(uuid4())
    current = [
        {
            "schema_version": 1,
            "uuid": first_uuid,
            "template_name": "StrictCarrier",
            "material_uuid": owner_uuid,
            "index": 0,
            "label": "A1",
            "visible": True,
            "occupied_material_uuid": None,
            "meta_data": {"vendor": {"stable": 1, "old": 2}},
        },
        {
            "schema_version": 1,
            "uuid": second_uuid,
            "template_name": "StrictCarrier",
            "material_uuid": owner_uuid,
            "index": 1,
            "label": "A2",
            "visible": True,
            "occupied_material_uuid": None,
            "meta_data": {},
        },
    ]
    occupant_uuid = str(uuid4())
    incoming = [
        {
            **current[0],
            "occupied_material_uuid": occupant_uuid,
        }
    ]

    merged = merge_resource_sites(current, incoming)
    assert merged is not None
    assert [site["uuid"] for site in merged] == [first_uuid, second_uuid]
    assert merged[0]["occupied_material_uuid"] == occupant_uuid
    assert merged[0]["visible"] is True
    assert merged[0]["meta_data"] == current[0]["meta_data"]

    fixed_field_change = [{**incoming[0], "visible": False}]
    with pytest.raises(ValueError, match="不可变字段 visible 冲突"):
        merge_resource_sites(current, fixed_field_change)

    metadata_change = [{**incoming[0], "meta_data": {"vendor": {"stable": 9}}}]
    with pytest.raises(ValueError, match="不可变字段 meta_data 冲突"):
        merge_resource_sites(current, metadata_change)

    conflicting = [{**incoming[0], "uuid": str(uuid4())}]
    with pytest.raises(ValueError, match="身份冲突"):
        merge_resource_sites(current, conflicting)


def test_graph_loader_does_not_promote_class_to_template_name():
    """图装配只认当前契约：class 不再回填 template_name（旧图由 legacy 适配层转换）。"""
    from unilabos.server.services.materials.graph import graph_resource_tree

    node = _resource_payload(**{"class": "virtual_workbench"})
    node.pop("template_name", None)
    dumped = graph_resource_tree([node]).dump()[0][0]

    assert dumped["class"] == "virtual_workbench"
    # template_name 只按 ResourceDict 自己的规则推导（这里来自 config.type）。
    assert dumped["template_name"] == "StrictCarrier"


def test_graph_loader_keeps_protocol_fields_at_resource_root():
    from unilabos.server.services.materials.graph import graph_resource_tree

    owner_uuid = str(uuid4())
    node = _resource_payload(
        uuid=owner_uuid,
        template_name="StrictCarrier",
        resource_template_uuid="template-uuid",
        meta_data={"vendor": {"lot": "A-1"}},
        sites=[
            _site_payload(
                owner_uuid,
                pose={
                    "position": {"x": 4, "y": 5, "z": 6},
                    "position3d": {"x": 4, "y": 5, "z": 6},
                },
            )
        ],
        sites_initialized=True,
        vendor_config="driver-only",
    )
    tree = graph_resource_tree([node])
    dumped = tree.dump()[0][0]

    assert dumped["template_name"] == "StrictCarrier"
    assert dumped["resource_template_uuid"] == "template-uuid"
    assert "position" not in dumped
    assert dumped["pose"]["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert dumped["meta_data"] == {"vendor": {"lot": "A-1"}}
    assert "meta_data" not in dumped["config"]
    assert "meta_data" not in dumped["data"]
    assert dumped["sites"][0]["pose"]["position3d"]["x"] == 4.0
    assert dumped["config"]["vendor_config"] == "driver-only"
    assert "sites" not in dumped["config"]
    assert "template_name" not in dumped["config"]

    transport_uuid = str(uuid4())
    transport_node = _resource_payload(
        uuid="",
        data={"unilabos_uuid": transport_uuid},
    )
    transport_tree = graph_resource_tree([transport_node])
    transported = transport_tree.dump()[0][0]
    assert transported["uuid"] == transport_uuid
    assert "unilabos_uuid" not in transported["data"]

    unknown_position = _resource_payload(pose={"position": None})
    unknown_tree = graph_resource_tree([unknown_position])
    assert unknown_tree.dump()[0][0]["pose"]["position"] is None

    # 根级 position 不属于当前契约（旧格式图在 -g 读取边界由 legacy 适配层转换）。
    legacy_root_position = _resource_payload(position={"x": 1, "y": 2, "z": 3})
    with pytest.raises(ValueError, match="根字段 position 不受支持"):
        graph_resource_tree([legacy_root_position])

    conflicting_xyz = _resource_payload(x=1)
    with pytest.raises(ValueError, match="与旧 x/y/z 坐标冲突"):
        graph_resource_tree([conflicting_xyz])


def test_graph_loader_keeps_graph_defined_host_node_and_links():
    """图可显式声明 host_node（多主机设计），导入导出必须保留其身份与连接。"""

    from unilabos.server.services.materials.graph import load_graph_payload

    host_uuid = str(uuid4())
    device = _resource_payload(
        id="device-1",
        name="device-1",
        type="device",
        template_name="dummy_device",
        parent="host_node",
        parent_uuid=host_uuid,
        **{"class": "dummy_device"},
    )
    loaded = load_graph_payload(
        {
            "nodes": [
                {
                    "id": "host_node",
                    "uuid": host_uuid,
                    "name": "host_node",
                    "type": "device",
                    "class": "host_node",
                    "config": {},
                    "data": {},
                },
                device,
            ],
            "links": [
                {
                    "source": "host_node",
                    "target": "device-1",
                    "type": "communication",
                }
            ],
        },
        machine_name="lab-pc",
    )
    tree, links, graph = loaded.resource_tree_set, loaded.links, loaded.physical_setup_graph

    assert [node.res_content.id for node in tree.root_nodes] == ["host_node"]
    host_root = tree.root_nodes[0]
    assert host_root.res_content.uuid == host_uuid
    assert [child.res_content.id for child in host_root.children] == ["device-1"]
    # 设备树节点打上本机 machine_name
    assert {node.res_content.machine_name for node in tree.all_nodes} == {"lab-pc"}
    assert set(graph.nodes) == {"host_node", "device-1"}
    assert len(links) == 1
    assert links[0]["source_uuid"] == host_uuid


def test_ros_resource_transport_roundtrip_single_json_field():
    """Resource msg 整 JSON 单字段传输：``data`` 承载全部内容并无损往返。"""

    message_converter = pytest.importorskip(
        "unilabos.backend.ros2.msgs.message_converter",
        reason="ROS 消息转换依赖 Jazzy 版 unilabos_msgs.Resource",
        exc_type=ImportError,
    )
    site_uuid = str(uuid4())
    owner_uuid = str(uuid4())
    payload = {
        "id": "carrier",
        "uuid": owner_uuid,
        "name": "carrier",
        "type": "carrier",
        "class": "",
        "template_name": "StrictCarrier",
        "resource_template_uuid": "template-uuid",
        "barcode": "BC-ROS-001",
        "barcode_symbology": "code128",
        "meta_data": {"vendor": {"lot": "A-1"}},
        "pose": {
            "size": {"width": 100, "height": 200, "depth": 30},
            "position": {"x": 1, "y": 2, "z": 3},
            "position3d": {"x": 10, "y": 20, "z": 30},
        },
        "config": {"type": "StrictCarrier"},
        "data": {},
        "extra": {},
        "sites": [
            {
                "schema_version": 1,
                "uuid": site_uuid,
                "template_name": "StrictCarrier",
                "material_uuid": owner_uuid,
                "index": "A1",
                "label": "A1",
                "pose": {
                    "position": {"x": 1, "y": 0, "z": 0},
                    "position3d": {"x": 1, "y": 0, "z": 0},
                },
            }
        ],
        "sites_initialized": True,
    }

    ros_resource = message_converter.convert_to_ros_msg(
        message_converter.Resource, payload
    )
    # id/name 仅冗余填充便于排查；全部数据都在 data 单字段里
    assert ros_resource.id == "carrier"
    assert ros_resource.name == "carrier"
    assert ros_resource.config == ""
    assert json.loads(ros_resource.data) == payload

    restored = message_converter.convert_from_ros_msg(ros_resource)
    assert restored == payload

    canonical = ResourceDict.model_validate(restored)
    assert canonical.uuid == owner_uuid
    assert canonical.template_name == "StrictCarrier"
    assert canonical.barcode == "BC-ROS-001"
    assert canonical.meta_data == {"vendor": {"lot": "A-1"}}
    assert canonical.pose.position.model_dump() == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert canonical.sites[0].uuid == site_uuid


def test_ros_resource_transport_preserves_unknown_position():
    """pose.position=None 通过完整 JSON 保持为空。"""

    message_converter = pytest.importorskip(
        "unilabos.backend.ros2.msgs.message_converter",
        reason="ROS 消息转换依赖 Jazzy 版 unilabos_msgs.Resource",
        exc_type=ImportError,
    )
    owner_uuid = str(uuid4())
    payload = {
        "id": "unknown-position",
        "uuid": owner_uuid,
        "name": "unknown-position",
        "type": "carrier",
        "class": "",
        "pose": {"position": None},
        "meta_data": {"vendor": {"lot": "A-1"}},
        "config": {},
        "data": {},
        "extra": {},
    }
    ros_resource = message_converter.convert_to_ros_msg(
        message_converter.Resource, payload
    )
    restored = message_converter.convert_from_ros_msg(ros_resource)
    assert restored == payload
    assert restored["pose"]["position"] is None

    canonical = ResourceDict.model_validate(restored)
    assert canonical.pose.position is None
    assert canonical.meta_data == {"vendor": {"lot": "A-1"}}
