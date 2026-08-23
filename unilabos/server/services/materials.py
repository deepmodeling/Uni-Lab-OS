"""以 ``materials.db`` 为唯一权威的物料聚合服务。"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Optional, TypeVar
from uuid import uuid4

from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.database.tables.materials import (
    InventoryCommandEffectRecord,
    InventoryLedgerRecord,
    InventoryLotRecord,
    InventoryReservationRecord,
    MaterialDataRecord,
    MaterialPositionRecord,
    MaterialRecord,
    MaterialSubstanceRecord,
    ResourceTemplateRecord,
    SiteRecord,
)
from unilabos.server.protocol.common import (
    AggregatePrecondition,
    AggregateVersion,
    InventoryMutation,
    InventoryChange,
    MutationResult,
    canonical_hash,
)
from unilabos.server.protocol.materials import (
    InventoryAllocation,
    InventoryLotInbound,
    InventoryLotRead,
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationRead,
    InventoryReservationTransition,
    InventoryTaskReservationCreate,
    InventoryTaskReservationRead,
    MaterialAggregateRead,
    MaterialDataRead,
    MaterialDataWrite,
    MaterialDelete,
    MaterialDeleteResult,
    MaterialDeviceSync,
    MaterialIdentityRead,
    MaterialMove,
    MaterialPatch,
    MaterialPosition,
    MaterialSnapshot,
    MaterialSnapshotDiff,
    MaterialSubstance,
    MaterialTreeCreate,
    MaterialTreeRead,
    MaterialTransfer,
    MaterialTransferResult,
    ResourceTemplateRead,
    ResourceTemplateWrite,
    SiteCreate,
    SiteRead,
)
from unilabos.server.services.material_snapshot import (
    compare_material_snapshot,
    material_sections,
    site_semantic,
)


class MaterialsServiceError(RuntimeError):
    code = "materials_error"


class MaterialNotFoundError(MaterialsServiceError):
    code = "not_found"


class MaterialConflictError(MaterialsServiceError):
    code = "conflict"


class MaterialValidationError(MaterialsServiceError):
    code = "invalid_material"


class MaterialNoChangeError(MaterialsServiceError):
    code = "no_change"


class RejectedMutationError(MaterialsServiceError):
    code = "rejected"


class MaterialTransferSyncError(MaterialsServiceError):
    """权威位置已提交，但设备本地投影尚未完成收敛。"""

    code = "transfer_sync_failed"


class InsufficientInventoryError(MaterialsServiceError):
    """库存无法在一个事务内满足调度器冻结的完整需求集合。"""

    code = "insufficient_inventory"


DataT = TypeVar("DataT")
ResourceSyncDispatcher = Callable[[MaterialDeviceSync], Any]


@dataclass
class _Applied(Generic[DataT]):
    data: DataT
    affected: list[AggregateVersion]
    sequences: list[int]


class MaterialsService:
    """Edge 侧聚合 CRUD、幂等命令和账本的唯一物料写入口。

    TODO(materials-backend-proxy): 正式 Backend 接入后，创建等全局写操作由本服务
    转发给 Backend，再把带权威 UUID/版本的回执落入本地投影。本版本不接入该转发，
    也不允许设备、Host 或 Slave 绕过本服务直连 Backend。
    """

    def __init__(
        self,
        repository: MaterialsRepository,
        *,
        resource_sync_dispatcher: ResourceSyncDispatcher | None = None,
    ):
        if not isinstance(repository, MaterialsRepository):
            raise TypeError("repository must be a MaterialsRepository")
        self.repository = repository
        self._resource_sync_dispatcher = resource_sync_dispatcher
        self._transfer_lock = threading.RLock()

    def set_resource_sync_dispatcher(
        self,
        dispatcher: ResourceSyncDispatcher | None,
    ) -> None:
        """绑定当前 backend 的设备 resource-service 传输适配器。"""

        self._resource_sync_dispatcher = dispatcher

    @staticmethod
    def _now_ms(mutation: Optional[InventoryMutation] = None) -> int:
        current = int(time.time() * 1000)
        if mutation is not None:
            current = max(current, mutation.observed_at_ms)
        return current

    @staticmethod
    def _bound_request(mutation: InventoryMutation, body: Any) -> dict[str, Any]:
        payload = (
            body.model_dump(mode="json", exclude_none=False)
            if hasattr(body, "model_dump")
            else body
        )
        if not isinstance(payload, dict):
            raise MaterialValidationError("mutation payload must be an object")
        if mutation.payload and mutation.payload != payload:
            raise MaterialValidationError(
                "mutation.payload differs from the typed request body"
            )
        return {
            "protocol_version": mutation.protocol_version,
            "command_uuid": mutation.command_uuid,
            "effect_key": mutation.effect_key,
            "operation": mutation.operation,
            "actor_type": mutation.actor_type,
            "actor_uuid": mutation.actor_uuid,
            "job_uuid": mutation.job_uuid,
            "observed_at_ms": mutation.observed_at_ms,
            "preconditions": [
                item.model_dump(mode="json", exclude_none=False)
                for item in mutation.preconditions
            ],
            "payload": payload,
        }

    def _run_mutation(
        self,
        mutation: InventoryMutation,
        body: Any,
        result_model: Any,
        apply: Callable[[int], _Applied[Any]],
    ) -> Any:
        request = self._bound_request(mutation, body)
        request_hash = canonical_hash(request)
        timestamp = self._now_ms(mutation)
        effect_started = False
        try:
            with self.repository.write():
                existing = self.repository.get_effect(
                    mutation.command_uuid, mutation.effect_key
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise MaterialConflictError(
                            "command_uuid/effect_key was already used with another request"
                        )
                    if existing.status == "rejected":
                        error_types = {
                            MaterialNotFoundError.code: MaterialNotFoundError,
                            MaterialConflictError.code: MaterialConflictError,
                            MaterialValidationError.code: MaterialValidationError,
                            MaterialNoChangeError.code: MaterialNoChangeError,
                            InsufficientInventoryError.code: InsufficientInventoryError,
                        }
                        error_type = error_types.get(
                            existing.error_code or "", RejectedMutationError
                        )
                        raise error_type(
                            existing.error_message or "mutation was previously rejected"
                        )
                    if existing.status != "applied":
                        raise MaterialConflictError("mutation is already applying")
                    replayed = dict(existing.result_json)
                    replayed["replayed"] = True
                    return result_model.model_validate(replayed)

                self.repository.insert_effect(
                    InventoryCommandEffectRecord(
                        command_uuid=mutation.command_uuid,
                        effect_key=mutation.effect_key,
                        job_uuid=mutation.job_uuid,
                        operation=mutation.operation,
                        request_json=request,
                        request_hash=request_hash,
                        status="applying",
                        started_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                )
                effect_started = True
                self._check_preconditions(mutation.preconditions)
                applied = apply(timestamp)
                if not applied.sequences:
                    raise MaterialNoChangeError("mutation produced no aggregate change")
                result = result_model(
                    command_uuid=mutation.command_uuid,
                    effect_key=mutation.effect_key,
                    ledger_sequence_start=min(applied.sequences),
                    ledger_sequence_end=max(applied.sequences),
                    affected=applied.affected,
                    data=applied.data,
                )
                self.repository.complete_effect(
                    command_uuid=mutation.command_uuid,
                    effect_key=mutation.effect_key,
                    result=result.model_dump(mode="json", exclude_none=False),
                    ledger_sequence_start=result.ledger_sequence_start,
                    ledger_sequence_end=result.ledger_sequence_end,
                    completed_at_ms=timestamp,
                )
                return result
        except MaterialsServiceError as exc:
            if effect_started:
                self._persist_rejection(
                    mutation,
                    request=request,
                    request_hash=request_hash,
                    timestamp=timestamp,
                    error=exc,
                )
            raise
        except sqlite3.IntegrityError as exc:
            error = MaterialConflictError(str(exc))
            if effect_started:
                self._persist_rejection(
                    mutation,
                    request=request,
                    request_hash=request_hash,
                    timestamp=timestamp,
                    error=error,
                )
            raise error from exc

    def _persist_rejection(
        self,
        mutation: InventoryMutation,
        *,
        request: dict[str, Any],
        request_hash: str,
        timestamp: int,
        error: MaterialsServiceError,
    ) -> None:
        """失败事务回滚后单独保存幂等拒绝结果。"""

        with self.repository.write():
            if self.repository.get_effect(
                mutation.command_uuid, mutation.effect_key
            ) is not None:
                return
            self.repository.insert_effect(
                InventoryCommandEffectRecord(
                    command_uuid=mutation.command_uuid,
                    effect_key=mutation.effect_key,
                    job_uuid=mutation.job_uuid,
                    operation=mutation.operation,
                    request_json=request,
                    request_hash=request_hash,
                    status="rejected",
                    error_code=error.code,
                    error_message=str(error),
                    started_at_ms=timestamp,
                    updated_at_ms=timestamp,
                    completed_at_ms=timestamp,
                )
            )

    def _check_preconditions(
        self, preconditions: list[AggregatePrecondition]
    ) -> None:
        for condition in preconditions:
            version, state_hash = self._aggregate_version_hash(
                condition.aggregate_type, condition.aggregate_uuid
            )
            if condition.expected_version is not None and (
                version != condition.expected_version
            ):
                raise MaterialConflictError(
                    f"{condition.aggregate_type} {condition.aggregate_uuid} "
                    f"version is {version}, expected {condition.expected_version}"
                )
            if condition.expected_state_hash is not None and (
                state_hash != condition.expected_state_hash
            ):
                raise MaterialConflictError(
                    f"{condition.aggregate_type} {condition.aggregate_uuid} state changed"
                )

    def _aggregate_version_hash(
        self, aggregate_type: str, aggregate_uuid: str
    ) -> tuple[int, str]:
        if aggregate_type == "resource_template":
            record = self.repository.get_template(aggregate_uuid)
            if record is None:
                raise MaterialNotFoundError(f"template not found: {aggregate_uuid}")
            return record.version, self._template_state_hash(record)
        if aggregate_type == "material":
            aggregate = self.get_material(aggregate_uuid)
            return aggregate.material.version, aggregate.state_hash
        if aggregate_type == "site":
            record = self.repository.get_site(aggregate_uuid)
            if record is None:
                raise MaterialNotFoundError(f"site not found: {aggregate_uuid}")
            return record.version, self._site_state_hash(record)
        if aggregate_type == "lot":
            record = self.repository.get_lot(aggregate_uuid)
            if record is None:
                raise MaterialNotFoundError(f"inventory lot not found: {aggregate_uuid}")
            return record.version, self._lot_state_hash(record)
        if aggregate_type == "reservation":
            record = self.repository.get_reservation(aggregate_uuid)
            if record is None:
                raise MaterialNotFoundError(
                    f"inventory reservation not found: {aggregate_uuid}"
                )
            return record.version, self._reservation_state_hash(record)
        raise MaterialValidationError(
            f"precondition for {aggregate_type!r} is not implemented by material service"
        )

    def _ledger(
        self,
        mutation: InventoryMutation,
        *,
        aggregate_type: str,
        aggregate_uuid: str,
        operation: str,
        previous_version: int,
        aggregate_version: int,
        state_hash: str,
        delta: dict[str, Any],
        timestamp: int,
    ) -> int:
        return self.repository.append_ledger(
            InventoryLedgerRecord(
                event_uuid=str(uuid4()),
                aggregate_type=aggregate_type,
                aggregate_uuid=aggregate_uuid,
                operation=operation,
                previous_version=previous_version,
                aggregate_version=aggregate_version,
                state_hash=state_hash,
                delta_json=delta,
                job_uuid=mutation.job_uuid,
                command_uuid=mutation.command_uuid,
                effect_key=mutation.effect_key,
                actor_type=mutation.actor_type,
                actor_uuid=mutation.actor_uuid,
                occurred_at_ms=timestamp,
                available_at_ms=timestamp,
            )
        )

    # -- Scheduler inventory ---------------------------------------------

    @staticmethod
    def _lot_state_hash(record: InventoryLotRecord) -> str:
        return canonical_hash(
            record.model_dump(
                mode="json",
                exclude={"created_at_ms", "updated_at_ms", "version"},
            )
        )

    @staticmethod
    def _reservation_state_hash(record: InventoryReservationRecord) -> str:
        return canonical_hash(
            record.model_dump(
                mode="json",
                exclude={"created_at_ms", "updated_at_ms", "version"},
            )
        )

    @staticmethod
    def _lot_read(record: InventoryLotRecord) -> InventoryLotRead:
        return InventoryLotRead.model_validate(record.model_dump(mode="json"))

    @staticmethod
    def _reservation_read(
        record: InventoryReservationRecord,
    ) -> InventoryReservationRead:
        return InventoryReservationRead.model_validate(record.model_dump(mode="json"))

    def get_inventory_lot(self, lot_uuid: str) -> InventoryLotRead:
        record = self.repository.get_lot(lot_uuid)
        if record is None:
            raise MaterialNotFoundError(f"inventory lot not found: {lot_uuid}")
        return self._lot_read(record)

    def list_inventory_lots(
        self,
        *,
        template_uuid: Optional[str] = None,
        unit: Optional[str] = None,
        include_quarantined: bool = False,
    ) -> list[InventoryLotRead]:
        return [
            self._lot_read(item)
            for item in self.repository.list_lots(
                template_uuid=template_uuid,
                unit=unit,
                include_quarantined=include_quarantined,
            )
        ]

    def inbound_inventory_lot(
        self,
        mutation: InventoryMutation,
        value: InventoryLotInbound,
    ) -> MutationResult[InventoryLotRead]:
        if mutation.operation != "inbound_inventory_lot":
            raise MaterialValidationError("inventory lot mutation operation is invalid")

        def apply(timestamp: int) -> _Applied[InventoryLotRead]:
            if self.repository.get_template(value.template_uuid) is None:
                raise MaterialNotFoundError(
                    f"template not found: {value.template_uuid}"
                )
            lot_uuid = value.lot_uuid or str(uuid4())
            current = self.repository.get_lot(lot_uuid)
            if current is None:
                updated = InventoryLotRecord(
                    lot_uuid=lot_uuid,
                    template_uuid=value.template_uuid,
                    batch_no=value.batch_no,
                    unit=value.unit,
                    quantity_total=value.quantity,
                    quantity_available=value.quantity,
                    quantity_reserved=0,
                    expiry_at_ms=value.expiry_at_ms,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
                self.repository.insert_lot(updated)
                previous_version = 0
            else:
                if (
                    current.template_uuid != value.template_uuid
                    or current.unit != value.unit
                    or current.batch_no != value.batch_no
                    or current.expiry_at_ms != value.expiry_at_ms
                ):
                    raise MaterialConflictError(
                        "existing lot identity differs from inbound request"
                    )
                if current.quarantined:
                    raise MaterialConflictError("cannot add stock to a quarantined lot")
                previous_version = current.version
                updated = current.model_copy(
                    update={
                        "quantity_total": current.quantity_total + value.quantity,
                        "quantity_available": current.quantity_available + value.quantity,
                        "updated_at_ms": timestamp,
                        "version": current.version + 1,
                    }
                )
                self.repository.update_lot(updated)
            state_hash = self._lot_state_hash(updated)
            sequence = self._ledger(
                mutation,
                aggregate_type="lot",
                aggregate_uuid=lot_uuid,
                operation="inventory.lot.inbound",
                previous_version=previous_version,
                aggregate_version=updated.version,
                state_hash=state_hash,
                delta={
                    "quantity": value.quantity,
                    "unit": value.unit,
                    "quantity_total": updated.quantity_total,
                    "quantity_available": updated.quantity_available,
                },
                timestamp=timestamp,
            )
            return _Applied(
                data=self._lot_read(updated),
                affected=[AggregateVersion(
                    aggregate_type="lot",
                    aggregate_uuid=lot_uuid,
                    version=updated.version,
                    state_hash=state_hash,
                )],
                sequences=[sequence],
            )

        return self._run_mutation(
            mutation,
            value,
            MutationResult[InventoryLotRead],
            apply,
        )

    def get_inventory_reservation(
        self, reservation_uuid: str
    ) -> InventoryReservationRead:
        record = self.repository.get_reservation(reservation_uuid)
        if record is None:
            raise MaterialNotFoundError(
                f"inventory reservation not found: {reservation_uuid}"
            )
        return self._reservation_read(record)

    def get_inventory_reservation_by_job(
        self, job_uuid: str
    ) -> InventoryReservationRead:
        record = self.repository.get_reservation_by_job(job_uuid)
        if record is None:
            raise MaterialNotFoundError(
                f"inventory reservation not found for job: {job_uuid}"
            )
        return self._reservation_read(record)

    def list_inventory_reservations(
        self,
        *,
        task_uuid: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[InventoryReservationRead]:
        return [
            self._reservation_read(item)
            for item in self.repository.list_reservations(
                task_uuid=task_uuid,
                status=status,
            )
        ]

    def reserve_inventory(
        self,
        mutation: InventoryMutation,
        value: InventoryReservationCreate,
    ) -> MutationResult[InventoryReservationRead]:
        """为一个调度 Job 原子预留全部物料实例和试剂数量。"""

        if mutation.operation != "reserve_inventory":
            raise MaterialValidationError("inventory reservation operation is invalid")
        if mutation.job_uuid not in {None, value.job_uuid}:
            raise MaterialValidationError("mutation job_uuid differs from reservation job")
        request_hash = canonical_hash(value.model_dump(mode="json", exclude_none=False))

        def apply(timestamp: int) -> _Applied[InventoryReservationRead]:
            if self.repository.get_reservation_by_job(value.job_uuid) is not None:
                raise MaterialConflictError(
                    f"job already has an inventory reservation: {value.job_uuid}"
                )
            reservation_uuid = value.reservation_uuid or str(uuid4())
            if self.repository.get_reservation(reservation_uuid) is not None:
                raise MaterialConflictError(
                    f"inventory reservation already exists: {reservation_uuid}"
                )

            allocations: list[InventoryAllocation] = []
            selected_materials: set[str] = set()
            material_updates: list[tuple[MaterialRecord, MaterialRecord]] = []
            lot_reserved: dict[str, float] = {}

            for requirement in value.requirements:
                if requirement.kind == "material":
                    material = self._select_inventory_material(
                        requirement,
                        selected_materials=selected_materials,
                    )
                    selected_materials.add(material.material_uuid)
                    updated = material.model_copy(
                        update={
                            "lifecycle_status": "reserved",
                            "updated_at_ms": timestamp,
                            "version": material.version + 1,
                        }
                    )
                    material_updates.append((material, updated))
                    allocations.append(
                        InventoryAllocation(
                            key=requirement.key,
                            kind="material",
                            material_uuid=material.material_uuid,
                            template_uuid=material.template_uuid,
                        )
                    )
                    continue

                remaining = float(requirement.quantity or 0)
                candidates = self._candidate_inventory_lots(requirement, timestamp)
                for lot in candidates:
                    already = lot_reserved.get(lot.lot_uuid, 0.0)
                    available = lot.quantity_available - already
                    take = min(available, remaining)
                    if take <= 1e-9:
                        continue
                    lot_reserved[lot.lot_uuid] = already + take
                    allocations.append(
                        InventoryAllocation(
                            key=requirement.key,
                            kind="reagent",
                            template_uuid=lot.template_uuid,
                            lot_uuid=lot.lot_uuid,
                            quantity=take,
                            unit=lot.unit,
                        )
                    )
                    remaining -= take
                    if remaining <= 1e-9:
                        break
                if remaining > 1e-9:
                    raise InsufficientInventoryError(
                        f"requirement {requirement.key!r} is short by {remaining:g} "
                        f"{requirement.unit}"
                    )

            affected: list[AggregateVersion] = []
            sequences: list[int] = []
            for current, updated in material_updates:
                self.repository.update_material(updated)
                state_hash = self.get_material(updated.material_uuid).state_hash
                sequences.append(self._ledger(
                    mutation,
                    aggregate_type="material",
                    aggregate_uuid=updated.material_uuid,
                    operation="inventory.material.reserved",
                    previous_version=current.version,
                    aggregate_version=updated.version,
                    state_hash=state_hash,
                    delta={"lifecycle_status": "reserved", "job_uuid": value.job_uuid},
                    timestamp=timestamp,
                ))
                affected.append(AggregateVersion(
                    aggregate_type="material",
                    aggregate_uuid=updated.material_uuid,
                    version=updated.version,
                    state_hash=state_hash,
                ))

            for lot_uuid, quantity in sorted(lot_reserved.items()):
                current = self.repository.get_lot(lot_uuid)
                if current is None:
                    raise MaterialNotFoundError(f"inventory lot not found: {lot_uuid}")
                updated = self._advance_lot(
                    current,
                    available_delta=-quantity,
                    reserved_delta=quantity,
                    timestamp=timestamp,
                )
                self.repository.update_lot(updated)
                state_hash = self._lot_state_hash(updated)
                sequences.append(self._ledger(
                    mutation,
                    aggregate_type="lot",
                    aggregate_uuid=lot_uuid,
                    operation="inventory.lot.reserved",
                    previous_version=current.version,
                    aggregate_version=updated.version,
                    state_hash=state_hash,
                    delta={
                        "quantity": quantity,
                        "unit": updated.unit,
                        "job_uuid": value.job_uuid,
                    },
                    timestamp=timestamp,
                ))
                affected.append(AggregateVersion(
                    aggregate_type="lot",
                    aggregate_uuid=lot_uuid,
                    version=updated.version,
                    state_hash=state_hash,
                ))

            reservation = InventoryReservationRecord(
                reservation_uuid=reservation_uuid,
                task_uuid=value.task_uuid,
                node_uuid=value.node_uuid,
                job_uuid=value.job_uuid,
                scheduler_revision=value.scheduler_revision,
                request_hash=request_hash,
                items=[item.model_dump(mode="json") for item in allocations],
                status="active",
                expires_at_ms=value.expires_at_ms,
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
            )
            self.repository.insert_reservation(reservation)
            state_hash = self._reservation_state_hash(reservation)
            sequences.append(self._ledger(
                mutation,
                aggregate_type="reservation",
                aggregate_uuid=reservation_uuid,
                operation="inventory.reservation.created",
                previous_version=0,
                aggregate_version=1,
                state_hash=state_hash,
                delta={
                    "task_uuid": value.task_uuid,
                    "node_uuid": value.node_uuid,
                    "job_uuid": value.job_uuid,
                    "items": reservation.items,
                },
                timestamp=timestamp,
            ))
            affected.append(AggregateVersion(
                aggregate_type="reservation",
                aggregate_uuid=reservation_uuid,
                version=1,
                state_hash=state_hash,
            ))
            return _Applied(
                data=self._reservation_read(reservation),
                affected=affected,
                sequences=sequences,
            )

        return self._run_mutation(
            mutation,
            value,
            MutationResult[InventoryReservationRead],
            apply,
        )

    def consume_inventory_reservation(
        self,
        mutation: InventoryMutation,
        value: InventoryReservationTransition,
    ) -> MutationResult[InventoryReservationRead]:
        return self._transition_inventory_reservation(
            mutation,
            value,
            target_status="consumed",
        )

    def reserve_task_inventory(
        self,
        mutation: InventoryMutation,
        value: InventoryTaskReservationCreate,
    ) -> MutationResult[InventoryTaskReservationRead]:
        """整张任务 all-or-nothing 预留，语义对齐 durable scheduler。"""

        if mutation.operation != "reserve_task_inventory":
            raise MaterialValidationError("task inventory reservation operation is invalid")
        if mutation.job_uuid is not None:
            raise MaterialValidationError("task reservation mutation cannot bind one job")

        def apply(_timestamp: int) -> _Applied[InventoryTaskReservationRead]:
            reservations: list[InventoryReservationRead] = []
            affected: list[AggregateVersion] = []
            sequences: list[int] = []
            for request in value.reservations:
                child_mutation = InventoryMutation(
                    command_uuid=mutation.command_uuid,
                    effect_key=f"{mutation.effect_key}:{request.job_uuid}",
                    operation="reserve_inventory",
                    actor_type=mutation.actor_type,
                    actor_uuid=mutation.actor_uuid,
                    job_uuid=request.job_uuid,
                    observed_at_ms=mutation.observed_at_ms,
                )
                child = self.reserve_inventory(child_mutation, request)
                reservations.append(child.data)
                affected.extend(child.affected)
                sequences.extend(
                    range(
                        child.ledger_sequence_start,
                        child.ledger_sequence_end + 1,
                    )
                )
            return _Applied(
                data=InventoryTaskReservationRead(
                    task_uuid=value.task_uuid,
                    scheduler_revision=value.scheduler_revision,
                    reservations=reservations,
                ),
                affected=affected,
                sequences=sequences,
            )

        return self._run_mutation(
            mutation,
            value,
            MutationResult[InventoryTaskReservationRead],
            apply,
        )

    def release_inventory_reservation(
        self,
        mutation: InventoryMutation,
        value: InventoryReservationTransition,
    ) -> MutationResult[InventoryReservationRead]:
        return self._transition_inventory_reservation(
            mutation,
            value,
            target_status="released",
        )

    def quarantine_inventory_reservation(
        self,
        mutation: InventoryMutation,
        value: InventoryReservationTransition,
    ) -> MutationResult[InventoryReservationRead]:
        return self._transition_inventory_reservation(
            mutation,
            value,
            target_status="quarantined",
        )

    def _select_inventory_material(
        self,
        requirement: InventoryRequirement,
        *,
        selected_materials: set[str],
    ) -> MaterialRecord:
        if requirement.material_uuid is not None:
            candidates = [self.repository.get_material(requirement.material_uuid)]
        elif requirement.site_uuid is not None:
            site = self.repository.get_site(requirement.site_uuid)
            if site is None:
                raise MaterialNotFoundError(
                    f"warehouse Site not found: {requirement.site_uuid}"
                )
            candidates = [
                self.repository.get_material(site.occupied_material_uuid)
                if site.occupied_material_uuid is not None
                else None
            ]
        else:
            candidates = sorted(
                self.repository.list_materials(),
                key=lambda item: (item.created_at_ms, item.material_uuid),
            )
        for material in candidates:
            if material is None or material.material_uuid in selected_materials:
                continue
            if material.lifecycle_status != "active":
                continue
            if (
                requirement.template_uuid is not None
                and material.template_uuid != requirement.template_uuid
            ):
                continue
            if (
                requirement.parent_material_uuid is not None
                and material.parent_material_uuid
                != requirement.parent_material_uuid
            ):
                continue
            return material
        identity = requirement.material_uuid or requirement.template_uuid
        raise InsufficientInventoryError(
            f"no active material instance satisfies requirement {requirement.key!r} "
            f"({identity})"
        )

    def _candidate_inventory_lots(
        self,
        requirement: InventoryRequirement,
        timestamp: int,
    ) -> list[InventoryLotRecord]:
        if requirement.lot_uuid is not None:
            lot = self.repository.get_lot(requirement.lot_uuid)
            if lot is None:
                raise MaterialNotFoundError(
                    f"inventory lot not found: {requirement.lot_uuid}"
                )
            candidates = [lot]
        else:
            candidates = self.repository.list_lots(
                template_uuid=requirement.template_uuid,
                unit=requirement.unit,
                available_only=True,
            )
        result: list[InventoryLotRecord] = []
        for lot in candidates:
            if lot.quarantined or lot.quantity_available <= 1e-9:
                continue
            if requirement.template_uuid is not None and (
                lot.template_uuid != requirement.template_uuid
            ):
                continue
            if lot.unit != requirement.unit:
                continue
            if lot.expiry_at_ms is not None and lot.expiry_at_ms <= timestamp:
                continue
            result.append(lot)
        return result

    @staticmethod
    def _advance_lot(
        current: InventoryLotRecord,
        *,
        total_delta: float = 0,
        available_delta: float = 0,
        reserved_delta: float = 0,
        timestamp: int,
    ) -> InventoryLotRecord:
        def normalized(value: float) -> float:
            return 0.0 if abs(value) <= 1e-9 else value

        total = normalized(current.quantity_total + total_delta)
        available = normalized(current.quantity_available + available_delta)
        reserved = normalized(current.quantity_reserved + reserved_delta)
        if min(total, available, reserved) < 0 or available + reserved > total + 1e-9:
            raise MaterialConflictError(
                f"inventory lot invariant failed for {current.lot_uuid}"
            )
        return current.model_copy(
            update={
                "quantity_total": total,
                "quantity_available": available,
                "quantity_reserved": reserved,
                "updated_at_ms": timestamp,
                "version": current.version + 1,
            }
        )

    def _transition_inventory_reservation(
        self,
        mutation: InventoryMutation,
        value: InventoryReservationTransition,
        *,
        target_status: str,
    ) -> MutationResult[InventoryReservationRead]:
        expected_operation = {
            "consumed": "consume_inventory_reservation",
            "released": "release_inventory_reservation",
            "quarantined": "quarantine_inventory_reservation",
        }[target_status]
        if mutation.operation != expected_operation:
            raise MaterialValidationError(
                "inventory reservation transition operation is invalid"
            )

        def apply(timestamp: int) -> _Applied[InventoryReservationRead]:
            current = self.repository.get_reservation(value.reservation_uuid)
            if current is None:
                raise MaterialNotFoundError(
                    f"inventory reservation not found: {value.reservation_uuid}"
                )
            if target_status in {"consumed", "released"} and current.status != "active":
                raise MaterialConflictError(
                    f"reservation {current.reservation_uuid} is {current.status}, "
                    f"cannot become {target_status}"
                )
            if target_status == "quarantined" and current.status not in {
                "active",
                "consumed",
            }:
                raise MaterialConflictError(
                    f"reservation {current.reservation_uuid} is {current.status}, "
                    "cannot be quarantined"
                )

            allocations = [
                InventoryAllocation.model_validate(item) for item in current.items
            ]
            lot_quantities: dict[str, float] = {}
            material_uuids: set[str] = set()
            for item in allocations:
                if item.kind == "reagent" and item.lot_uuid is not None:
                    lot_quantities[item.lot_uuid] = (
                        lot_quantities.get(item.lot_uuid, 0.0)
                        + float(item.quantity or 0)
                    )
                elif item.kind == "material" and item.material_uuid is not None:
                    material_uuids.add(item.material_uuid)

            affected: list[AggregateVersion] = []
            sequences: list[int] = []
            if current.status == "active":
                for lot_uuid, quantity in sorted(lot_quantities.items()):
                    lot = self.repository.get_lot(lot_uuid)
                    if lot is None:
                        raise MaterialNotFoundError(
                            f"inventory lot not found: {lot_uuid}"
                        )
                    if target_status == "released":
                        updated_lot = self._advance_lot(
                            lot,
                            available_delta=quantity,
                            reserved_delta=-quantity,
                            timestamp=timestamp,
                        )
                    else:
                        # 动作开始即实际扣减；失败隔离不能把可能已使用的数量加回。
                        updated_lot = self._advance_lot(
                            lot,
                            total_delta=-quantity,
                            reserved_delta=-quantity,
                            timestamp=timestamp,
                        )
                    self.repository.update_lot(updated_lot)
                    lot_hash = self._lot_state_hash(updated_lot)
                    sequences.append(self._ledger(
                        mutation,
                        aggregate_type="lot",
                        aggregate_uuid=lot_uuid,
                        operation=f"inventory.lot.{target_status}",
                        previous_version=lot.version,
                        aggregate_version=updated_lot.version,
                        state_hash=lot_hash,
                        delta={
                            "quantity": quantity,
                            "unit": lot.unit,
                            "reason": value.reason,
                        },
                        timestamp=timestamp,
                    ))
                    affected.append(AggregateVersion(
                        aggregate_type="lot",
                        aggregate_uuid=lot_uuid,
                        version=updated_lot.version,
                        state_hash=lot_hash,
                    ))

            for material_uuid in sorted(material_uuids):
                material = self.repository.get_material(material_uuid)
                if material is None:
                    raise MaterialNotFoundError(
                        f"material not found: {material_uuid}"
                    )
                desired = {
                    "consumed": "in_use",
                    "released": "active",
                    "quarantined": "quarantined",
                }[target_status]
                if material.lifecycle_status == desired:
                    continue
                allowed = (
                    {"reserved"}
                    if current.status == "active"
                    else {"in_use", "reserved"}
                )
                if material.lifecycle_status not in allowed:
                    raise MaterialConflictError(
                        f"material {material_uuid} is {material.lifecycle_status}, "
                        f"cannot become {desired}"
                    )
                updated_material = material.model_copy(
                    update={
                        "lifecycle_status": desired,
                        "updated_at_ms": timestamp,
                        "version": material.version + 1,
                    }
                )
                self.repository.update_material(updated_material)
                material_hash = self.get_material(material_uuid).state_hash
                sequences.append(self._ledger(
                    mutation,
                    aggregate_type="material",
                    aggregate_uuid=material_uuid,
                    operation=f"inventory.material.{desired}",
                    previous_version=material.version,
                    aggregate_version=updated_material.version,
                    state_hash=material_hash,
                    delta={
                        "lifecycle_status": desired,
                        "reason": value.reason,
                    },
                    timestamp=timestamp,
                ))
                affected.append(AggregateVersion(
                    aggregate_type="material",
                    aggregate_uuid=material_uuid,
                    version=updated_material.version,
                    state_hash=material_hash,
                ))

            updated = current.model_copy(
                update={
                    "status": target_status,
                    "updated_at_ms": timestamp,
                    "version": current.version + 1,
                }
            )
            self.repository.update_reservation(updated)
            reservation_hash = self._reservation_state_hash(updated)
            sequences.append(self._ledger(
                mutation,
                aggregate_type="reservation",
                aggregate_uuid=updated.reservation_uuid,
                operation=f"inventory.reservation.{target_status}",
                previous_version=current.version,
                aggregate_version=updated.version,
                state_hash=reservation_hash,
                delta={"status": target_status, "reason": value.reason},
                timestamp=timestamp,
            ))
            affected.append(AggregateVersion(
                aggregate_type="reservation",
                aggregate_uuid=updated.reservation_uuid,
                version=updated.version,
                state_hash=reservation_hash,
            ))
            return _Applied(
                data=self._reservation_read(updated),
                affected=affected,
                sequences=sequences,
            )

        return self._run_mutation(
            mutation,
            value,
            MutationResult[InventoryReservationRead],
            apply,
        )

    # -- Template CRUD ----------------------------------------------------

    @staticmethod
    def _template_definition_hash(value: ResourceTemplateWrite) -> str:
        return canonical_hash(
            {
                "name": value.name,
                "display_name": value.display_name or value.name,
                "resource_type": value.resource_type,
                "class_name": value.class_name,
                "module_name": value.module_name,
                "template_version": value.template_version,
                "category": value.category,
                "available_sites": value.available_sites,
                "handles": [item.model_dump(mode="json") for item in value.handles],
                "definition": value.definition,
                "status": value.status,
            }
        )

    @staticmethod
    def _template_state_hash(record: ResourceTemplateRecord) -> str:
        return canonical_hash(
            record.model_dump(
                mode="json",
                exclude={"created_at_ms", "updated_at_ms", "deleted_at_ms", "version"},
            )
        )

    @staticmethod
    def _template_read(record: ResourceTemplateRecord) -> ResourceTemplateRead:
        values = record.model_dump(mode="json")
        values["definition"] = values.pop("definition_json")
        return ResourceTemplateRead.model_validate(values)

    def put_template(
        self, mutation: InventoryMutation, value: ResourceTemplateWrite
    ) -> MutationResult[ResourceTemplateRead]:
        if mutation.operation not in {"create_template", "put_template", "sync_template"}:
            raise MaterialValidationError("template mutation operation is invalid")

        def apply(timestamp: int) -> _Applied[ResourceTemplateRead]:
            template_uuid = value.template_uuid or str(uuid4())
            current = self.repository.get_template(template_uuid, include_deleted=True)
            definition_hash = self._template_definition_hash(value)
            if current is not None and current.definition_hash == definition_hash:
                raise MaterialNoChangeError("template already has this definition")
            version = 1 if current is None else current.version + 1
            created_at = timestamp if current is None else current.created_at_ms
            record = ResourceTemplateRecord(
                template_uuid=template_uuid,
                name=value.name,
                display_name=value.display_name or value.name,
                resource_type=value.resource_type,
                class_name=value.class_name,
                module_name=value.module_name,
                template_version=value.template_version,
                category=value.category,
                available_sites=value.available_sites,
                handles=value.handles,
                definition_json=value.definition,
                definition_hash=definition_hash,
                status=value.status,
                created_at_ms=created_at,
                updated_at_ms=timestamp,
                version=version,
            )
            if current is None:
                self.repository.insert_template(record)
            else:
                self.repository.update_template(record)
            state_hash = self._template_state_hash(record)
            sequence = self._ledger(
                mutation,
                aggregate_type="resource_template",
                aggregate_uuid=record.template_uuid,
                operation="create" if current is None else "update",
                previous_version=version - 1,
                aggregate_version=version,
                state_hash=state_hash,
                delta={"definition_hash": definition_hash},
                timestamp=timestamp,
            )
            return _Applied(
                data=self._template_read(record),
                affected=[
                    AggregateVersion(
                        aggregate_type="resource_template",
                        aggregate_uuid=record.template_uuid,
                        version=version,
                        state_hash=state_hash,
                    )
                ],
                sequences=[sequence],
            )

        return self._run_mutation(
            mutation, value, MutationResult[ResourceTemplateRead], apply
        )

    def create_template(
        self, mutation: InventoryMutation, value: ResourceTemplateWrite
    ) -> MutationResult[ResourceTemplateRead]:
        if value.template_uuid is not None:
            raise MaterialValidationError(
                "create_template requires the authority to allocate template_uuid"
            )
        return self.put_template(mutation, value)

    def get_template(self, template_uuid: str) -> ResourceTemplateRead:
        record = self.repository.get_template(template_uuid)
        if record is None:
            raise MaterialNotFoundError(f"template not found: {template_uuid}")
        return self._template_read(record)

    def list_templates(self) -> list[ResourceTemplateRead]:
        return [self._template_read(item) for item in self.repository.list_templates()]

    def delete_template(
        self, mutation: InventoryMutation, template_uuid: str
    ) -> MutationResult[ResourceTemplateRead]:
        if mutation.operation != "delete_template":
            raise MaterialValidationError("template delete operation is invalid")
        body = {"template_uuid": template_uuid}

        def apply(timestamp: int) -> _Applied[ResourceTemplateRead]:
            current = self.repository.get_template(template_uuid)
            if current is None:
                raise MaterialNotFoundError(f"template not found: {template_uuid}")
            if self.repository.count_active_materials_for_template(template_uuid):
                raise MaterialConflictError("template still has active materials")
            updated = ResourceTemplateRecord.model_validate(
                {
                    **current.model_dump(mode="json"),
                    "status": "deleted",
                    "deleted_at_ms": timestamp,
                    "updated_at_ms": timestamp,
                    "version": current.version + 1,
                }
            )
            self.repository.update_template(updated)
            state_hash = self._template_state_hash(updated)
            sequence = self._ledger(
                mutation,
                aggregate_type="resource_template",
                aggregate_uuid=template_uuid,
                operation="delete",
                previous_version=current.version,
                aggregate_version=updated.version,
                state_hash=state_hash,
                delta={"status": "deleted"},
                timestamp=timestamp,
            )
            return _Applied(
                self._template_read(updated),
                [
                    AggregateVersion(
                        aggregate_type="resource_template",
                        aggregate_uuid=template_uuid,
                        version=updated.version,
                        state_hash=state_hash,
                    )
                ],
                [sequence],
            )

        return self._run_mutation(
            mutation, body, MutationResult[ResourceTemplateRead], apply
        )

    # -- Read material aggregates ----------------------------------------

    @staticmethod
    def _identity_read(record: MaterialRecord) -> MaterialIdentityRead:
        values = record.model_dump(mode="json")
        for source, target in (
            ("resource_schema_json", "resource_schema"),
            ("model_json", "model"),
            ("config_json", "config"),
            ("extra_json", "extra"),
            ("meta_data_json", "meta_data"),
        ):
            values[target] = values.pop(source)
        return MaterialIdentityRead.model_validate(values)

    @staticmethod
    def _position_read(record: MaterialPositionRecord) -> MaterialPosition:
        values = record.model_dump(
            mode="json", exclude={"material_uuid", "updated_at_ms", "version"}
        )
        values["extra"] = values.pop("extra_json")
        return MaterialPosition.model_validate(values)

    @staticmethod
    def _substance_read(record: MaterialSubstanceRecord) -> MaterialSubstance:
        values = record.model_dump(
            mode="json",
            exclude={
                "material_uuid",
                "ordinal",
                "content_version",
                "observed_at_ms",
                "updated_at_ms",
                "version",
            },
        )
        values["meta_data"] = values.pop("meta_data_json")
        return MaterialSubstance.model_validate(values)

    @classmethod
    def _data_read(cls, record: MaterialDataRecord) -> MaterialDataRead:
        values = record.model_dump(mode="json", exclude={"material_uuid", "substances"})
        values["data"] = values.pop("data_json")
        values["substances"] = [cls._substance_read(item) for item in record.substances]
        values["state_hash"] = record.state_hash or cls._data_state_hash(values)
        return MaterialDataRead.model_validate(values)

    @staticmethod
    def _site_read(record: SiteRecord) -> SiteRead:
        values = record.model_dump(mode="json")
        values["meta_data"] = values.pop("meta_data_json")
        values["extra"] = values.pop("extra_json")
        return SiteRead.model_validate(values)

    @staticmethod
    def _data_state_hash(value: Any) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", exclude_none=False)
        semantic = dict(value)
        for key in (
            "content_version",
            "state_hash",
            "updated_at_ms",
            "version",
            "source_event_uuid",
            "source_job_uuid",
            "source_command_uuid",
            "observed_at_ms",
        ):
            semantic.pop(key, None)
        return canonical_hash(semantic)

    @staticmethod
    def _site_state_hash(record: SiteRecord) -> str:
        return canonical_hash(
            record.model_dump(
                mode="json",
                exclude={
                    "changed_by_job_uuid",
                    "changed_by_command_uuid",
                    "changed_at_ms",
                    "created_at_ms",
                    "updated_at_ms",
                    "deleted_at_ms",
                    "version",
                },
            )
        )

    @staticmethod
    def _material_state_hash(
        material: MaterialIdentityRead,
        position: MaterialPosition,
        data: MaterialDataRead,
    ) -> str:
        material_values = material.model_dump(
            mode="json",
            exclude={"created_at_ms", "updated_at_ms", "deleted_at_ms", "version"},
        )
        data_values = data.model_dump(
            mode="json",
            exclude={
                "content_version",
                "state_hash",
                "updated_at_ms",
                "version",
                "source_event_uuid",
                "source_job_uuid",
                "source_command_uuid",
                "observed_at_ms",
            },
        )
        return canonical_hash(
            {
                "material": material_values,
                "position": position.model_dump(mode="json"),
                "data": data_values,
            }
        )

    def get_material(self, material_uuid: str) -> MaterialAggregateRead:
        material = self.repository.get_material(material_uuid)
        if material is None:
            raise MaterialNotFoundError(f"material not found: {material_uuid}")
        position_record = self.repository.get_position(material_uuid)
        data_record = self.repository.get_data(material_uuid)
        if position_record is None or data_record is None:
            raise MaterialValidationError(
                f"material aggregate is incomplete: {material_uuid}"
            )
        identity = self._identity_read(material)
        position = self._position_read(position_record)
        data = self._data_read(data_record)
        return MaterialAggregateRead(
            material=identity,
            position=position,
            position_version=position_record.version,
            data=data,
            sites=[
                self._site_read(item) for item in self.repository.list_sites(material_uuid)
            ],
            state_hash=self._material_state_hash(identity, position, data),
        )

    def get_material_by_resource_id(self, resource_id: str) -> MaterialAggregateRead:
        record = self.repository.get_material_by_resource_id(resource_id)
        if record is None:
            raise MaterialNotFoundError(f"material not found: {resource_id}")
        return self.get_material(record.material_uuid)

    def get_tree(
        self, root_material_uuid: str, *, client_ref_map: Optional[dict[str, str]] = None
    ) -> MaterialTreeRead:
        records = self.repository.tree_materials(root_material_uuid)
        if not records:
            raise MaterialNotFoundError(f"material root not found: {root_material_uuid}")
        nodes = [self.get_material(item.material_uuid) for item in records]
        tree_hash = canonical_hash(
            [
                {
                    "material_uuid": item.material.material_uuid,
                    "sections": material_sections(item),
                    "sites": [
                        site_semantic(site)
                        for site in sorted(item.sites, key=lambda value: value.site_uuid)
                    ],
                }
                for item in sorted(
                    nodes, key=lambda value: value.material.material_uuid
                )
            ]
        )
        return MaterialTreeRead(
            root_material_uuid=root_material_uuid,
            snapshot_sequence=self.repository.latest_ledger_sequence(),
            nodes=nodes,
            client_ref_map=client_ref_map or {},
            state_hash=tree_hash,
        )

    def list_materials(self, *, roots_only: bool = False) -> list[MaterialAggregateRead]:
        return [
            self.get_material(item.material_uuid)
            for item in self.repository.list_materials(roots_only=roots_only)
        ]

    # -- Create -----------------------------------------------------------

    @staticmethod
    def _validate_template_identity(
        template: ResourceTemplateRecord, identity: Any
    ) -> None:
        if template.status != "active":
            raise MaterialValidationError(
                f"template is not active: {template.template_uuid}"
            )
        if identity.template_name.casefold() != template.name.casefold():
            raise MaterialValidationError(
                f"template_name {identity.template_name!r} does not match "
                f"registered template {template.name!r}"
            )
        if identity.resource_type != template.resource_type:
            raise MaterialValidationError(
                f"resource_type {identity.resource_type!r} does not match "
                f"template {template.name!r} resource_type "
                f"{template.resource_type!r}"
            )
        if template.class_name and identity.class_name != template.class_name:
            raise MaterialValidationError(
                f"class_name {identity.class_name!r} does not match template "
                f"{template.name!r} class_name {template.class_name!r}"
            )

    @staticmethod
    def _site_from_template(
        value: dict[str, Any], *, template_name: str, ordinal: int
    ) -> SiteCreate:
        payload = dict(value)
        for field in (
            "uuid",
            "site_uuid",
            "owner_material_uuid",
            "material_uuid",
            "occupied_by",
            "occupied_material_uuid",
        ):
            payload.pop(field, None)
        index = payload.pop("site_index", payload.pop("index", ordinal))
        label = payload.pop("label", payload.pop("name", str(index)))
        categories = payload.pop(
            "allowed_resource_categories", payload.pop("content_type", [])
        )
        known = {
            "schema_version",
            "visible",
            "pose",
            "parent_link",
            "description",
            "meta_data",
            "extra",
        }
        extras = {key: payload.pop(key) for key in list(payload) if key not in known}
        extra = dict(payload.pop("extra", {}) or {})
        extra.update(extras)
        return SiteCreate(
            schema_version=int(payload.pop("schema_version", 1)),
            template_name=template_name,
            site_index=index,
            label=str(label),
            visible=bool(payload.pop("visible", True)),
            pose=dict(payload.pop("pose", {}) or {}),
            allowed_resource_categories=list(categories or []),
            parent_link=str(payload.pop("parent_link", "") or ""),
            description=str(payload.pop("description", "") or ""),
            meta_data=dict(payload.pop("meta_data", {}) or {}),
            extra=extra,
        )

    def create_tree(
        self, mutation: InventoryMutation, value: MaterialTreeCreate
    ) -> MutationResult[MaterialTreeRead]:
        if mutation.operation not in {"create_material", "create_material_tree"}:
            raise MaterialValidationError("material create operation is invalid")

        def apply(timestamp: int) -> _Applied[MaterialTreeRead]:
            client_map = {node.client_ref: str(uuid4()) for node in value.nodes}
            node_ordinals: dict[str, int] = {}
            used_ordinals: dict[str | None, set[int]] = {}
            next_ordinals: dict[str | None, int] = {}
            for node in value.nodes:
                parent_ref = node.parent_client_ref
                ordinal = (
                    node.ordinal
                    if node.ordinal is not None
                    else next_ordinals.get(parent_ref, 0)
                )
                used = used_ordinals.setdefault(parent_ref, set())
                if ordinal in used:
                    raise MaterialValidationError(
                        f"duplicate child ordinal {ordinal} under {parent_ref!r}"
                    )
                used.add(ordinal)
                next_ordinals[parent_ref] = max(
                    next_ordinals.get(parent_ref, 0), ordinal + 1
                )
                node_ordinals[node.client_ref] = ordinal
            templates: dict[str, ResourceTemplateRecord] = {}
            resolved_templates: dict[str, ResourceTemplateRecord] = {}
            affected: list[AggregateVersion] = []
            sequences: list[int] = []
            node_sites: dict[str, list[SiteCreate]] = {}
            for node in value.nodes:
                if node.identity.parent_material_uuid is not None:
                    raise MaterialValidationError(
                        "create tree parent must use parent_client_ref"
                    )
                template_key = node.identity.template_name.casefold()
                template = templates.get(template_key)
                if template is None:
                    template = self.repository.get_template_by_name(
                        node.identity.template_name
                    )
                    if template is None:
                        template_value = ResourceTemplateWrite(
                            name=node.identity.template_name,
                            display_name=node.identity.template_name,
                            resource_type=node.identity.resource_type,
                            class_name=node.identity.class_name,
                            available_sites=[
                                site.model_dump(
                                    mode="json",
                                    exclude={"occupied_client_ref"},
                                )
                                for site in node.sites
                            ],
                            definition={
                                "source": "material_create",
                                "resource_schema": node.identity.resource_schema,
                                "model": node.identity.model,
                                "config": node.identity.config,
                            },
                        )
                        template = ResourceTemplateRecord(
                            template_uuid=str(uuid4()),
                            name=template_value.name,
                            display_name=(
                                template_value.display_name or template_value.name
                            ),
                            resource_type=template_value.resource_type,
                            class_name=template_value.class_name,
                            module_name=template_value.module_name,
                            template_version=template_value.template_version,
                            category=template_value.category,
                            available_sites=template_value.available_sites,
                            handles=template_value.handles,
                            definition_json=template_value.definition,
                            definition_hash=self._template_definition_hash(
                                template_value
                            ),
                            status="active",
                            created_at_ms=timestamp,
                            updated_at_ms=timestamp,
                        )
                        self.repository.insert_template(template)
                        template_state_hash = self._template_state_hash(template)
                        affected.append(
                            AggregateVersion(
                                aggregate_type="resource_template",
                                aggregate_uuid=template.template_uuid,
                                version=1,
                                state_hash=template_state_hash,
                            )
                        )
                        sequences.append(
                            self._ledger(
                                mutation,
                                aggregate_type="resource_template",
                                aggregate_uuid=template.template_uuid,
                                operation="create",
                                previous_version=0,
                                aggregate_version=1,
                                state_hash=template_state_hash,
                                delta={
                                    "definition_hash": template.definition_hash,
                                    "source": "material_create",
                                },
                                timestamp=timestamp,
                            )
                        )
                    templates[template_key] = template
                resolved_templates[node.client_ref] = template
                self._validate_template_identity(template, node.identity)
                explicit_sites = list(node.sites)
                if not explicit_sites:
                    explicit_sites = [
                        self._site_from_template(
                            dict(site), template_name=template.name, ordinal=ordinal
                        )
                        for ordinal, site in enumerate(template.available_sites)
                    ]
                node_sites[node.client_ref] = explicit_sites

            for node in value.nodes:
                identity = node.identity
                template = resolved_templates[node.client_ref]
                material_uuid = client_map[node.client_ref]
                parent_uuid = (
                    client_map[node.parent_client_ref]
                    if node.parent_client_ref is not None
                    else None
                )
                record = MaterialRecord(
                    material_uuid=material_uuid,
                    resource_id=identity.resource_id,
                    template_uuid=template.template_uuid,
                    parent_material_uuid=parent_uuid,
                    ordinal=node_ordinals[node.client_ref],
                    lot_uuid=identity.lot_uuid,
                    name=identity.name,
                    description=identity.description,
                    resource_type=identity.resource_type,
                    class_name=identity.class_name,
                    machine_name=identity.machine_name,
                    barcode=identity.barcode,
                    barcode_symbology=identity.barcode_symbology,
                    template_name=identity.template_name,
                    resource_schema_json=identity.resource_schema,
                    model_json=identity.model,
                    icon_uri=identity.icon_uri,
                    config_json=identity.config,
                    extra_json=identity.extra,
                    meta_data_json=identity.meta_data,
                    lifecycle_status=identity.lifecycle_status,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
                self.repository.insert_material(record)
                position_values = node.position.model_dump(mode="json")
                position_values["extra_json"] = position_values.pop("extra")
                self.repository.replace_position(
                    MaterialPositionRecord(
                        material_uuid=material_uuid,
                        updated_at_ms=timestamp,
                        **position_values,
                    )
                )

                substances = self._new_substance_records(
                    material_uuid,
                    node.data,
                    content_version=1,
                    timestamp=timestamp,
                )
                data_values = node.data.model_dump(
                    mode="json", exclude={"data", "substances"}
                )
                data_values["data_json"] = node.data.data
                # create_tree 已由 authority 完整解析模板；即使模板没有 Site，
                # 空数组也仍是权威快照，不能退回“尚未初始化”的第三态。
                data_values["sites_initialized"] = True
                state_hash = self._data_state_hash(
                    {
                        **node.data.model_dump(mode="json"),
                        "substances": [
                            self._substance_read(item).model_dump(mode="json")
                            for item in substances
                        ],
                    }
                )
                data_record = MaterialDataRecord(
                    material_uuid=material_uuid,
                    substances=substances,
                    content_version=1,
                    state_hash=state_hash,
                    updated_at_ms=timestamp,
                    **data_values,
                )
                self.repository.replace_data(data_record)
                self.repository.replace_substances(material_uuid, substances)

            site_records: list[SiteRecord] = []
            for node in value.nodes:
                owner_uuid = client_map[node.client_ref]
                for ordinal, site in enumerate(node_sites[node.client_ref]):
                    occupant = site.occupied_client_ref
                    if occupant is not None:
                        occupant = client_map[occupant]
                    record = SiteRecord(
                        site_uuid=str(uuid4()),
                        schema_version=site.schema_version,
                        owner_material_uuid=owner_uuid,
                        ordinal=ordinal,
                        template_name=site.template_name,
                        site_index=site.site_index,
                        label=site.label,
                        visible=site.visible,
                        occupied_material_uuid=occupant,
                        pose=site.pose,
                        allowed_resource_categories=site.allowed_resource_categories,
                        parent_link=site.parent_link,
                        description=site.description,
                        meta_data_json=site.meta_data,
                        extra_json=site.extra,
                        changed_by_job_uuid=mutation.job_uuid,
                        changed_by_command_uuid=mutation.command_uuid,
                        changed_at_ms=timestamp,
                        created_at_ms=timestamp,
                        updated_at_ms=timestamp,
                    )
                    self.repository.insert_site(record)
                    site_records.append(record)

            for node in value.nodes:
                material_uuid = client_map[node.client_ref]
                aggregate = self.get_material(material_uuid)
                affected.append(
                    AggregateVersion(
                        aggregate_type="material",
                        aggregate_uuid=material_uuid,
                        version=1,
                        state_hash=aggregate.state_hash,
                    )
                )
                sequences.append(
                    self._ledger(
                        mutation,
                        aggregate_type="material",
                        aggregate_uuid=material_uuid,
                        operation="create",
                        previous_version=0,
                        aggregate_version=1,
                        state_hash=aggregate.state_hash,
                        delta={
                            "resource_id": aggregate.material.resource_id,
                            "parent_material_uuid": aggregate.material.parent_material_uuid,
                        },
                        timestamp=timestamp,
                    )
                )
            for site in site_records:
                state_hash = self._site_state_hash(site)
                affected.append(
                    AggregateVersion(
                        aggregate_type="site",
                        aggregate_uuid=site.site_uuid,
                        version=1,
                        state_hash=state_hash,
                    )
                )
                sequences.append(
                    self._ledger(
                        mutation,
                        aggregate_type="site",
                        aggregate_uuid=site.site_uuid,
                        operation="create",
                        previous_version=0,
                        aggregate_version=1,
                        state_hash=state_hash,
                        delta={"owner_material_uuid": site.owner_material_uuid},
                        timestamp=timestamp,
                    )
                )
            root_ref = next(
                node.client_ref
                for node in value.nodes
                if node.parent_client_ref is None
            )
            tree = self.get_tree(client_map[root_ref], client_ref_map=client_map)
            return _Applied(data=tree, affected=affected, sequences=sequences)

        return self._run_mutation(
            mutation, value, MutationResult[MaterialTreeRead], apply
        )

    def _new_substance_records(
        self,
        material_uuid: str,
        value: MaterialDataWrite,
        *,
        content_version: int,
        timestamp: int,
        previous: Optional[list[MaterialSubstanceRecord]] = None,
    ) -> list[MaterialSubstanceRecord]:
        records: list[MaterialSubstanceRecord] = []
        seen: set[str] = set()
        previous_by_uuid = {
            item.substance_uuid: item for item in (previous or [])
        }
        for ordinal, substance in enumerate(value.substances):
            prior_at_ordinal = (
                previous[ordinal]
                if previous is not None and ordinal < len(previous)
                else None
            )
            substance_uuid = substance.substance_uuid
            if (
                substance_uuid is None
                and prior_at_ordinal is not None
                and prior_at_ordinal.name == substance.name
                and prior_at_ordinal.quantity_unit == substance.quantity_unit
            ):
                substance_uuid = prior_at_ordinal.substance_uuid
            substance_uuid = substance_uuid or str(uuid4())
            if substance_uuid in seen:
                raise MaterialValidationError("duplicate substance_uuid")
            seen.add(substance_uuid)
            records.append(
                MaterialSubstanceRecord(
                    substance_uuid=substance_uuid,
                    material_uuid=material_uuid,
                    ordinal=ordinal,
                    name=substance.name,
                    quantity=substance.quantity,
                    quantity_unit=substance.quantity_unit,
                    physical_state=substance.physical_state,
                    composition=substance.composition,
                    meta_data_json=substance.meta_data,
                    content_version=content_version,
                    observed_at_ms=max(timestamp, value.observed_at_ms),
                    updated_at_ms=max(timestamp, value.observed_at_ms),
                    version=(
                        previous_by_uuid[substance_uuid].version + 1
                        if substance_uuid in previous_by_uuid
                        else 1
                    ),
                )
            )
        return records

    # -- Material aggregate updates --------------------------------------

    def patch_material(
        self,
        mutation: InventoryMutation,
        material_uuid: str,
        value: MaterialPatch,
    ) -> MutationResult[MaterialAggregateRead]:
        if mutation.operation not in {"patch_material", "update_material"}:
            raise MaterialValidationError("material patch operation is invalid")

        def apply(timestamp: int) -> _Applied[MaterialAggregateRead]:
            current = self.repository.get_material(material_uuid)
            if current is None:
                raise MaterialNotFoundError(f"material not found: {material_uuid}")
            changes = value.model_dump(exclude_none=True)
            field_map = {
                "config": "config_json",
                "extra": "extra_json",
                "meta_data": "meta_data_json",
            }
            changes = {field_map.get(key, key): item for key, item in changes.items()}
            semantic = current.model_dump(mode="json")
            if all(semantic.get(key) == item for key, item in changes.items()):
                raise MaterialNoChangeError("material already has these values")
            updated = MaterialRecord.model_validate(
                {
                    **semantic,
                    **changes,
                    "updated_at_ms": timestamp,
                    "version": current.version + 1,
                }
            )
            self.repository.update_material(updated)
            aggregate = self.get_material(material_uuid)
            sequence = self._ledger(
                mutation,
                aggregate_type="material",
                aggregate_uuid=material_uuid,
                operation="patch",
                previous_version=current.version,
                aggregate_version=updated.version,
                state_hash=aggregate.state_hash,
                delta={"fields": sorted(changes)},
                timestamp=timestamp,
            )
            affected = AggregateVersion(
                aggregate_type="material",
                aggregate_uuid=material_uuid,
                version=updated.version,
                state_hash=aggregate.state_hash,
            )
            return _Applied(aggregate, [affected], [sequence])

        return self._run_mutation(
            mutation, value, MutationResult[MaterialAggregateRead], apply
        )

    def put_position(
        self,
        mutation: InventoryMutation,
        material_uuid: str,
        value: MaterialPosition,
    ) -> MutationResult[MaterialAggregateRead]:
        if mutation.operation not in {"put_position", "update_position"}:
            raise MaterialValidationError("position operation is invalid")

        def apply(timestamp: int) -> _Applied[MaterialAggregateRead]:
            material = self.repository.get_material(material_uuid)
            current = self.repository.get_position(material_uuid)
            if material is None or current is None:
                raise MaterialNotFoundError(f"material not found: {material_uuid}")
            if self._position_read(current) == value:
                raise MaterialNoChangeError("position is unchanged")
            values = value.model_dump(mode="json")
            values["extra_json"] = values.pop("extra")
            self.repository.replace_position(
                MaterialPositionRecord(
                    material_uuid=material_uuid,
                    updated_at_ms=timestamp,
                    version=current.version + 1,
                    **values,
                )
            )
            updated_material = MaterialRecord.model_validate(
                {
                    **material.model_dump(mode="json"),
                    "updated_at_ms": timestamp,
                    "version": material.version + 1,
                }
            )
            self.repository.update_material(updated_material)
            aggregate = self.get_material(material_uuid)
            sequence = self._ledger(
                mutation,
                aggregate_type="material",
                aggregate_uuid=material_uuid,
                operation="update_position",
                previous_version=material.version,
                aggregate_version=updated_material.version,
                state_hash=aggregate.state_hash,
                delta={"position_version": current.version + 1},
                timestamp=timestamp,
            )
            return _Applied(
                aggregate,
                [
                    AggregateVersion(
                        aggregate_type="material",
                        aggregate_uuid=material_uuid,
                        version=updated_material.version,
                        state_hash=aggregate.state_hash,
                    )
                ],
                [sequence],
            )

        return self._run_mutation(
            mutation, value, MutationResult[MaterialAggregateRead], apply
        )

    def put_data(
        self,
        mutation: InventoryMutation,
        material_uuid: str,
        value: MaterialDataWrite,
    ) -> MutationResult[MaterialAggregateRead]:
        if mutation.operation not in {"put_data", "update_substances", "update_data"}:
            raise MaterialValidationError("material data operation is invalid")

        def apply(timestamp: int) -> _Applied[MaterialAggregateRead]:
            material = self.repository.get_material(material_uuid)
            current = self.repository.get_data(material_uuid)
            if material is None or current is None:
                raise MaterialNotFoundError(f"material not found: {material_uuid}")
            new_content_version = current.content_version + 1
            substances = self._new_substance_records(
                material_uuid,
                value,
                content_version=new_content_version,
                timestamp=timestamp,
                previous=current.substances,
            )
            public_substances = [self._substance_read(item) for item in substances]
            state_hash = self._data_state_hash(
                {
                    **value.model_dump(mode="json"),
                    "substances": [
                        item.model_dump(mode="json") for item in public_substances
                    ],
                }
            )
            if current.state_hash == state_hash:
                raise MaterialNoChangeError("material data is unchanged")
            data_values = value.model_dump(
                mode="json", exclude={"data", "substances"}
            )
            data_values["data_json"] = value.data
            record = MaterialDataRecord(
                material_uuid=material_uuid,
                substances=substances,
                content_version=new_content_version,
                state_hash=state_hash,
                updated_at_ms=timestamp,
                version=current.version + 1,
                **data_values,
            )
            self.repository.replace_data(record)
            self.repository.replace_substances(material_uuid, substances)
            updated_material = MaterialRecord.model_validate(
                {
                    **material.model_dump(mode="json"),
                    "updated_at_ms": timestamp,
                    "version": material.version + 1,
                }
            )
            self.repository.update_material(updated_material)
            aggregate = self.get_material(material_uuid)
            sequence = self._ledger(
                mutation,
                aggregate_type="material",
                aggregate_uuid=material_uuid,
                operation="update_data",
                previous_version=material.version,
                aggregate_version=updated_material.version,
                state_hash=aggregate.state_hash,
                delta={
                    "content_version": new_content_version,
                    "data_state_hash": state_hash,
                },
                timestamp=timestamp,
            )
            return _Applied(
                aggregate,
                [
                    AggregateVersion(
                        aggregate_type="material",
                        aggregate_uuid=material_uuid,
                        version=updated_material.version,
                        state_hash=aggregate.state_hash,
                    )
                ],
                [sequence],
            )

        return self._run_mutation(
            mutation, value, MutationResult[MaterialAggregateRead], apply
        )

    def _apply_material_move(
        self,
        mutation: InventoryMutation,
        value: MaterialMove,
        timestamp: int,
    ) -> _Applied[MaterialAggregateRead]:
        material = self.repository.get_material(value.material_uuid)
        if material is None:
            raise MaterialNotFoundError(
                f"material not found: {value.material_uuid}"
            )
        source = self.repository.occupied_site(value.material_uuid)
        destination = (
            self.repository.get_site(value.destination_site_uuid)
            if value.destination_site_uuid is not None
            else None
        )
        if value.destination_site_uuid is not None and destination is None:
            raise MaterialNotFoundError(
                f"site not found: {value.destination_site_uuid}"
            )
        parent_uuid = (
            destination.owner_material_uuid
            if destination is not None
            else value.parent_material_uuid
        )
        if destination is not None and destination.occupied_material_uuid not in (
            None,
            value.material_uuid,
        ):
            raise MaterialConflictError("destination site is occupied")
        if (
            source is not None
            and destination is not None
            and source.site_uuid == destination.site_uuid
            and material.parent_material_uuid == parent_uuid
        ):
            raise MaterialNoChangeError("material is already at destination")
        if (
            source is None
            and destination is None
            and material.parent_material_uuid == parent_uuid
        ):
            raise MaterialNoChangeError("material is already at destination")

        affected: list[AggregateVersion] = []
        sequences: list[int] = []
        if source is not None and (
            destination is None or source.site_uuid != destination.site_uuid
        ):
            updated_source = SiteRecord.model_validate(
                {
                    **source.model_dump(mode="json"),
                    "occupied_material_uuid": None,
                    "changed_by_job_uuid": mutation.job_uuid,
                    "changed_by_command_uuid": mutation.command_uuid,
                    "changed_at_ms": timestamp,
                    "updated_at_ms": timestamp,
                    "version": source.version + 1,
                }
            )
            self.repository.update_site(updated_source)
            state_hash = self._site_state_hash(updated_source)
            affected.append(
                AggregateVersion(
                    aggregate_type="site",
                    aggregate_uuid=source.site_uuid,
                    version=updated_source.version,
                    state_hash=state_hash,
                )
            )
            sequences.append(
                self._ledger(
                    mutation,
                    aggregate_type="site",
                    aggregate_uuid=source.site_uuid,
                    operation="vacate",
                    previous_version=source.version,
                    aggregate_version=updated_source.version,
                    state_hash=state_hash,
                    delta={"occupied_material_uuid": None},
                    timestamp=timestamp,
                )
            )

        updated_material = MaterialRecord.model_validate(
            {
                **material.model_dump(mode="json"),
                "parent_material_uuid": parent_uuid,
                "updated_at_ms": timestamp,
                "version": material.version + 1,
            }
        )
        self.repository.update_material(updated_material)

        if destination is not None and (
            source is None or source.site_uuid != destination.site_uuid
        ):
            updated_destination = SiteRecord.model_validate(
                {
                    **destination.model_dump(mode="json"),
                    "occupied_material_uuid": value.material_uuid,
                    "changed_by_job_uuid": mutation.job_uuid,
                    "changed_by_command_uuid": mutation.command_uuid,
                    "changed_at_ms": timestamp,
                    "updated_at_ms": timestamp,
                    "version": destination.version + 1,
                }
            )
            self.repository.update_site(updated_destination)
            state_hash = self._site_state_hash(updated_destination)
            affected.append(
                AggregateVersion(
                    aggregate_type="site",
                    aggregate_uuid=destination.site_uuid,
                    version=updated_destination.version,
                    state_hash=state_hash,
                )
            )
            sequences.append(
                self._ledger(
                    mutation,
                    aggregate_type="site",
                    aggregate_uuid=destination.site_uuid,
                    operation="occupy",
                    previous_version=destination.version,
                    aggregate_version=updated_destination.version,
                    state_hash=state_hash,
                    delta={"occupied_material_uuid": value.material_uuid},
                    timestamp=timestamp,
                )
            )

        aggregate = self.get_material(value.material_uuid)
        affected.append(
            AggregateVersion(
                aggregate_type="material",
                aggregate_uuid=value.material_uuid,
                version=updated_material.version,
                state_hash=aggregate.state_hash,
            )
        )
        sequences.append(
            self._ledger(
                mutation,
                aggregate_type="material",
                aggregate_uuid=value.material_uuid,
                operation="move",
                previous_version=material.version,
                aggregate_version=updated_material.version,
                state_hash=aggregate.state_hash,
                delta={
                    "parent_material_uuid": parent_uuid,
                    "destination_site_uuid": value.destination_site_uuid,
                },
                timestamp=timestamp,
            )
        )
        return _Applied(aggregate, affected, sequences)

    def move_material(
        self, mutation: InventoryMutation, value: MaterialMove
    ) -> MutationResult[MaterialAggregateRead]:
        if mutation.operation != "move_material":
            raise MaterialValidationError("material move operation is invalid")

        def apply(timestamp: int) -> _Applied[MaterialAggregateRead]:
            return self._apply_material_move(mutation, value, timestamp)

        with self._transfer_lock:
            return self._run_mutation(
                mutation, value, MutationResult[MaterialAggregateRead], apply
            )

    @staticmethod
    def _transfer_destination_site(
        target: MaterialAggregateRead,
        selector: Any | None,
    ) -> SiteRead | None:
        if selector is None or str(selector).strip() == "":
            return None
        normalized = str(selector).strip()
        matches = [
            site
            for site in target.sites
            if normalized
            in {
                site.site_uuid,
                site.label,
                str(site.site_index),
            }
        ]
        if not matches:
            raise MaterialValidationError(
                f"target material {target.material.material_uuid} "
                f"has no Site {selector!r}"
            )
        if len(matches) > 1:
            raise MaterialValidationError(
                f"target material {target.material.material_uuid} "
                f"has ambiguous Site {selector!r}"
            )
        return matches[0]

    def _transfer_result_is_current(self, value: MaterialTransferResult) -> None:
        """拒绝重放已被后续位置变更覆盖的设备同步命令。"""

        for material_uuid, target_uuid, site_uuid in zip(
            value.material_uuids,
            value.target_material_uuids,
            value.destination_site_uuids,
        ):
            material = self.repository.get_material(material_uuid)
            if material is None or material.parent_material_uuid != target_uuid:
                raise MaterialConflictError(
                    f"material transfer {material_uuid} was superseded"
                )
            occupied_site = self.repository.occupied_site(material_uuid)
            occupied_site_uuid = (
                occupied_site.site_uuid if occupied_site is not None else None
            )
            if occupied_site_uuid != site_uuid:
                raise MaterialConflictError(
                    f"material transfer {material_uuid} Site was superseded"
                )

    @staticmethod
    def _dispatch_device_sync(
        dispatcher: ResourceSyncDispatcher,
        command: MaterialDeviceSync,
    ) -> None:
        try:
            response = dispatcher(command)
        except MaterialTransferSyncError:
            raise
        except Exception as exc:
            raise MaterialTransferSyncError(
                f"device {command.device_id!r} {command.action} failed: {exc}"
            ) from exc
        if response is False:
            raise MaterialTransferSyncError(
                f"device {command.device_id!r} rejected {command.action}"
            )
        if isinstance(response, dict) and response.get("success") is False:
            raise MaterialTransferSyncError(
                f"device {command.device_id!r} {command.action} failed: {response}"
            )

    def transfer_material(
        self,
        mutation: InventoryMutation,
        value: MaterialTransfer,
    ) -> MutationResult[MaterialTransferResult]:
        """提交权威位置，再按 unload -> load 收敛两端本地投影。

        同一个幂等命令在设备同步阶段失败后可以安全重试：数据库 mutation
        会 replay，但 unload/load 仍会使用相同 ``transfer_uuid`` 重放。目标
        load 失败时不回滚权威位置，也不会重新挂回来源端，因此不会产生双挂载。
        """

        if mutation.operation != "transfer_material":
            raise MaterialValidationError("material transfer operation is invalid")
        dispatcher = self._resource_sync_dispatcher
        if dispatcher is None:
            raise MaterialTransferSyncError(
                "material transfer resource-service dispatcher is not configured"
            )

        def apply(timestamp: int) -> _Applied[MaterialTransferResult]:
            resolved: list[tuple[Any, SiteRead | None]] = []
            destination_sites: set[str] = set()
            for item in value.items:
                target = self.get_material(item.target_material_uuid)
                destination = self._transfer_destination_site(
                    target,
                    item.target_site,
                )
                if destination is not None:
                    if destination.site_uuid in destination_sites:
                        raise MaterialConflictError(
                            "one transfer cannot target the same Site twice"
                        )
                    destination_sites.add(destination.site_uuid)
                    if destination.occupied_material_uuid not in (
                        None,
                        item.material_uuid,
                    ):
                        raise MaterialConflictError(
                            f"destination Site {destination.site_uuid} is occupied"
                        )
                resolved.append((item, destination))

            affected: list[AggregateVersion] = []
            sequences: list[int] = []
            materials: list[MaterialAggregateRead] = []
            site_uuids: list[str | None] = []
            for item, destination in resolved:
                moved = self._apply_material_move(
                    mutation,
                    MaterialMove(
                        material_uuid=item.material_uuid,
                        destination_site_uuid=(
                            destination.site_uuid
                            if destination is not None
                            else None
                        ),
                        parent_material_uuid=(
                            None
                            if destination is not None
                            else item.target_material_uuid
                        ),
                    ),
                    timestamp,
                )
                affected.extend(moved.affected)
                sequences.extend(moved.sequences)
                materials.append(moved.data)
                site_uuids.append(
                    destination.site_uuid if destination is not None else None
                )

            return _Applied(
                MaterialTransferResult(
                    source_device_id=value.source_device_id,
                    target_device_id=value.target_device_id,
                    material_uuids=[item.material_uuid for item in value.items],
                    target_material_uuids=[
                        item.target_material_uuid for item in value.items
                    ],
                    destination_site_uuids=site_uuids,
                    materials=materials,
                ),
                affected,
                sequences,
            )

        with self._transfer_lock:
            result = self._run_mutation(
                mutation,
                value,
                MutationResult[MaterialTransferResult],
                apply,
            )
            self._transfer_result_is_current(result.data)
            self._dispatch_device_sync(
                dispatcher,
                MaterialDeviceSync(
                    transfer_uuid=mutation.command_uuid,
                    device_id=result.data.source_device_id,
                    action="unload",
                    material_uuids=result.data.material_uuids,
                ),
            )
            self._dispatch_device_sync(
                dispatcher,
                MaterialDeviceSync(
                    transfer_uuid=mutation.command_uuid,
                    device_id=result.data.target_device_id,
                    action="load",
                    material_uuids=result.data.material_uuids,
                    destination_site_uuids=(
                        result.data.destination_site_uuids
                    ),
                ),
            )
            return result

    # -- Delete / Snapshot ------------------------------------------------

    def delete_material(
        self, mutation: InventoryMutation, value: MaterialDelete
    ) -> MutationResult[MaterialDeleteResult]:
        if mutation.operation != "delete_material":
            raise MaterialValidationError("material delete operation is invalid")

        def apply(timestamp: int) -> _Applied[MaterialDeleteResult]:
            records = self.repository.tree_materials(value.material_uuid)
            if not records:
                raise MaterialNotFoundError(
                    f"material not found: {value.material_uuid}"
                )
            if len(records) > 1 and not value.recursive:
                raise MaterialConflictError(
                    "material has children; recursive delete is required"
                )
            material_uuids = [record.material_uuid for record in records]
            owned_sites = {
                site.site_uuid: site
                for material_uuid in material_uuids
                for site in self.repository.list_sites(material_uuid)
            }
            occupied_sites = {
                site.site_uuid: site
                for site in self.repository.sites_occupied_by(material_uuids)
            }
            changed_sites = {**occupied_sites, **owned_sites}
            self.repository.clear_site_occupants(sorted(changed_sites))

            affected: list[AggregateVersion] = []
            sequences: list[int] = []
            for site_uuid in sorted(changed_sites):
                current = changed_sites[site_uuid]
                deleting = site_uuid in owned_sites
                updated = SiteRecord.model_validate(
                    {
                        **current.model_dump(mode="json"),
                        "occupied_material_uuid": None,
                        "changed_by_job_uuid": mutation.job_uuid,
                        "changed_by_command_uuid": mutation.command_uuid,
                        "changed_at_ms": timestamp,
                        "updated_at_ms": timestamp,
                        "deleted_at_ms": timestamp if deleting else None,
                        "version": current.version + 1,
                    }
                )
                self.repository.update_site(updated)
                state_hash = self._site_state_hash(updated)
                affected.append(
                    AggregateVersion(
                        aggregate_type="site",
                        aggregate_uuid=site_uuid,
                        version=updated.version,
                        state_hash=state_hash,
                    )
                )
                sequences.append(
                    self._ledger(
                        mutation,
                        aggregate_type="site",
                        aggregate_uuid=site_uuid,
                        operation="delete" if deleting else "vacate",
                        previous_version=current.version,
                        aggregate_version=updated.version,
                        state_hash=state_hash,
                        delta={
                            "deleted": deleting,
                            "occupied_material_uuid": None,
                        },
                        timestamp=timestamp,
                    )
                )

            for current in reversed(records):
                updated = MaterialRecord.model_validate(
                    {
                        **current.model_dump(mode="json"),
                        "lifecycle_status": "retired",
                        "deleted_at_ms": timestamp,
                        "updated_at_ms": timestamp,
                        "version": current.version + 1,
                    }
                )
                self.repository.update_material(updated)
                position = self.repository.get_position(current.material_uuid)
                data = self.repository.get_data(current.material_uuid)
                if position is None or data is None:
                    raise MaterialValidationError("material aggregate is incomplete")
                identity = self._identity_read(updated)
                position_read = self._position_read(position)
                data_read = self._data_read(data)
                state_hash = self._material_state_hash(
                    identity, position_read, data_read
                )
                affected.append(
                    AggregateVersion(
                        aggregate_type="material",
                        aggregate_uuid=current.material_uuid,
                        version=updated.version,
                        state_hash=state_hash,
                    )
                )
                sequences.append(
                    self._ledger(
                        mutation,
                        aggregate_type="material",
                        aggregate_uuid=current.material_uuid,
                        operation="delete",
                        previous_version=current.version,
                        aggregate_version=updated.version,
                        state_hash=state_hash,
                        delta={"lifecycle_status": "retired", "deleted": True},
                        timestamp=timestamp,
                    )
                )
            return _Applied(
                MaterialDeleteResult(
                    root_material_uuid=value.material_uuid,
                    deleted_material_uuids=material_uuids,
                    deleted_site_uuids=sorted(owned_sites),
                ),
                affected,
                sequences,
            )

        return self._run_mutation(
            mutation, value, MutationResult[MaterialDeleteResult], apply
        )

    # -- Snapshot ---------------------------------------------------------

    def compare_snapshot(self, value: MaterialSnapshot) -> MaterialSnapshotDiff:
        return compare_material_snapshot(
            self.get_tree(value.root_material_uuid), value
        )

    def apply_snapshot(
        self, mutation: InventoryMutation, value: MaterialSnapshot
    ) -> MutationResult[MaterialTreeRead]:
        """在一个 writer 事务中应用完整、同构的物料树快照。

        快照只更新既有聚合。新增树走 ``create_tree``，删除走显式 delete；这样
        不会把一次设备状态上报误解释成库存创建或销毁。
        """

        if mutation.operation != "apply_material_snapshot":
            raise MaterialValidationError("snapshot operation is invalid")

        def apply(timestamp: int) -> _Applied[MaterialTreeRead]:
            authoritative = self.get_tree(value.root_material_uuid)
            diff = compare_material_snapshot(authoritative, value)
            if value.state_hash is not None and value.state_hash != diff.observed_state_hash:
                raise MaterialValidationError("snapshot state_hash is invalid")
            presence_changes = [
                change
                for change in diff.changes
                if "presence" in change.changed_fields
            ]
            if presence_changes:
                raise MaterialValidationError(
                    "snapshot cannot create or delete material/site aggregates"
                )
            if not diff.changed:
                raise MaterialNoChangeError("snapshot matches authoritative state")

            current_nodes = {
                node.material.material_uuid: node for node in authoritative.nodes
            }
            desired_nodes = {
                node.material.material_uuid: node for node in value.nodes
            }
            if current_nodes.keys() != desired_nodes.keys():
                raise MaterialValidationError("snapshot material set differs")

            changes_by_material: dict[str, set[str]] = {}
            changed_site_uuids: set[str] = set()
            for change in diff.changes:
                if change.aggregate_type == "material":
                    changes_by_material.setdefault(change.aggregate_uuid, set()).add(
                        change.section
                    )
                else:
                    changed_site_uuids.add(change.aggregate_uuid)

            current_sites = {
                site.site_uuid: site
                for node in authoritative.nodes
                for site in node.sites
            }
            desired_sites = {
                site.site_uuid: site for node in value.nodes for site in node.sites
            }
            if current_sites.keys() != desired_sites.keys():
                raise MaterialValidationError("snapshot Site set differs")

            immutable_identity = (
                "material_uuid",
                "resource_id",
                "template_uuid",
                "resource_type",
                "class_name",
                "template_name",
                "created_at_ms",
            )
            for material_uuid, desired in desired_nodes.items():
                current = current_nodes[material_uuid]
                for field in immutable_identity:
                    if getattr(current.material, field) != getattr(
                        desired.material, field
                    ):
                        raise MaterialValidationError(
                            f"snapshot cannot change immutable material field {field}"
                        )
                if material_uuid == value.root_material_uuid and (
                    desired.material.parent_material_uuid is not None
                ):
                    raise MaterialValidationError("snapshot root cannot have a parent")

            for site_uuid, desired in desired_sites.items():
                current = current_sites[site_uuid]
                if desired.owner_material_uuid != current.owner_material_uuid:
                    raise MaterialValidationError(
                        "snapshot cannot change Site owner_material_uuid"
                    )

            # 先清空将变化的占用关系，随后改 parent，最后恢复新的占用关系。
            # 整个过程在同一事务内，对读者不可见，Site 也只增长一个版本。
            self.repository.clear_site_occupants(sorted(changed_site_uuids))

            changed_material_records: dict[str, tuple[MaterialRecord, MaterialRecord]] = {}
            for material_uuid, sections in changes_by_material.items():
                desired_node = desired_nodes[material_uuid]
                current_record = self.repository.get_material(material_uuid)
                current_position = self.repository.get_position(material_uuid)
                current_data = self.repository.get_data(material_uuid)
                if (
                    current_record is None
                    or current_position is None
                    or current_data is None
                ):
                    raise MaterialValidationError(
                        f"material aggregate is incomplete: {material_uuid}"
                    )

                if "position" in sections:
                    position_values = desired_node.position.model_dump(mode="json")
                    position_values["extra_json"] = position_values.pop("extra")
                    self.repository.replace_position(
                        MaterialPositionRecord(
                            material_uuid=material_uuid,
                            updated_at_ms=timestamp,
                            version=current_position.version + 1,
                            **position_values,
                        )
                    )

                if "data" in sections:
                    desired_data = MaterialDataWrite(
                        data=desired_node.data.data,
                        substances=desired_node.data.substances,
                        sites_initialized=desired_node.data.sites_initialized,
                        unknown_counter=desired_node.data.unknown_counter,
                        state_status=desired_node.data.state_status,
                        source_event_uuid=desired_node.data.source_event_uuid,
                        source_job_uuid=desired_node.data.source_job_uuid,
                        source_command_uuid=(
                            desired_node.data.source_command_uuid
                            or mutation.command_uuid
                        ),
                        observed_at_ms=desired_node.data.observed_at_ms,
                    )
                    content_version = current_data.content_version + 1
                    substances = self._new_substance_records(
                        material_uuid,
                        desired_data,
                        content_version=content_version,
                        timestamp=timestamp,
                        previous=current_data.substances,
                    )
                    state_hash = self._data_state_hash(
                        {
                            **desired_data.model_dump(mode="json"),
                            "substances": [
                                self._substance_read(item).model_dump(mode="json")
                                for item in substances
                            ],
                        }
                    )
                    self.repository.replace_data(
                        MaterialDataRecord(
                            material_uuid=material_uuid,
                            data_json=desired_data.data,
                            substances=substances,
                            sites_initialized=desired_data.sites_initialized,
                            unknown_counter=desired_data.unknown_counter,
                            state_status=desired_data.state_status,
                            content_version=content_version,
                            state_hash=state_hash,
                            source_event_uuid=desired_data.source_event_uuid,
                            source_job_uuid=desired_data.source_job_uuid,
                            source_command_uuid=desired_data.source_command_uuid,
                            observed_at_ms=desired_data.observed_at_ms,
                            updated_at_ms=timestamp,
                            version=current_data.version + 1,
                        )
                    )
                    self.repository.replace_substances(material_uuid, substances)

                identity = desired_node.material
                updated_record = MaterialRecord.model_validate(
                    {
                        **current_record.model_dump(mode="json"),
                        "parent_material_uuid": identity.parent_material_uuid,
                        "lot_uuid": identity.lot_uuid,
                        "name": identity.name,
                        "description": identity.description,
                        "machine_name": identity.machine_name,
                        "barcode": identity.barcode,
                        "barcode_symbology": identity.barcode_symbology,
                        "resource_schema_json": identity.resource_schema,
                        "model_json": identity.model,
                        "icon_uri": identity.icon_uri,
                        "config_json": identity.config,
                        "extra_json": identity.extra,
                        "meta_data_json": identity.meta_data,
                        "lifecycle_status": identity.lifecycle_status,
                        "updated_at_ms": timestamp,
                        "version": current_record.version + 1,
                    }
                )
                self.repository.update_material(updated_record)
                changed_material_records[material_uuid] = (
                    current_record,
                    updated_record,
                )

            changed_site_records: dict[str, tuple[SiteRecord, SiteRecord]] = {}
            for site_uuid in sorted(changed_site_uuids):
                current_record = self.repository.get_site(site_uuid)
                desired = desired_sites[site_uuid]
                if current_record is None:
                    raise MaterialNotFoundError(f"site not found: {site_uuid}")
                updated_record = SiteRecord(
                    site_uuid=site_uuid,
                    schema_version=desired.schema_version,
                    owner_material_uuid=current_record.owner_material_uuid,
                    ordinal=current_record.ordinal,
                    template_name=desired.template_name,
                    site_index=desired.site_index,
                    label=desired.label,
                    visible=desired.visible,
                    occupied_material_uuid=desired.occupied_material_uuid,
                    pose=desired.pose,
                    allowed_resource_categories=desired.allowed_resource_categories,
                    parent_link=desired.parent_link,
                    description=desired.description,
                    meta_data_json=desired.meta_data,
                    extra_json=desired.extra,
                    changed_by_job_uuid=mutation.job_uuid,
                    changed_by_command_uuid=mutation.command_uuid,
                    changed_at_ms=timestamp,
                    created_at_ms=current_record.created_at_ms,
                    updated_at_ms=timestamp,
                    deleted_at_ms=desired.deleted_at_ms,
                    version=current_record.version + 1,
                )
                self.repository.update_site(updated_record)
                changed_site_records[site_uuid] = (current_record, updated_record)

            affected: list[AggregateVersion] = []
            sequences: list[int] = []
            for material_uuid, (before, after) in changed_material_records.items():
                aggregate = self.get_material(material_uuid)
                affected.append(
                    AggregateVersion(
                        aggregate_type="material",
                        aggregate_uuid=material_uuid,
                        version=after.version,
                        state_hash=aggregate.state_hash,
                    )
                )
                sequences.append(
                    self._ledger(
                        mutation,
                        aggregate_type="material",
                        aggregate_uuid=material_uuid,
                        operation="apply_snapshot",
                        previous_version=before.version,
                        aggregate_version=after.version,
                        state_hash=aggregate.state_hash,
                        delta={
                            "sections": sorted(changes_by_material[material_uuid]),
                            "observed_state_hash": diff.observed_state_hash,
                        },
                        timestamp=timestamp,
                    )
                )
            for site_uuid, (before, after) in changed_site_records.items():
                state_hash = self._site_state_hash(after)
                affected.append(
                    AggregateVersion(
                        aggregate_type="site",
                        aggregate_uuid=site_uuid,
                        version=after.version,
                        state_hash=state_hash,
                    )
                )
                sequences.append(
                    self._ledger(
                        mutation,
                        aggregate_type="site",
                        aggregate_uuid=site_uuid,
                        operation="apply_snapshot",
                        previous_version=before.version,
                        aggregate_version=after.version,
                        state_hash=state_hash,
                        delta={"observed_state_hash": diff.observed_state_hash},
                        timestamp=timestamp,
                    )
                )
            return _Applied(
                self.get_tree(value.root_material_uuid), affected, sequences
            )

        return self._run_mutation(
            mutation, value, MutationResult[MaterialTreeRead], apply
        )

    # -- Ledger transport -------------------------------------------------

    def changes(self, *, after_sequence: int = 0, limit: int = 100) -> list[InventoryChange]:
        result: list[InventoryChange] = []
        for record in self.repository.list_ledger(
            after_sequence=after_sequence, limit=limit
        ):
            values = record.model_dump(
                mode="json",
                exclude={
                    "delivery_attempt_count",
                    "available_at_ms",
                    "last_sent_at_ms",
                    "acked_at_ms",
                    "last_error",
                },
            )
            values["delta"] = values.pop("delta_json")
            result.append(InventoryChange.model_validate(values))
        return result

    def acknowledge_changes(self, through_sequence: int) -> int:
        with self.repository.write():
            return self.repository.acknowledge_ledger(
                through_sequence, acknowledged_at_ms=self._now_ms()
            )


__all__ = [
    "InsufficientInventoryError",
    "MaterialConflictError",
    "MaterialNoChangeError",
    "MaterialNotFoundError",
    "MaterialTransferSyncError",
    "MaterialValidationError",
    "MaterialsService",
    "MaterialsServiceError",
    "RejectedMutationError",
]
