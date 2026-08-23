from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import inspect
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from unilabos.registry.ast_registry_scanner import _parse_file, scan_directory
from unilabos.registry.decorators import device, get_device_meta
from unilabos.devices.virtual.workbench import VirtualWorkbench
from unilabos.resources.device_site_adapter import (
    apply_device_available_sites,
    prepare_devices_for_report,
)
from unilabos.resources.resource_tracker import (
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.resources.objects.site import SiteDefinition, normalize_available_sites


AVAILABLE_SITES = [
    {
        "index": "A1",
        "label": "A1",
        "pose": {
            "position": {"x": 1, "y": 2, "z": 0},
            "position3d": {"x": 1, "y": 2, "z": 3},
            "size": {"width": 10, "height": 20, "depth": 30},
            "rotation": {"x": 0, "y": 0, "z": 90},
        },
        "allowed_resource_categories": ["plate", "plate"],
    }
]


def _device_resource(**overrides) -> ResourceDictInstance:
    payload = {
        "id": "device-1",
        "uuid": str(uuid4()),
        "name": "device-1",
        "type": "device",
        "class": "available_sites_test_device",
        "template_name": "available_sites_test_device",
        "config": {},
        "data": {},
        "extra": {},
        "sites": [],
        "sites_initialized": True,
    }
    payload.update(overrides)
    return ResourceDictInstance(ResourceDict.model_validate(payload))


def _instantiated_sites(
    owner_uuid: str, template_name: str, definitions=AVAILABLE_SITES
):
    return [
        {
            **definition,
            "uuid": str(uuid4()),
            "template_name": template_name,
            "material_uuid": owner_uuid,
            "occupied_material_uuid": None,
        }
        for definition in normalize_available_sites(definitions)
    ]


def test_device_decorator_emits_root_available_sites_without_instance_identity():
    @device(
        id="available_sites_test_device",
        category=["test"],
        available_sites=AVAILABLE_SITES,
    )
    class AvailableSitesDevice:
        pass

    meta = get_device_meta(AvailableSitesDevice, "available_sites_test_device")
    assert meta is not None
    site = meta["available_sites"][0]
    assert site["pose"]["position"] == {"x": 1.0, "y": 2.0, "z": 0.0}
    assert site["pose"]["position3d"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert site["pose"]["size"]["height"] == 20
    assert site["pose"]["rotation"]["z"] == 90
    assert site["allowed_resource_categories"] == ["plate"]
    assert {
        "uuid",
        "material_uuid",
        "occupied_material_uuid",
        "template_name",
    }.isdisjoint(site)


def test_available_sites_accepts_declared_sequence_and_mapping_inputs():
    normalized = normalize_available_sites(
        (
            MappingProxyType(
                {
                    "index": "A1",
                    "label": "A1",
                    "pose": MappingProxyType(
                        {"position": MappingProxyType({"x": 1, "y": 2, "z": 3})}
                    ),
                }
            ),
        )
    )

    assert normalized[0]["pose"]["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}


def test_site_pose_rejects_unknown_geometry_fields():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SiteDefinition.model_validate(
            {
                "index": "A1",
                "label": "A1",
                "pose": {"position": {"x": 1, "y": 2, "z": 3, "frame": "deck"}},
            }
        )


def test_virtual_workbench_available_sites_validate_backend_instance_sites():
    from unilabos.devices.virtual.workbench import VIRTUAL_WORKBENCH_AVAILABLE_SITES

    assert all(
        isinstance(site, SiteDefinition) for site in VIRTUAL_WORKBENCH_AVAILABLE_SITES
    )
    meta = get_device_meta(VirtualWorkbench, "virtual_workbench")
    assert meta is not None
    assert [site["label"] for site in meta["available_sites"]] == [
        "heating_station_1",
        "heating_station_2",
        "heating_station_3",
    ]
    assert all(
        {"uuid", "material_uuid", "occupied_material_uuid", "template_name"}.isdisjoint(
            site
        )
        for site in meta["available_sites"]
    )

    owner_uuid = str(uuid4())
    device_config = _device_resource(
        **{
            "class": "virtual_workbench",
            "uuid": owner_uuid,
            "template_name": "virtual_workbench",
            "sites": _instantiated_sites(
                owner_uuid,
                "virtual_workbench",
                VIRTUAL_WORKBENCH_AVAILABLE_SITES,
            ),
        }
    )
    apply_device_available_sites(device_config, meta, "virtual_workbench")

    sites = device_config.res_content.sites
    assert sites is not None
    assert [site.label for site in sites] == [
        "heating_station_1",
        "heating_station_2",
        "heating_station_3",
    ]
    assert all(site.material_uuid == device_config.res_content.uuid for site in sites)
    assert "available_sites" not in device_config.res_content.model_dump()


def test_ast_scanner_parses_available_sites(tmp_path):
    source = tmp_path / "device_fixture.py"
    source.write_text(
        "\n".join(
            [
                "from unilabos.registry.decorators import device",
                "",
                "DEVICE_SITES = [{",
                "    'label': 'slot-1',",
                "    'pose': {",
                "        'position': {'x': 4, 'y': 5, 'z': 0},",
                "        'position3d': {'x': 4, 'y': 5, 'z': 6},",
                "        'size': {'width': 7, 'height': 8, 'depth': 9},",
                "    },",
                "}]",
                "",
                "@device(",
                "    id='ast_available_sites_device',",
                "    category=['test'],",
                "    available_sites=DEVICE_SITES,",
                ")",
                "class AstAvailableSitesDevice:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    devices, _ = _parse_file(source, tmp_path)
    assert len(devices) == 1
    site = devices[0]["available_sites"][0]
    assert site["index"] == 0
    assert site["label"] == "slot-1"
    assert site["pose"]["position"] == {"x": 4.0, "y": 5.0, "z": 0.0}
    assert site["pose"]["position3d"] == {"x": 4.0, "y": 5.0, "z": 6.0}
    assert site["pose"]["size"] == {"width": 7.0, "height": 8.0, "depth": 9.0}


def test_ast_scanner_parses_typed_site_definition_constant(tmp_path):
    source = tmp_path / "typed_device_fixture.py"
    source.write_text(
        "\n".join(
            [
                "from unilabos.registry.decorators import device",
                "from unilabos.resources.objects.site import SiteDefinition",
                "",
                "DEVICE_SITES: list[SiteDefinition] = [SiteDefinition(",
                "    index='A1',",
                "    label='slot-1',",
                "    pose={'size': {'width': 7, 'height': 8, 'depth': 9}},",
                ")]",
                "",
                "@device(",
                "    id='typed_ast_available_sites_device',",
                "    category=['test'],",
                "    available_sites=DEVICE_SITES,",
                ")",
                "class TypedAstAvailableSitesDevice:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    devices, _ = _parse_file(source, tmp_path)
    assert len(devices) == 1
    site = devices[0]["available_sites"][0]
    assert site["index"] == "A1"
    assert site["label"] == "slot-1"
    assert site["pose"]["size"] == {"width": 7.0, "height": 8.0, "depth": 9.0}


def test_ast_scanner_parses_real_workbench_typed_pose_models():
    source = Path(inspect.getfile(VirtualWorkbench)).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    expected_sites = [
        {
            "label": f"heating_station_{station_id}",
            "position3d": {"x": x, "y": 100.0, "z": 0.0},
            "parent_link": f"heating_station_{station_id}",
            "meta_data": {"station_id": station_id, "role": "heating"},
        }
        for station_id, x in enumerate((100.0, 250.0, 400.0), start=1)
    ]

    def assert_workbench_metadata(metadata):
        assert metadata["supported_backends"] == ["hostlink", "ros2"]
        assert [
            {
                "label": site["label"],
                "position3d": site["pose"]["position3d"],
                "parent_link": site["parent_link"],
                "meta_data": site["meta_data"],
            }
            for site in metadata["available_sites"]
        ] == expected_sites

    devices, _ = _parse_file(source, repository_root)
    metadata = next(
        device for device in devices if device["device_id"] == "virtual_workbench"
    )

    assert_workbench_metadata(metadata)
    assert metadata["available_sites"][0]["pose"]["position"] == {
        "x": 100.0,
        "y": 100.0,
        "z": 0.0,
    }
    assert metadata["available_sites"][0]["pose"]["size"] == {
        "width": 100.0,
        "height": 100.0,
        "depth": 20.0,
    }

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = scan_directory(
            source.parent,
            python_path=repository_root,
            executor=executor,
        )

    scanned = result["devices"]["virtual_workbench"]
    assert_workbench_metadata(scanned)


def test_device_site_adapter_only_validates_identity_and_preserves_occupancy():
    owner_uuid = str(uuid4())
    sites = _instantiated_sites(owner_uuid, "available_sites_test_device")
    sites[0]["occupied_material_uuid"] = str(uuid4())
    device_config = _device_resource(uuid=owner_uuid, sites=sites)
    registry_entry = {"available_sites": AVAILABLE_SITES}

    apply_device_available_sites(
        device_config,
        registry_entry,
        "available_sites_test_device",
    )
    resource = device_config.res_content
    assert resource.template_name == "available_sites_test_device"
    assert resource.sites_initialized is True
    assert resource.sites is not None
    assert len(resource.sites) == 1
    site_uuid = resource.sites[0].uuid
    UUID(site_uuid)
    assert resource.sites[0].material_uuid == resource.uuid
    occupant_uuid = resource.sites[0].occupied_material_uuid
    assert occupant_uuid is not None
    assert {
        "uuid",
        "material_uuid",
        "occupied_material_uuid",
        "template_name",
    }.isdisjoint(registry_entry["available_sites"][0])

    apply_device_available_sites(
        device_config,
        registry_entry,
        "available_sites_test_device",
    )
    restored_site = device_config.res_content.sites[0]
    assert restored_site.uuid == site_uuid
    assert restored_site.occupied_material_uuid == occupant_uuid


def test_device_site_adapter_rejects_generic_device_template_name():
    owner_uuid = str(uuid4())
    device_config = _device_resource(
        uuid=owner_uuid,
        template_name="device",
        sites=_instantiated_sites(owner_uuid, "device"),
    )
    with pytest.raises(ValueError, match="template_name.*注册表"):
        apply_device_available_sites(
            device_config,
            {"available_sites": AVAILABLE_SITES},
            "available_sites_test_device",
        )


def test_device_report_validates_backend_snapshot_without_mutation():
    owner_uuid = str(uuid4())
    device_config = _device_resource(
        uuid=owner_uuid,
        pose={"position": {"x": 40, "y": 50, "z": 60}},
        sites=_instantiated_sites(owner_uuid, "available_sites_test_device"),
    )
    resources = ResourceTreeSet([ResourceTreeInstance(device_config)])
    registry = {
        "available_sites_test_device": {
            "available_sites": AVAILABLE_SITES,
        }
    }

    assert prepare_devices_for_report(resources, registry) == 1
    resource = device_config.res_content
    assert not hasattr(resource, "position")
    assert resource.pose.position.model_dump() == {"x": 40.0, "y": 50.0, "z": 60.0}
    assert resource.template_name == "available_sites_test_device"
    assert resource.sites is not None
    first_site_uuid = resource.sites[0].uuid

    assert prepare_devices_for_report(resources, registry) == 1
    assert device_config.res_content.sites is not None
    assert device_config.res_content.sites[0].uuid == first_site_uuid
    startup_json = resources.dump()[0][0]
    assert startup_json["sites_initialized"] is True
    assert "available_sites" not in startup_json
    assert startup_json["sites"][0]["material_uuid"] == resource.uuid


def test_device_report_accepts_authoritative_empty_snapshot_without_expansion():
    device_config = _device_resource()
    resources = ResourceTreeSet([ResourceTreeInstance(device_config)])

    prepare_devices_for_report(
        resources,
        {"available_sites_test_device": {"available_sites": []}},
    )

    assert device_config.res_content.template_name == "available_sites_test_device"
    assert device_config.res_content.sites == []
    assert device_config.res_content.sites_initialized is True


def test_device_report_rejects_uninitialized_template_sites():
    device_config = _device_resource(sites=None, sites_initialized=False)
    resources = ResourceTreeSet([ResourceTreeInstance(device_config)])

    with pytest.raises(ValueError, match="微后端实例化"):
        prepare_devices_for_report(
            resources,
            {"available_sites_test_device": {"available_sites": AVAILABLE_SITES}},
        )


def test_device_site_adapter_rejects_fixed_definition_changes():
    owner_uuid = str(uuid4())
    device_config = _device_resource(
        uuid=owner_uuid,
        sites=_instantiated_sites(owner_uuid, "available_sites_test_device"),
    )
    registry_entry = {"available_sites": AVAILABLE_SITES}
    apply_device_available_sites(
        device_config,
        registry_entry,
        "available_sites_test_device",
    )

    changed = normalize_available_sites(AVAILABLE_SITES)
    changed[0]["pose"]["size"]["width"] = 999
    with pytest.raises(ValueError, match="固定定义.*冲突"):
        apply_device_available_sites(
            device_config,
            {"available_sites": changed},
            "available_sites_test_device",
        )


def test_material_sites_require_backend_identity_and_drop_available_sites():
    material_uuid = str(uuid4())
    sites = _instantiated_sites(material_uuid, "CarrierTemplate")
    resource = ResourceDict.model_validate(
        {
            "id": "carrier",
            "uuid": material_uuid,
            "name": "carrier",
            "type": "carrier",
            "class": "",
            "template_name": "CarrierTemplate",
            "config": {},
            "data": {},
            "extra": {},
            "available_sites": AVAILABLE_SITES,
            "sites": sites,
            "sites_initialized": True,
        }
    )

    assert resource.sites is not None
    assert resource.sites[0].uuid == sites[0]["uuid"]
    assert resource.sites[0].material_uuid == material_uuid
    assert resource.sites[0].template_name == "CarrierTemplate"
    assert "available_sites" not in resource.model_dump()

    invalid = {**sites[0]}
    invalid.pop("uuid")
    with pytest.raises(Exception, match="uuid"):
        ResourceDict.model_validate(
            {
                "id": "carrier",
                "uuid": material_uuid,
                "name": "carrier",
                "type": "carrier",
                "class": "",
                "template_name": "CarrierTemplate",
                "config": {},
                "data": {},
                "extra": {},
                "sites": [invalid],
                "sites_initialized": True,
            }
        )
