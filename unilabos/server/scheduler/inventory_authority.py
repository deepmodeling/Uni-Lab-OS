"""Scheduler-facing inventory lifecycle backed only by ``materials.v1``.

The bridge deliberately knows no SQLite details.  Local and remote material
authorities expose the same client methods, so HostLink/ROS2 execution and a
future real Backend use one reservation protocol.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.server.protocol.common import InventoryMutation
from unilabos.server.protocol.materials import (
    InventoryRequirement,
    InventoryReservationCreate,
    InventoryReservationRead,
    InventoryReservationTransition,
)


class SchedulerInventoryError(RuntimeError):
    """A job cannot safely advance its authority-owned inventory lifecycle."""


class SchedulerInventoryAuthority:
    """Reserve before queueing and consume under the action material lock."""

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
            actor_type="scheduler",
            job_uuid=job_uuid,
        )

    def prepare(
        self, payload: Mapping[str, Any]
    ) -> InventoryReservationRead | None:
        job_uuid = str(payload.get("job_id") or "").strip()
        if not job_uuid:
            raise SchedulerInventoryError("inventory job_id is required")
        raw_requirements = payload.get("inventory_requirements") or []
        if not isinstance(raw_requirements, Sequence) or isinstance(
            raw_requirements, (str, bytes)
        ):
            raise SchedulerInventoryError("inventory_requirements must be a list")
        requirements = [
            InventoryRequirement.model_validate(item) for item in raw_requirements
        ]
        requested_reservation = str(
            payload.get("inventory_reservation_uuid") or ""
        ).strip()
        if not requirements and not requested_reservation:
            return None

        if requested_reservation:
            reservation = self.gateway.get_inventory_reservation(
                requested_reservation
            )
            self._validate_reservation(reservation, payload)
        else:
            try:
                request = InventoryReservationCreate(
                    task_uuid=str(payload.get("task_id") or "").strip(),
                    node_uuid=str(payload.get("node_id") or "").strip(),
                    job_uuid=job_uuid,
                    scheduler_revision=int(payload.get("scheduler_revision") or 0),
                    requirements=requirements,
                )
                reservation = self.gateway.reserve_inventory(
                    self._mutation(job_uuid, "reserve", "reserve_inventory"),
                    request,
                ).data
            except Exception as reserve_error:  # noqa: BLE001 - preserve authority cause
                raise SchedulerInventoryError(
                    f"cannot reserve inventory for job {job_uuid}: {reserve_error}"
                ) from reserve_error
        with self._guard:
            existing = self._reservation_by_job.get(job_uuid)
            if existing is not None and existing != reservation.reservation_uuid:
                raise SchedulerInventoryError(
                    "one job cannot switch inventory reservation identity"
                )
            self._reservation_by_job[job_uuid] = reservation.reservation_uuid
        return reservation

    @staticmethod
    def _validate_reservation(
        reservation: InventoryReservationRead,
        payload: Mapping[str, Any],
    ) -> None:
        expected = {
            "job_uuid": str(payload.get("job_id") or ""),
            "task_uuid": str(payload.get("task_id") or ""),
            "node_uuid": str(payload.get("node_id") or ""),
        }
        for field, value in expected.items():
            if getattr(reservation, field) != value:
                raise SchedulerInventoryError(
                    f"inventory reservation {field} does not match dispatched job"
                )
        if reservation.status not in {"active", "consumed"}:
            raise SchedulerInventoryError(
                f"inventory reservation is {reservation.status}, cannot execute"
            )

    def consume(self, job_uuid: str) -> InventoryReservationRead | None:
        reservation = self._current(job_uuid)
        if reservation is None:
            return None
        if reservation.status == "consumed":
            return reservation
        if reservation.status != "active":
            raise SchedulerInventoryError(
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
            raise SchedulerInventoryError(
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
            raise SchedulerInventoryError(
                f"cannot {action} inventory for job {job_uuid}: {exc}"
            ) from exc


__all__ = ["SchedulerInventoryAuthority", "SchedulerInventoryError"]
