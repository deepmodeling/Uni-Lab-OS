"""显式启用的本站手工交接操作入口；不接受客户端提供的中心可信包。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from unilabos.app.center_client.handoff import HandoffBridge
from unilabos.app.scheduler.inventory.dispatch_admission import InventoryMutationConflict
from unilabos.app.scheduler.inventory.handoff import HandoffInventoryError
from unilabos.app.scheduler.inventory.ingress import IngressReservationError


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FreezeRequest(_Request):
    physical_ready: bool
    evidence_refs: list[str] = Field(min_length=1, max_length=32)


class ReceiveRequest(_Request):
    barcode_namespace: str
    barcode: str
    evidence_refs: list[str] = Field(min_length=1, max_length=32)


class SourceVerificationRequest(ReceiveRequest):
    physical_present: bool


class MovementVerificationRequest(SourceVerificationRequest):
    arrival_plan: dict[str, Any] | None = None


class SourceDepartureRequest(_Request):
    physical_absent: bool
    evidence_refs: list[str] = Field(min_length=1, max_length=32)


class BindRequest(_Request):
    local_carrier_material_uuid: str
    barcode_namespace: str
    barcode: str


def create_handoff_router(bridge: HandoffBridge, *, operator_dependency: Callable[..., Any]) -> APIRouter:
    """调用方必须提供操作者鉴权依赖；不自动开放任意库存控制面。"""
    if not callable(operator_dependency):
        raise ValueError("交接操作必须提供本站操作者鉴权依赖")
    router = APIRouter(
        prefix="/center-handoffs", tags=["center-handoffs"], dependencies=[Depends(operator_dependency)]
    )

    def call(operation: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except (HandoffInventoryError, IngressReservationError, InventoryMutationConflict) as error:
            raise HTTPException(
                409, detail={"code": getattr(error, "code", "inventory_claimed"), "message": str(error)}
            ) from error
        except ValueError as error:
            raise HTTPException(422, detail=str(error)) from error

    @router.post("/transfer-plan/validate")
    async def validate_plan(request: Request) -> dict[str, Any]:
        # 保留重复键检测；不能先让普通 JSON 解析覆盖前一个相同字段。
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "搬运选择请求过大")
        import json

        from unilabos.center_protocol.placement import _pairs
        try:
            body = json.loads(raw, object_pairs_hook=_pairs)
        except (ValueError, UnicodeError) as error:
            raise HTTPException(422, "搬运选择 JSON 无效") from error
        return call(bridge.validate_transfer_plan, body)

    @router.post("/containers/{container_uuid}/bind")
    def bind(container_uuid: str, body: BindRequest) -> dict[str, Any]:
        return call(bridge.authority.bind_container, container_uuid=container_uuid, **body.model_dump())

    @router.post("/{handoff_uuid}/release")
    def release(handoff_uuid: str, body: FreezeRequest) -> dict[str, Any]:
        return call(bridge.freeze_source, handoff_uuid, **body.model_dump())

    @router.post("/{handoff_uuid}/items/{handoff_item_uuid}/receive")
    def receive(handoff_uuid: str, handoff_item_uuid: str, body: ReceiveRequest) -> dict[str, Any]:
        return call(bridge.authority.receive, handoff_uuid, handoff_item_uuid, **body.model_dump())

    @router.post("/{handoff_uuid}/items/{handoff_item_uuid}/verify-source-presence")
    def verify_source_presence(handoff_uuid: str, handoff_item_uuid: str, body: SourceVerificationRequest) -> dict[str, Any]:
        return call(bridge.verify_source_presence, handoff_uuid,
                    handoff_item_uuid=handoff_item_uuid, **body.model_dump())

    @router.post("/{handoff_uuid}/items/{handoff_item_uuid}/verify-source-departure")
    def verify_source_departure(handoff_uuid: str, handoff_item_uuid: str, body: SourceDepartureRequest) -> dict[str, Any]:
        return call(bridge.verify_source_departure, handoff_uuid,
                    handoff_item_uuid=handoff_item_uuid, **body.model_dump())

    @router.post("/{handoff_uuid}/items/{handoff_item_uuid}/verify-movement")
    def verify_movement(handoff_uuid: str, handoff_item_uuid: str, body: MovementVerificationRequest) -> dict[str, Any]:
        return call(bridge.verify_movement, handoff_uuid,
                    handoff_item_uuid=handoff_item_uuid, **body.model_dump())

    return router
