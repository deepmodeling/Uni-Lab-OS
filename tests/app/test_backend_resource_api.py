"""Shared Backend/Edge Resource Interface contract tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import (
    install_backend_resource_api,
)
from unilabos.app.scheduler.inventory.backend_contract import (
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.package_manager import WorkspaceMaterialModelAsset


def _client(tmp_path):
    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    return TestClient(app), store


def test_material_shapes_use_backend_envelope_without_inventory_routes(
    tmp_path,
) -> None:
    """静态物料外形必须通过公共后端（Backend）信封独立发布。

    参数：``tmp_path`` 提供隔离的库存数据库。返回：无；断言前端可直接读取
    ``/api/v1/material-shapes``，且响应不依赖私有库存路由。异常：路由缺失或
    wire 数据被改写时测试失败。
    """

    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    # ``material_shapes`` 是工作区资产编译器已经校验过的静态公共投影。
    material_shapes = (
        {
            "id": "beaker",
            "bundle": "szlab-poly-studio",
            "categories": ["beaker"],
            "categoryTokens": [],
            "parts": [
                {
                    "type": "box",
                    "style": "glass",
                    "from": [0, 0, 0],
                    "to": [1, 1, 1],
                }
            ],
        },
    )
    install_backend_resource_api(
        app,
        BackendResourceService(store),
        material_shapes=material_shapes,
    )

    response = TestClient(app).get("/api/v1/material-shapes")

    assert response.status_code == 200
    assert response.json() == {"code": 0, "data": {"items": list(material_shapes)}}
    store.close()


def test_material_model_assets_use_public_route_with_cache_identity(tmp_path) -> None:
    """工作区模型资产必须通过公共路由返回真实字节和内容摘要。

    参数：``tmp_path`` 提供隔离库存数据库。返回：无；断言 Xacro 成功且未知资产
    返回 404。异常：模型目录没有装配到资源 API 或响应丢失媒体信息时测试失败。
    """

    class _ModelCatalog:
        """只允许读取一个固定 Xacro 的模型目录替身。"""

        def read_asset(self, public_path: str) -> WorkspaceMaterialModelAsset:
            """读取固定资产。参数：公共路径。返回：模型字节。异常：未知路径失败。"""

            if public_path != "/api/v1/material-models/szlab/device.xacro":
                raise KeyError("模型资产未授权")
            return WorkspaceMaterialModelAsset(
                content=b"<robot/>",
                media_type="application/xml",
                etag="sha256:model",
            )

    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(
        app,
        BackendResourceService(store),
        material_model_catalog=_ModelCatalog(),
    )
    client = TestClient(app)

    response = client.get("/api/v1/material-models/szlab/device.xacro")
    missing = client.get("/api/v1/material-models/szlab/missing.stl")

    assert response.status_code == 200
    assert response.content == b"<robot/>"
    assert response.headers["content-type"].startswith("application/xml")
    assert response.headers["etag"] == '"sha256:model"'
    assert missing.status_code == 404
    store.close()


def _sync_template(client: TestClient) -> str:
    response = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": "device.pump",
                    "display_name": "Pump",
                    "registry_type": "device",
                    "model": {},
                    "class": {
                        "module": "drivers.pump",
                        "type": "python",
                        "action_value_mappings": {},
                    },
                    "handles": [
                        {
                            "handler_key": "sample",
                            "label": "Sample",
                            "data_type": "material",
                            "io_type": "target",
                            "data_key": "sample_uuid",
                            "data_source": "param",
                            "side": "left",
                        }
                    ],
                    "category": [],
                    "config_info": [],
                    "scene": [],
                    "device_params": {},
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["code"] == 0
    return response.json()["data"]["templates"][0]["uuid"]


def test_material_routes_use_backend_envelope_and_soft_delete(tmp_path):
    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)

    created = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "parent_uuid": None,
            "barcode": "PUMP-001",
            "name": "Pump 1",
            "meta_data": {},
            "config": {"port": "loopback"},
        },
    )
    assert created.status_code == 201
    assert created.json()["code"] == 0
    material = created.json()["data"]
    material_uuid = material["uuid"]
    assert material["resource_template_uuid"] == template_uuid
    assert "deleted_at" not in material

    listed = client.get("/api/v1/materials").json()["data"]
    assert [row["uuid"] for row in listed["items"]] == [material_uuid]

    deleted = client.delete(f"/api/v1/materials/{material_uuid}")
    assert deleted.status_code == 200
    assert deleted.json() == {"code": 0}
    assert client.get(f"/api/v1/materials/{material_uuid}").json()["code"] == 6000
    assert store.query_one(
        "SELECT deleted_at FROM material WHERE uuid=?", (material_uuid,)
    )["deleted_at"] is not None
    store.close()


def test_created_material_is_immediately_reservable_by_stable_uuid(tmp_path):
    """HTTP 创建的固定物料应立即进入同一库存权威的预留路径。

    参数：``tmp_path`` 提供隔离的 ``inventory.db``。返回：无；断言
    ``POST /api/v1/materials`` 返回的稳定物料 UUID 可由真实库存服务
    （InventoryService）直接建立短期遗留库存预留（inventory_reservation），且
    预留结果仍引用原工作流任务和节点身份；该兼容事实不是正式任务物料预留
    （TaskMaterialReservation）。异常：模板同步、HTTP 创建或预留任一步失败都
    应直接使测试失败，禁止退化为第二份物料身份映射。
    """

    client, store = _client(tmp_path)
    # ``template_uuid`` 是 HTTP 创建具体物料时引用的资源模板（ResourceTemplate）
    # 稳定身份。
    template_uuid = _sync_template(client)
    created = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "barcode": "RESERVABLE-001",
            "name": "Reservable material",
        },
    )
    # ``material_uuid`` 是 HTTP 与短期预留兼容路径必须共享的稳定物料身份。
    material_uuid = created.json()["data"]["uuid"]
    # ``workflow_task_uuid`` 与 ``workflow_node_uuid`` 分别代表本次测试预留的
    # 工作流任务（WorkflowTask）及其首个物理消费者节点身份。
    workflow_task_uuid = "21000000-0000-4000-8000-000000000401"
    workflow_node_uuid = "31000000-0000-4000-8000-000000000401"
    inventory = InventoryService(store)

    reserved = inventory.reserve_workflow(
        workflow_task_uuid,
        {
            workflow_node_uuid: [
                MaterialRequirement(instance_uuid=material_uuid),
            ]
        },
    )

    assert created.status_code == 201
    assert created.json()["code"] == 0
    assert reserved == {
        "workflow_id": workflow_task_uuid,
        "reserved_nodes": [workflow_node_uuid],
        "allocations": {workflow_node_uuid: [material_uuid]},
    }
    inventory.release_workflow(workflow_task_uuid, reason="test_cleanup")
    store.close()


def test_resource_handle_uuid_is_stable_and_omitted_update_preserves_it(tmp_path):
    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)
    detail = client.get(f"/api/v1/resource-templates/{template_uuid}").json()[
        "data"
    ]
    assert len(detail["handles"]) == 1
    handle = detail["handles"][0]
    assert handle["resource_template_uuid"] == template_uuid
    assert handle["name"] == "sample"
    assert handle["io_type"] == "target"

    updated = client.put(
        f"/api/v1/resource-templates/{template_uuid}",
        json={"display_name": "Pump v2", "registry_type": "device"},
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["handles"][0]["uuid"] == handle["uuid"]
    assert updated.json()["data"]["display_name"] == "Pump v2"
    store.close()


def test_site_uuid_is_position_identity_and_state_updates_material_projection(
    tmp_path,
):
    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)
    owner = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "barcode": "OWNER",
            "name": "Owner",
        },
    ).json()["data"]
    occupant = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "barcode": "OCCUPANT",
            "name": "Occupant",
            "parent_uuid": owner["uuid"],
        },
    ).json()["data"]
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO site(
                create_time,update_time,meta_data,material_uuid,name,sort_order,
                allowed_resource_template_uuids,occupied_material_uuid,
                position_x,position_y,position_z,depth,length,width
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "2026-08-04T00:00:00Z",
                "2026-08-04T00:00:00Z",
                "{}",
                owner["uuid"],
                "A1",
                0,
                "[]",
                occupant["uuid"],
                0,
                0,
                0,
                1,
                1,
                1,
            ),
        )
    site = client.get(f"/api/v1/materials/{owner['uuid']}/sites").json()[
        "data"
    ][0]
    assert site["uuid"] not in {owner["uuid"], occupant["uuid"]}
    assert site["material_uuid"] == owner["uuid"]
    assert site["occupied_material_uuid"] == occupant["uuid"]

    state = client.post(
        f"/api/v1/materials/{occupant['uuid']}/states",
        json={
            "status": "observed",
            "state_data": {"temperature": 25},
            "source": "test",
        },
    ).json()
    assert state["code"] == 0
    detail = client.get(f"/api/v1/materials/{occupant['uuid']}").json()["data"]
    assert detail["data"] == {"temperature": 25}
    store.close()


def test_relative_position_round_trips_and_explicit_null_soft_deletes(tmp_path):
    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)
    payload = {
        "resource_template_uuid": template_uuid,
        "barcode": "POSITIONED",
        "name": "Positioned",
        "relative_position": {
            "position_x": 1.5,
            "position_y": 2,
            "position_z": 3,
            "depth": 4,
            "length": 5,
            "width": 6,
            "scale_x": 1,
            "scale_y": 1,
            "scale_z": 1,
        },
    }
    created = client.post("/api/v1/materials", json=payload)
    assert created.status_code == 201
    material = created.json()["data"]
    assert material["relative_position"]["material_uuid"] == material["uuid"]
    assert material["relative_position"]["position_x"] == 1.5

    payload["relative_position"] = None
    updated = client.put(f"/api/v1/materials/{material['uuid']}", json=payload)
    assert updated.status_code == 200
    assert updated.json()["data"]["relative_position"] is None
    assert store.query_one(
        "SELECT deleted_at FROM relative_position WHERE material_uuid=?",
        (material["uuid"],),
    )["deleted_at"] is not None
    store.close()


def test_openapi_exposes_backend_resource_paths_not_only_inventory_paths(tmp_path):
    client, store = _client(tmp_path)
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/resource-templates" in paths
    assert "/api/v1/materials" in paths
    assert "/api/v1/materials/{material_uuid}/sites" in paths
    assert "/api/v1/materials/{material_uuid}/states" in paths
    assert "/api/v1/sites/{site_uuid}" in paths
    store.close()
