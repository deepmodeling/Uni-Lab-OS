"""Edge 仓储本地 FastAPI 路由（薄层：解析请求 → service/commands → 序列化）.

可独立挂载，也可通过 create_router 接入现有 edge composition root 的 FastAPI app。
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response

from unilabos.server.scheduler.inventory.commands import execute_command
from unilabos.server.scheduler.inventory.domain import (
    InstanceState,
    InventoryError,
    ReservationState,
)
from unilabos.server.scheduler.inventory.schemas import (
    ContentListResponse,
    ErrorResponse,
    InstanceDetailResponse,
    InstanceListResponse,
    InventoryCommand,
    InventoryCommandResult,
    InventoryHealthResponse,
    InventoryLotResponse,
    InventoryReservationResponse,
    InventorySnapshotResponse,
    LedgerListResponse,
    LegacyMaterialQueryRequest,
    LegacyMaterialQueryResponse,
    LotListResponse,
    OutboxBacklogResponse,
    OutboxListResponse,
    ProcessedCommandListResponse,
    RelationListResponse,
    ReservationListResponse,
    ResourceTemplateResponse,
    TemplateListResponse,
    SyncCursorListResponse,
    WorkflowReservationListResponse,
)
from unilabos.server.scheduler.inventory.service import InventoryService
from unilabos.server.scheduler.inventory.sync import build_snapshot
from unilabos.server.scheduler.inventory.material_compat import (
    build_legacy_material_nodes,
)
from unilabos.utils.tracing import install_http_tracing


def create_router(service: InventoryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])

    @router.get("/health", response_model=InventoryHealthResponse)
    def health() -> InventoryHealthResponse:
        return {
            "status": "ok",
            "edge_id": service.edge_id,
            "lab_id": service.lab_id,
        }

    @router.post(
        "/commands",
        response_model=InventoryCommandResult,
        response_model_exclude_none=True,
        responses={409: {"model": InventoryCommandResult}},
    )
    def post_command(
        command: InventoryCommand,
        response: Response,
        _request: Request,
    ) -> InventoryCommandResult:
        """统一 command 入口（与 WS 下发同一执行路径，幂等）."""
        result = execute_command(
            service,
            command,
            trusted_actor="edge:local-api",
        )
        if result.get("error_code") == "version_conflict":
            response.status_code = 409
        return result

    @router.get("/lots", response_model=LotListResponse)
    def list_lots(
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
    ) -> LotListResponse:
        return {"lots": service.store.list_lots(limit)}

    @router.get(
        "/lots/{lot_id}",
        response_model=InventoryLotResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def get_lot(lot_id: str) -> InventoryLotResponse:
        lot = service.store.get_lot(lot_id)
        if lot is None:
            raise HTTPException(status_code=404, detail=f"lot {lot_id} not found")
        return lot

    @router.get("/instances", response_model=InstanceListResponse)
    def list_instances(
        status: Optional[InstanceState] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
    ) -> InstanceListResponse:
        rows = service.store.list_instances(status.value if status else "", limit)
        return {"instances": rows}

    @router.get("/reservations", response_model=ReservationListResponse)
    def list_reservations(
        status: Optional[ReservationState] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
    ) -> ReservationListResponse:
        rows = service.store.list_reservations(status.value if status else "", limit)
        return {"reservations": rows}

    @router.get("/templates", response_model=TemplateListResponse)
    def list_templates(
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
    ) -> TemplateListResponse:
        return {"templates": service.store.list_templates(limit)}

    @router.get(
        "/templates/{template_id}",
        response_model=ResourceTemplateResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def get_template(template_id: str) -> ResourceTemplateResponse:
        template = service.store.get_template(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail=f"template {template_id} not found")
        return template

    @router.get(
        "/instances/{edge_uuid}",
        response_model=InstanceDetailResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def get_instance(edge_uuid: str) -> InstanceDetailResponse:
        inst = service.store.get_instance(edge_uuid)
        if inst is None:
            raise HTTPException(status_code=404, detail=f"instance {edge_uuid} not found")
        relation = service.store.get_relation(edge_uuid)
        content = service.store.get_content(edge_uuid)
        return {**inst, "relation": relation, "content": content}

    @router.get(
        "/reservations/{reservation_id}",
        response_model=InventoryReservationResponse,
        responses={404: {"model": ErrorResponse}},
    )
    def get_reservation(reservation_id: str) -> InventoryReservationResponse:
        reservation = service.store.get_reservation_by_id(reservation_id)
        if reservation is None:
            raise HTTPException(
                status_code=404, detail=f"reservation {reservation_id} not found"
            )
        return reservation

    @router.get(
        "/workflows/{workflow_id}/reservations",
        response_model=WorkflowReservationListResponse,
    )
    def get_reservations(workflow_id: str) -> WorkflowReservationListResponse:
        return {
            "workflow_id": workflow_id,
            "reservations": service.store.reservations_for_workflow(workflow_id),
        }

    @router.get("/relations", response_model=RelationListResponse)
    def list_relations() -> RelationListResponse:
        return {"relations": service.store.list_relations()}

    @router.get("/contents", response_model=ContentListResponse)
    def list_contents() -> ContentListResponse:
        return {"contents": service.store.list_contents()}

    @router.get("/snapshot", response_model=InventorySnapshotResponse)
    def snapshot() -> InventorySnapshotResponse:
        """全量状态导出（云端初次接入/缺口重建 projection 用）."""
        return build_snapshot(service.store)

    @router.get("/ledger", response_model=LedgerListResponse)
    def ledger(
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        after_id: Annotated[int, Query(ge=0)] = 0,
    ) -> LedgerListResponse:
        return {"entries": service.store.list_ledger(after_id, limit)}

    @router.get("/outbox/backlog", response_model=OutboxBacklogResponse)
    def outbox_backlog() -> OutboxBacklogResponse:
        return {
            "max_sequence": service.store.max_outbox_sequence(),
            "acked_sequence": service.store.get_cursor(),
        }

    @router.get("/outbox/events", response_model=OutboxListResponse)
    def outbox_events(
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> OutboxListResponse:
        """按 sequence 顺序读取 outbox 原始行，供本地实体检查器诊断。"""
        return {"events": service.store.pending_outbox(after_sequence, limit)}

    @router.get(
        "/commands/processed",
        response_model=ProcessedCommandListResponse,
    )
    def processed_commands(
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> ProcessedCommandListResponse:
        """最近幂等命令结果；只读且不改变首次执行结果。"""
        return {"commands": service.store.list_processed_commands(limit)}

    @router.get("/sync/cursors", response_model=SyncCursorListResponse)
    def sync_cursors() -> SyncCursorListResponse:
        """同步游标只读视图；游标写入仍由连续 ACK 协议独占。"""
        return {"cursors": service.store.list_cursors()}

    return router


def create_legacy_material_router(service: InventoryService) -> APIRouter:
    """Expose inventory through the query contract already used by HostNode."""

    router = APIRouter(
        prefix="/api/v1/edge/material",
        tags=["inventory-material-compat"],
    )

    @router.post("/query", response_model=LegacyMaterialQueryResponse)
    def query_material(
        request: LegacyMaterialQueryRequest,
    ) -> LegacyMaterialQueryResponse:
        nodes = build_legacy_material_nodes(
            service.store,
            uuids=request.uuids,
            resource_id=request.id,
            with_children=request.with_children,
        )
        return {"code": 0, "data": {"nodes": nodes}}

    return router


def create_app(service: Optional[InventoryService] = None) -> FastAPI:
    """独立运行入口（测试/调试用；生产建议挂到现有 edge app）."""
    from unilabos.server.scheduler.inventory.store import InventoryStore

    if service is None:
        service = InventoryService(InventoryStore(":memory:"))
    app = FastAPI(title="Uni-Lab Edge Inventory", version="0.1.0")
    install_http_tracing(app)
    app.include_router(create_router(service))
    app.include_router(create_legacy_material_router(service))

    @app.exception_handler(InventoryError)
    def _domain_error(_request, exc: InventoryError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"error": str(exc), "code": exc.code})

    return app
