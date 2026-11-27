"""试剂身份与试剂库存批量 JSON/文件导入接口测试。"""

from __future__ import annotations

import csv
import io

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from unilabos.app.scheduler.inventory.backend_api import install_backend_resource_api
from unilabos.app.scheduler.inventory.backend_contract import (
    MATERIAL_NOT_FOUND,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.store import InventoryStore


def _client(tmp_path) -> tuple[TestClient, InventoryStore]:
    store = InventoryStore(str(tmp_path / "inventory.db"))
    app = FastAPI()
    install_backend_resource_api(app, BackendResourceService(store))
    return TestClient(app), store


def _write_csv(headers: list[str], rows: list[list[object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _reagent_info(client: TestClient, *, cas: str = "64-17-5") -> str:
    response = client.post(
        "/api/v1/reagent-infos",
        json={"cas": cas, "name": f"试剂-{cas}", "physical_state": "liquid"},
    )
    assert response.status_code == 201
    return response.json()["data"]["uuid"]


def _containers(client: TestClient, count: int = 2) -> list[str]:
    template = client.post(
        "/api/v1/resource-templates",
        json={
            "resources": [
                {
                    "id": "local.batch.reagent_bottle",
                    "display_name": "批量试剂瓶",
                    "registry_type": "material",
                    "category": ["container"],
                    "metadata": {"capacity": {"max_volume_ul": 1000000}},
                    "model": {},
                    "class": {},
                    "handles": [],
                    "config_info": [],
                    "scene": [],
                    "device_params": {},
                }
            ]
        },
    ).json()["data"]["templates"][0]["uuid"]
    result: list[str] = []
    for index in range(count):
        response = client.post(
            "/api/v1/materials",
            json={
                "resource_template_uuid": template,
                "name": f"批量容器-{index + 1}",
                "barcode": f"BATCH-REAGENT-{index + 1}",
            },
        )
        assert response.status_code == 201
        result.append(response.json()["data"]["uuid"])
    return result


def test_reagent_info_json_batch_is_atomic(tmp_path) -> None:
    client, store = _client(tmp_path)

    response = client.post(
        "/api/v1/reagent-infos/batch",
        json={
            "items": [
                {"cas": "64-17-5", "name": "乙醇", "physical_state": "liquid"},
                {"cas": "67-56-1", "name": "甲醇", "physical_state": "liquid"},
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["total"] == 2
    assert payload["data"]["created"] == 2
    assert payload["data"]["failed"] == 0
    assert len(payload["data"]["items"]) == 2
    assert store.query_one("SELECT COUNT(*) AS count FROM reagent_info") == {"count": 2}
    store.close()


def test_reagent_info_csv_import_supports_json_cells(tmp_path) -> None:
    client, store = _client(tmp_path)
    content = _write_csv(
        ["cas", "name", "physical_state", "aliases", "meta_data"],
        [
            [
                "64-17-5",
                "乙醇",
                "liquid",
                '["酒精", "Ethanol"]',
                '{"storage":"阴凉通风"}',
            ],
            ["67-56-1", "甲醇", "liquid", "", ""],
        ],
    )

    response = client.post(
        "/api/v1/reagent-infos/import",
        files={"file": ("reagent-infos.csv", content, "text/csv")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["created"] == 2
    assert payload["data"]["errors"] == []
    ethanol = client.get("/api/v1/reagent-infos").json()["data"]["items"][0]
    assert ethanol["aliases"] == ["酒精", "Ethanol"]
    assert ethanol["meta_data"] == {"storage": "阴凉通风"}
    store.close()


def test_reagent_info_json_file_import_accepts_items_envelope(tmp_path) -> None:
    client, store = _client(tmp_path)
    content = (
        '{"items":[{"cas":"64-17-5","name":"乙醇","physical_state":"liquid"}]}'
    ).encode("utf-8")

    response = client.post(
        "/api/v1/reagent-infos/import",
        files={"file": ("reagent-infos.json", content, "application/json")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["created"] == 1
    assert store.query_one("SELECT COUNT(*) AS count FROM reagent_info") == {"count": 1}
    store.close()


def test_reagent_info_xlsx_import_reads_first_sheet(tmp_path) -> None:
    client, store = _client(tmp_path)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["cas", "name", "physical_state", "aliases", "meta_data"])
    worksheet.append(
        ["64-17-5", "乙醇", "liquid", '["酒精"]', '{"storage":"阴凉通风"}']
    )
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()

    response = client.post(
        "/api/v1/reagent-infos/import",
        files={
            "file": (
                "reagent-infos.xlsx",
                output.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["created"] == 1
    item = client.get("/api/v1/reagent-infos").json()["data"]["items"][0]
    assert item["aliases"] == ["酒精"]
    assert item["meta_data"] == {"storage": "阴凉通风"}
    store.close()


def test_reagent_csv_import_rolls_back_when_one_container_is_invalid(tmp_path) -> None:
    client, store = _client(tmp_path)
    info_uuid = _reagent_info(client)
    containers = _containers(client)
    content = _write_csv(
        ["material_uuid", "reagent_info_uuid", "quantity", "quantity_unit"],
        [
            [containers[0], info_uuid, 100, "mL"],
            ["00000000-0000-0000-0000-000000000000", info_uuid, 50, "mL"],
        ],
    )

    response = client.post(
        "/api/v1/reagents/import",
        files={"file": ("reagents.csv", content, "text/csv")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == MATERIAL_NOT_FOUND
    assert payload["error"]["details"]["created"] == 0
    assert payload["error"]["details"]["failed"] == 2
    assert payload["error"]["details"]["errors"][0]["row"] is None
    assert store.query_one("SELECT COUNT(*) AS count FROM reagent") == {"count": 0}
    assert store.query_one("SELECT COUNT(*) AS count FROM inventory_ledger") == {"count": 0}
    store.close()


def test_reagent_json_batch_non_atomic_reports_row_errors(tmp_path) -> None:
    client, store = _client(tmp_path)
    info_uuid = _reagent_info(client)
    container = _containers(client, count=1)[0]

    response = client.post(
        "/api/v1/reagents/batch",
        json={
            "atomic": False,
            "items": [
                {
                    "material_uuid": container,
                    "reagent_info_uuid": info_uuid,
                    "quantity": 10,
                    "quantity_unit": "mL",
                },
                {
                    "material_uuid": "not-a-uuid",
                    "reagent_info_uuid": info_uuid,
                    "quantity": 5,
                    "quantity_unit": "mL",
                },
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["created"] == 1
    assert payload["data"]["failed"] == 1
    assert payload["data"]["errors"][0]["row"] == 2
    assert store.query_one("SELECT COUNT(*) AS count FROM reagent") == {"count": 1}
    store.close()
