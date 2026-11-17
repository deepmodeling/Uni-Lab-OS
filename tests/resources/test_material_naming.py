"""物料命名：``display_name`` 是人读标签，PLR 键单独保存，物料层 ``name`` 等于 uuid。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.plr_fixtures import plate
from unilabos.client.materials import LocalMaterialsClient
from unilabos.protocol.materials import (
    InventoryMutation,
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialPatch,
    MaterialTreeCreate,
)
from unilabos.resources import materials
from unilabos.resources.adapters.plr_materials import resource_tree_to_snapshot
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(command_uuid=str(uuid4()), effect_key=operation, operation=operation)


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr("unilabos.resources.resource_tracker.register", lambda: None)
    materials_service = MaterialsService(tmp_path / "materials.db")
    yield materials_service
    materials_service.close()


def test_plr_labels_become_display_names() -> None:
    """根的标签是 PLR 名，孔位取 ordering 标识，而不是带板名前缀的 PLR 名。"""

    tree = ResourceTreeSet.from_plr_resources([plate("p1")], known_random_uuid=True)
    root = tree.root_nodes[0]
    assert root.res_content.display_name == "p1"
    first = root.children[0].res_content
    assert first.plr_name == "p1_well_A1"
    assert first.display_name == "A1"


def test_runtime_snapshot_keeps_the_authority_display_name(service) -> None:
    """设备快照不改展示名：用户在权威里改过的标签不被 PLR 序列化出的标签覆盖。"""

    root = materials.create(plate("p1"), gateway=LocalMaterialsClient(service))
    service.patch_material(
        _mutation("patch_material"), root.unilabos_uuid, MaterialPatch(display_name="我的板")
    )
    baseline = service.get_tree(root.unilabos_uuid)
    snapshot = resource_tree_to_snapshot(ResourceTreeSet.from_plr_resources([root]), baseline)
    by_uuid = {node.material.material_uuid: node.material for node in snapshot.nodes}
    assert by_uuid[root.unilabos_uuid].display_name == "我的板"


def _root(resource_id: str, *, name: str, display_name: str = "") -> MaterialTreeCreate:
    return MaterialTreeCreate(
        nodes=[
            MaterialNodeCreate(
                client_ref="root",
                identity=MaterialIdentityWrite(
                    resource_id=resource_id,
                    name=name,
                    display_name=display_name,
                    resource_type="container",
                    class_name="Container",
                    template_name="bottle",
                ),
            )
        ]
    )


def test_search_and_root_order_use_the_display_name(service) -> None:
    """按名字搜索与根物料排序都按人读标签，而不是物料 name。"""

    first = service.create_tree(
        _mutation("create_material_tree"), _root("bottle-b", name="bottle-b", display_name="alpha")
    ).data.root_material_uuid
    second = service.create_tree(
        _mutation("create_material_tree"), _root("bottle-a", name="bottle-a", display_name="beta")
    ).data.root_material_uuid

    assert [item.material.material_uuid for item in service.search_materials("alpha")] == [first]
    roots = [item.material.material_uuid for item in service.list_materials(roots_only=True)]
    assert roots == [first, second]


# ── 第 2 期：物料 name = uuid，PLR 键在 config.name ─────────────────────────


def _node(ref: str, *, name: str = "", display_name: str = "", parent: str | None = None,
          material_uuid: str | None = None, resource_type: str = "container",
          config: dict | None = None) -> MaterialNodeCreate:
    return MaterialNodeCreate(
        client_ref=ref,
        parent_client_ref=parent,
        material_uuid=material_uuid,
        identity=MaterialIdentityWrite(
            resource_id=f"{parent}/{ref}" if parent else ref,
            name=name,
            display_name=display_name,
            resource_type=resource_type,
            class_name="Container",
            template_name="bottle" if resource_type != "device" else "station",
            config=config or {},
        ),
    )


def _created(service, *nodes: MaterialNodeCreate):
    tree = service.create_tree(_mutation("create_material_tree"), MaterialTreeCreate(nodes=list(nodes))).data
    return {ref: service.get_material(uuid).material for ref, uuid in tree.client_ref_map.items()}


def test_create_tree_writes_the_uuid_as_name_and_migrates_old_labels(service) -> None:
    created = _created(
        service,
        _node("rack", name="rack-1"),
        _node("tube", name="tube-a", display_name="一号管", parent="rack"),
        _node("child", parent="rack"),
    )
    rack, tube, child = created["rack"], created["tube"], created["child"]
    for material in created.values():
        assert material.name == material.material_uuid
    # 旧名迁到 display_name（为空时）与 PLR 键 config.name（缺失时）
    assert (rack.display_name, rack.config["name"]) == ("rack-1", "rack-1")
    assert (tube.display_name, tube.config["name"]) == ("一号管", "tube-a")
    # 没有旧名的孩子：PLR 键取 uuid，展示名回退业务 id
    assert child.config["name"] == child.material_uuid and child.display_name == "rack/child"


def test_new_root_without_an_old_name_uses_its_resource_id_as_plr_key(service) -> None:
    first = _created(service, _node("bottle-1", display_name="试剂瓶"))["bottle-1"]
    second = _created(service, _node("bottle-2", display_name="试剂瓶"))["bottle-2"]
    assert first.config["name"] == "bottle-1" and second.config["name"] == "bottle-2"
    # 根物料的人读标签可以重名（不再有根物料名唯一索引）
    assert first.display_name == second.display_name == "试剂瓶"


def test_a_uuid_name_is_reused_as_the_identity(service) -> None:
    identity = str(uuid4())
    created = _created(service, _node("bottle", name=identity.upper()))["bottle"]
    assert created.material_uuid == created.name == identity
    assert created.display_name == "bottle"


def test_a_uuid_name_that_disagrees_with_the_identity_is_rejected(service) -> None:
    from unilabos.server.services.materials import MaterialConflictError

    with pytest.raises(MaterialConflictError) as mismatch:
        _created(service, _node("bottle", name=str(uuid4()), material_uuid=str(uuid4())))
    assert mismatch.value.conflict == "identity"
    existing = _created(service, _node("first"))["first"]
    with pytest.raises(MaterialConflictError, match="already exists"):
        _created(service, _node("second", name=existing.material_uuid))


def test_devices_keep_their_config_and_get_a_display_name(service) -> None:
    station = _created(service, _node("station", name="工作站", resource_type="device", config={"port": "COM3"}))
    device = station["station"]
    assert device.name == device.material_uuid
    assert device.config == {"port": "COM3"}
    assert device.display_name == "工作站"


def test_reagent_views_name_the_container_by_its_display_label(service) -> None:
    from unilabos.server.services.materials.contents import ContentContract

    bottle = _created(service, _node("bottle-1", display_name="缓冲液瓶"))["bottle-1"]
    contents = ContentContract(service, compound_source=None)
    chemical = contents.create_reagent_info({"name": "水", "cas": "7732-18-5"})["uuid"]
    contents.create_reagent({"material_uuid": bottle.material_uuid, "reagent_info_uuid": chemical,
                             "quantity": 5, "quantity_unit": "mL"})
    listed = contents.list_reagents(keyword="缓冲液")
    assert [item["container_name"] for item in listed["items"]] == ["缓冲液瓶"]


def test_the_database_refuses_a_name_other_than_the_uuid(service) -> None:
    import sqlite3

    material = _created(service, _node("bottle"))["bottle"]
    with pytest.raises(sqlite3.IntegrityError):
        with service.write() as connection:
            connection.execute("UPDATE material SET name='bottle' WHERE material_uuid=?", (material.material_uuid,))


def test_the_plr_key_is_immutable_and_name_is_not_patchable(service) -> None:
    from unilabos.server.services.materials import MaterialValidationError

    material = _created(service, _node("bottle", name="bottle-1", config={"max_volume": 5}))["bottle"]
    kept = service.patch_material(
        _mutation("patch_material"), material.material_uuid, MaterialPatch(config={"max_volume": 9})
    ).data.material
    assert kept.config == {"max_volume": 9, "name": "bottle-1"}
    with pytest.raises(MaterialValidationError, match="config.name"):
        service.patch_material(
            _mutation("patch_material"), material.material_uuid, MaterialPatch(config={"name": "other"})
        )
    with pytest.raises(ValueError):
        MaterialPatch.model_validate({"name": "改名"})


def test_plr_import_and_snapshot_keep_name_as_uuid(service) -> None:
    root = materials.create(plate("p1"), gateway=LocalMaterialsClient(service))
    authority = service.get_material(root.unilabos_uuid).material
    assert authority.name == authority.material_uuid
    assert authority.config["name"] == "p1" and authority.display_name == "p1"

    tree = ResourceTreeSet.from_plr_resources([root])
    node = tree.root_nodes[0].res_content
    assert node.name == node.uuid and node.plr_name == "p1"
    # 往返 PLR：名字仍是 PLR 键，不是 uuid
    restored = tree.to_plr_resources()[0]
    assert restored.name == "p1" and restored.children[0].name == "p1_well_A1"

    # 快照不回写 name；运行时 config 缺 PLR 键时沿用权威值
    node.config.pop("name")
    snapshot = resource_tree_to_snapshot(tree, service.get_tree(root.unilabos_uuid))
    written = next(item.material for item in snapshot.nodes if item.material.material_uuid == root.unilabos_uuid)
    assert written.name == root.unilabos_uuid and written.config["name"] == "p1"


def test_a_snapshot_cannot_rename_a_material(service) -> None:
    from unilabos.protocol.materials import MaterialSnapshot
    from unilabos.server.services.materials import MaterialValidationError

    material = _created(service, _node("bottle"))["bottle"]
    tree = service.get_tree(material.material_uuid)
    node = tree.nodes[0].model_copy(deep=True)
    node.material = node.material.model_copy(update={"name": "bottle", "description": "x"})
    snapshot = MaterialSnapshot(root_material_uuid=material.material_uuid, nodes=[node])
    with pytest.raises(MaterialValidationError, match="name"):
        service.apply_snapshot(_mutation("apply_material_snapshot"), snapshot)


def test_graph_upload_writes_uuid_names_and_migrates_labels(tmp_path) -> None:
    from unilabos.server.services.materials.graph import GraphService

    payload = {
        "nodes": [
            {"id": "station", "name": "工作站", "type": "device", "template_name": "station", "config": {"port": "COM3"}},
            {"id": "deck", "name": "deck_main", "type": "deck", "template_name": "Deck", "parent": "station",
             "config": {"type": "Deck"}},
            {"id": "plate", "name": "p1", "display_name": "样品板", "type": "plate", "template_name": "Plate",
             "parent": "deck", "config": {"type": "Plate"}},
        ],
        "links": [],
    }
    graphs = GraphService(tmp_path / "materials.db")
    try:
        first = graphs.upsert_graph(name="lab", payload=payload, device_site_templates={})
        nodes = {node["id"]: node for node in first["payload"]["nodes"]}
        for node in nodes.values():
            assert node["name"] == node["uuid"]
        assert nodes["station"]["display_name"] == "工作站" and nodes["station"]["config"] == {"port": "COM3"}
        assert nodes["deck"]["display_name"] == "deck_main" and nodes["deck"]["config"]["name"] == "deck_main"
        assert nodes["plate"]["display_name"] == "样品板" and nodes["plate"]["config"]["name"] == "p1"

        # 重新上传同一份旧名图、或导出的新图：幂等
        again = graphs.upsert_graph(name="lab", payload=payload, device_site_templates={})
        assert again["revision"] == first["revision"]
        exported = graphs.upsert_graph(name="lab", payload=first["payload"], device_site_templates={})
        assert exported["revision"] == first["revision"]
    finally:
        graphs.close()


def test_graph_upload_reuses_uuid_names_and_rejects_conflicts(tmp_path) -> None:
    from unilabos.server.services.materials.graph import GraphError, GraphService

    identity = str(uuid4())
    graphs = GraphService(tmp_path / "materials.db")
    try:
        stored = graphs.upsert_graph(
            name="lab", payload={"nodes": [{"id": "pump", "name": identity.upper(), "type": "device"}]}
        )
        node = stored["payload"]["nodes"][0]
        assert node["uuid"] == node["name"] == identity and node["display_name"] == "pump"
        with pytest.raises(GraphError) as conflict:
            graphs.upsert_graph(
                name="other",
                payload={"nodes": [{"id": "pump", "uuid": str(uuid4()), "name": str(uuid4()), "type": "device"}]},
            )
        assert conflict.value.code == "identity_conflict"
    finally:
        graphs.close()


def test_legacy_graph_boundary_moves_names_out_of_the_way() -> None:
    from unilabos.server.backend.legacy_adaptor.legacy.graph import (
        legacy_graph_markers,
        upgrade_legacy_graph_payload,
    )

    identity = str(uuid4())
    payload = {
        "nodes": [
            {"id": "station", "name": "工作站", "type": "device", "config": {}},
            {"id": "deck", "name": "deck_main", "type": "deck", "parent": "station", "config": {"type": "Deck"}},
            {"id": "plate", "name": identity, "type": "plate", "parent": "deck", "config": {"type": "Plate"}},
        ],
        "links": [],
    }
    assert legacy_graph_markers(payload) == {"name 不是 uuid": 2}
    reports: list[str] = []
    upgraded = upgrade_legacy_graph_payload(
        payload, source="lab.json", report=reports.append, resource_entry=lambda _name: None
    )
    station, deck, plate = upgraded["nodes"]
    assert "name 不是 uuid ×2" in reports[0]
    assert "name" not in station and station["display_name"] == "工作站" and station["config"] == {}
    assert "name" not in deck and deck["config"] == {"type": "Deck", "name": "deck_main"}
    # UUID 形式的 name 是身份，原样交给 Graph Authority 复用
    assert plate["name"] == identity
    assert legacy_graph_markers(upgraded) == {}


# ── 第 3 期：裸标签孔和管以 uuid 作 PLR 键 ───────────────────────────────


def test_two_opentrons_plates_and_two_tube_racks_share_one_deck(service) -> None:
    """Opentrons 板 / 管架的孔和管直接叫 "A1"：同一台面放两块时改键前在 PLR 树里撞名。"""

    from pylabrobot.resources.opentrons.deck import OTDeck
    from pylabrobot.resources.opentrons.plates import corning_96_wellplate_360ul_flat
    from pylabrobot.resources.opentrons.tube_racks import opentrons_24_tuberack_nest_1point5ml_screwcap

    from unilabos.protocol.materials import MaterialMove
    from unilabos.resources.adapters.plr_materials import material_tree_to_plr_resources

    gateway = LocalMaterialsClient(service)
    deck = materials.create(OTDeck(name="ot_deck", with_trash=False), gateway=gateway)
    labware = [
        materials.create(factory(name), gateway=gateway)
        for factory, name in (
            (corning_96_wellplate_360ul_flat, "plate_1"),
            (corning_96_wellplate_360ul_flat, "plate_2"),
            (opentrons_24_tuberack_nest_1point5ml_screwcap, "rack_1"),
            (opentrons_24_tuberack_nest_1point5ml_screwcap, "rack_2"),
        )
    ]
    first_items = []
    for item in labware:
        first = item.get_item("A1")
        first_items.append(first.unilabos_uuid)
        # PLR 键是 uuid，ordering 标识与人读标签仍是 "A1"
        assert first.name == first.unilabos_uuid and item["A1"][0] is first
        stored = service.get_material(first.unilabos_uuid).material
        assert stored.display_name == "A1" and stored.config["name"] == first.unilabos_uuid
        assert service.get_material(item.unilabos_uuid).material.config["ordering"]["A1"] == first.unilabos_uuid

    for slot, item in enumerate(labware, start=1):
        deck.assign_child_at_slot(item, slot)

    holders = {child.name: child.unilabos_uuid for child in deck.children}
    for slot, item in enumerate(labware, start=1):
        service.move_material(
            _mutation("move_material"),
            MaterialMove(material_uuid=item.unilabos_uuid, parent_material_uuid=holders[f"ot_deck_slot_{slot}"]),
        )
    snapshot = resource_tree_to_snapshot(
        ResourceTreeSet.from_plr_resources([deck]), service.get_tree(deck.unilabos_uuid)
    )
    service.apply_snapshot(_mutation("apply_material_snapshot"), snapshot)

    restored = material_tree_to_plr_resources(service.get_tree(deck.unilabos_uuid))[0]
    placed = restored.slots[:4]
    assert [item.unilabos_uuid for item in placed] == [item.unilabos_uuid for item in labware]
    assert [item.get_item("A1").unilabos_uuid for item in placed] == first_items


def test_graph_keys_bare_wells_by_uuid_and_leaves_prefixed_children(tmp_path) -> None:
    from unilabos.server.services.materials.graph import GraphService

    nodes: list[dict] = []
    expected: dict[str, dict[str, str]] = {}
    for plate in ("plate_1", "plate_2"):
        plate_uuid = str(uuid4())
        nodes.append({"id": plate, "uuid": plate_uuid, "name": plate, "type": "plate", "template_name": "Plate",
                      "config": {"type": "Plate", "ordering": {"A1": "A1", "B1": "B1"}}})
        expected[plate] = {}
        for well in ("A1", "B1"):
            well_uuid = str(uuid4())
            expected[plate][well] = well_uuid
            nodes.append({"id": well, "uuid": well_uuid, "name": well, "type": "well", "template_name": "Well",
                          "parent": plate, "parent_uuid": plate_uuid, "config": {"type": "Well"}})
    nodes.append({"id": "tips", "name": "tips", "type": "tip_rack", "template_name": "TipRack",
                  "config": {"type": "TipRack", "ordering": {"A1": "tips_A1"}}})
    nodes.append({"id": "tips_A1", "name": "tips_A1", "type": "tip_spot", "template_name": "TipSpot",
                  "parent": "tips", "config": {"type": "TipSpot"}})

    graphs = GraphService(tmp_path / "materials.db")
    try:
        stored = graphs.upsert_graph(name="ot", payload={"nodes": nodes, "links": []}, device_site_templates={})
        by_uuid = {node["uuid"]: node for node in stored["payload"]["nodes"]}
        by_id = {node["id"]: node for node in stored["payload"]["nodes"] if node["type"] != "well"}
        for plate, wells in expected.items():
            assert by_id[plate]["config"]["ordering"] == {label: uuid for label, uuid in wells.items()}
            for label, well_uuid in wells.items():
                well = by_uuid[well_uuid]
                assert well["config"]["name"] == well_uuid and well["display_name"] == label
        # 不是「名字等于 ordering 标识」的孩子不改键
        assert by_id["tips_A1"]["config"]["name"] == "tips_A1"
        assert by_id["tips"]["config"]["ordering"] == {"A1": "tips_A1"}
        again = graphs.upsert_graph(name="ot", payload={"nodes": nodes, "links": []}, device_site_templates={})
        assert again["revision"] == stored["revision"]
    finally:
        graphs.close()
