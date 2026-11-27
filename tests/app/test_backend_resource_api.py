"""Shared Backend/Edge Resource Interface contract tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import (
    create_material_asset_router,
    install_backend_resource_api,
)
from unilabos.app.scheduler.inventory.backend_contract import (
    MATERIAL_IDENTITY_CONFLICT,
    MAX_SITE_IDENTITY_RESOLUTION_IDS,
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


def test_workspace_authoring_can_publish_model_assets_without_inventory() -> None:
    """纯 Authoring 进程必须能独立发布模型，且不暴露资源写路由。

    参数：无。返回：无；断言只读资产 Router 不依赖 Inventory Store。异常：模型
    路由仍与资源权威耦合或意外开放写接口时测试失败。
    """

    class _ModelCatalog:
        def read_asset(self, public_path: str) -> WorkspaceMaterialModelAsset:
            if public_path != "/api/v1/material-models/szlab/device.xacro":
                raise KeyError("模型资产未授权")
            return WorkspaceMaterialModelAsset(
                content=b"<robot/>",
                media_type="application/xml",
                etag="sha256:workspace-model",
            )

    app = FastAPI()
    app.include_router(
        create_material_asset_router(
            material_shapes=({"id": "szlab-device", "parts": []},),
            material_model_catalog=_ModelCatalog(),
        ),
        prefix="/api/v1",
    )
    client = TestClient(app)

    model = client.get("/api/v1/material-models/szlab/device.xacro")
    shapes = client.get("/api/v1/material-shapes")

    assert model.status_code == 200
    assert model.content == b"<robot/>"
    assert shapes.json() == {
        "code": 0,
        "data": {"items": [{"id": "szlab-device", "parts": []}]},
    }
    assert client.get("/api/v1/materials").status_code == 404


@pytest.mark.parametrize(
    ("validator", "status"),
    [
        ('"sha256:cached"', 304),
        ('W/"sha256:cached"', 304),
        ('"old", W/"sha256:cached"', 304),
        ("*", 304),
        ('"old"', 200),
        ('"sha256:CACHED"', 200),
        (' "old" , W/"sha256:cached" ', 304),
        ('"sha256:cached-extra"', 200),
    ],
)
def test_material_asset_conditional_get(validator: str, status: int) -> None:
    """未变化资产返回无正文的 304，失配校验器返回最新字节。

    参数：``validator`` 是请求中的实体标签，``status`` 是预期状态码。返回：无。
    异常：弱比较、列表匹配或缓存响应头不符合 GET 合同时断言失败。
    """

    class _ModelCatalog:
        asset = WorkspaceMaterialModelAsset(
            content=b"model-bytes", media_type="model/stl", etag="sha256:cached"
        )

        def read_asset(self, public_path: str) -> WorkspaceMaterialModelAsset:
            if public_path != "/api/v1/material-models/lab/model.stl":
                raise KeyError("模型资产未授权")
            return self.asset

    catalog = _ModelCatalog()
    app = FastAPI()
    app.include_router(
        create_material_asset_router(material_model_catalog=catalog),
        prefix="/api/v1",
    )
    client = TestClient(app)
    response = client.get(
        "/api/v1/material-models/lab/model.stl",
        headers={"If-None-Match": validator},
    )
    assert response.status_code == status
    assert response.headers["etag"] == '"sha256:cached"'
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert response.content == (b"" if status == 304 else b"model-bytes")
    if status == 304:
        assert "content-length" not in response.headers
    missing = client.get(
        "/api/v1/material-models/lab/missing.stl",
        headers={"If-None-Match": "*"},
    )
    assert missing.status_code == 404
    catalog.asset = WorkspaceMaterialModelAsset(
        content=b"updated-model", media_type="model/stl", etag="sha256:updated"
    )
    changed = client.get(
        "/api/v1/material-models/lab/model.stl",
        headers={"If-None-Match": '"sha256:cached"'},
    )
    assert changed.status_code == 200
    assert changed.content == b"updated-model"
    assert changed.headers["etag"] == '"sha256:updated"'


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


def test_material_list_category_filters_before_paging_and_preserves_existing_filters(
    tmp_path,
) -> None:
    """分类查询须覆盖全部非设备类型，分页和总数沿用相同筛选条件。"""

    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)
    ids = {}
    for index, material_type in enumerate(
        ("container", "warehouse", "deck", "", "device", "device", "container", "device")
    ):
        response = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template_uuid,
                "name": f"Filter item {index}",
                "barcode": f"FILTER-{index}",
            },
        )
        assert response.status_code == 201
        ids[index] = response.json()["data"]["uuid"]
        with store.transaction() as connection:
            connection.execute(
                "UPDATE material SET type=?,create_time=? WHERE uuid=?",
                (material_type, f"2026-10-30T00:00:{index:02d}Z", ids[index]),
            )
    for index in (6, 7):
        assert client.delete(f"/api/v1/materials/{ids[index]}").json()["code"] == 0

    all_rows = client.get("/api/v1/materials").json()["data"]
    assert all_rows["total"] == 6
    assert [row["uuid"] for row in all_rows["items"]] == [
        ids[index] for index in (5, 4, 3, 2, 1, 0)
    ]
    for category, expected in (("material", (3, 2, 1, 0)), ("device", (5, 4))):
        for page, index in enumerate(expected, start=1):
            data = client.get(
                "/api/v1/materials",
                params={"category": category, "page": page, "page_size": 1},
            ).json()["data"]
            assert data["total"] == len(expected)
            assert [row["uuid"] for row in data["items"]] == [ids[index]]
            assert data["page"] == page
            assert data["page_size"] == 1
        exhausted = client.get(
            "/api/v1/materials",
            params={"category": category, "page": len(expected) + 1, "page_size": 1},
        ).json()["data"]
        assert exhausted["items"] == []
        assert exhausted["total"] == len(expected)

    filters = {"name": "item", "barcode": "FILTER-4", "resource_template_uuid": template_uuid}
    matched = client.get(
        "/api/v1/materials", params={**filters, "category": "device"}
    ).json()["data"]
    assert matched["total"] == 1
    assert [row["uuid"] for row in matched["items"]] == [ids[4]]
    for override in (
        {"category": "material"},
        {"name": "missing"},
        {"barcode": "missing"},
        {"resource_template_uuid": str(uuid4())},
    ):
        unmatched = client.get(
            "/api/v1/materials", params={**filters, "category": "device", **override}
        ).json()["data"]
        assert unmatched["total"] == 0
        assert unmatched["items"] == []
    store.close()


@pytest.mark.parametrize("category", ["", "all", "container", "invalid"])
def test_material_list_rejects_unknown_category(tmp_path, category: str) -> None:
    """无效分类须返回现有参数错误信封，不能悄悄变成全部查询。"""

    client, store = _client(tmp_path)
    response = client.get("/api/v1/materials", params={"category": category})
    assert response.status_code == 200
    assert response.json()["code"] == 1000
    store.close()


def test_material_graph_matches_backend_type_revision_and_template_summary(tmp_path):
    """验证直连 OS 的物料图与 Go Backend 使用同一位置和身份读模型。

    Args:
        tmp_path: pytest 提供的隔离 SQLite 目录。

    Returns:
        无返回值；物料（Material）身份、修订版本、资源模板或相对位置漂移时失败。
    """

    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)
    # local_position 是前端直连 OS 时应从公共物料图原样读取的设备包位置。
    local_position = {
        "position_x": 10,
        "position_y": 20,
        "position_z": 30,
        "depth": 60,
        "length": 80,
        "width": 100,
        "scale_x": 1,
        "scale_y": 1,
        "scale_z": 1,
        "rotation_x": 0,
        "rotation_y": 0,
        "rotation_z": 90,
    }
    created = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "barcode": "GRAPH-CONTRACT-001",
            "name": "Graph contract material",
            "relative_position": local_position,
        },
    ).json()["data"]
    with store.transaction() as connection:
        connection.execute(
            "UPDATE material SET type=? WHERE uuid=?",
            ("device", created["uuid"]),
        )
        connection.execute(
            "UPDATE material SET aggregate_version=? WHERE uuid=?",
            (7, created["uuid"]),
        )

    node = client.get("/api/v1/materials/graph").json()["data"]["nodes"][0]

    assert node["material"]["type"] == "device"
    assert node["material"]["revision"] == 7
    assert {
        field: node["relative_position"][field]
        for field in local_position
    } == local_position
    assert node["resource_template"] == {
        "uuid": template_uuid,
        "name": "device.pump",
        "display_name": "Pump",
        "resource_type": "device",
        "tags": [],
    }
    store.close()


def test_resource_template_list_uses_backend_page_more_contract(tmp_path) -> None:
    """Local 资源模板目录必须使用 Go Backend 的页码摘要合同。"""

    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)

    data = client.get(
        "/api/v1/resource-templates",
        params={"page": 1, "page_size": 1, "keyword": "pump"},
    ).json()["data"]

    assert set(data) == {"items", "has_more", "page", "page_size"}
    assert data["has_more"] is False
    assert data["page"] == 1
    assert data["page_size"] == 1
    assert data["items"] == [
        {
            "uuid": template_uuid,
            "name": "device.pump",
            "display_name": "Pump",
            "resource_type": "device",
            "tags": [],
        }
    ]
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


def test_relative_position_round_trips_and_explicit_null_preserves_value(tmp_path):
    """物料相对位置应完整往返，显式 null 不删除已有位置。

    参数：``tmp_path`` 提供隔离库存目录。返回：无；断言位置投影
    与持久行都保持有效。异常：位置三态更新合同回归时测试失败。
    """

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
    assert updated.json()["data"]["relative_position"]["position_x"] == 1.5
    assert store.query_one(
        "SELECT position_x FROM material WHERE uuid=?",
        (material["uuid"],),
    )["position_x"] == 1.5
    store.close()


def test_material_update_explicit_null_preserves_mutable_fields(tmp_path):
    """Backend 兼容 PUT 应把显式 null 解释为保持物料当前值。

    参数：``tmp_path`` 提供隔离库存目录。返回：无；断言所有显式
    null 字段均保持原值。异常：本地接口与 Backend 三态更新合同
    不一致时测试失败。
    """

    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)
    parent = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "barcode": "NULL-PARENT",
            "name": "Null Parent",
        },
    ).json()["data"]
    created = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "parent_uuid": parent["uuid"],
            "barcode": "NULL-PRESERVE",
            "name": "Null Preserve",
            "description": "原描述",
            "meta_data": {"source": "test"},
            "config": {"mode": "safe"},
        },
    ).json()["data"]

    updated = client.put(
        f"/api/v1/materials/{created['uuid']}",
        json={
            "parent_uuid": None,
            "barcode": None,
            "name": None,
            "description": None,
            "meta_data": None,
            "config": None,
            "site_placement": None,
        },
    )

    assert updated.status_code == 200
    detail = updated.json()["data"]
    assert detail["parent_uuid"] == parent["uuid"]
    assert detail["barcode"] == "NULL-PRESERVE"
    assert detail["name"] == "Null Preserve"
    assert detail["description"] == "原描述"
    assert detail["meta_data"] == {"source": "test"}
    assert detail["config"] == {"mode": "safe"}

    cleared = client.put(
        f"/api/v1/materials/{created['uuid']}",
        json={"parent_uuid": "00000000-0000-0000-0000-000000000000"},
    )
    assert cleared.json()["code"] == 0
    assert cleared.json()["data"]["parent_uuid"] is None
    store.close()


def test_material_update_rejects_resource_template_change(tmp_path):
    """既有物料不得通过更新接口换用另一个模板。

    参数：``tmp_path`` 提供隔离库存目录。返回：无；断言模板不可
    变规则返回 ``6002``，且不推进物料修订。异常：合同回归时测试
    失败。
    """

    client, store = _client(tmp_path)
    current_template_uuid = _sync_template(client)
    created = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": current_template_uuid,
            "barcode": "IMMUTABLE-TEMPLATE",
            "name": "Immutable Template",
        },
    ).json()["data"]

    response = client.put(
        f"/api/v1/materials/{created['uuid']}",
        json={"resource_template_uuid": str(uuid4())},
    )

    assert response.json()["code"] == 6002
    detail = client.get(f"/api/v1/materials/{created['uuid']}").json()["data"]
    assert detail["resource_template_uuid"] == current_template_uuid
    assert detail["revision"] == 1
    store.close()


def test_openapi_exposes_backend_resource_paths_not_only_inventory_paths(tmp_path):
    client, store = _client(tmp_path)
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/resource-templates" in paths
    assert "/api/v1/materials" in paths
    assert "/api/v1/materials/sites/resolve-identities" in paths
    assert "/api/v1/materials/{material_uuid}/sites" in paths
    assert "/api/v1/materials/{material_uuid}/states" in paths
    assert "/api/v1/sites/{site_uuid}" in paths
    store.close()


def test_site_identity_resolution_uses_metadata_with_legacy_name_fallback(tmp_path):
    """批量解析应优先使用 source_node_id，并兼容旧 Site 名称。"""

    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)
    owner = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "barcode": "IDENTITY-OWNER",
            "name": "Identity Owner",
        },
    ).json()["data"]
    with store.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO site(
                uuid,create_time,update_time,meta_data,material_uuid,name,
                sort_order,allowed_resource_template_uuids,
                position_x,position_y,position_z,depth,length,width
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                (
                    "10000000-0000-4000-8000-000000000001",
                    "2026-09-13T00:00:00Z",
                    "2026-09-13T00:00:00Z",
                    '{"source_node_id":"rack-A1"}',
                    owner["uuid"],
                    "Display A1",
                    0,
                    "[]",
                    0,
                    0,
                    0,
                    1,
                    1,
                    1,
                ),
                (
                    "10000000-0000-4000-8000-000000000002",
                    "2026-09-13T00:00:00Z",
                    "2026-09-13T00:00:00Z",
                    "{}",
                    owner["uuid"],
                    "legacy-B1",
                    1,
                    "[]",
                    0,
                    0,
                    0,
                    1,
                    1,
                    1,
                ),
                (
                    "10000000-0000-4000-8000-000000000003",
                    "2026-09-13T00:00:00Z",
                    "2026-09-13T00:00:00Z",
                    '{"source_node_id":"deleted-C1"}',
                    owner["uuid"],
                    "Deleted C1",
                    2,
                    "[]",
                    0,
                    0,
                    0,
                    1,
                    1,
                    1,
                ),
            ),
        )
        connection.execute(
            "UPDATE site SET deleted_at=? WHERE uuid=?",
            (
                "2026-09-13T00:01:00Z",
                "10000000-0000-4000-8000-000000000003",
            ),
        )

    response = client.post(
        "/api/v1/materials/sites/resolve-identities",
        json={
            "source_node_ids": [
                " rack-A1 ",
                "rack-A1",
                "",
                " legacy-B1 ",
                "deleted-C1",
                "unknown",
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "data": {
            "site_uuids_by_source_node_id": {
                "legacy-B1": "10000000-0000-4000-8000-000000000002",
                "rack-A1": "10000000-0000-4000-8000-000000000001",
            }
        },
    }
    store.close()


def test_site_identity_resolution_fails_closed_on_ambiguous_active_sites(tmp_path):
    """一个业务 ID 对应多个活动 Site 时不得返回部分解析结果。"""

    client, store = _client(tmp_path)
    template_uuid = _sync_template(client)
    owner = client.post(
        "/api/v1/materials",
        json={
            "resource_template_uuid": template_uuid,
            "barcode": "AMBIGUOUS-OWNER",
            "name": "Ambiguous Owner",
        },
    ).json()["data"]
    with store.transaction() as connection:
        for index in (1, 2):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    position_x,position_y,position_z,depth,length,width
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"20000000-0000-4000-8000-00000000000{index}",
                    "2026-09-13T00:00:00Z",
                    "2026-09-13T00:00:00Z",
                    '{"source_node_id":"duplicate-site"}',
                    owner["uuid"],
                    f"Duplicate {index}",
                    index,
                    "[]",
                    0,
                    0,
                    0,
                    1,
                    1,
                    1,
                ),
            )

    response = client.post(
        "/api/v1/materials/sites/resolve-identities",
        json={"source_node_ids": ["duplicate-site"]},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 4002
    assert "data" not in response.json()
    store.close()


def test_site_identity_resolution_rejects_oversized_batch(tmp_path):
    """请求体应在进入库存扫描前限制批量身份数量。"""

    client, store = _client(tmp_path)

    response = client.post(
        "/api/v1/materials/sites/resolve-identities",
        json={
            "source_node_ids": [
                f"site-{index}"
                for index in range(MAX_SITE_IDENTITY_RESOLUTION_IDS + 1)
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["code"] == 1000
    store.close()


def test_create_material_accepts_missing_null_and_blank_barcode(tmp_path):
    """创建时无条码可重复；非空条码经去空白后仍唯一。"""
    client, store = _client(tmp_path)
    try:
        backend = BackendResourceService(store)
        template = backend.sync_resource_templates([{
            "id": "identity.vessel", "display_name": "容器", "registry_type": "material", "class": {}
        }])["templates"][0]["uuid"]
        identities = []
        for value in ({}, {"barcode": None}, {"barcode": ""}, {"barcode": "   "}):
            response = client.post("/api/v1/materials", json={
                "resource_template_uuid": template, "name": "同名无条码", **value,
            })
            assert response.status_code == 201, response.json()
            assert response.json()["data"]["barcode"] == ""
            identities.append(response.json()["data"]["uuid"])
        assert len(set(identities)) == 4
        first = client.post("/api/v1/materials", json={
            "resource_template_uuid": template, "name": "同名无条码", "barcode": "UNIQUE",
        })
        assert first.status_code == 201
        duplicate = client.post("/api/v1/materials", json={
            "resource_template_uuid": template, "name": "其他名称", "barcode": " unique ",
        })
        assert duplicate.status_code == 200
        assert duplicate.json()["code"] == MATERIAL_IDENTITY_CONFLICT
    finally:
        store.close()
