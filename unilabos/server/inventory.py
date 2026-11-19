"""执行阶段的库存生命周期桥，只通过 ``materials.v1`` 工作。"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.protocol.base import canonical_hash

from unilabos.protocol.materials import InventoryMutation
from unilabos.protocol.materials import (
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationRead,
    InventoryReservationTransition,
)


class ExecutionInventoryError(RuntimeError):
    """A job cannot safely advance its authority-owned inventory lifecycle."""


class ExecutionInventoryCoordinator:
    """校验 Scheduler 的预占，并在驱动调用前消费。"""

    def __init__(self, gateway: Any):
        self.gateway = gateway
        self._guard = threading.RLock()
        self._reservation_by_job: dict[str, str] = {}

    @staticmethod
    def _mutation(job_uuid: str, effect: str, operation: str) -> InventoryMutation:
        return InventoryMutation(
            command_uuid=job_uuid,
            effect_key=f"inventory.{effect}",
            operation=operation,
            actor_type="edge",
            job_uuid=job_uuid,
        )

    def prepare(
        self, payload: Mapping[str, Any]
    ) -> InventoryReservationRead | None:
        job_uuid = str(payload.get("job_id") or "").strip()
        if not job_uuid:
            raise ExecutionInventoryError("inventory job_id is required")
        raw_requirements = payload.get("inventory_requirements") or []
        if not isinstance(raw_requirements, Sequence) or isinstance(
            raw_requirements, (str, bytes)
        ):
            raise ExecutionInventoryError("inventory_requirements must be a list")
        requirements = [
            InventoryRequirement.model_validate(item) for item in raw_requirements
        ]
        requested_reservation = str(
            payload.get("inventory_reservation_uuid") or ""
        ).strip()
        if not requirements and not requested_reservation:
            return None

        if not requested_reservation:
            raise ExecutionInventoryError(
                "inventory requirements must be reserved by Backend Scheduler "
                "before dispatch"
            )
        try:
            reservation = self.gateway.get_inventory_reservation(
                requested_reservation
            )
        except Exception as exc:  # noqa: BLE001 - preserve authority cause
            raise ExecutionInventoryError(
                f"cannot load inventory reservation for job {job_uuid}: {exc}"
            ) from exc
        self._validate_reservation(reservation, payload)
        with self._guard:
            existing = self._reservation_by_job.get(job_uuid)
            if existing is not None and existing != reservation.reservation_uuid:
                raise ExecutionInventoryError(
                    "one job cannot switch inventory reservation identity"
                )
            self._reservation_by_job[job_uuid] = reservation.reservation_uuid
        return reservation

    @staticmethod
    def _validate_reservation(
        reservation: InventoryReservationRead,
        payload: Mapping[str, Any],
        *,
        allowed_statuses: frozenset[str] = frozenset({"active", "consumed"}),
    ) -> None:
        expected = {
            "job_uuid": str(payload.get("job_id") or ""),
            "task_uuid": str(payload.get("task_id") or ""),
            "node_uuid": str(payload.get("node_id") or ""),
        }
        for field, value in expected.items():
            if getattr(reservation, field) != value:
                raise ExecutionInventoryError(
                    f"inventory reservation {field} does not match dispatched job"
                )
        try:
            scheduler_revision = int(payload.get("scheduler_revision"))
        except (TypeError, ValueError) as exc:
            raise ExecutionInventoryError(
                "dispatched job has an invalid scheduler_revision"
            ) from exc
        if reservation.scheduler_revision != scheduler_revision:
            raise ExecutionInventoryError(
                "inventory reservation scheduler_revision does not match "
                "dispatched job"
            )

        requirements = [
            InventoryRequirement.model_validate(item)
            for item in (payload.get("inventory_requirements") or [])
        ]
        request_values = {
            "task_uuid": expected["task_uuid"],
            "node_uuid": expected["node_uuid"],
            "job_uuid": expected["job_uuid"],
            "scheduler_revision": scheduler_revision,
            "requirements": requirements,
            "expires_at_ms": reservation.expires_at_ms,
        }
        candidate_hashes = {
            canonical_hash(
                InventoryReservationCreate(
                    reservation_uuid=reservation_uuid,
                    **request_values,
                ).model_dump(mode="json", exclude_none=False)
            )
            for reservation_uuid in (None, reservation.reservation_uuid)
        }
        if reservation.request_hash not in candidate_hashes:
            raise ExecutionInventoryError(
                "inventory reservation requirements do not match dispatched job"
            )
        if reservation.status not in allowed_statuses:
            raise ExecutionInventoryError(
                f"inventory reservation is {reservation.status}, cannot execute"
            )

    def consume(self, job_uuid: str) -> InventoryReservationRead | None:
        reservation = self._current(job_uuid)
        if reservation is None:
            return None
        if reservation.status == "consumed":
            return reservation
        if reservation.status != "active":
            raise ExecutionInventoryError(
                f"inventory reservation is {reservation.status}, cannot consume"
            )
        value = InventoryReservationTransition(
            reservation_uuid=reservation.reservation_uuid,
            reason="action_start",
        )
        try:
            return self.gateway.consume_inventory_reservation(
                self._mutation(
                    job_uuid,
                    "consume",
                    "consume_inventory_reservation",
                ),
                value,
            ).data
        except Exception as exc:  # noqa: BLE001 - preserve Local/HTTP authority error
            raise ExecutionInventoryError(
                f"cannot consume inventory for job {job_uuid}: {exc}"
            ) from exc

    def cancel(self, job_uuid: str, *, reason: str) -> None:
        reservation = self._current(job_uuid)
        if reservation is None:
            return
        if reservation.status == "active":
            self._transition(
                job_uuid,
                reservation,
                action="release",
                operation="release_inventory_reservation",
                reason=reason,
            )
        elif reservation.status == "consumed":
            self._transition(
                job_uuid,
                reservation,
                action="quarantine",
                operation="quarantine_inventory_reservation",
                reason=reason,
            )
        with self._guard:
            self._reservation_by_job.pop(job_uuid, None)

    def terminal(self, job_uuid: str, *, success: bool, reason: str) -> None:
        if success:
            with self._guard:
                self._reservation_by_job.pop(job_uuid, None)
            return
        reservation = self._current(job_uuid)
        if reservation is None:
            return
        if reservation.status == "active":
            self.cancel(job_uuid, reason=reason)
        elif reservation.status == "consumed":
            self._transition(
                job_uuid,
                reservation,
                action="quarantine",
                operation="quarantine_inventory_reservation",
                reason=reason,
            )
        with self._guard:
            self._reservation_by_job.pop(job_uuid, None)

    def _current(self, job_uuid: str) -> InventoryReservationRead | None:
        with self._guard:
            reservation_uuid = self._reservation_by_job.get(job_uuid)
        if reservation_uuid is None:
            return None
        return self.gateway.get_inventory_reservation(reservation_uuid)

    def _transition(
        self,
        job_uuid: str,
        reservation: InventoryReservationRead,
        *,
        action: str,
        operation: str,
        reason: str,
    ) -> InventoryReservationRead:
        value = InventoryReservationTransition(
            reservation_uuid=reservation.reservation_uuid,
            reason=reason,
        )
        method = getattr(self.gateway, f"{action}_inventory_reservation")
        try:
            return method(
                self._mutation(job_uuid, action, operation),
                value,
            ).data
        except Exception as exc:  # noqa: BLE001 - preserve Local/HTTP authority error
            raise ExecutionInventoryError(
                f"cannot {action} inventory for job {job_uuid}: {exc}"
            ) from exc


__all__ = ["ExecutionInventoryCoordinator", "ExecutionInventoryError"]
