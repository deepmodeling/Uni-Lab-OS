"""工站入站库位预留（V1 ``/inventory/ingress-reservations``）。

AGV 运输载体前在目标工站的候选入口库位里预留一个空库位：``reserved``（TTL 内有效）→
``in_transit``（已装载，不再自然过期）→ ``received``（物理交接，经 dev ``move_material``
把载体放进库位）；也可人工 ``canceled``。预留与 ``site_uuid`` 部分唯一索引保证同一库位
同时只有一个活动预留；库位占用、父级与账本沿用 dev 物料移动路径。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from unilabos.protocol.base import canonical_hash
from unilabos.protocol.materials import MaterialMove
from unilabos.server.api.v1_fields import ms_to_iso
from unilabos.server.services.materials.core import (
    MaterialConflictError,
    MaterialNotFoundError,
    MaterialsService,
    MaterialValidationError,
)
from unilabos.server.services.materials.resource_contract import new_mutation
from unilabos.server.services.materials.store import MaterialsRepository

_ACTIVE = ("reserved", "in_transit")


class IngressReservations:
    def __init__(self, materials: MaterialsService) -> None:
        self.materials = materials

    @property
    def connection(self) -> Any:
        return self.materials.connection

    @staticmethod
    def _projection(row: Any) -> dict[str, Any]:
        return {
            "uuid": row["reservation_uuid"],
            "idempotency_key": row["idempotency_key"],
            "backend_task_uuid": row["backend_task_uuid"],
            "invocation_key": row["invocation_key"],
            "carrier_material_uuid": row["carrier_material_uuid"],
            "site_uuid": row["site_uuid"],
            "state": row["state"],
            "expires_at": ms_to_iso(row["expires_at_ms"]),
            "transport_started_at": ms_to_iso(row["transport_started_at_ms"]),
            "received_at": ms_to_iso(row["received_at_ms"]),
            "canceled_at": ms_to_iso(row["canceled_at_ms"]),
            "cancel_reason": row["cancel_reason"],
            "revision": row["revision"],
            "create_time": ms_to_iso(row["created_at_ms"]),
            "update_time": ms_to_iso(row["updated_at_ms"]),
        }

    def _row(self, reservation_uuid: str) -> Any:
        row = self.connection.execute(
            "SELECT * FROM station_ingress_reservation WHERE reservation_uuid=?", (reservation_uuid,)
        ).fetchone()
        if row is None:
            raise MaterialNotFoundError(f"ingress reservation not found: {reservation_uuid}")
        return row

    def _expire_due(self, now: int) -> None:
        """只有 ``reserved`` 会按 TTL 自然过期；运输中的预留只能人工取消或接收。"""

        self.connection.execute(
            "UPDATE station_ingress_reservation SET state='expired', updated_at_ms=MAX(?, created_at_ms), "
            "revision=revision+1 WHERE state='reserved' AND expires_at_ms <= ?", (now, now),
        )

    def reserve(self, values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """返回 (投影, 是否新建)；同一幂等键重放必须是同一请求。"""

        key = str(values.get("idempotency_key") or "").strip()
        carrier = str(values.get("carrier_material_uuid") or "").strip()
        candidates: Sequence[str] = [str(item).strip() for item in values.get("candidate_site_uuids") or [] if str(item).strip()]
        ttl = values.get("ttl_seconds")
        if not key or not carrier or not candidates:
            raise MaterialValidationError("idempotency_key, carrier_material_uuid and candidate_site_uuids are required")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise MaterialValidationError("ttl_seconds 必须是正整数")
        request = {
            "carrier_material_uuid": carrier, "candidate_site_uuids": list(candidates), "ttl_seconds": ttl,
            "backend_task_uuid": values.get("backend_task_uuid"), "invocation_key": values.get("invocation_key"),
        }
        request_hash = canonical_hash(request)
        now = self.materials._now_ms()
        with self.materials.write():
            self._expire_due(now)
            replay = self.connection.execute(
                "SELECT * FROM station_ingress_reservation WHERE idempotency_key=?", (key,)
            ).fetchone()
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise MaterialConflictError("入口预留幂等键已用于不同请求", conflict="state")
                return self._projection(replay), False
            if MaterialsRepository.get_material(self.materials, carrier) is None:
                raise MaterialNotFoundError("可搬运载体尚未同步到目标工站", subject="material")
            marks = ",".join("?" for _ in candidates)
            found = {
                row[0] for row in self.connection.execute(
                    f"SELECT site_uuid FROM site WHERE site_uuid IN ({marks}) AND deleted_at_ms IS NULL", tuple(candidates)
                ).fetchall()
            }
            missing = sorted(set(candidates) - found)
            if missing:
                raise MaterialNotFoundError("逻辑入口引用不存在的库位：" + ",".join(missing), subject="site")
            available = self.connection.execute(
                f"SELECT site.site_uuid FROM site LEFT JOIN station_ingress_reservation AS ingress "
                f"ON ingress.site_uuid = site.site_uuid AND ingress.state IN ('reserved','in_transit') "
                f"LEFT JOIN material_execution_hold AS hold "
                f"ON hold.aggregate_type='site' AND hold.aggregate_uuid = site.site_uuid "
                f"WHERE site.site_uuid IN ({marks}) AND site.deleted_at_ms IS NULL "
                f"AND site.occupied_material_uuid IS NULL AND ingress.reservation_uuid IS NULL "
                f"AND hold.job_uuid IS NULL ORDER BY site.ordinal, site.created_at_ms, site.site_uuid",
                tuple(candidates),
            ).fetchall()
            if not available:
                raise MaterialConflictError("逻辑入口当前没有可接收载体的库位", conflict="site_occupied")
            reservation_uuid = str(uuid4())
            self.connection.execute(
                "INSERT INTO station_ingress_reservation(reservation_uuid, idempotency_key, request_hash, "
                "backend_task_uuid, invocation_key, carrier_material_uuid, site_uuid, state, expires_at_ms, "
                "created_at_ms, updated_at_ms, revision) VALUES (?,?,?,?,?,?,?,'reserved',?,?,?,1)",
                (reservation_uuid, key, request_hash, values.get("backend_task_uuid"), values.get("invocation_key"),
                 carrier, available[0][0], now + ttl * 1000, now, now),
            )
            return self._projection(self._row(reservation_uuid)), True

    def get(self, reservation_uuid: str) -> dict[str, Any]:
        with self.materials.write():
            self._expire_due(self.materials._now_ms())
        return self._projection(self._row(reservation_uuid))

    def _transition(self, reservation_uuid: str, state: str, now: int, **fields: Any) -> None:
        assignments = "".join(f", {key}=?" for key in fields)
        self.connection.execute(
            f"UPDATE station_ingress_reservation SET state=?, updated_at_ms=MAX(?, created_at_ms), "
            f"revision=revision+1{assignments} WHERE reservation_uuid=?",
            (state, now, *fields.values(), reservation_uuid),
        )

    def mark_in_transit(self, reservation_uuid: str) -> dict[str, Any]:
        now = self.materials._now_ms()
        with self.materials.write():
            self._expire_due(now)
            current = self._row(reservation_uuid)
            if current["state"] != "in_transit":
                if current["state"] != "reserved":
                    raise MaterialConflictError(f"入口预留 {current['state']} 状态不能进入运输中", conflict="state")
                self._transition(reservation_uuid, "in_transit", now, transport_started_at_ms=now)
        return self._projection(self._row(reservation_uuid))

    def receive(self, reservation_uuid: str) -> dict[str, Any]:
        now = self.materials._now_ms()
        with self.materials.write():
            current = self._row(reservation_uuid)
            if current["state"] == "received":
                return self._projection(current)
            if current["state"] != "in_transit":
                raise MaterialConflictError(f"入口预留 {current['state']} 状态不能完成接收", conflict="state")
            site = self.materials.get_site(current["site_uuid"])
            if site is None or site.occupied_material_uuid is not None:
                raise MaterialConflictError("运输中的目标入口库位不再可接收", conflict="site_occupied")
            # 物理交接：沿用 dev 物料移动（库位占用、父级、账本同一事务）
            move = MaterialMove(material_uuid=current["carrier_material_uuid"], destination_site_uuid=site.site_uuid)
            self.materials.move_material(
                new_mutation("move_material", actor_type="device", payload=move.model_dump(mode="json", exclude_none=False)), move
            )
            self._transition(reservation_uuid, "received", now, received_at_ms=now)
        return self._projection(self._row(reservation_uuid))

    def cancel(self, reservation_uuid: str, *, reason: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise MaterialValidationError("reason must not be blank")
        now = self.materials._now_ms()
        with self.materials.write():
            self._expire_due(now)
            current = self._row(reservation_uuid)
            if current["state"] == "canceled":
                if (current["cancel_reason"] or "") != reason:
                    raise MaterialConflictError("入口预留取消重放必须使用同一理由", conflict="state")
                return self._projection(current)
            if current["state"] not in _ACTIVE:
                raise MaterialConflictError(f"入口预留 {current['state']} 状态不能取消", conflict="state")
            self._transition(reservation_uuid, "canceled", now, canceled_at_ms=now, cancel_reason=reason)
        return self._projection(self._row(reservation_uuid))

    def list(self, *, state: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        clause, params = ("WHERE state=?", [state]) if state else ("", [])
        rows = self.connection.execute(
            f"SELECT * FROM station_ingress_reservation {clause} ORDER BY created_at_ms DESC, reservation_uuid LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._projection(row) for row in rows]


__all__ = ["IngressReservations"]
