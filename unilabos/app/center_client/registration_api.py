"""工站已授权控制入口下的容器登记查询和原申请恢复。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from unilabos.app.center_client.client import CenterClientError
from unilabos.app.center_client.container_registration import ContainerRegistrationBridge
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.persistence.recovery_mode import EvidenceOnlyError
from unilabos.workflow.store import StoreConflict


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    barcode_namespace: str = Field(min_length=1, max_length=128)
    barcode: str = Field(min_length=1, max_length=128)
    evidence_refs: list[str] = Field(min_length=1, max_length=32)


def create_router(get_runtime: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/center-containers", tags=["Center container"])

    def bridge(*, execution: bool = True):
        runtime = get_runtime()
        if runtime is None or runtime.handoffs is None:
            raise HTTPException(503, "中心库存桥未就绪")
        if execution:
            runtime.store.require_execution()
        return ContainerRegistrationBridge(runtime.handoffs.client, runtime.handoffs.authority)

    @router.get("/{local_carrier_uuid}/registration")
    def registration(local_carrier_uuid: UUID):
        return {"code": 0, "data": bridge(execution=False).registration(str(local_carrier_uuid))}

    @router.post("/{local_carrier_uuid}/registration")
    def ensure(local_carrier_uuid: UUID, body: Registration):
        try:
            return {
                "code": 0,
                "data": bridge().ensure(local_carrier_material_uuid=str(local_carrier_uuid), **body.model_dump()),
            }
        except (ValueError, CenterClientError, CenterTransportError, EvidenceOnlyError, StoreConflict) as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/{local_carrier_uuid}/material-inputs")
    def material_inputs(local_carrier_uuid: UUID):
        try:
            return {"code": 0, "data": bridge().attest_material_inputs(str(local_carrier_uuid))}
        except (ValueError, CenterClientError, CenterTransportError, EvidenceOnlyError, StoreConflict) as error:
            raise HTTPException(409, str(error)) from error

    return router
