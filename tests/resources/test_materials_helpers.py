from uuid import uuid4

from unilabos.client.materials import LocalMaterialsClient
from unilabos.resources import materials
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.services.materials import MaterialsService


class _SubstanceTarget:
    name = "target"

    def __init__(self) -> None:
        self.substances = []

    def set_liquids(self, substances) -> None:
        self.substances = substances


def test_material_helper_writes_solid_substance() -> None:
    target = _SubstanceTarget()

    result = materials.apply_substances(
        target,
        names=["NaCl"],
        amounts=[250.0],
        is_solid=[True],
    )

    assert result == [target]
    assert target.substances == [("NaCl", 250.0, materials.SOLID_UNIT)]


class _OrderedParent:
    """带 _ordering 的最小父级替身（deck/carrier 的 slot 解析面）。"""

    def __init__(self, keys) -> None:
        self._ordering = {key: None for key in keys}


def test_resolve_site_spot_prefers_label_over_digits() -> None:
    parent = _OrderedParent(["12", "A1", "B1"])

    # "12" 是 label：优先按 label 命中 index 0，而不是被 isdigit 当索引 12
    assert materials.resolve_site_spot(parent, "12") == 0
    assert materials.resolve_site_spot(parent, "A1") == 1
    # 非 label 的纯数字字符串按 0-based 索引
    assert materials.resolve_site_spot(parent, "2") == 2
    # int 是内部宽容形态（等价数字字符串）
    assert materials.resolve_site_spot(parent, 1) == 1
    # 未指定：由父级默认排布
    assert materials.resolve_site_spot(parent, None) is None
    assert materials.resolve_site_spot(parent, "") is None


def _graph_tree(root_uuid: str, child_uuid: str) -> ResourceTreeSet:
    """模拟开机图中的一棵「已带 uuid」的物料树。"""

    return ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "boot-carrier",
                "uuid": root_uuid,
                "name": "boot-carrier",
                "type": "container",
                "class": "Container",
                "config": {"type": "Container"},
                "data": {},
                "extra": {},
                "template_name": "boot-carrier-template",
                "pose": {"position": {"x": 0, "y": 0, "z": 0}},
                "sites": [],
                "sites_initialized": True,
            },
            {
                "id": "boot-tube",
                "uuid": child_uuid,
                "parent_uuid": root_uuid,
                "name": "boot-tube",
                "type": "container",
                "class": "Container",
                "config": {"type": "Container"},
                "data": {},
                "extra": {},
                "template_name": "boot-tube-template",
                "pose": {"position": {"x": 0, "y": 0, "z": 0}},
                "sites": [],
                "sites_initialized": True,
            },
        ]
    )


def test_materials_ensure_adopts_graph_uuid_and_is_idempotent(tmp_path) -> None:
    """开机权威对齐采用图中 uuid，并在重复调用时复用权威记录。

    Host 与 Slave 使用同一个 ``materials.ensure`` 入口。
    """
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    try:
        root_uuid, child_uuid = str(uuid4()), str(uuid4())

        ensured = materials.ensure(
            _graph_tree(root_uuid, child_uuid), gateway=gateway
        )
        assert len(ensured.trees) == 1
        assert ensured.trees[0].root_node.res_content.uuid == root_uuid
        assert {node.res_content.uuid for node in ensured.all_nodes} == {
            root_uuid,
            child_uuid,
        }
        # 权威记录采用图中 UUID。
        assert (
            service.get_material(root_uuid).material.material_uuid == root_uuid
        )

        # 同一张图再次对齐时命中已有记录。
        again = materials.ensure(
            _graph_tree(root_uuid, child_uuid), gateway=gateway
        )
        assert again.trees[0].root_node.res_content.uuid == root_uuid
        roots = [
            item
            for item in service.list_materials(roots_only=True)
        ]
        assert len(roots) == 1
    finally:
        service.close()


def test_materials_ensure_instantiates_device_sites_from_registry(tmp_path, monkeypatch) -> None:
    """图里未实例化 Site 的设备节点（Graph Authority 不可达时的原始启动文件）：
    ensure 把注册表 available_sites 定义带进创建请求，权威发号；设备一落库就带齐位点。"""
    from unilabos.resources.adapters import device_site

    monkeypatch.setattr(
        device_site,
        "registry_device_site_templates",
        lambda: {
            "boot_device": [
                {"index": 1, "label": "slot_1", "allowed_resource_categories": ["plate"]},
                {"index": 2, "label": "slot_2"},
            ]
        },
    )
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    try:
        device_uuid, deck_uuid = str(uuid4()), str(uuid4())
        tree = ResourceTreeSet.from_raw_dict_list(
            [
                {
                    "id": "heater",
                    "uuid": device_uuid,
                    "name": "heater",
                    "type": "device",
                    "class": "boot_device",
                    "template_name": "boot_device",
                    "config": {},
                    "data": {},
                    "extra": {},
                },
                {
                    "id": "deck",
                    "uuid": deck_uuid,
                    "parent_uuid": device_uuid,
                    "name": "deck",
                    "type": "container",
                    "class": "Container",
                    "config": {"type": "Container"},
                    "data": {},
                    "extra": {},
                    "template_name": "boot-deck-template",
                    "sites": [],
                    "sites_initialized": True,
                },
            ]
        )
        assert tree.root_nodes[0].res_content.sites_initialized is False

        ensured = materials.ensure(tree, gateway=gateway)

        device = ensured.trees[0].root_node.res_content
        assert device.uuid == device_uuid
        assert device.sites_initialized is True
        assert [site.label for site in device.sites] == ["slot_1", "slot_2"]
        assert all(site.material_uuid == device_uuid and site.uuid for site in device.sites)
        assert device.sites[0].allowed_resource_categories == ["plate"]
        # 非设备节点不受影响；子树仍随根一起落库
        assert service.get_material(deck_uuid).material.parent_material_uuid == device_uuid
        assert service.get_material(deck_uuid).sites == []
    finally:
        service.close()


def _device_graph(device_uuid: str, *children: dict) -> ResourceTreeSet:
    return ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "heater",
                "uuid": device_uuid,
                "name": "heater",
                "type": "device",
                "class": "boot_device",
                "template_name": "boot_device",
                "config": {},
                "data": {},
                "extra": {},
            },
            *children,
        ]
    )


def _plate(plate_uuid: str, parent_uuid: str, name: str = "plate") -> dict:
    return {
        "id": name,
        "uuid": plate_uuid,
        "parent_uuid": parent_uuid,
        "name": name,
        "type": "container",
        "class": "Container",
        "config": {"type": "Container"},
        "data": {},
        "extra": {},
        "template_name": "boot-plate-template",
        "sites": [],
        "sites_initialized": True,
    }


def test_materials_ensure_creates_graph_additions_under_existing_root(tmp_path, monkeypatch) -> None:
    """根已在权威：图里新挂到设备下的物料补建（adopt uuid）并挂到图声明的父节点与位点。"""
    from unilabos.resources.adapters import device_site

    monkeypatch.setattr(
        device_site,
        "registry_device_site_templates",
        lambda: {"boot_device": [{"index": 1, "label": "slot_1"}, {"index": 2, "label": "slot_2"}]},
    )
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    try:
        device_uuid = str(uuid4())
        first = materials.ensure(_device_graph(device_uuid), gateway=gateway)
        device = first.trees[0].root_node.res_content
        assert [site.label for site in device.sites] == ["slot_1", "slot_2"]

        # 图文件里给设备加了一块板，放在 slot_2；权威此前不知道它
        plate_uuid = str(uuid4())
        graph = _device_graph(device_uuid, _plate(plate_uuid, device_uuid))
        graph_device = graph.root_nodes[0].res_content
        graph_device.sites = [
            site.model_copy(update={"occupied_material_uuid": plate_uuid})
            if site.label == "slot_2"
            else site
            for site in device.sites
        ]
        graph_device.sites_initialized = True

        ensured = materials.ensure(graph, gateway=gateway)

        plate = service.get_material(plate_uuid)
        assert plate.material.parent_material_uuid == device_uuid
        slot_2 = next(site for site in ensured.trees[0].root_node.res_content.sites if site.label == "slot_2")
        assert slot_2.occupied_material_uuid == plate_uuid
        assert {node.res_content.uuid for node in ensured.all_nodes} == {device_uuid, plate_uuid}
        assert len(service.list_materials(roots_only=True)) == 1  # 板不是独立根，挂在设备下

        # 再对齐一次：幂等，不重复创建、不重复 move
        again = materials.ensure(graph, gateway=gateway)
        assert {node.res_content.uuid for node in again.all_nodes} == {device_uuid, plate_uuid}
        assert service.get_material(plate_uuid).material.version == plate.material.version
    finally:
        service.close()


def test_materials_ensure_leaves_materials_moved_elsewhere_alone(tmp_path, monkeypatch) -> None:
    """图还说板在 A 设备下，但运行期它已被移到 B：以权威为准，不重建也不挪回。"""
    from unilabos.protocol.materials import MaterialMove, InventoryMutation
    from unilabos.resources.adapters import device_site

    monkeypatch.setattr(device_site, "registry_device_site_templates", lambda: {})
    service = MaterialsService(tmp_path / "materials.db")
    gateway = LocalMaterialsClient(service)
    try:
        device_a, device_b, plate_uuid = str(uuid4()), str(uuid4()), str(uuid4())
        graph_a = _device_graph(device_a, _plate(plate_uuid, device_a))
        graph_b = ResourceTreeSet.from_raw_dict_list(
            [{**_device_graph(device_b).dump()[0][0], "id": "heater_b", "name": "heater_b"}]
        )
        materials.ensure(graph_a, gateway=gateway)
        materials.ensure(graph_b, gateway=gateway)
        command_uuid = str(uuid4())
        gateway.move_material(
            InventoryMutation(
                command_uuid=command_uuid, effect_key=f"move:{command_uuid}", operation="move_material"
            ),
            MaterialMove(material_uuid=plate_uuid, parent_material_uuid=device_b),
        )

        ensured = materials.ensure(graph_a, gateway=gateway)

        assert {node.res_content.uuid for node in ensured.all_nodes} == {device_a}
        assert service.get_material(plate_uuid).material.parent_material_uuid == device_b
    finally:
        service.close()


def test_materials_ensure_records_actor_in_ledger(tmp_path) -> None:
    """ensure 的调用方身份落到账本 actor_type/actor_uuid，前端据此渲染来源 tag。

    未显式传入时兜底 ``edge``；开机图对齐传 ``graph`` + 图 uuid。
    """
    from unilabos.protocol.materials import ACTOR_EDGE, ACTOR_GRAPH

    def _rows_for(service: MaterialsService, root_uuid: str):
        return [
            row
            for row in service.changes(after_sequence=0, limit=100)
            if row.aggregate_uuid == root_uuid
        ]

    # 根物料名在同一权威内唯一，两种来源各用一份 db。
    default_service = MaterialsService(tmp_path / "default.db")
    try:
        default_root = str(uuid4())
        materials.ensure(
            _graph_tree(default_root, str(uuid4())),
            gateway=LocalMaterialsClient(default_service),
        )
        default_rows = _rows_for(default_service, default_root)
        assert default_rows
        assert {row.actor_type for row in default_rows} == {ACTOR_EDGE}
    finally:
        default_service.close()

    graph_service = MaterialsService(tmp_path / "graph.db")
    try:
        graph_root = str(uuid4())
        materials.ensure(
            _graph_tree(graph_root, str(uuid4())),
            gateway=LocalMaterialsClient(graph_service),
            actor_type=ACTOR_GRAPH,
            actor_uuid="graph-uuid-1",
        )
        graph_rows = _rows_for(graph_service, graph_root)
        assert graph_rows
        assert {row.actor_type for row in graph_rows} == {ACTOR_GRAPH}
        assert {row.actor_uuid for row in graph_rows} == {"graph-uuid-1"}
    finally:
        graph_service.close()
