"""materials.v1 HTTP 与 Local client 契约测试。"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import command_result, error_of, ok_data
from unilabos.server.api.materials import install_materials_api
from unilabos.client.materials import LocalMaterialsClient, bind_payload
from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import ResourceTemplateWrite
from unilabos.protocol.materials import (
    MaterialIdentityWrite,
    MaterialNodeCreate,
    MaterialTreeCreate,
)
from unilabos.server.services.materials import MaterialsService


def _mutation(operation: str) -> InventoryMutation:
    return InventoryMutation(
        command_uuid=str(uuid4()), effect_key=operation, operation=operation
    )


def test_http_protocol_uses_mutation_payload(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    template = ResourceTemplateWrite(
        template_uuid="beaker-template",
        name="beaker",
        display_name="Beaker",
        resource_type="container",
        class_name="RegularContainer",
    )
    mutation = bind_payload(_mutation("put_template"), template)
    try:
        with TestClient(app) as client:
            # materials.v1 写入统一经 V1 库存命令入口（裸结果，不包信封）
            response = client.post(
                "/api/v1/inventory/commands",
                json=mutation.model_dump(mode="json"),
            )
            assert command_result(response)["data"]["definition_hash"]

            fetched = client.get(
                "/api/v1/inventory/templates/beaker-template"
            )
            assert fetched.status_code == 200
            assert ok_data(fetched)["name"] == "beaker"

            # 列表按 name 精确筛选，默认目录模式：存在性检查只回一条、不带 definition
            listed = client.get(
                "/api/v1/inventory/templates",
                params={"name": "beaker"},
            )
            assert listed.status_code == 200
            assert [item["template_uuid"] for item in ok_data(listed)["templates"]] == ["beaker-template"]
            assert ok_data(listed)["templates"][0]["definition"] == {}
            assert ok_data(client.get(
                "/api/v1/inventory/templates", params={"name": "flask"}
            ))["templates"] == []
            # dev 旧路径已删除（V1 只有一套路径）：/materials/templates 现在落到 V1 的
            # /materials/{material_uuid}，按物料不存在回答
            error_of(client.get("/api/v1/materials/templates"), 6000)
    finally:
        service.close()


def test_post_template_allocates_authoritative_uuid(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    client = LocalMaterialsClient(service)
    template = ResourceTemplateWrite(
        name="beaker",
        display_name="Beaker",
        resource_type="container",
        class_name="RegularContainer",
    )
    try:
        created = client.create_template(
            _mutation("create_template"),
            template,
        )

        assert created.data.template_uuid
        assert client.get_template(created.data.template_uuid).name == "beaker"
    finally:
        service.close()


def test_notify_device_dispatches_via_downlink_and_returns_receipt(
    tmp_path, monkeypatch
) -> None:
    """物料创建只发生在微后端；变更经模块级 downlink 分发到设备并透传回执。"""
    from unilabos.backend.hostlink import downlink

    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)

    calls = []

    def _notify(device_id, action, resource_uuids):
        calls.append((device_id, action, resource_uuids))
        return True

    monkeypatch.setattr(downlink, "notify_resource_tree_update", _notify)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/materials/notify-device",
                json={
                    "device_id": "edge-device",
                    "action": "add",
                    "resource_uuids": ["uuid-1", "uuid-2"],
                },
            )
        assert response.status_code == 200, response.text
        assert ok_data(response)["notified"] is True
        assert calls == [("edge-device", "add", ["uuid-1", "uuid-2"])]
    finally:
        service.close()


def test_notify_device_reports_skip_when_device_unreachable(tmp_path) -> None:
    """设备既不在本进程、也不在 HostLink 在线表：notified 为 null（有意跳过）。"""
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/materials/notify-device",
                json={
                    "device_id": "edge-device",
                    "action": "add",
                    "resource_uuids": ["uuid-1"],
                },
            )
        assert response.status_code == 200, response.text
        assert ok_data(response)["notified"] is None
    finally:
        service.close()


def test_post_tree_resolves_template_name_and_allocates_all_uuids(tmp_path) -> None:
    service = MaterialsService(tmp_path / "materials.db")
    app = FastAPI()
    install_materials_api(app, service)
    tree = MaterialTreeCreate(
        nodes=[
            MaterialNodeCreate(
                client_ref="root",
                identity=MaterialIdentityWrite(
                    resource_id="custom-tube-1",
                    name="custom-tube-1",
                    resource_type="container",
                    class_name="Container",
                    template_name="custom-tube",
                ),
            )
        ]
    )
    payload = tree.model_dump(mode="json")
    assert "template_uuid" not in payload["nodes"][0]["identity"]
    mutation = bind_payload(_mutation("create_material_tree"), tree)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/inventory/commands",
                json=mutation.model_dump(mode="json"),
            )

        body = command_result(response)["data"]
        assert body["root_material_uuid"]
        assert body["nodes"][0]["material"]["template_uuid"]
    finally:
        service.close()
