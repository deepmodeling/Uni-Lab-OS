"""批量创建物料及内联试剂的公共接口与事务边界测试。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import (
    INVALID_PARAMETER,
    MATERIAL_IDENTITY_CONFLICT,
    MATERIAL_SITE_OCCUPIED,
    MATERIAL_TEMPLATE_NOT_FOUND,
    RESOURCE_NOT_FOUND,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.monitor import MonitorBus

Scene = tuple[TestClient, InventoryStore, BackendResourceService, MonitorBus, str, str]


@pytest.fixture
def scene(tmp_path: Path) -> Iterator[Scene]:
    """提供真实库存、试剂瓶模板、试剂身份和物料通知总线。"""

    store = InventoryStore(str(tmp_path / "inventory.db"))
    monitor = MonitorBus()
    service = BackendResourceService(store, monitor=monitor)
    template_uuid = service.sync_resource_templates(
        [
            {
                "id": "local.batch.bottle",
                "display_name": "批量试剂瓶",
                "registry_type": "material",
                "category": ["container"],
                "metadata": {"capacity": {"max_volume_ul": 1000000}},
            }
        ]
    )["templates"][0]["uuid"]
    app = FastAPI()
    install_backend_resource_api(app, service)
    with TestClient(app) as client:
        info = client.post(
            "/api/v1/reagent-infos",
            json={"cas": "64-17-5", "name": "乙醇", "physical_state": "liquid"},
        )
        assert info.status_code == 201
        yield client, store, service, monitor, template_uuid, info.json()["data"]["uuid"]
    store.close()


def _item(template_uuid: str, name: str, **extra: Any) -> dict[str, Any]:
    """构造单条物料输入，条码用于核对真实落库结果。"""

    return {
        "resource_template_uuid": template_uuid,
        "name": name,
        "barcode": f"BATCH-{name}",
        **extra,
    }


def _reagent(info_uuid: str, **extra: Any) -> dict[str, Any]:
    return {
        "reagent_info_uuid": info_uuid,
        "quantity": 50,
        "quantity_unit": "mL",
        **extra,
    }


def test_batch_creates_empty_materials_and_reagents_in_request_order(scene: Scene) -> None:
    """批量入口须创建真实物料及试剂台账，并保留单条接口的字段。"""

    client, store, _, monitor, template, info = scene
    response = client.post(
        "/api/v1/materials/batch",
        json={
            "items": [
                _item(template, "空瓶", config={"label": "备用"}, relative_position={"position_x": 12}),
                _item(template, "乙醇一", reagent=_reagent(info)),
                _item(
                    template,
                    "乙醇二",
                    reagent={
                        "cas": "64-17-5",
                        "quantity": 25,
                        "quantity_unit": "mL",
                    },
                ),
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    data = response.json()["data"]
    assert {key: data[key] for key in ("total", "created", "failed", "atomic")} == {
        "total": 3,
        "created": 3,
        "failed": 0,
        "atomic": True,
    }
    assert data["errors"] == []
    empty, first, second = data["items"]
    assert [item["name"] for item in data["items"]] == ["空瓶", "乙醇一", "乙醇二"]
    assert len({item["uuid"] for item in data["items"]}) == 3
    assert empty["config"]["label"] == "备用"
    assert empty["relative_position"]["position_x"] == 12
    assert empty["children"] == []
    assert empty["site_uuids"] == []
    for material, quantity in ((first, 50), (second, 25)):
        assert material["reagent"]["material_uuid"] == material["uuid"]
        assert material["reagent"]["reagent_info_uuid"] == info
        assert material["reagent"]["quantity"] == quantity
        assert material["reagent_info"]["uuid"] == info
        assert client.get(f"/api/v1/materials/{material['uuid']}").json()["code"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM material") == {"n": 3}
    assert store.query_one("SELECT COUNT(*) AS n FROM reagent") == {"n": 2}
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger") == {"n": 2}
    assert store.query_one("SELECT COUNT(*) AS n FROM sync_outbox") == {"n": 2}
    assert [event["data"]["material_uuid"] for event in monitor.recent("material")] == [
        item["uuid"] for item in data["items"]
    ]


@pytest.mark.parametrize(
    "failure,code",
    [
        ("template", MATERIAL_TEMPLATE_NOT_FOUND),
        ("barcode", MATERIAL_IDENTITY_CONFLICT),
        ("identity", RESOURCE_NOT_FOUND),
        ("capacity", INVALID_PARAMETER),
        ("validation", INVALID_PARAMETER),
    ],
)
def test_atomic_batch_failure_leaves_no_material_reagent_or_notification(scene: Scene, failure: str, code: int) -> None:
    """后行失败须连同前行物料、试剂、台账、发件箱和实时通知一起撤销。"""

    client, store, _, monitor, template, info = scene
    first = _item(template, "第一瓶", reagent=_reagent(info))
    bad = _item(template, "失败瓶", reagent=_reagent(info))
    if failure == "template":
        bad["resource_template_uuid"] = str(uuid4())
    elif failure == "barcode":
        bad["barcode"] = first["barcode"]
    elif failure == "identity":
        bad["reagent"]["reagent_info_uuid"] = str(uuid4())
    elif failure == "capacity":
        bad["reagent"]["quantity"] = 1001
    else:
        del bad["reagent"]["quantity"]

    response = client.post("/api/v1/materials/batch", json={"items": [first, bad]})

    assert response.status_code == 200
    assert response.json()["code"] == code
    assert response.json()["error"]["details"]["created"] == 0
    for table in ("material", "reagent", "inventory_ledger", "sync_outbox"):
        assert store.query_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 0}
    assert monitor.recent("material") == []


def test_non_atomic_batch_keeps_successes_and_reports_original_row_numbers(scene: Scene) -> None:
    """部分成功模式须继续处理后续行，且失败行不得留下空容器。"""

    client, store, _, monitor, template, info = scene
    invalid = _item(template, "字段失败", reagent=_reagent(info))
    del invalid["reagent"]["quantity"]
    response = client.post(
        "/api/v1/materials/batch",
        json={
            "atomic": False,
            "items": [
                _item(template, "成功试剂", reagent=_reagent(info)),
                invalid,
                _item(template, "业务失败", reagent=_reagent(str(uuid4()))),
                _item(template, "成功空瓶"),
            ],
        },
    )

    assert response.json()["code"] == 0
    data = response.json()["data"]
    assert {key: data[key] for key in ("total", "created", "failed", "atomic")} == {
        "total": 4,
        "created": 2,
        "failed": 2,
        "atomic": False,
    }
    assert [item["name"] for item in data["items"]] == ["成功试剂", "成功空瓶"]
    errors = {error["row"]: error["errors"] for error in data["errors"]}
    assert set(errors) == {2, 3}
    assert errors[2][0]["field"] == "reagent.quantity"
    assert errors[3][0]["message"]
    assert store.query_one("SELECT COUNT(*) AS n FROM material") == {"n": 2}
    assert store.query_one("SELECT COUNT(*) AS n FROM reagent") == {"n": 1}
    assert store.query_one("SELECT COUNT(*) AS n FROM inventory_ledger") == {"n": 1}
    assert store.query_one("SELECT COUNT(*) AS n FROM sync_outbox") == {"n": 1}
    assert len(monitor.recent("material")) == 2


@pytest.mark.parametrize("body", [{}, {"items": []}, {"items": [{}] * 501}])
def test_batch_rejects_missing_empty_or_oversized_items(scene: Scene, body: dict[str, Any]) -> None:
    client, store, *_ = scene
    response = client.post("/api/v1/materials/batch", json=body)
    assert response.status_code == 200
    assert response.json()["code"] == INVALID_PARAMETER
    assert store.query_one("SELECT COUNT(*) AS n FROM material") == {"n": 0}


def test_atomic_batch_site_conflict_restores_occupancy(scene: Scene) -> None:
    """批内重复占位须恢复库位，且不得保留先写入的试剂内容。"""

    client, store, service, monitor, template, info = scene
    rack_template = service.sync_resource_templates(
        [
            {
                "id": "local.batch.rack",
                "registry_type": "material",
                "available_sites": [{"index": "A1", "label": "瓶位"}],
            }
        ]
    )["templates"][0]["uuid"]
    rack = service.create_material(_item(rack_template, "瓶架"))
    site_uuid = rack["site_uuids"][0]
    placement = {"action": "place", "site_uuid": site_uuid}

    response = client.post(
        "/api/v1/materials/batch",
        json={
            "items": [
                _item(template, "先占位", reagent=_reagent(info), site_placement=placement),
                _item(template, "后占位", site_placement=placement),
            ]
        },
    )

    assert response.json()["code"] == MATERIAL_SITE_OCCUPIED
    assert store.query_one("SELECT COUNT(*) AS n FROM material") == {"n": 1}
    assert store.query_one("SELECT COUNT(*) AS n FROM reagent") == {"n": 0}
    assert store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (site_uuid,)) == {
        "occupied_material_uuid": None
    }
    assert len(monitor.recent("material")) == 1


@pytest.mark.parametrize("invalid_second", [False, True])
def test_batch_template_sites_and_initial_materials_share_the_transaction(scene: Scene, invalid_second: bool) -> None:
    """模板展开的库位及默认子物料须随整批创建一起提交或回滚。"""

    client, store, service, monitor, template, _ = scene
    rack_template = service.sync_resource_templates(
        [
            {
                "id": "local.batch.prefilled-rack",
                "registry_type": "material",
                "available_sites": [
                    {
                        "index": "A1",
                        "label": "预装瓶位",
                        "initial_material_template_uuid": template,
                    }
                ],
            }
        ]
    )["templates"][0]["uuid"]
    second_template = str(uuid4()) if invalid_second else rack_template

    response = client.post(
        "/api/v1/materials/batch",
        json={
            "items": [
                _item(rack_template, "预装架一"),
                _item(second_template, "预装架二"),
            ]
        },
    )

    if invalid_second:
        assert response.json()["code"] == MATERIAL_TEMPLATE_NOT_FOUND
        for table in ("material", "site", "inventory_ledger", "sync_outbox"):
            assert store.query_one(f"SELECT COUNT(*) AS n FROM {table}") == {"n": 0}
        assert monitor.recent("material") == []
    else:
        assert response.json()["code"] == 0
        assert store.query_one("SELECT COUNT(*) AS n FROM material") == {"n": 4}
        assert store.query_one("SELECT COUNT(*) AS n FROM site") == {"n": 2}
        for rack in response.json()["data"]["items"]:
            assert len(rack["site_uuids"]) == 1
            assert len(rack["initial_material_uuids"]) == 1
            child = rack["initial_material_uuids"][0]
            assert rack["children"] == [{"uuid": child}]
            assert store.query_one(
                "SELECT occupied_material_uuid FROM site WHERE uuid=?",
                (rack["site_uuids"][0],),
            ) == {"occupied_material_uuid": child}
            assert store.query_one("SELECT parent_uuid FROM material WHERE uuid=?", (child,)) == {
                "parent_uuid": rack["uuid"]
            }
