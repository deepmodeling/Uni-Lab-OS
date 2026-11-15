from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import inspect
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from unilabos.client.materials import LocalMaterialsClient
from unilabos.registry.ast_registry_scanner import _parse_file, scan_directory
from unilabos.registry.decorators import device, get_device_meta
from unilabos.devices.virtual.workbench import VirtualWorkbench
from unilabos.resources.adapters.device_site import (
    apply_device_authority_state,
    prepare_devices_for_report,
)
from unilabos.resources.resource_tracker import (
    ResourceDict,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.resources.objects.site import SiteDefinition, normalize_available_sites
from unilabos.server.services.materials import MaterialsService


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
    apply_device_authority_state(device_config, meta, "virtual_workbench")

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

    devices, _, _ = _parse_file(source, tmp_path)
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

    devices, _, _ = _parse_file(source, tmp_path)
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
        assert metadata["supported_backends"] == ["ros2"]
        assert [
            {
                "label": site["label"],
                "position3d": site["pose"]["position3d"],
                "parent_link": site["parent_link"],
                "meta_data": site["meta_data"],
            }
            for site in metadata["available_sites"]
        ] == expected_sites

    devices, _, _ = _parse_file(source, repository_root)
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

    apply_device_authority_state(
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

    apply_device_authority_state(
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
        apply_device_authority_state(
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


def test_device_report_resolves_registry_by_template_name_not_class():
    """运行态注册表解析只读 template_name；class 为空的新图节点照常通过。"""
    by_template = _device_resource(**{"class": ""})
    registry = {"available_sites_test_device": {"available_sites": []}}

    assert prepare_devices_for_report(
        ResourceTreeSet([ResourceTreeInstance(by_template)]), registry
    ) == 1

    legacy_class_only = _device_resource(
        **{"class": "available_sites_test_device", "template_name": "other_template"}
    )
    with pytest.raises(ValueError, match="template_name='other_template' 不在注册表中"):
        prepare_devices_for_report(
            ResourceTreeSet([ResourceTreeInstance(legacy_class_only)]), registry
        )


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
    apply_device_authority_state(
        device_config,
        registry_entry,
        "available_sites_test_device",
    )

    changed = normalize_available_sites(AVAILABLE_SITES)
    changed[0]["pose"]["size"]["width"] = 999
    with pytest.raises(ValueError, match="固定定义.*冲突"):
        apply_device_authority_state(
            device_config,
            {"available_sites": changed},
            "available_sites_test_device",
        )


# ── 权威优先：设备装配时从物料权威取 / 建 Site ──────────────────────────


@pytest.fixture
def authority(tmp_path):
    service = MaterialsService(tmp_path / "materials.db")
    try:
        yield LocalMaterialsClient(service)
    finally:
        service.close()


def test_device_missing_in_authority_is_created_from_registry_definition(authority):
    """权威没有该设备：以图中 uuid + 注册表 available_sites 在权威创建并取回。"""

    owner_uuid = str(uuid4())
    device_config = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)

    apply_device_authority_state(
        device_config,
        {"available_sites": AVAILABLE_SITES},
        "available_sites_test_device",
        gateway=authority,
    )

    resource = device_config.res_content
    assert resource.sites_initialized is True
    assert [site.label for site in resource.sites] == ["A1"]
    UUID(resource.sites[0].uuid)
    assert resource.sites[0].material_uuid == owner_uuid
    assert resource.sites[0].template_name == "available_sites_test_device"
    assert resource.sites[0].pose.size.width == 10

    # 权威里就是这台设备：同 uuid、type=device，Site uuid 与本地一致
    aggregate = authority.get_material(owner_uuid)
    assert aggregate.material.resource_type == "device"
    assert aggregate.material.resource_id == "device-1"
    assert aggregate.data.sites_initialized is True
    assert [site.site_uuid for site in aggregate.sites] == [resource.sites[0].uuid]
    # 设备模板随之登记，带上注册表槽位定义
    template = authority.list_templates(name="available_sites_test_device")[0]
    assert [site["label"] for site in template.available_sites] == ["A1"]


def test_device_present_in_authority_adopts_authority_sites(authority):
    """权威已有该设备（开机图对齐落的）：本地快照被权威 Site 覆盖，不再新建。"""

    owner_uuid = str(uuid4())
    seed = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    apply_device_authority_state(
        seed, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device",
        gateway=authority,
    )
    authority_site_uuid = seed.res_content.sites[0].uuid

    # 本地快照带的是别处（如图权威）派生的 Site uuid，和物料权威不一致
    stale = _device_resource(
        uuid=owner_uuid,
        sites=_instantiated_sites(owner_uuid, "available_sites_test_device"),
    )
    assert stale.res_content.sites[0].uuid != authority_site_uuid

    apply_device_authority_state(
        stale, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device",
        gateway=authority,
    )
    assert stale.res_content.sites[0].uuid == authority_site_uuid
    assert len(authority.list_materials(roots_only=True)) == 1


def test_device_created_with_held_materials_subtree(authority):
    """设备持有物料：权威缺设备时连同下挂子树一起创建，与 materials.ensure 按根对齐一致。"""

    owner_uuid = str(uuid4())
    device_config = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    child = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "deck-1",
                "uuid": str(uuid4()),
                "name": "deck-1",
                "type": "deck",
                "class": "Deck",
                "template_name": "Deck",
                "parent_uuid": owner_uuid,
                "config": {},
                "data": {},
                "extra": {},
            }
        )
    )
    child.res_content.parent = device_config.res_content
    device_config.children.append(child)

    apply_device_authority_state(
        device_config,
        {"available_sites": AVAILABLE_SITES},
        "available_sites_test_device",
        gateway=authority,
    )

    deck = authority.get_material(child.res_content.uuid)
    assert deck.material.parent_material_uuid == owner_uuid
    assert deck.material.resource_id == "device-1/deck-1"
    assert device_config.res_content.sites is not None
    assert len(device_config.res_content.sites) == 1


def test_authority_device_missing_declared_site_is_rejected(authority):
    """权威里的设备缺注册表声明的槽位：权威不能补 Site，必须报错而不是带 0 个位点跑。"""

    owner_uuid = str(uuid4())
    device_config = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    apply_device_authority_state(
        device_config, {"available_sites": []}, "available_sites_test_device",
        gateway=authority,
    )
    assert device_config.res_content.sites == []

    with pytest.raises(ValueError, match="缺少注册表声明的 Site A1"):
        apply_device_authority_state(
            _device_resource(uuid=owner_uuid),
            {"available_sites": AVAILABLE_SITES},
            "available_sites_test_device",
            gateway=authority,
        )


def test_authority_definition_drift_warns_but_adopts(authority, caplog):
    """注册表改了 pose 之类的定义字段：权威保留首次实例化的定义，只告警不阻断。"""

    owner_uuid = str(uuid4())
    device_config = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    apply_device_authority_state(
        device_config, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device",
        gateway=authority,
    )

    changed = normalize_available_sites(AVAILABLE_SITES)
    changed[0]["pose"]["size"]["width"] = 999
    again = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    with caplog.at_level("WARNING"):
        apply_device_authority_state(
            again, {"available_sites": changed}, "available_sites_test_device",
            gateway=authority,
        )
    assert again.res_content.sites[0].pose.size.width == 10  # 以权威为准
    assert "available_sites" in caplog.text and "A1" in caplog.text


def test_authority_uuid_belonging_to_other_template_is_rejected(authority):
    owner_uuid = str(uuid4())
    seed = _device_resource(
        uuid=owner_uuid, template_name="other_device", **{"class": "other_device"},
        sites=None, sites_initialized=False,
    )
    apply_device_authority_state(seed, {"available_sites": []}, "other_device", gateway=authority)

    with pytest.raises(ValueError, match="不是设备 device-1"):
        apply_device_authority_state(
            _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False),
            {"available_sites": AVAILABLE_SITES},
            "available_sites_test_device",
            gateway=authority,
        )


def _material_payload(material_id: str, material_uuid: str, parent_uuid: str | None = None):
    payload = {
        "id": material_id,
        "uuid": material_uuid,
        "name": material_id,
        "type": "container",
        "class": "Container",
        "template_name": "test-container-template",
        "config": {"type": "Container"},
        "data": {"volume": 0},
        "extra": {},
        "sites": [],
        "sites_initialized": True,
    }
    if parent_uuid is not None:
        payload["parent_uuid"] = parent_uuid
    return payload


def _mutation(operation: str):
    from unilabos.protocol.materials import InventoryMutation

    command_uuid = str(uuid4())
    return InventoryMutation(
        command_uuid=command_uuid, effect_key=f"{operation}:{command_uuid}", operation=operation
    )


def test_device_children_are_seeded_from_authority_not_graph(authority):
    """设备持有的物料以权威为准：Site 上的占用物、直接挂在设备上的台面一并装载，
    图中过期的子节点不再装载。"""
    from unilabos.protocol.materials import MaterialMove
    from unilabos.resources import materials
    from unilabos.resources.adapters.plr_materials import resource_tree_to_create

    owner_uuid = str(uuid4())
    device_config = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    apply_device_authority_state(
        device_config, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device",
        gateway=authority,
    )
    site_uuid = device_config.res_content.sites[0].uuid

    # 权威侧后来发生的事：一块板放到 A1 位点、一个台面直接挂到设备下
    plate_uuid, bench_uuid = str(uuid4()), str(uuid4())
    for material_id, material_uuid in (("plate-1", plate_uuid), ("bench-1", bench_uuid)):
        authority.create_tree(
            _mutation("create_material_tree"),
            resource_tree_to_create(
                ResourceTreeSet.from_raw_dict_list([_material_payload(material_id, material_uuid)]),
                adopt_uuid=True,
            ),
        )
    authority.move_material(
        _mutation("move_material"),
        MaterialMove(material_uuid=plate_uuid, destination_site_uuid=site_uuid),
    )
    authority.move_material(
        _mutation("move_material"),
        MaterialMove(material_uuid=bench_uuid, parent_material_uuid=owner_uuid),
    )

    # 图文件还停留在开机那一刻：设备下只有一块早已不在的旧板
    stale_uuid = str(uuid4())
    booted = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    stale = ResourceDictInstance(
        ResourceDict.model_validate(_material_payload("old-plate", stale_uuid, owner_uuid))
    )
    stale.res_content.parent = booted.res_content
    booted.children.append(stale)

    apply_device_authority_state(
        booted, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device",
        gateway=authority,
    )

    held = {child.res_content.uuid: child for child in booted.children}
    assert set(held) == {plate_uuid, bench_uuid}
    assert all(child.res_content.parent is booted.res_content for child in booted.children)
    assert held[plate_uuid].res_content.name == "plate-1"
    assert booted.res_content.sites[0].occupied_material_uuid == plate_uuid
    # 权威树里的形态与运行期 append_resource 装载的一致（materials.get 同源）
    assert materials.get(plate_uuid, gateway=authority).trees[0].root_node.res_content.uuid_parent == owner_uuid


def test_device_children_keep_graph_sub_devices_and_warn_on_orphans(authority, caplog):
    owner_uuid = str(uuid4())
    seed = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    apply_device_authority_state(
        seed, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device",
        gateway=authority,
    )

    booted = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    sub_device = _device_resource(
        id="pump-1", name="pump-1", uuid=str(uuid4()), template_name="pump", **{"class": "pump"},
        sites=None, sites_initialized=False, parent_uuid=owner_uuid,
    )
    sub_device.res_content.parent = booted.res_content
    orphan = ResourceDictInstance(
        ResourceDict.model_validate(_material_payload("ghost", str(uuid4()), owner_uuid))
    )
    orphan.res_content.parent = booted.res_content
    booted.children.extend([sub_device, orphan])

    with caplog.at_level("WARNING"):
        apply_device_authority_state(
            booted, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device",
            gateway=authority,
        )
    assert booted.children == [sub_device]
    assert "ghost" in caplog.text and "以权威为准" in caplog.text


def test_device_created_with_children_adopts_them_back(authority):
    """权威缺设备时连子树一起建，随后装载的子节点就是权威发回的那一份。"""
    owner_uuid, deck_uuid = str(uuid4()), str(uuid4())
    device_config = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    deck = ResourceDictInstance(
        ResourceDict.model_validate(_material_payload("deck-1", deck_uuid, owner_uuid))
    )
    deck.res_content.parent = device_config.res_content
    device_config.children.append(deck)

    apply_device_authority_state(
        device_config, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device",
        gateway=authority,
    )
    assert [child.res_content.uuid for child in device_config.children] == [deck_uuid]
    assert device_config.children[0].res_content.id == "device-1/deck-1"
    assert device_config.children[0].res_content.name == "deck-1"
    assert authority.get_material(deck_uuid).material.parent_material_uuid == owner_uuid


def test_without_reachable_authority_only_validates_local_snapshot(monkeypatch):
    """权威不可达（Slave 未连上 / 未装配）：退化为核验本地快照，Edge 不补齐。"""

    from unilabos.resources.adapters import device_site

    monkeypatch.setattr(device_site, "_resolve_optional_gateway", lambda: None)
    uninitialized = _device_resource(sites=None, sites_initialized=False)
    with pytest.raises(ValueError, match="微后端实例化"):
        apply_device_authority_state(
            uninitialized, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device"
        )

    owner_uuid = str(uuid4())
    local = _device_resource(
        uuid=owner_uuid,
        sites=_instantiated_sites(owner_uuid, "available_sites_test_device"),
    )
    graph_child = ResourceDictInstance(
        ResourceDict.model_validate(_material_payload("graph-plate", str(uuid4()), owner_uuid))
    )
    graph_child.res_content.parent = local.res_content
    local.children.append(graph_child)
    local_uuid = local.res_content.sites[0].uuid
    apply_device_authority_state(
        local, {"available_sites": AVAILABLE_SITES}, "available_sites_test_device"
    )
    assert local.res_content.sites[0].uuid == local_uuid
    assert local.children == [graph_child]  # 权威不可达：沿用图中子节点


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


# ── 库位模板白名单：包作者的声明随实例化进权威并在放入时生效 ────────────────

WHITELIST_SITES = [
    {"index": "A1", "label": "A1", "allowed_resource_template_uuids": ["plate_96", "PLATE_96"]},
    {"index": "A2", "label": "A2"},
]


def test_site_template_whitelist_is_a_definition_field_kept_in_instance_extra():
    definitions = normalize_available_sites(WHITELIST_SITES)
    assert definitions[0]["allowed_resource_template_uuids"] == ["plate_96"]
    # 不限制模板时定义形状与引入白名单前一致
    assert "allowed_resource_template_uuids" not in definitions[1]
    assert "allowed_resource_template_uuids" not in normalize_available_sites(AVAILABLE_SITES)[0]

    from unilabos.resources.objects.site import ResourceSite, validate_instantiated_sites

    owner_uuid = str(uuid4())
    instance = {
        **{key: value for key, value in definitions[0].items() if key != "allowed_resource_template_uuids"},
        "uuid": str(uuid4()), "template_name": "dev_t", "material_uuid": owner_uuid,
        "extra": {"allowed_resource_template_uuids": ["plate_96"]},
    }
    ResourceSite.model_validate(instance)
    second = {**definitions[1], "uuid": str(uuid4()), "template_name": "dev_t", "material_uuid": owner_uuid}
    assert len(validate_instantiated_sites(
        definitions, owner_uuid=owner_uuid, template_name="dev_t", current_sites=[instance, second],
        sites_initialized=True,
    )) == 2
    with pytest.raises(ValueError, match="固定定义.*冲突"):
        validate_instantiated_sites(
            definitions, owner_uuid=owner_uuid, template_name="dev_t",
            current_sites=[{**instance, "extra": {}}, second], sites_initialized=True,
        )


def test_graph_instantiates_registry_site_whitelist_into_extra():
    from unilabos.resources.objects.site import ResourceSite
    from unilabos.server.services.materials.graph import reconcile_graph_payload

    payload = {"nodes": [{"id": "dev-1", "type": "device", "template_name": "dev_t", "config": {}}], "links": []}
    normalized, _summary = reconcile_graph_payload(
        payload, None, str(uuid4()), device_site_templates={"dev_t": WHITELIST_SITES},
    )
    first, second = normalized["nodes"][0]["sites"]
    assert "allowed_resource_template_uuids" not in first
    assert first["extra"] == {"allowed_resource_template_uuids": ["plate_96"]}
    assert not second.get("extra")
    for site in (first, second):
        ResourceSite.model_validate(site)


def test_registry_site_template_whitelist_takes_effect_in_authority(authority):
    """注册表 available_sites 里写的模板名白名单：设备在权威实例化后，放入不在白名单的模板报 6008。"""

    from unilabos.protocol.materials import (
        InventoryMutation,
        MaterialIdentityWrite,
        MaterialMove,
        MaterialNodeCreate,
        MaterialTreeCreate,
    )
    from unilabos.server.services.materials import MaterialConflictError

    owner_uuid = str(uuid4())
    device_config = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    apply_device_authority_state(
        device_config, {"available_sites": WHITELIST_SITES}, "available_sites_test_device", gateway=authority,
    )
    a1, a2 = device_config.res_content.sites
    assert a1.extra == {"allowed_resource_template_uuids": ["plate_96"]} and a2.extra == {}
    # 再次装配同一设备：权威快照与注册表定义一致（白名单从 extra 读回），不算定义漂移
    again = _device_resource(uuid=owner_uuid, sites=None, sites_initialized=False)
    apply_device_authority_state(
        again, {"available_sites": WHITELIST_SITES}, "available_sites_test_device", gateway=authority,
    )
    assert [site.uuid for site in again.res_content.sites] == [a1.uuid, a2.uuid]

    service = authority.service

    def mutation(operation: str) -> InventoryMutation:
        return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation,
                                 actor_type="scheduler")

    def material(template_name: str) -> str:
        created = service.create_tree(mutation("create_material_tree"), MaterialTreeCreate(nodes=[MaterialNodeCreate(
            client_ref="m", identity=MaterialIdentityWrite(
                resource_id=f"{template_name}-{uuid4().hex[:6]}", name=template_name, resource_type="container",
                class_name="Container", template_name=template_name),
        )]))
        return created.data.root_material_uuid

    plate, tube = material("plate_96"), material("tube_15ml")
    with pytest.raises(MaterialConflictError, match="只接受资源模板 plate_96") as raised:
        service.move_material(mutation("move_material"), MaterialMove(material_uuid=tube, destination_site_uuid=a1.uuid))
    assert raised.value.conflict == "site_template"
    service.move_material(mutation("move_material"), MaterialMove(material_uuid=tube, destination_site_uuid=a2.uuid))
    service.move_material(mutation("move_material"), MaterialMove(material_uuid=plate, destination_site_uuid=a1.uuid))
    assert service.get_site(a1.uuid).occupied_material_uuid == plate
