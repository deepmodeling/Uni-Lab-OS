"""materials.v1 出库入口测试：GET /registry-classes 与 POST /instantiate。

微前端出库闭环的服务端半程：前端只提供「registry 资源类 + 实例名」，
实例化（PLR）与权威登记（发 uuid）都发生在微后端。
"""

from __future__ import annotations

from typing import Any, Dict
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import command_rejected, command_result, error_of, ok_data
from unilabos.registry.registry import lab_registry
from unilabos.server.api.materials import install_materials_api
from unilabos.protocol.materials import InventoryMutation
from unilabos.server.services.materials import MaterialsService

REGISTRY_CLASS = "test_tip_rack_300ul"


@pytest.fixture()
def registry_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        lab_registry.resource_type_registry,
        REGISTRY_CLASS,
        {
            "class": {
                "module": "pylabrobot.resources.opentrons.tip_racks:opentrons_96_tiprack_300ul",
                "type": "pylabrobot",
            },
            "display_name": "300ul 枪头盒",
        },
    )


def _mutation_body(operation: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    mutation = InventoryMutation(
        command_uuid=str(uuid4()), effect_key=operation, operation=operation
    )
    body = mutation.model_dump(mode="json")
    body["payload"] = payload
    return body


def test_registry_classes_lists_instantiable_entries(
    tmp_path, registry_entry
) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    try:
        with TestClient(app) as client:
            listed = client.get("/api/v1/materials/registry-classes")
            assert listed.status_code == 200
            row = next(
                item
                for item in ok_data(listed)
                if item["registry_class"] == REGISTRY_CLASS
            )
            assert row["display_name"] == "300ul 枪头盒"
    finally:
        service.close()


def test_instantiate_creates_authoritative_tree(tmp_path, registry_entry) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/inventory/commands",
                json=_mutation_body(
                    "instantiate_material",
                    {"registry_class": REGISTRY_CLASS, "name": "tips_outbound_1"},
                ),
            )
            tree = command_result(resp)["data"]
            root_uuid = tree["root_material_uuid"]
            assert root_uuid
            root = next(
                node
                for node in tree["nodes"]
                if node["material"]["material_uuid"] == root_uuid
            )
            assert root["material"]["name"] == "tips_outbound_1"
            # tip rack 带 96 个 tip spot 子节点，每个孔位下一支已命名的 Tip：
            # 维护版 PLR 起枪头是孔位的结构孩子，与孔位一样整棵树权威登记（1 + 96 + 96）
            materials = {
                node["material"]["material_uuid"]: node["material"]
                for node in tree["nodes"]
            }
            spots = [
                item
                for item in materials.values()
                if item["parent_material_uuid"] == root_uuid
            ]
            tips = [
                item for item in materials.values() if item["resource_type"] == "tip"
            ]
            assert len(spots) == 96
            assert {item["resource_type"] for item in spots} == {"tip_spot"}
            assert len(tips) == 96
            assert sorted(item["parent_material_uuid"] for item in tips) == sorted(
                item["material_uuid"] for item in spots
            )
            assert len(tree["nodes"]) == 1 + 96 + 96

            fetched = client.get(f"/api/v1/inventory/instances/{root_uuid}")
            assert fetched.status_code == 200
            assert ok_data(fetched)["material"]["name"] == "tips_outbound_1"
    finally:
        service.close()


def test_instantiate_with_barcode_writes_root_barcode(
    tmp_path, registry_entry
) -> None:
    """带 barcode 出库：条码只写入根节点，子节点不带。"""
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/inventory/commands",
                json=_mutation_body(
                    "instantiate_material",
                    {
                        "registry_class": REGISTRY_CLASS,
                        "name": "tips_outbound_bc",
                        "barcode": "BC-20260830-001",
                    },
                ),
            )
            tree = command_result(resp)["data"]
            root_uuid = tree["root_material_uuid"]
            root = next(
                node
                for node in tree["nodes"]
                if node["material"]["material_uuid"] == root_uuid
            )
            assert root["material"]["barcode"] == "BC-20260830-001"
            children = [
                node
                for node in tree["nodes"]
                if node["material"]["material_uuid"] != root_uuid
            ]
            assert all(not node["material"]["barcode"] for node in children)

            fetched = client.get(f"/api/v1/inventory/instances/{root_uuid}")
            assert fetched.status_code == 200
            assert ok_data(fetched)["material"]["barcode"] == "BC-20260830-001"
    finally:
        service.close()


def test_instantiate_unknown_registry_class_rejected(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/inventory/commands",
                json=_mutation_body(
                    "instantiate_material",
                    {"registry_class": "not_exist_class", "name": "x"},
                ),
            )
            # registry 资源类不可实例化：参数不合法 1000（命令入口的拒绝结果）
            command_rejected(resp, 1000)
    finally:
        service.close()
