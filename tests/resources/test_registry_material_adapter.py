"""Registry 生成定义与微后端模板模型的映射测试。"""

from uuid import uuid4

from unilabos.resources.adapters.registry_materials import register_resource_definitions
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.client.materials import LocalMaterialsClient
from unilabos.server.services.materials import MaterialsService


def test_plr_definition_with_float_keys_is_registered_and_reads_back(tmp_path) -> None:
    """PyLabRobot 1.0 的孔把 ``height_volume_data`` 序列化成 ``{float: float}``（PLR 库里的 Corning 96 孔板）。

    物料边界已把键转成字符串，但注册表条目不一定经过那里：上一版写下的 config_info 缓存（按资源模块
    源码哈希复用）、注册表 YAML 里的数字键。模板同步把定义规范成 JSON 时同样按 JSON 规则转成字符串，
    不能报 "Dict key must be str" 让 Host 起不来；读回的模板交给 PLR 反序列化，孔的
    ``height_volume_data`` 还原成 float 键，按液面高度算出的体积不变。
    """

    from pylabrobot.resources import cor_96_wellplate_360uL_Fb

    nodes = ResourceTreeSet.from_plr_resources(
        [cor_96_wellplate_360uL_Fb(name="corning_plate")], known_random_uuid=True, old_size=True
    ).dump_templates()[0]
    # 旧缓存里的 config_info：键还是 PLR 原样的 float
    for node in nodes:
        volumes = node["config"].get("height_volume_data")
        if volumes:
            node["config"]["height_volume_data"] = {float(key): value for key, value in volumes.items()}
    assert all(isinstance(key, float) for key in nodes[1]["config"]["height_volume_data"])
    definition = {
        "id": "corning_plate",
        "display_name": "Corning 96",
        "class": {"module": "pylabrobot.resources:cor_96_wellplate_360uL_Fb", "type": "pylabrobot"},
        "category": ["plate"],
        "config_info": nodes,
        "handles": [],
    }
    service = MaterialsService(tmp_path / "materials.db")
    client = LocalMaterialsClient(service)
    try:
        report = register_resource_definitions([definition], client)
        stored = client.get_template(report.template_uuids["corning_plate"]).definition["config_info"]
        # 再同步一次：同一份定义的哈希稳定，不因键的形态变化而反复更新
        assert register_resource_definitions([definition], client) == report
    finally:
        service.close()

    assert all(isinstance(key, str) for key in stored[1]["config"]["height_volume_data"])
    uuids = {node["id"]: str(uuid4()) for node in stored}
    flat = [
        {
            **{key: value for key, value in node.items() if key != "parent_id"},
            "uuid": uuids[node["id"]],
            "parent_uuid": uuids[node["parent_id"]] if node["parent_id"] else None,
        }
        for node in stored
    ]
    plate = ResourceTreeSet.load([flat]).to_plr_resources()[0]
    original = cor_96_wellplate_360uL_Fb(name="reference").children[0]
    well = plate.children[0]
    assert well.height_volume_data == original.height_volume_data
    assert all(isinstance(key, float) for key in well.height_volume_data)
    assert well.compute_volume_from_height(3.3) == original.compute_volume_from_height(3.3)


def test_registry_definition_is_registered_once_with_promoted_fields(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    client = LocalMaterialsClient(service)
    definitions = [
        {
            "id": "lab_beaker",
            "display_name": "Lab Beaker",
            "class": {
                "module": "pylabrobot.resources",
                "type": "RegularContainer",
            },
            "category": ["container"],
            "config_info": [
                {
                    "id": "root",
                    "type": "container",
                    "config": {
                        "sites": [
                            {"index": 0, "label": "slot", "content_type": []}
                        ]
                    },
                }
            ],
            "handles": [],
        }
    ]
    try:
        first = register_resource_definitions(definitions, client)
        second = register_resource_definitions(definitions, client)
        template = client.get_template(first.template_uuids["lab_beaker"])

        assert second == first
        assert template.resource_type == "container"
        # 模板的 class 是初始化物料用的注册表名（条目 id），实现模块在 module_name
        assert template.class_name == "lab_beaker"
        assert template.category == ["container"]
        assert template.available_sites[0]["label"] == "slot"
        assert "category" not in template.definition
        assert "handles" not in template.definition
    finally:
        service.close()


def test_registry_sync_only_records_material_changes_that_change_something(tmp_path) -> None:
    """模板同步是需要上报的物料变更，但只在真的有变化时才登记。

    首次登记 → 一条 create；同一注册表再同步（每次重启都会发生）→ 零条新账本行、
    零次写操作；定义变了 → 恰好一条 update。前端"物料变更"列表以账本为数据源，
    hash 一致的重复同步不能把它刷屏。
    """

    service = MaterialsService(tmp_path / "materials.db")
    client = LocalMaterialsClient(service)

    def _definition(name: str, *, display_name: str) -> dict:
        return {
            "id": name,
            "display_name": display_name,
            "class": {"module": "pylabrobot.resources", "type": "RegularContainer"},
            "category": ["container"],
            "config_info": [{"id": "root", "type": "container", "config": {"sites": []}}],
            "handles": [],
        }

    def _template_rows():
        return [
            (row.aggregate_uuid, row.operation, row.actor_type, row.actor_uuid)
            for row in service.changes(after_sequence=0, limit=100)
            if row.aggregate_type == "resource_template"
        ]

    try:
        definitions = [_definition("plate_a", display_name="A"), _definition("plate_b", display_name="B")]
        first = register_resource_definitions(definitions, client)
        rows = _template_rows()
        assert [row[1:] for row in rows] == [
            ("create", "registry", "plate_a"),
            ("create", "registry", "plate_b"),
        ]

        # 重启重放同一注册表：hash 一致，不登记任何变更
        writes: list[str] = []
        original_put = client.put_template

        def _spy_put(mutation, value):
            writes.append(value.name)
            return original_put(mutation, value)

        client.put_template = _spy_put  # type: ignore[method-assign]
        second = register_resource_definitions(definitions, client)
        assert second == first
        assert writes == []
        assert _template_rows() == rows

        # 注册表里某个定义真的变了：只有它进变更流，且是 update 而不是重新 create
        definitions[1] = _definition("plate_b", display_name="B v2")
        third = register_resource_definitions(definitions, client)
        assert third.template_uuids == first.template_uuids
        assert writes == ["plate_b"]
        new_rows = _template_rows()
        assert new_rows[:2] == rows
        assert new_rows[2] == (first.template_uuids["plate_b"], "update", "registry", "plate_b")
        assert len(new_rows) == 3
    finally:
        service.close()
