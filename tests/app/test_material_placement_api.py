"""物料（Material）人工上下料的公共 HTTP 接口测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import BackendResourceService
from unilabos.app.scheduler.inventory.resource_graph_bootstrap import (
    bootstrap_local_resource_graph,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot


class _Registry:
    """提供真实资源图启动所需的单一设备模板。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回承载库位的单一设备模板。

        参数：无。返回：资源图启动编译器可消费的设备定义。异常：无；定义中的
        业务名和实现身份均为测试固定值。
        """

        return [
            {
                "id": "placement_carrier",
                "display_name": "Placement Carrier",
                "type": "device",
                "class": {
                    "module": "tests.placement:Carrier",
                    "type": "Carrier",
                    "action_value_mappings": {},
                },
                "handles": [],
                "category": ["carrier"],
                "config_info": [],
                "scene": [],
                "device_params": {},
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回空物料模板集合。

        参数：无。返回：空列表；业务物料模板稍后通过公共 HTTP 同步。异常：无。
        """

        return []


class _ResourceTree:
    """提供一个承载设备及两个空库位的产品资源树快照。"""

    def dump(self) -> list[list[dict[str, Any]]]:
        """返回一个设备物料及两个空库位的资源树。

        参数：无。返回：与产品 ``ResourceTreeSet.dump`` 相同的嵌套节点列表。
        异常：无；运行时 UUID 与位置均为固定、合法测试向量。
        """

        owner_uuid = "64000000-0000-4000-8000-0000000003a0"
        return [[
            {
                "id": "placement_carrier",
                "uuid": owner_uuid,
                "name": "上下料载具",
                "parent_uuid": None,
                "type": "device",
                "class": "placement_carrier",
                "pose": _pose(0, 0, 0, 300, 200, 100),
                "config": {},
                "data": {},
                "barcode": "",
            },
            {
                "id": "slot_a",
                "uuid": "64000000-0000-4000-8000-0000000003a1",
                "name": "A1",
                "parent_uuid": owner_uuid,
                "type": "well",
                "class": "",
                "pose": _pose(0, 0, 20, 80, 80, 20),
                "config": {},
                "data": {},
                "barcode": "",
            },
            {
                "id": "slot_b",
                "uuid": "64000000-0000-4000-8000-0000000003a2",
                "name": "B1",
                "parent_uuid": owner_uuid,
                "type": "well",
                "class": "",
                "pose": _pose(100, 0, 20, 80, 80, 20),
                "config": {},
                "data": {},
                "barcode": "",
            },
        ]]


def _pose(
    x: float,
    y: float,
    z: float,
    width: float,
    height: float,
    depth: float,
) -> dict[str, Any]:
    """构造资源图节点使用的有限位置与尺寸。

    参数：前三项为位置，后三项为宽、高、深。返回：带单位缩放和零旋转的 pose
    字典。异常：无；调用方只提供测试常量。
    """

    return {
        "position": {"x": x, "y": y, "z": z},
        "size": {"width": width, "height": height, "depth": depth},
        "scale": {"x": 1, "y": 1, "z": 1},
        "rotation": {"x": 0, "y": 0, "z": 0},
    }


def _sync_template(client: TestClient, *, name: str) -> str:
    """创建测试资源模板。

    参数：``client`` 是正式资源接口客户端，``name`` 是模板业务名称。返回：接口
    分配的资源模板 UUID。异常：资源接口未成功时由断言终止测试。
    """

    response = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": name,
                    "display_name": name,
                    "registry_type": "resource",
                    "class": {},
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["code"] == 0
    return str(response.json()["data"]["templates"][0]["uuid"])


def _create_material(
    client: TestClient,
    *,
    template_uuid: str,
    name: str,
) -> str:
    """通过正式接口创建物料（Material）。

    参数：``client`` 是资源接口客户端，``template_uuid`` 是所属资源模板身份，
    ``name`` 是物料名称。返回：服务端分配的物料 UUID。异常：创建失败时由断言
    终止测试。
    """

    response = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "barcode": f"BARCODE-{uuid4()}",
            "name": name,
        },
    )
    assert response.status_code == 201
    assert response.json()["code"] == 0
    return str(response.json()["data"]["uuid"])


def _create_runtime(
    tmp_path: Path,
) -> tuple[TestClient, InventoryStore, str, list[str]]:
    """通过正式资源图启动链路建立物料、库位与公共资源接口。

    参数：``tmp_path`` 提供隔离库存目录。返回：测试客户端、库存权威和按业务
    顺序生成的两个库位 UUID。异常：启动投影或公共查询失败时由断言终止测试，
    不直接构造数据库占用事实。
    """

    store = InventoryStore(str(tmp_path / "inventory.db"))
    bootstrap_local_resource_graph(
        store=store,
        resource_tree_set=_ResourceTree(),
        registry_snapshot=RegistryTemplateSnapshot.from_registry(_Registry()),
        source_id="/workspace/placement/graph.json",
    )
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    client = TestClient(app)
    graph_response = client.get("/api/v1/materials/graph")
    assert graph_response.status_code == 200
    owner_uuid = str(graph_response.json()["data"]["nodes"][0]["material"]["uuid"])
    response = client.get(f"/api/v1/materials/{owner_uuid}/sites")
    assert response.status_code == 200
    assert response.json()["code"] == 0
    site_uuids = [str(site["uuid"]) for site in response.json()["data"]]
    assert len(site_uuids) == 2
    return client, store, owner_uuid, site_uuids


def test_material_update_places_moves_and_removes_atomically(
    tmp_path: Path,
) -> None:
    """轻量接口应完成上料、换位和下料，并保持库位占用唯一。

    参数：``tmp_path`` 提供隔离的库存数据库目录。返回：无；断言同一个物料先
    放入来源库位，再原子换到目标库位，最后解除库位占用；每次响应均返回最新
    物料详情。异常：任一步违反库位占用（SiteOccupancy）规则时测试失败。
    """

    client, store, owner_uuid, site_uuids = _create_runtime(tmp_path)
    try:
        # 物料模板继续由 SQLite 权威保存；承载设备及其两个库位来自正式启动投影。
        material_template_uuid = _sync_template(client, name="placement.material")
        material_uuid = _create_material(
            client,
            template_uuid=material_template_uuid,
            name="待上下料物料",
        )
        first_site_uuid, second_site_uuid = site_uuids

        placed = client.put(
            f"/api/v1/materials/{material_uuid}",
            json={
                "site_placement": {
                    "action": "place",
                    "site_uuid": first_site_uuid,
                }
            },
        )
        moved = client.put(
            f"/api/v1/materials/{material_uuid}",
            json={
                "site_placement": {
                    "action": "place",
                    "site_uuid": second_site_uuid,
                }
            },
        )
        removed = client.put(
            f"/api/v1/materials/{material_uuid}",
            json={"site_placement": {"action": "remove"}},
        )

        assert placed.status_code == 200
        assert placed.json()["data"]["current_site"]["uuid"] == first_site_uuid
        assert moved.json()["data"]["current_site"]["uuid"] == second_site_uuid
        assert moved.json()["data"]["parent_uuid"] == owner_uuid
        assert removed.json()["code"] == 0
        assert removed.json()["data"]["current_site"] is None
        assert removed.json()["data"]["parent_uuid"] == owner_uuid
        assert store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?",
            (first_site_uuid,),
        )["occupied_material_uuid"] is None
        assert store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?",
            (second_site_uuid,),
        )["occupied_material_uuid"] is None
        first_name = placed.json()["data"]["current_site"]["name"]
        second_name = moved.json()["data"]["current_site"]["name"]
        removed_again = client.put(
            f"/api/v1/materials/{material_uuid}",
            json={"site_placement": {"action": "remove"}},
        )
        assert removed_again.status_code == 200
        placed_again = client.put(
            f"/api/v1/materials/{material_uuid}",
            json={
                "site_placement": {
                    "action": "place",
                    "site_uuid": first_site_uuid,
                }
            },
        )
        placed_same = client.put(
            f"/api/v1/materials/{material_uuid}",
            json={
                "site_placement": {
                    "action": "place",
                    "site_uuid": first_site_uuid,
                }
            },
        )
        assert placed_again.status_code == 200
        assert placed_same.status_code == 200
        moves = [
            json.loads(row["delta_json"])["changes"]
            for row in store.query_all(
                "SELECT delta_json FROM inventory_ledger "
                "WHERE subject_type='material' AND material_uuid=? "
                "AND op_type='material.move' ORDER BY ledger_id",
                (material_uuid,),
            )
        ]
        assert moves == [
            {"name": "待上下料物料", "site_name_before": "", "site_name_after": first_name},
            {"name": "待上下料物料", "site_name_before": first_name, "site_name_after": second_name},
            {"name": "待上下料物料", "site_name_before": second_name, "site_name_after": ""},
            {"name": "待上下料物料", "site_name_before": "", "site_name_after": first_name},
        ]
    finally:
        store.close()


def test_material_update_rejects_an_occupied_site(
    tmp_path: Path,
) -> None:
    """上料不得覆盖另一个物料已经形成的库位占用（SiteOccupancy）。

    参数：``tmp_path`` 提供隔离库存目录。返回：无；断言目标库位已被占用时返回
    Backend 数值错误 ``6007``，原占用事实保持不变。异常：接口错误映射或事务
    回滚失效时测试失败。
    """

    client, store, _owner_uuid, site_uuids = _create_runtime(tmp_path)
    try:
        material_template_uuid = _sync_template(client, name="occupied.material")
        existing_material_uuid = _create_material(
            client,
            template_uuid=material_template_uuid,
            name="已在库位中的物料",
        )
        candidate_material_uuid = _create_material(
            client,
            template_uuid=material_template_uuid,
            name="尝试上料的物料",
        )
        occupied_site_uuid, candidate_site_uuid = site_uuids
        occupied = client.put(
            f"/api/v1/materials/{existing_material_uuid}",
            json={
                "site_placement": {
                    "action": "place",
                    "site_uuid": occupied_site_uuid,
                }
            },
        )
        assert occupied.json()["code"] == 0
        candidate_placed = client.put(
            f"/api/v1/materials/{candidate_material_uuid}",
            json={
                "site_placement": {
                    "action": "place",
                    "site_uuid": candidate_site_uuid,
                }
            },
        )
        assert candidate_placed.json()["code"] == 0

        response = client.put(
            f"/api/v1/materials/{candidate_material_uuid}",
            json={
                "site_placement": {
                    "action": "place",
                    "site_uuid": occupied_site_uuid,
                }
            },
        )

        assert response.status_code == 200
        assert response.json()["code"] == 6007
        assert store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?",
            (occupied_site_uuid,),
        )["occupied_material_uuid"] == existing_material_uuid
        assert store.query_one(
            "SELECT occupied_material_uuid FROM site WHERE uuid=?",
            (candidate_site_uuid,),
        )["occupied_material_uuid"] == candidate_material_uuid
    finally:
        store.close()


def test_material_update_rejects_stale_revision(
    tmp_path: Path,
) -> None:
    """上下料更新应用修订条件阻止过期写入。

    参数：``tmp_path`` 提供隔离库存目录。返回：无；断言过期修订返回 ``4002``
    且不改变当前库位，使用最新修订后可以继续换位。异常：修订检查与库位更新
    未共享同一事务时测试失败。
    """

    client, store, _owner_uuid, site_uuids = _create_runtime(tmp_path)
    try:
        template_uuid = _sync_template(client, name="revisioned.material")
        material_uuid = _create_material(
            client,
            template_uuid=template_uuid,
            name="带修订条件的上下料物料",
        )
        first_site_uuid, second_site_uuid = site_uuids
        first_request = {
            "site_placement": {
                "action": "place",
                "site_uuid": first_site_uuid,
            },
            "expected_revision": 1,
        }

        first = client.put(
            f"/api/v1/materials/{material_uuid}",
            json=first_request,
        )
        stale = client.put(
            f"/api/v1/materials/{material_uuid}",
            json={
                "site_placement": {"action": "remove"},
                "expected_revision": 1,
            },
        )
        moved = client.put(
            f"/api/v1/materials/{material_uuid}",
            json={
                "site_placement": {
                    "action": "place",
                    "site_uuid": second_site_uuid,
                },
                "expected_revision": 2,
            },
        )

        assert first.json()["code"] == 0
        assert first.json()["data"]["revision"] == 2
        assert stale.json()["code"] == 4002
        assert moved.json()["data"]["revision"] == 3
        detail = client.get(f"/api/v1/materials/{material_uuid}").json()["data"]
        assert detail["current_site"]["uuid"] == second_site_uuid
        assert detail["revision"] == 3
    finally:
        store.close()
