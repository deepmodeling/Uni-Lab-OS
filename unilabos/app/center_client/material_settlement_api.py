"""取消后的物料结果核实入口；真实事实由后端读取，不接受前端伪造终态或释放标记。"""
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from unilabos.app.center_client.client import CenterClientError
from unilabos.app.center_client.material_settlement import settle_material_cancellation
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.persistence.recovery_mode import EvidenceOnlyError
from unilabos.workflow.store import StoreConflict


class Settlement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(min_length=1, max_length=32)

class RecordedCancellation(Settlement):
    expected_local_facts_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    expected_center_evidence_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')

def create_router(get_runtime: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix='/api/v1/center-material-settlements', tags=['Center material cancellation'])
    def runtime():
        value = get_runtime()
        if value is None or value.materials is None or value.handoffs is None:
            raise HTTPException(503, '中心物料结算桥未就绪')
        return value
    @router.get('/{invocation_uuid}')
    def detail(invocation_uuid: UUID):
        value = runtime()
        with value._poll_lock:
            return {'code':0, 'data':value.materials.workflow.center_material_settlement(str(invocation_uuid))}
    @router.post('/{invocation_uuid}')
    def settle(invocation_uuid: UUID, body: Settlement):
        value = runtime()
        try:
            with value._poll_lock:
                state = settle_material_cancellation(value, str(invocation_uuid), **body.model_dump())
                notifier = getattr(value, 'notify_attention_changed', None)
                if callable(notifier): notifier()
                value.wake()
                return {'code':0, 'data':state}
        except (ValueError, CenterClientError, CenterTransportError, StoreConflict, EvidenceOnlyError) as error:
            raise HTTPException(409, str(error)) from error

    @router.get('/{invocation_uuid}/recorded-cancellation')
    def recorded_cancellation_detail(invocation_uuid: UUID):
        from unilabos.app.center_client.recorded_cancellation import inspect_recorded_cancellation
        value = runtime()
        try:
            with value._poll_lock:
                return {'code': 0, 'data': inspect_recorded_cancellation(value, str(invocation_uuid))}
        except (ValueError, CenterClientError, CenterTransportError, StoreConflict, EvidenceOnlyError) as error:
            raise HTTPException(409, str(error)) from error

    @router.post('/{invocation_uuid}/recorded-cancellation')
    def reconcile_recorded_cancellation(invocation_uuid: UUID, body: RecordedCancellation):
        from unilabos.app.center_client.recorded_cancellation import reconcile_recorded_cancellation as reconcile
        value = runtime()
        try:
            with value._poll_lock:
                state = reconcile(value, str(invocation_uuid), **body.model_dump())
                notifier = getattr(value, 'notify_attention_changed', None)
                if callable(notifier):
                    notifier()
                value.wake()
                return {'code': 0, 'data': state}
        except (ValueError, CenterClientError, CenterTransportError, StoreConflict, EvidenceOnlyError) as error:
            raise HTTPException(409, str(error)) from error
    return router
