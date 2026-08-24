from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.client.materials import bind_payload
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.api.materials import create_materials_router
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTransfer,
    MaterialTransferItem,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import (
    MaterialTransferSyncError,
    MaterialsService,
)


def _mutation(operation: str, *, command_uuid: str | None = None) -> InventoryMutation:
    command = command_uuid or str(uuid4())
    return InventoryMutation(
        command_uuid=command,
        effect_key=f"{operation}:{command}",
        operation=operation,
        actor_type="test",
        actor_uuid="material-transfer-test",
    )


def _create_material(
    service: MaterialsService,
    *,
    resource_id: str,
    template_name: str,
) -> str:
    if not any(item.name == template_name for item in service.list_templates()):
        service.put_template(
            _mutation("put_template"),
            ResourceTemplateWrite(
                template_uuid=f"{template_name}-template",
                name=template_name,
                display_name=template_name,
                class_name="Resource",
            ),
        )
    created = service.create_tree(
        _mutation("create_material_tree"),
        MaterialTreeCreate(
            nodes=[
                MaterialNodeCreate(
                    client_ref=resource_id,
                    identity=MaterialIdentityWrite(
                        resource_id=resource_id,
                        name=resource_id,
                        template_name=template_name,
                    ),
                )
            ]
        ),
    )
    return created.data.root_material_uuid


class _DeviceProjection:
    def __init__(self, service: MaterialsService, material_uuid: str) -> None:
        self.service = service
        self.material_uuid = material_uuid
        self.mounts = {
            "source-device": {material_uuid},
            "target-device": set(),
        }
        self.calls = []
        self.fail_load_after_apply = False

    def __call__(self, command):
        # 两端收到通知时，materials.db 必须已经是目标位置。
        current = self.service.get_material(self.material_uuid)
        assert current.material.parent_material_uuid == self.target_uuid
        self.calls.append(command)
        if command.action == "unload":
            self.mounts[command.device_id].discard(self.material_uuid)
        else:
            self.mounts[command.device_id].add(self.material_uuid)
            if self.fail_load_after_apply:
                self.fail_load_after_apply = False
                raise ConnectionError("load reply was lost")
        return {"success": True}


def _prepared_transfer(tmp_path):
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    source_uuid = _create_material(
        service,
        resource_id="source-mount",
        template_name="mount",
    )
    target_uuid = _create_material(
        service,
        resource_id="target-mount",
        template_name="mount",
    )
    material_uuid = _create_material(
        service,
        resource_id="tube-1",
        template_name="tube",
    )
    service.move_material(
        _mutation("move_material"),
        MaterialMove(
            material_uuid=material_uuid,
            parent_material_uuid=source_uuid,
        ),
    )
    projection = _DeviceProjection(service, material_uuid)
    projection.target_uuid = target_uuid
    service.set_resource_sync_dispatcher(projection)
    request = MaterialTransfer(
        source_device_id="source-device",
        target_device_id="target-device",
        items=[
            MaterialTransferItem(
                material_uuid=material_uuid,
                target_material_uuid=target_uuid,
            )
        ],
    )
    return service, projection, request, material_uuid, target_uuid


def test_transfer_commits_before_unload_then_load_and_replays_idempotently(
    tmp_path,
) -> None:
    service, projection, request, material_uuid, target_uuid = _prepared_transfer(
        tmp_path
    )
    mutation = _mutation("transfer_material")
    try:
        first = service.transfer_material(mutation, request)
        first_version = service.get_material(material_uuid).material.version
        replay = service.transfer_material(mutation, request)

        assert first.replayed is False
        assert replay.replayed is True
        assert first.data.destination_site_uuids == [None]
        assert service.get_material(material_uuid).material.parent_material_uuid == (
            target_uuid
        )
        assert service.get_material(material_uuid).material.version == first_version
        assert [item.action for item in projection.calls] == [
            "unload",
            "load",
            "unload",
            "load",
        ]
        assert {item.transfer_uuid for item in projection.calls} == {
            mutation.command_uuid
        }
        assert projection.mounts == {
            "source-device": set(),
            "target-device": {material_uuid},
        }
    finally:
        service.repository.close()


def test_transfer_load_reply_loss_keeps_single_mount_and_recovers_on_retry(
    tmp_path,
) -> None:
    service, projection, request, material_uuid, target_uuid = _prepared_transfer(
        tmp_path
    )
    mutation = _mutation("transfer_material")
    projection.fail_load_after_apply = True
    try:
        with pytest.raises(MaterialTransferSyncError, match="load reply was lost"):
            service.transfer_material(mutation, request)

        assert service.get_material(material_uuid).material.parent_material_uuid == (
            target_uuid
        )
        assert projection.mounts == {
            "source-device": set(),
            "target-device": {material_uuid},
        }

        replay = service.transfer_material(mutation, request)
        assert replay.replayed is True
        assert projection.mounts == {
            "source-device": set(),
            "target-device": {material_uuid},
        }
    finally:
        service.repository.close()


def test_materials_api_exposes_authoritative_transfer(tmp_path) -> None:
    service, projection, request, material_uuid, target_uuid = _prepared_transfer(
        tmp_path
    )
    app = FastAPI()
    app.include_router(create_materials_router(service))
    mutation = _mutation("transfer_material")
    try:
        response = TestClient(app).post(
            "/api/v1/materials/transfer",
            json=bind_payload(mutation, request).model_dump(
                mode="json",
                exclude_none=False,
            ),
        )

        assert response.status_code == 200
        assert response.json()["data"]["material_uuids"] == [material_uuid]
        assert service.get_material(material_uuid).material.parent_material_uuid == (
            target_uuid
        )
        assert [item.action for item in projection.calls] == ["unload", "load"]
    finally:
        service.repository.close()
