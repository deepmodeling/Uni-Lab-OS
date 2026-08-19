"""``materials.v1`` FastAPI 路由；HTTP 仅做协议校验和错误映射。"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import Field

from unilabos.server.models.base import ServerObject
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    MaterialDataWrite,
    MaterialDelete,
    MaterialMove,
    MaterialPatch,
    MaterialPosition,
    MaterialSnapshot,
    MaterialTreeCreate,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import (
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialNotFoundError,
    MaterialValidationError,
    MaterialsService,
    RejectedMutationError,
)


class LedgerAcknowledge(ServerObject):
    through_sequence: int = Field(ge=0)


def _payload(mutation: InventoryMutation, model: type):
    try:
        return model.model_validate(mutation.payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except MaterialNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (MaterialConflictError, MaterialNoChangeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RejectedMutationError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except MaterialValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_materials_router(service: MaterialsService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/materials", tags=["materials-v1"])

    @router.put("/templates/{template_uuid}")
    async def put_template(template_uuid: str, mutation: InventoryMutation):
        value = _payload(mutation, ResourceTemplateWrite)
        if value.template_uuid != template_uuid:
            raise HTTPException(status_code=422, detail="template UUID path mismatch")
        return _call(service.put_template, mutation, value)

    @router.post("/templates")
    async def create_template(mutation: InventoryMutation):
        value = _payload(mutation, ResourceTemplateWrite)
        if value.template_uuid is not None:
            raise HTTPException(
                status_code=422,
                detail="POST template lets the materials authority allocate UUID",
            )
        return _call(service.put_template, mutation, value)

    @router.get("/templates")
    async def list_templates():
        return _call(service.list_templates)

    @router.get("/templates/{template_uuid}")
    async def get_template(template_uuid: str):
        return _call(service.get_template, template_uuid)

    @router.delete("/templates/{template_uuid}")
    async def delete_template(template_uuid: str, mutation: InventoryMutation):
        expected = {"template_uuid": template_uuid}
        if mutation.payload and mutation.payload != expected:
            raise HTTPException(status_code=422, detail="template UUID path mismatch")
        mutation = mutation.model_copy(update={"payload": expected})
        return _call(service.delete_template, mutation, template_uuid)

    @router.post("/trees")
    async def create_tree(mutation: InventoryMutation):
        return _call(
            service.create_tree,
            mutation,
            _payload(mutation, MaterialTreeCreate),
        )

    @router.get("/instances")
    async def list_materials(roots_only: bool = Query(default=False)):
        return _call(service.list_materials, roots_only=roots_only)

    @router.get("/instances/by-resource-id/{resource_id}")
    async def get_material_by_resource_id(resource_id: str):
        return _call(service.get_material_by_resource_id, resource_id)

    @router.get("/instances/{material_uuid}")
    async def get_material(material_uuid: str):
        return _call(service.get_material, material_uuid)

    @router.get("/instances/{material_uuid}/tree")
    async def get_tree(material_uuid: str):
        return _call(service.get_tree, material_uuid)

    @router.patch("/instances/{material_uuid}")
    async def patch_material(material_uuid: str, mutation: InventoryMutation):
        return _call(
            service.patch_material,
            mutation,
            material_uuid,
            _payload(mutation, MaterialPatch),
        )

    @router.put("/instances/{material_uuid}/position")
    async def put_position(material_uuid: str, mutation: InventoryMutation):
        return _call(
            service.put_position,
            mutation,
            material_uuid,
            _payload(mutation, MaterialPosition),
        )

    @router.put("/instances/{material_uuid}/data")
    async def put_data(material_uuid: str, mutation: InventoryMutation):
        return _call(
            service.put_data,
            mutation,
            material_uuid,
            _payload(mutation, MaterialDataWrite),
        )

    @router.delete("/instances/{material_uuid}")
    async def delete_material(material_uuid: str, mutation: InventoryMutation):
        value = _payload(mutation, MaterialDelete)
        if value.material_uuid != material_uuid:
            raise HTTPException(status_code=422, detail="material UUID path mismatch")
        return _call(service.delete_material, mutation, value)

    @router.post("/move")
    async def move_material(mutation: InventoryMutation):
        return _call(
            service.move_material,
            mutation,
            _payload(mutation, MaterialMove),
        )

    @router.post("/snapshots/compare")
    async def compare_snapshot(snapshot: MaterialSnapshot):
        return _call(service.compare_snapshot, snapshot)

    @router.post("/snapshots/apply")
    async def apply_snapshot(mutation: InventoryMutation):
        return _call(
            service.apply_snapshot,
            mutation,
            _payload(mutation, MaterialSnapshot),
        )

    @router.get("/changes")
    async def changes(
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return service.changes(after_sequence=after_sequence, limit=limit)

    @router.post("/changes/ack")
    async def acknowledge_changes(value: LedgerAcknowledge):
        return {"acknowledged": service.acknowledge_changes(value.through_sequence)}

    return router


def install_materials_api(app: FastAPI, service: MaterialsService) -> None:
    app.include_router(create_materials_router(service))


__all__ = [
    "LedgerAcknowledge",
    "create_materials_router",
    "install_materials_api",
]
