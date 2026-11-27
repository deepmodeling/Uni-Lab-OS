"""验证库存（Inventory）把权威库位（Site）身份投影为真实 PLR 载架。"""

from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.api import create_app
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.resources.itemized_carrier import BottleCarrier
from unilabos.resources.resource_tracker import ResourceTreeSet

SITE_UUID = "61000000-0000-4000-8000-000000000001"


def _carrier_prototype() -> dict[str, object]:
    """构造注册表（Registry）保存的离散载架资源原型。

    参数：无。返回：与 ``config_info`` 单根资源形状一致的父物料模板；库位 UUID
    故意留空，证明稳定身份只能来自库存权威（Inventory Authority）。
    """

    return {
        "id": "carrier-prototype",
        "uuid": "prototype-carrier",
        "name": "carrier",
        "description": "",
        "schema": {},
        "model": {},
        "icon": "",
        "parent_uuid": None,
        "type": "bottle_carrier",
        "class": "",
        "pose": {
            "position": {"x": 0, "y": 0, "z": 0},
            "size": {"width": 100, "height": 100, "depth": 20},
        },
        "config": {
            "type": "BottleCarrier",
            "size_x": 100,
            "size_y": 100,
            "size_z": 20,
            "category": "bottle_carrier",
            "sites": [
                {
                    "label": "L1B1",
                    "name": "L1B1",
                    "visible": True,
                    "occupied_by": "material",
                    "position": {"x": 0, "y": 0, "z": 0},
                    "size": {"width": 10, "height": 10, "depth": 10},
                    "content_type": ["container"],
                }
            ],
        },
        "data": {},
        "extra": {},
        "machine_name": "",
        "barcode": "",
        "barcode_symbology": "",
        "liquids": None,
        "liquid_history": None,
        "unknown_counter": None,
    }


def _service() -> InventoryService:
    """建立带注册表原型、父子物料和一个权威库位的内存服务。

    参数：无。返回：可走真实 HTTP 兼容查询的库存服务。异常：模板同步、物料
    登记或库位约束错误原样传播，避免测试以伪造投影绕过产品路径。
    """

    service = InventoryService(InventoryStore(":memory:"))
    synchronization = BackendResourceService(service.store).sync_resource_templates(
        [
            {
                "id": "carrier-template",
                "display_name": "Carrier",
                "registry_type": "resource",
                "class": {},
                "config_info": [_carrier_prototype()],
            },
            {
                "id": "material-template",
                "display_name": "Material",
                "registry_type": "resource",
                "class": {},
            },
        ]
    )
    # ``template_uuid_by_name`` 是同步回执确认的本代资源模板（ResourceTemplate）身份。
    template_uuid_by_name = {
        item["name"]: item["uuid"] for item in synchronization["templates"]
    }
    service.register_instance(
        template_id=template_uuid_by_name["carrier-template"],
        edge_uuid="carrier",
    )
    service.register_instance(
        template_id=template_uuid_by_name["material-template"],
        edge_uuid="material",
        parent_uuid="carrier",
        slot_id="L1B1",
    )
    with service.store.transaction() as connection:
        # ``material.name`` 必须和载架原型的 ``occupied_by`` 一致，供 PLR 恢复父子占用。
        connection.execute("UPDATE material SET name='material' WHERE uuid='material'")
        # 旧关系写路径已创建正确占用的库位行；测试只冻结其稳定 UUID。
        connection.execute(
            "UPDATE site SET uuid=? WHERE material_uuid=? AND name=? AND deleted_at IS NULL",
            (SITE_UUID, "carrier", "L1B1"),
        )
    return service


def test_inventory_projects_authoritative_site_uuid_into_real_plr_carrier() -> None:
    """库存查询必须恢复真实载架并把 UUID 放在一等 ``sites[]`` 字段。

    参数：无。返回：无；断言注册表原型决定 PLR 子类，库存权威决定稳定库位
    UUID，且扩展字段不再保存第二份名称映射。
    """

    nodes = TestClient(create_app(_service())).post(
        "/api/v1/edge/material/query",
        json={"uuids": ["carrier"], "with_children": True},
    ).json()["data"]["nodes"]

    parent_node = nodes[0]
    parent_resource = ResourceTreeSet.from_raw_dict_list(
        deepcopy(nodes)
    ).to_plr_resources()[0]

    assert parent_node["config"]["sites"][0]["uuid"] == SITE_UUID
    assert "unilabos_site_name_by_uuid" not in parent_node["extra"]
    assert isinstance(parent_resource, BottleCarrier)
    assert parent_resource.site_name_for_uuid(SITE_UUID) == "L1B1"
    assert parent_resource.site_name_for_child(parent_resource.children[0]) == "L1B1"
