"""materials.v1 HTTP 与 Local client 契约测试。"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.api.materials import install_materials_api
from unilabos.server.clients.materials import LocalMaterialsClient, bind_payload
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import ResourceTemplateWrite
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
            response = client.put(
                "/api/v1/materials/templates/beaker-template",
                json=mutation.model_dump(mode="json"),
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["definition_hash"]

            fetched = client.get(
                "/api/v1/materials/templates/beaker-template"
            )
            assert fetched.status_code == 200
            assert fetched.json()["name"] == "beaker"
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
