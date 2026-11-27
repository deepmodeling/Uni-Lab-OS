"""验证 OS 本地库存（Inventory）的旧 HostNode 物料查询兼容投影。"""

from __future__ import annotations

import json
from copy import deepcopy

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.api import create_app
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.resources.resource_tracker import (
    ResourceDict,
    ResourceTreeSet,
)


def _service() -> InventoryService:
    """建立含父子物料和旧 relation 库位关系的内存库存服务。

    参数：无。返回：可通过公共 HTTP 兼容接口查询的 ``InventoryService``；
    初始化或数据库异常原样传播。
    """

    service = InventoryService(InventoryStore(":memory:"))
    service.upsert_template(
        "tpl-rack",
        name="Rack template",
        category="container",
        spec={
            "storage_class": "ambient",
            "resource": {
                "id": "rack-logical-id",
                "name": "rack-a",
                "type": "container",
                "class": "",
                "config": {"size_x": 120, "size_y": 80, "size_z": 20},
                "data": {"template_state": True},
                "extra": {"fixture": "rack"},
            },
        },
    )
    service.upsert_template(
        "tpl-tube",
        name="Tube template",
        category="container",
        spec={
            "resource_dict": {
                "id": "tube-logical-id",
                "name": "tube-a1",
                "type": "container",
                "class": "",
                "config": {},
                "data": {"max_volume": 2.0},
                "extra": {},
            }
        },
    )
    service.register_instance(
        template_id="tpl-rack",
        edge_uuid="edge-rack",
        legacy_cloud_id="cloud-rack",
        barcode="RACK-001",
    )
    service.register_instance(
        template_id="tpl-tube",
        edge_uuid="edge-tube",
        barcode="TUBE-001",
        parent_uuid="edge-rack",
        slot_id="A1",
    )
    service.update_content(
        "edge-tube",
        {
            "data": {"temperature_c": 4},
            "liquids": [["water", 1.5]],
            "liquid_history": [["water", 1.5]],
            "unknown_counter": 2,
            "substance": "water",
        },
    )
    return service


def test_legacy_query_returns_flat_resource_dict_tree() -> None:
    client = TestClient(create_app(_service()))

    response = client.post(
        "/api/v1/edge/material/query",
        json={"uuids": ["edge-rack"], "with_children": True},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    nodes = response.json()["data"]["nodes"]
    assert [node["uuid"] for node in nodes] == ["edge-rack", "edge-tube"]
    assert nodes[1]["parent_uuid"] == "edge-rack"
    assert nodes[1]["extra"]["update_resource_site"] == "A1"
    assert nodes[1]["extra"]["edge_inventory"]["template_id"] == "tpl-tube"
    assert nodes[1]["data"] == {
        "max_volume": 2.0,
        "temperature_c": 4,
        "substance": "water",
    }
    assert nodes[1]["liquids"] == [["water", 1.5]]
    assert nodes[1]["liquid_history"] == [["water", 1.5]]
    assert nodes[1]["unknown_counter"] == 2

    for node in nodes:
        ResourceDict.model_validate(node)
    tree_set = ResourceTreeSet.from_raw_dict_list(deepcopy(nodes))
    assert len(tree_set.trees) == 1
    assert tree_set.trees[0].root_node.res_content.uuid == "edge-rack"
    assert tree_set.trees[0].root_node.children[0].res_content.uuid == "edge-tube"


def test_canonical_generic_resource_instance_remains_plr_convertible() -> None:
    """规范物料字段不得在旧 HostNode 查询边界降级或丢失。"""

    service = InventoryService(InventoryStore(":memory:"))
    service.upsert_template(
        "tpl-beaker",
        name="SZLab 500 mL 烧杯",
        category="resource",
        spec={"format": "xacro", "entry": "beaker/resource.xacro"},
    )
    service.register_instance(
        template_id="tpl-beaker",
        edge_uuid="edge-beaker",
    )
    with service.store.transaction() as connection:
        connection.execute(
            "UPDATE material SET description=?,class=?,name=?,config=?,data=? "
            "WHERE uuid=?",
            (
                "S03/S11 使用的 500 mL 烧杯",
                "community.szlab.beaker_500ml",
                "烧杯堆栈 L1B1 烧杯",
                json.dumps(
                    {
                        "size_x": 86,
                        "size_y": 86,
                        "size_z": 120,
                        "category": "beaker",
                        "num_items_x": 6,
                    }
                ),
                json.dumps({"sample_id": "sample-001"}),
                "edge-beaker",
            ),
        )

    nodes = TestClient(create_app(service)).post(
        "/api/v1/edge/material/query",
        json={"uuids": ["edge-beaker"], "with_children": True},
    ).json()["data"]["nodes"]
    resource = ResourceTreeSet.from_raw_dict_list(deepcopy(nodes)).to_plr_resources()[0]

    assert nodes[0]["name"] == "烧杯堆栈 L1B1 烧杯"
    assert nodes[0]["description"] == "S03/S11 使用的 500 mL 烧杯"
    assert nodes[0]["class"] == "community.szlab.beaker_500ml"
    assert nodes[0]["config"]["category"] == "beaker"
    assert nodes[0]["config"]["num_items_x"] == 6
    assert nodes[0]["data"]["sample_id"] == "sample-001"
    assert resource.name == "烧杯堆栈 L1B1 烧杯"
    assert resource.category == "beaker"


def test_query_supports_legacy_cloud_uuid_id_and_without_children() -> None:
    client = TestClient(create_app(_service()))

    by_cloud_uuid = client.post(
        "/api/v1/edge/material/query",
        json={"uuids": ["cloud-rack"], "with_children": False},
    ).json()["data"]["nodes"]
    by_logical_id = client.post(
        "/api/v1/edge/material/query",
        json={"id": "tube-logical-id", "with_children": True},
    ).json()["data"]["nodes"]

    assert [node["uuid"] for node in by_cloud_uuid] == ["edge-rack"]
    assert [node["uuid"] for node in by_logical_id] == ["edge-tube"]


def test_query_deduplicates_overlapping_roots_and_validates_selector() -> None:
    client = TestClient(create_app(_service()))

    response = client.post(
        "/api/v1/edge/material/query",
        json={"uuids": ["edge-rack", "edge-tube"], "with_children": True},
    )

    assert [node["uuid"] for node in response.json()["data"]["nodes"]] == [
        "edge-rack",
        "edge-tube",
    ]
    assert (
        client.post(
            "/api/v1/edge/material/query",
            json={"uuids": [], "with_children": True},
        ).status_code
        == 422
    )
