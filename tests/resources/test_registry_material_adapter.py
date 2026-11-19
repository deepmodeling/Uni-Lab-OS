"""Registry 生成定义与微后端模板模型的映射测试。"""

from unilabos.resources.adapters.registry_materials import register_resource_definitions
from unilabos.client.materials import LocalMaterialsClient
from unilabos.server.services.materials import MaterialsService


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
        assert template.class_name == "RegularContainer"
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
