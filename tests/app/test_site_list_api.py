"""工站全库位列表的公共 HTTP 合同与资源归属测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_material_placement_api import _Registry, _ResourceTree
from unilabos.app.scheduler.inventory.backend_api import (
    SiteListResponse,
    install_backend_resource_api,
)
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.resource_graph_bootstrap import (
    bootstrap_local_resource_graph,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot


class _NestedDeviceTree(_ResourceTree):
    """在既有正式资源图夹具中增加一台内层设备及其库位。"""

    def dump(self) -> list[list[dict[str, Any]]]:
        """返回外层设备、内层设备及三个空库位的启动快照。"""

        graph = super().dump()
        outer, first, _ = graph[0]
        inner = deepcopy(outer)
        inner.update(
            id="inner_device",
            uuid="64000000-0000-4000-8000-0000000003b0",
            name="内层设备",
            parent_uuid=outer["uuid"],
        )
        site = deepcopy(first)
        site.update(
            id="inner_site",
            uuid="64000000-0000-4000-8000-0000000003b1",
            name="C1",
            parent_uuid=inner["uuid"],
        )
        graph[0].extend([inner, site])
        return graph


@pytest.fixture
def inventory(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, str]]]:
    """通过正式启动和 HTTP 写接口建立嵌套设备、载体、独立载体及占位物料。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        bootstrap_local_resource_graph(
            store=store,
            resource_tree_set=_NestedDeviceTree(),
            registry_snapshot=RegistryTemplateSnapshot.from_registry(_Registry()),
            source_id="/workspace/sites/graph.json",
        )
        app = FastAPI()
        install_backend_resource_api(app, BackendResourceService(store))
        with TestClient(app) as client:
            graph = client.get("/api/v1/materials/graph").json()["data"]
            ids = {node["material"]["name"]: node["material"]["uuid"] for node in graph["nodes"]}
            response = client.post(
                "/api/v1/resource-templates",
                json={
                    "resources": [
                        {"id": "site.sample", "registry_type": "resource", "class": {}},
                    ]
                },
            )
            assert response.json()["code"] == 0
            ids["sample_template"] = response.json()["data"]["templates"][0]["uuid"]
            response = client.post(
                "/api/v1/resource-templates",
                json={
                    "resources": [
                        {
                            "id": "site.rack",
                            "registry_type": "resource",
                            "class": {},
                            "available_sites": [
                                {
                                    "index": label,
                                    "label": label,
                                    "allowed_resource_template_uuids": [ids["sample_template"]],
                                }
                                for label in ("rack-1", "rack-2")
                            ],
                        }
                    ]
                },
            )
            assert response.json()["code"] == 0
            rack_template = response.json()["data"]["templates"][0]["uuid"]
            for name, parent in (
                ("仓架%_A", "内层设备"),
                ("载板", "仓架%_A"),
                ("独立载体", None),
                ("样品", None),
            ):
                response = client.post(
                    "/api/v1/materials",
                    json={
                        "name": name,
                        "resource_template_uuid": ids["sample_template"] if name == "样品" else rack_template,
                        "parent_uuid": ids[parent] if parent else None,
                    },
                )
                assert response.status_code == 201 and response.json()["code"] == 0
                ids[name] = response.json()["data"]["uuid"]
            ids["occupied_site"] = client.get(f"/api/v1/materials/{ids['仓架%_A']}/sites").json()["data"][0]["uuid"]
            response = client.put(
                f"/api/v1/materials/{ids['样品']}",
                json={
                    "site_placement": {"action": "place", "site_uuid": ids["occupied_site"]},
                },
            )
            assert response.json()["code"] == 0
            yield client, ids
    finally:
        store.close()


def _list(client: TestClient, **params: Any) -> dict[str, Any]:
    """读取公共全库位列表并确认标准成功信封。"""

    response = client.get("/api/v1/sites", params=params)
    assert response.status_code == 200
    assert response.json()["code"] == 0
    SiteListResponse.model_validate(response.json())
    return response.json()["data"]


def test_all_sites_paginate_without_duplicates_and_keep_nearest_device(inventory) -> None:
    """跨页汇集全库位；深层载体归属最近设备，独立载体允许设备为空。"""

    client, ids = inventory
    pages = [_list(client, page=page, page_size=2) for page in range(1, 6)]
    assert [page["has_more"] for page in pages] == [True, True, True, True, False]
    assert [page["page"] for page in pages] == [1, 2, 3, 4, 5]
    assert all(page["page_size"] == 2 for page in pages)
    items = [item for page in pages for item in page["items"]]
    assert len(items) == len({item["uuid"] for item in items}) == 9
    assert _list(client, page_size=100)["items"] == items
    for item in items:
        UUID(item["uuid"])
        UUID(item["material_uuid"])
        assert item["name"]
        owner = item["material_name"]
        if owner == "独立载体":
            assert item["device_uuid"] is None and item["device_name"] is None
        else:
            device = "上下料载具" if owner == "上下料载具" else "内层设备"
            assert (item["device_uuid"], item["device_name"]) == (ids[device], device)
        if item["uuid"] == ids["occupied_site"]:
            assert item["occupied_material_uuid"] == ids["样品"]
            assert item["occupied_material_name"] == "样品"
        else:
            assert item["occupied_material_uuid"] is None
            assert item["occupied_material_name"] is None
        if owner in ("仓架%_A", "载板", "独立载体"):
            assert item["allowed_resource_template_uuids"] == [ids["sample_template"]]


def test_filters_combine_and_keyword_is_literal(inventory) -> None:
    """设备、直接载体、物理占位及关键词筛选在分页前共同生效。"""

    client, ids = inventory
    assert len(_list(client, device_uuid=ids["上下料载具"])["items"]) == 2
    assert len(_list(client, device_uuid=ids["内层设备"])["items"]) == 5
    assert len(_list(client, material_uuid=ids["载板"])["items"]) == 2
    assert len(_list(client, occupied=False)["items"]) == 8
    occupied = _list(client, occupied=True, device_uuid=ids["内层设备"], keyword="%_A")
    assert [item["uuid"] for item in occupied["items"]] == [ids["occupied_site"]]
    assert not occupied["has_more"]
    assert len(_list(client, keyword="内层设备")["items"]) == 5
    assert len(_list(client, keyword="RACK-1")["items"]) == 3
    assert _list(client, device_uuid=ids["上下料载具"], occupied=True)["items"] == []


def test_defaults_limits_validation_and_existing_site_routes(inventory) -> None:
    """分页边界与参数错误沿用公共合同，已有按载体查询和详情不变。"""

    client, ids = inventory
    result = _list(client, page=-1, page_size=-1)
    assert set(result) == {"items", "has_more", "page", "page_size"}
    assert (result["page"], result["page_size"], result["has_more"]) == (1, 20, False)
    assert _list(client, page_size=101)["page_size"] == 100
    assert _list(client, page=20)["items"] == []
    assert _list(client, page=20)["has_more"] is False
    for params in ({"device_uuid": "bad"}, {"material_uuid": "bad"}, {"occupied": "bad"}):
        response = client.get("/api/v1/sites", params=params)
        assert response.status_code == 200 and response.json()["code"] == 1000
    existing = client.get(f"/api/v1/materials/{ids['仓架%_A']}/sites").json()["data"]
    assert len(existing) == 2
    assert "material_name" not in existing[0]
    assert client.get(f"/api/v1/sites/{existing[0]['uuid']}").json()["data"] == existing[0]


def test_deleted_carrier_sites_are_excluded(inventory) -> None:
    """删除独立载体后，其内部库位不再属于当前工站列表。"""

    client, ids = inventory
    response = client.delete(f"/api/v1/materials/{ids['独立载体']}", params={"scope": "material_with_contents"})
    assert response.json()["code"] == 0
    assert len(_list(client)["items"]) == 7
    assert _list(client, material_uuid=ids["独立载体"])["items"] == []


def test_empty_station_returns_empty_page(tmp_path: Path) -> None:
    """没有任何资源的工站返回空页而非错误。"""

    store = InventoryStore(str(tmp_path / "empty.db"))
    try:
        app = FastAPI()
        install_backend_resource_api(app, BackendResourceService(store))
        with TestClient(app) as client:
            assert _list(client) == {"items": [], "has_more": False, "page": 1, "page_size": 20}
    finally:
        store.close()
