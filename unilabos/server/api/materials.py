"""``materials.v1`` FastAPI 路由；HTTP 仅做协议校验和错误映射。"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import Field

from unilabos.config.config import BasicConfig
from unilabos.server.database.tables.base import ServerObject
from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    InventoryLotInbound,
    InventoryReservationCreate,
    InventoryReservationTransition,
    InventoryTaskReservationCreate,
    MaterialDataWrite,
    MaterialDelete,
    MaterialMove,
    MaterialPatch,
    MaterialPosition,
    MaterialSnapshot,
    MaterialTreeCreate,
    MaterialTransfer,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import (
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialNotFoundError,
    MaterialValidationError,
    MaterialTransferSyncError,
    MaterialsService,
    InsufficientInventoryError,
    RejectedMutationError,
)
from unilabos.server.protocol.virtual_environment import (
    VirtualEnvironmentId,
    VirtualEnvironmentResetRequest,
)
from unilabos.server.services.virtual_environment import VirtualEnvironmentService


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
    except (
        MaterialConflictError,
        MaterialNoChangeError,
        InsufficientInventoryError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RejectedMutationError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except MaterialValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MaterialTransferSyncError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def create_materials_router(service: MaterialsService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/materials", tags=["materials-v1"])
    virtual_environments = VirtualEnvironmentService(service)

    @router.get("/virtual-environments")
    async def list_virtual_environments():
        return virtual_environments.catalog(reset_allowed=BasicConfig.test_mode)

    @router.post("/virtual-environments/{preset_id}/reset")
    async def reset_virtual_environment(
        preset_id: VirtualEnvironmentId,
        value: VirtualEnvironmentResetRequest,
    ):
        if not BasicConfig.test_mode:
            raise HTTPException(
                status_code=403,
                detail="virtual material reset requires UniLabOS --test_mode",
            )
        return _call(
            virtual_environments.reset,
            preset_id,
            request_uuid=str(value.request_uuid),
        )

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

    @router.post("/lots/inbound")
    async def inbound_inventory_lot(mutation: InventoryMutation):
        return _call(
            service.inbound_inventory_lot,
            mutation,
            _payload(mutation, InventoryLotInbound),
        )

    @router.get("/lots")
    async def list_inventory_lots(
        template_uuid: str | None = Query(default=None),
        unit: str | None = Query(default=None),
        include_quarantined: bool = Query(default=False),
    ):
        return _call(
            service.list_inventory_lots,
            template_uuid=template_uuid,
            unit=unit,
            include_quarantined=include_quarantined,
        )

    @router.get("/lots/{lot_uuid}")
    async def get_inventory_lot(lot_uuid: str):
        return _call(service.get_inventory_lot, lot_uuid)

    @router.post("/reservations")
    async def reserve_inventory(mutation: InventoryMutation):
        return _call(
            service.reserve_inventory,
            mutation,
            _payload(mutation, InventoryReservationCreate),
        )

    @router.post("/reservations/batch")
    async def reserve_task_inventory(mutation: InventoryMutation):
        return _call(
            service.reserve_task_inventory,
            mutation,
            _payload(mutation, InventoryTaskReservationCreate),
        )

    @router.get("/reservations")
    async def list_inventory_reservations(
        task_uuid: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ):
        return _call(
            service.list_inventory_reservations,
            task_uuid=task_uuid,
            status=status,
        )

    @router.get("/reservations/by-job/{job_uuid}")
    async def get_inventory_reservation_by_job(job_uuid: str):
        return _call(service.get_inventory_reservation_by_job, job_uuid)

    @router.get("/reservations/{reservation_uuid}")
    async def get_inventory_reservation(reservation_uuid: str):
        return _call(service.get_inventory_reservation, reservation_uuid)

    def reservation_transition(
        reservation_uuid: str,
        mutation: InventoryMutation,
        method,
    ):
        value = _payload(mutation, InventoryReservationTransition)
        if value.reservation_uuid != reservation_uuid:
            raise HTTPException(
                status_code=422,
                detail="inventory reservation UUID path mismatch",
            )
        return _call(method, mutation, value)

    @router.post("/reservations/{reservation_uuid}/consume")
    async def consume_inventory_reservation(
        reservation_uuid: str, mutation: InventoryMutation
    ):
        return reservation_transition(
            reservation_uuid,
            mutation,
            service.consume_inventory_reservation,
        )

    @router.post("/reservations/{reservation_uuid}/release")
    async def release_inventory_reservation(
        reservation_uuid: str, mutation: InventoryMutation
    ):
        return reservation_transition(
            reservation_uuid,
            mutation,
            service.release_inventory_reservation,
        )

    @router.post("/reservations/{reservation_uuid}/quarantine")
    async def quarantine_inventory_reservation(
        reservation_uuid: str, mutation: InventoryMutation
    ):
        return reservation_transition(
            reservation_uuid,
            mutation,
            service.quarantine_inventory_reservation,
        )

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

    @router.post("/transfer")
    async def transfer_material(mutation: InventoryMutation):
        return _call(
            service.transfer_material,
            mutation,
            _payload(mutation, MaterialTransfer),
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
