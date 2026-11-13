"""V1 Edge 库存操作面 ``/api/v1/inventory/*``（统一信封，``limit`` 分页 + 具名数组）。

- ``POST /inventory/commands`` 是全部库存写入的唯一命令入口（dev ``materials.v1`` 变更与 V1
  类型化命令，见 ``services/materials/inventory_commands.py``）。它按 V1 原样返回裸结果
  ``{command_id, status, ...}``，不包信封：V1 控制台按 ``status`` 判断，业务拒绝也是 HTTP 200。
- 读路由返回 dev 的运营 DTO（批次 / 物料聚合 / 模板 / 预留），并附 V1 的标识别名
  （``lot_id`` / ``edge_uuid`` / ``template_id`` / ``reservation_id``）。
- ``/inventory/ingress-reservations`` 是工站入站库位预留。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from unilabos.server.api.envelope import (
    INTERNAL_ERROR,
    RESOURCE,
    EnvelopeResponse,
    envelope_router,
    raw_response,
    to_api_error,
)
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.contents import ContentContract
from unilabos.server.services.materials.core import MaterialsServiceError
from unilabos.server.services.materials.ingress import IngressReservations
from unilabos.server.services.materials.inventory_commands import InventoryCommands

_LIMIT = Query(default=500, ge=1, le=500)


class IngressReserveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: str = Field(min_length=1)
    carrier_material_uuid: str = Field(min_length=1)
    candidate_site_uuids: List[str] = Field(min_length=1)
    ttl_seconds: int = Field(strict=True, gt=0)
    backend_task_uuid: Optional[str] = None
    invocation_key: Optional[str] = None


class IngressCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1)


def _code_of(exc: BaseException) -> int:
    api_error = to_api_error(exc, RESOURCE)
    return api_error.code if api_error is not None else INTERNAL_ERROR


def lot_dto(lot: Any) -> Dict[str, Any]:
    data = lot.model_dump(mode="json")
    return {**data, "lot_id": data["lot_uuid"], "template_id": data["template_uuid"]}


def instance_dto(aggregate: Any) -> Dict[str, Any]:
    data = aggregate.model_dump(mode="json", exclude_none=False)
    material = data["material"]
    return {**data, "edge_uuid": material["material_uuid"], "template_id": material["template_uuid"],
            "barcode": material["barcode"], "status": material["lifecycle_status"]}


def template_dto(template: Any) -> Dict[str, Any]:
    data = template.model_dump(mode="json", exclude_none=False)
    return {**data, "template_id": data["template_uuid"]}


def reservation_dto(reservation: Any) -> Dict[str, Any]:
    data = reservation.model_dump(mode="json", exclude_none=False)
    return {**data, "reservation_id": data["reservation_uuid"], "workflow_id": data["task_uuid"], "node_id": data["node_uuid"]}


def create_inventory_router(service: MaterialsService) -> APIRouter:
    contents = ContentContract(service, compound_source=None)
    commands = InventoryCommands(service, contents=contents)
    ingress = IngressReservations(service)
    router = envelope_router(prefix="/api/v1/inventory", tags=["inventory"], profile=RESOURCE)

    @router.get("/health")
    def health() -> Any:
        from unilabos.server.database.tables.materials import MATERIALS_DATABASE

        return {"status": "ok", "edge_id": "edge-default", "lab_id": "edge-lab",
                "schema_version": MATERIALS_DATABASE.schema_version,
                "ledger_sequence": service.latest_ledger_sequence()}

    @router.post("/commands")
    @raw_response
    def post_command(body: Dict[str, Any] = Body(...)) -> JSONResponse:
        """统一库存命令入口（幂等）：成功与业务拒绝都是 HTTP 200 + 裸结果；未知异常照常上抛。"""

        try:
            result = commands.execute(body, _code_of)
        except MaterialsServiceError as exc:
            # 命令外形本身不合法（缺 command_id / type）
            result = {"command_id": str(body.get("command_id") or body.get("command_uuid") or ""),
                      "status": "rejected", "error": str(exc), "error_code": getattr(exc, "code", "invalid"),
                      "code": _code_of(exc)}
        return EnvelopeResponse(content=result)

    @router.get("/commands/processed")
    def processed_commands(limit: int = Query(default=200, ge=1, le=500)) -> Any:
        return {"commands": commands.processed(limit=limit)}

    @router.get("/lots")
    def list_lots(
        limit: int = _LIMIT,
        template_uuid: Optional[str] = Query(default=None),
        unit: Optional[str] = Query(default=None),
        include_quarantined: bool = Query(default=True),
    ) -> Any:
        lots = service.list_inventory_lots(template_uuid=template_uuid, unit=unit, include_quarantined=include_quarantined)
        return {"lots": [lot_dto(item) for item in lots[:limit]]}

    @router.get("/lots/{lot_id}")
    def get_lot(lot_id: str) -> Any:
        return lot_dto(service.get_inventory_lot(lot_id))

    @router.get("/instances")
    def list_instances(
        status: Optional[str] = Query(default=None),
        limit: int = _LIMIT,
        roots_only: bool = Query(default=False),
        name: Optional[str] = Query(default=None, description="按名称精确搜索"),
        all: bool = Query(default=False, description="dev 扩展：不截断（Host 同步全量读取）"),
    ) -> Any:
        items = service.search_materials(name) if name is not None else service.list_materials(roots_only=roots_only)
        if status:
            items = [item for item in items if item.material.lifecycle_status == status]
        return {"instances": [instance_dto(item) for item in (items if all else items[:limit])]}

    @router.get("/instances/{edge_uuid}")
    def get_instance(edge_uuid: str) -> Any:
        aggregate = service.get_material(edge_uuid)
        site = service.occupied_site(edge_uuid)
        relation = None
        if aggregate.material.parent_material_uuid is not None:
            relation = {"parent_uuid": aggregate.material.parent_material_uuid, "child_uuid": edge_uuid,
                        "slot_id": site.label if site is not None else "",
                        "site_uuid": site.site_uuid if site is not None else None}
        content = {"material_uuid": edge_uuid, "content_version": aggregate.data.content_version,
                   "substances": [item.model_dump(mode="json") for item in aggregate.data.substances]}
        return {**instance_dto(aggregate), "relation": relation, "content": content}

    @router.get("/reservations")
    def list_reservations(
        status: Optional[str] = Query(default=None),
        limit: int = _LIMIT,
        task_uuid: Optional[str] = Query(default=None),
    ) -> Any:
        items = service.list_inventory_reservations(task_uuid=task_uuid, status=status)
        return {
            "reservations": [reservation_dto(item) for item in items[:limit]],
            "content_reservations": contents.list_content_reservations(status=status or "", task_uuid=task_uuid or "", limit=limit),
            "source_bindings": contents.list_material_source_bindings(task_uuid=task_uuid or "", status="active" if status == "active" else ""),
        }

    @router.get("/reservations/{reservation_id}")
    def get_reservation(reservation_id: str) -> Any:
        try:
            return reservation_dto(service.get_inventory_reservation(reservation_id))
        except MaterialsServiceError:
            return contents.get_content_reservation(reservation_id)

    @router.get("/workflows/{workflow_id}/reservations")
    def workflow_reservations(workflow_id: str) -> Any:
        return {"workflow_id": workflow_id,
                "reservations": [reservation_dto(item) for item in service.list_inventory_reservations(task_uuid=workflow_id)],
                "content_reservations": contents.list_content_reservations(task_uuid=workflow_id),
                "source_bindings": contents.list_material_source_bindings(task_uuid=workflow_id)}

    @router.get("/templates")
    def list_templates(
        limit: int = _LIMIT,
        name: Optional[str] = Query(default=None, description="按模板 name 精确筛选"),
        include_definition: bool = Query(default=False, description="附带 registry 全量 definition"),
        all: bool = Query(default=False, description="dev 扩展：不截断（Host 同步全量读取）"),
    ) -> Any:
        items = service.list_templates(name=name, include_definition=include_definition)
        return {"templates": [template_dto(item) for item in (items if all else items[:limit])]}

    @router.get("/templates/{template_id}")
    def get_template(template_id: str) -> Any:
        return template_dto(service.get_template(template_id))

    @router.get("/relations")
    def list_relations(
        material_uuid: str = Query(default=""),
        source_material_uuid: str = Query(default=""),
        target_material_uuid: str = Query(default=""),
        link_type: Optional[str] = Query(default=None),
    ) -> Any:
        """父子 / 库位关系（V1 relations）+ 拓扑边（dev material_link）。"""

        rows = service.connection.execute(
            "SELECT m.parent_material_uuid AS parent_uuid, m.material_uuid AS child_uuid, s.label AS slot_id, "
            "s.site_uuid FROM material m LEFT JOIN site s ON s.occupied_material_uuid = m.material_uuid "
            "AND s.deleted_at_ms IS NULL WHERE m.deleted_at_ms IS NULL AND m.parent_material_uuid IS NOT NULL "
            "ORDER BY m.parent_material_uuid, m.ordinal, m.material_uuid"
        ).fetchall()
        relations = [
            {"parent_uuid": row["parent_uuid"], "child_uuid": row["child_uuid"], "slot_id": row["slot_id"] or "",
             "site_uuid": row["site_uuid"]}
            for row in rows
            if not material_uuid or material_uuid in (row["parent_uuid"], row["child_uuid"])
        ]
        links = service.list_links(
            material_uuid=material_uuid or None, source_material_uuid=source_material_uuid or None,
            target_material_uuid=target_material_uuid or None, link_type=link_type,
        )
        return {"relations": relations, "links": links}

    @router.get("/contents")
    def list_contents(limit: int = _LIMIT) -> Any:
        return {"contents": _contents(service, limit)}

    @router.get("/snapshot")
    def snapshot() -> Any:
        """全量状态导出（接入方首次对齐 / 缺口重建投影用）；带账本游标。"""

        return {
            "ledger_sequence": service.latest_ledger_sequence(),
            "templates": [template_dto(item) for item in service.list_templates()],
            "lots": [lot_dto(item) for item in service.list_inventory_lots(include_quarantined=True)],
            "instances": [instance_dto(item) for item in service.list_materials()],
            "reservations": [reservation_dto(item) for item in service.list_inventory_reservations()],
            "contents": _contents(service, None),
        }

    @router.get("/ledger")
    def ledger(limit: int = Query(default=200, ge=1, le=1000), after_id: int = Query(default=0, ge=0)) -> Any:
        """物料账本（dev ``inventory_ledger``，``after_id`` 即 ``sequence`` 游标）。"""

        return {"entries": [item.model_dump(mode="json") for item in service.changes(after_sequence=after_id, limit=limit)]}

    @router.get("/outbox/backlog")
    def outbox_backlog() -> Any:
        acked = service.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM inventory_ledger WHERE delivery_status='acknowledged'"
        ).fetchone()[0]
        pending = service.connection.execute(
            "SELECT COUNT(*) FROM inventory_ledger WHERE delivery_status IN ('pending','sent')"
        ).fetchone()[0]
        return {"max_sequence": service.latest_ledger_sequence(), "acked_sequence": int(acked), "pending_count": int(pending)}

    @router.get("/outbox/events")
    def outbox_events(after_sequence: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=500)) -> Any:
        events = [
            item.model_dump(mode="json") for item in service.changes(after_sequence=after_sequence, limit=limit)
            if item.delivery_status in ("pending", "sent", "dead_letter")
        ]
        return {"events": events}

    @router.get("/sync/cursors")
    def sync_cursors() -> Any:
        acked = service.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0), MAX(acked_at_ms) FROM inventory_ledger WHERE delivery_status='acknowledged'"
        ).fetchone()
        return {"cursors": [{"name": "materials.ledger", "acked_sequence": int(acked[0]), "acked_at_ms": acked[1],
                             "latest_sequence": service.latest_ledger_sequence()}]}

    # ── 工站入站预留 ──────────────────────────────────────────

    @router.post("/ingress-reservations", status_code=201)
    def reserve_ingress(body: IngressReserveRequest) -> Any:
        projection, _created = ingress.reserve(body.model_dump())
        return projection

    @router.get("/ingress-reservations")
    def list_ingress(state: Optional[str] = Query(default=None), limit: int = Query(default=200, ge=1, le=500)) -> Any:
        return {"reservations": ingress.list(state=state, limit=limit)}

    @router.get("/ingress-reservations/{reservation_uuid}")
    def get_ingress(reservation_uuid: str) -> Any:
        return ingress.get(reservation_uuid)

    @router.post("/ingress-reservations/{reservation_uuid}/in-transit")
    def ingress_in_transit(reservation_uuid: str) -> Any:
        return ingress.mark_in_transit(reservation_uuid)

    @router.post("/ingress-reservations/{reservation_uuid}/receive")
    def ingress_receive(reservation_uuid: str) -> Any:
        return ingress.receive(reservation_uuid)

    @router.post("/ingress-reservations/{reservation_uuid}/cancel")
    def ingress_cancel(reservation_uuid: str, body: IngressCancelRequest) -> Any:
        return ingress.cancel(reservation_uuid, reason=body.reason)

    return router


def _contents(service: MaterialsService, limit: Optional[int]) -> List[Dict[str, Any]]:
    rows = service.connection.execute(
        "SELECT d.material_uuid, d.content_version, d.state_hash FROM material_data d JOIN material m USING(material_uuid) "
        "WHERE m.deleted_at_ms IS NULL AND EXISTS (SELECT 1 FROM material_substance s WHERE s.material_uuid = d.material_uuid) "
        "ORDER BY d.material_uuid" + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    result = []
    for row in rows:
        substances = [item.model_dump(mode="json") for item in service.list_substances(row["material_uuid"])]
        result.append({"material_uuid": row["material_uuid"], "content_version": row["content_version"],
                       "state_hash": row["state_hash"], "substances": substances})
    return result


def install_inventory_api(app: FastAPI, service: MaterialsService) -> None:
    app.include_router(create_inventory_router(service))


__all__ = ["create_inventory_router", "install_inventory_api", "instance_dto", "lot_dto", "reservation_dto", "template_dto"]
