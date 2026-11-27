"""站内物料结果查询及原补报恢复；沿用工站操作台同源访问边界。"""
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from unilabos.app.center_client.client import CenterClientError
from unilabos.persistence.recovery_mode import EvidenceOnlyError
from unilabos.workflow.store import StoreConflict


class Resume(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: StrictInt = Field(ge=1, le=2**53-1)


def create_router(get_runtime: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix='/api/v1/center-material-exports', tags=['Center material recovery'])

    def runtime():
        value = get_runtime()
        if value is None or value.materials is None:
            raise HTTPException(503, '中心物料桥未就绪')
        return value

    @router.get('')
    def listing() -> dict[str, Any]:
        value = runtime()
        with value._poll_lock:
            return {'code': 0, 'data': {'items': value.materials.export_attention_items()}}

    @router.get('/{invocation_uuid}')
    def details(invocation_uuid: UUID) -> dict[str, Any]:
        value = runtime()
        with value._poll_lock:
            items = [i for i in value.materials.export_attention_items() if i['invocation_uuid'] == str(invocation_uuid)]
            if not items:
                raise HTTPException(404, '原调用没有待处理物料结果')
            return {'code': 0, 'data': {'items': items}}

    @router.post('/{invocation_uuid}/{phase}/resume')
    def resume(invocation_uuid: UUID, phase: str, body: Resume) -> dict[str, Any]:
        value = runtime()
        try:
            with value._poll_lock:
                value.store.require_execution()
                item = value.materials.resume_failure(str(invocation_uuid), phase,
                    expected_revision=body.expected_revision)
                notifier = getattr(value, 'notify_attention_changed', None)
                if callable(notifier):
                    notifier()
                value.wake()
                return {'code': 0, 'data': item}
        except (ValueError, CenterClientError, EvidenceOnlyError, StoreConflict) as error:
            raise HTTPException(409, str(error)) from error

    return router
