"""Registry 生成定义与微后端模板模型的映射测试。"""

from unilabos.server.adapters.registry_materials import register_resource_definitions
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
        assert template.category == ["container"]
        assert template.available_sites[0]["label"] == "slot"
        assert "category" not in template.definition
        assert "handles" not in template.definition
    finally:
        service.close()
