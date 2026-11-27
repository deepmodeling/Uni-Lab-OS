"""工站交接操作入口；所有包和条码均从已认证中心重新取得。"""
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from unilabos.app.center_client.handoff import HandoffBridge
    from unilabos.app.center_client.runtime import CenterRuntime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from unilabos.app.center_client.client import CenterClientError
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.persistence.recovery_mode import EvidenceOnlyError
from unilabos.workflow.store import StoreConflict


class Ready(BaseModel):
    model_config = ConfigDict(extra="forbid")
    physical_ready: StrictBool
    evidence_refs: list[str] = Field(min_length=1, max_length=100)


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plans: dict[str, dict[str, Any]] = Field(min_length=1, max_length=100)


class Receive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    barcode_namespace: str
    barcode: str
    evidence_refs: list[str] = Field(min_length=1, max_length=100)


class VerifyPresence(Receive):
    physical_present: StrictBool


class VerifyDeparture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    physical_absent: StrictBool
    evidence_refs: list[str] = Field(min_length=1, max_length=32)


class VerifyMovement(VerifyPresence):
    arrival_plan: dict[str, Any] | None = None


class RefreshSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")


def create_router(get_runtime: Callable[[], "CenterRuntime | None"]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/center-handoffs", tags=["Center handoff"])

    def bridge(*, execution: bool = True) -> "HandoffBridge":
        runtime = get_runtime()
        if runtime is None or runtime.handoffs is None:
            raise HTTPException(503, "中心库存桥未就绪")
        if execution:
            runtime.store.require_execution()
        return runtime.handoffs

    def operation(work: Callable[[], Any]) -> dict[str, Any]:
        try:
            return {"code": 0, "data": work()}
        except (ValueError, CenterClientError, CenterTransportError, EvidenceOnlyError, StoreConflict) as error:
            # 业务错误可纠正；私钥、请求头和机器密钥不进入响应。
            raise HTTPException(409, str(error)) from error

    @router.post("/transfer-plan/validate")
    async def validate_plan(request: Request) -> dict[str, Any]:
        # 只读也必须认证：当前会话 UUID 和浏览器 Origin 不是调用凭据。
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not values[0].startswith("Bearer ") or not values[0][7:] or len(values[0]) > 4103:
            raise HTTPException(401, "搬运检查缺少中心授权")
        import json

        from unilabos.center_protocol.placement import _pairs
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "搬运选择请求过大")
        try:
            body = json.loads(raw, object_pairs_hook=_pairs)
        except (ValueError, UnicodeError) as error:
            raise HTTPException(422, "搬运选择 JSON 无效") from error
        b=bridge(execution=False)
        try:
            b.authorize_transfer_plan(body,values[0][7:])
        except (ValueError,CenterClientError,CenterTransportError):
            raise HTTPException(401,"中心未授权此搬运检查") from None
        return operation(lambda: b.validate_transfer_plan(body))
    class TransportReconciliation(BaseModel):
        model_config = ConfigDict(extra="forbid")
        allocation_uuid: UUID
        physical_safe: StrictBool
        evidence_refs: list[str] = Field(min_length=1,max_length=32)

    @router.post("/{handoff_uuid}/transport-reconciliation")
    def reconcile_transport(handoff_uuid: UUID, body: TransportReconciliation) -> dict[str,Any]:
        def work():
            runtime=get_runtime()
            if runtime is None or runtime.transport_preparations is None:
                raise ValueError("物流车辆准备服务尚未就绪")
            runtime.store.require_execution()
            return runtime.transport_preparations.reconcile(str(handoff_uuid),allocation_uuid=str(body.allocation_uuid),
                physical_safe=body.physical_safe,evidence_refs=body.evidence_refs)
        return operation(work)

    @router.get("")
    def recent() -> dict[str, Any]:
        runtime = get_runtime()
        if runtime is None:
            raise HTTPException(503, "中心运行时未就绪")
        return {"code": 0, "data": {"handoff_uuids": runtime.store.recent_handoff_ids()}}

    @router.get("/attention/current")
    def attention() -> dict[str, Any]:
        from unilabos.app.scheduler.inventory.handoff_attention import attention_handoffs
        return {"code": 0, "data": {"items": attention_handoffs(bridge(execution=False).authority)}}

    @router.get("/{handoff_uuid}")
    def detail(handoff_uuid: UUID) -> dict[str, Any]:
        from unilabos.app.scheduler.inventory.handoff_attention import handoff_details
        rows = handoff_details(bridge(execution=False).authority, str(handoff_uuid))
        if not rows:
            raise HTTPException(404, "本站没有该交接的持久记录")
        return {"code": 0, "data": rows[0]}

    @router.post("/{handoff_uuid}/source-ready")
    def ready(handoff_uuid: UUID, body: Ready) -> dict[str, Any]:
        return operation(lambda: bridge().freeze_source(str(handoff_uuid), **body.model_dump()))

    @router.post("/{handoff_uuid}/refresh")
    def refresh(handoff_uuid: UUID, body: RefreshSnapshot | None = None) -> dict[str, Any]:
        return operation(lambda: bridge().refresh_snapshot(str(handoff_uuid)))

    @router.post("/{handoff_uuid}/prepare")
    def prepare(handoff_uuid: UUID, body: Prepare) -> dict[str, Any]:
        def work() -> Any:
            b = bridge()
            package = b.client.handoff_package(str(handoff_uuid), None, b.store.event_epoch_uuid)
            details = {item["container_uuid"]: b.client.container(item["container_uuid"], b.store.event_epoch_uuid,
                       handoff_uuid=str(handoff_uuid)) for item in package["payload"]["items"]}
            return b.authority.prepare_incoming(package, container_details=details, plans=body.plans)
        return operation(work)

    @router.post("/{handoff_uuid}/items/{item_uuid}/receive")
    def receive(handoff_uuid: UUID, item_uuid: UUID, body: Receive) -> dict[str, Any]:
        def work() -> Any:
            b = bridge()
            package = b.client.handoff_package(str(handoff_uuid), None, b.store.event_epoch_uuid)
            b.authority.apply_center_snapshot(package)
            return b.authority.receive(str(handoff_uuid), str(item_uuid), **body.model_dump())
        return operation(work)

    @router.post("/{handoff_uuid}/items/{item_uuid}/verify-source-presence")
    def verify_presence(handoff_uuid: UUID, item_uuid: UUID, body: VerifyPresence) -> dict[str, Any]:
        return operation(lambda: bridge().verify_source_presence(str(handoff_uuid),
            handoff_item_uuid=str(item_uuid), **body.model_dump()))

    @router.post("/{handoff_uuid}/items/{item_uuid}/verify-source-departure")
    def verify_departure(handoff_uuid: UUID, item_uuid: UUID, body: VerifyDeparture) -> dict[str, Any]:
        return operation(lambda: bridge().verify_source_departure(str(handoff_uuid),
            handoff_item_uuid=str(item_uuid), **body.model_dump()))

    @router.post("/{handoff_uuid}/items/{item_uuid}/verify-movement")
    def verify_movement(handoff_uuid: UUID, item_uuid: UUID, body: VerifyMovement) -> dict[str, Any]:
        return operation(lambda: bridge().verify_movement(str(handoff_uuid),
            handoff_item_uuid=str(item_uuid), **body.model_dump()))

    return router
