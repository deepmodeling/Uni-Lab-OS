"""OS 本地模式的样品与当前内容物 Backend 同形 HTTP 适配器。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from unilabos.app.scheduler.inventory.backend_response import call
from unilabos.app.scheduler.inventory.content_contract import (
    BackendContainerContentService,
)


class ContentModel(BaseModel):
    """忽略 Backend DTO 后续新增字段，保持 Local 模式向前兼容。"""

    model_config = ConfigDict(extra="ignore")


class SampleRequest(ContentModel):
    material_uuid: UUID
    code: str
    name: str
    quantity: float
    quantity_unit: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class CurrentSubstanceComponentRequest(ContentModel):
    reagent_uuid: UUID
    quantity: float
    quantity_unit: str


class CurrentSubstanceRequest(ContentModel):
    material_uuid: UUID
    name: Optional[str] = None
    components: List[CurrentSubstanceComponentRequest] = Field(default_factory=list)
    quantity: float
    quantity_unit: str
    physical_state: str
    observed_at: Optional[datetime] = None
    expected_revision: Optional[int] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


def create_container_content_router(
    service: BackendContainerContentService,
) -> APIRouter:
    """创建 Sample、CurrentSubstance 及内容物历史路由。"""

    router = APIRouter(tags=["backend-container-content-contract"])

    @router.post("/samples")
    def create_sample(body: SampleRequest) -> JSONResponse:
        return call(
            service.create_sample,
            body.model_dump(mode="json"),
            status_code=201,
        )

    @router.get("/samples")
    def list_samples(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        material_uuid: Optional[UUID] = Query(default=None),
        code: str = Query(default=""),
        name: str = Query(default=""),
        keyword: str = Query(default=""),
        barcode: str = Query(default=""),
    ) -> JSONResponse:
        return call(
            service.list_samples,
            page=page,
            page_size=page_size,
            material_uuid=str(material_uuid) if material_uuid else "",
            code=code,
            name=name,
            keyword=keyword,
            barcode=barcode,
        )

    @router.get("/samples/{sample_uuid}")
    def get_sample(sample_uuid: UUID) -> JSONResponse:
        return call(service.get_sample, str(sample_uuid))

    @router.put("/samples/{sample_uuid}")
    def update_sample(sample_uuid: UUID, body: SampleRequest) -> JSONResponse:
        return call(
            service.update_sample,
            str(sample_uuid),
            body.model_dump(mode="json"),
        )

    @router.delete("/samples/{sample_uuid}")
    def delete_sample(sample_uuid: UUID) -> JSONResponse:
        return call(service.delete_sample, str(sample_uuid))

    @router.post("/current-substances")
    def create_current_substance(
        body: CurrentSubstanceRequest,
    ) -> JSONResponse:
        return call(
            service.create_current_substance,
            body.model_dump(mode="json"),
            status_code=201,
        )

    @router.get("/current-substances")
    def list_current_substances(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        material_uuid: Optional[UUID] = Query(default=None),
    ) -> JSONResponse:
        return call(
            service.list_current_substances,
            page=page,
            page_size=page_size,
            material_uuid=str(material_uuid) if material_uuid else "",
        )

    @router.get("/current-substances/{substance_uuid}")
    def get_current_substance(substance_uuid: UUID) -> JSONResponse:
        return call(service.get_current_substance, str(substance_uuid))

    @router.put("/current-substances/{substance_uuid}")
    def update_current_substance(
        substance_uuid: UUID, body: CurrentSubstanceRequest
    ) -> JSONResponse:
        return call(
            service.update_current_substance,
            str(substance_uuid),
            body.model_dump(mode="json"),
        )

    @router.delete("/current-substances/{substance_uuid}")
    def delete_current_substance(substance_uuid: UUID) -> JSONResponse:
        return call(service.delete_current_substance, str(substance_uuid))

    @router.get("/substance-history/{history_uuid}")
    def get_substance_history(history_uuid: UUID) -> JSONResponse:
        return call(service.get_substance_history, str(history_uuid))

    @router.get("/materials/{material_uuid}/current-substance")
    def get_current_substance_by_material(material_uuid: UUID) -> JSONResponse:
        return call(service.get_current_substance_by_material, str(material_uuid))

    @router.get("/materials/{material_uuid}/substance-history")
    def list_substance_history(
        material_uuid: UUID,
        page: int = Query(default=0),
        page_size: int = Query(default=0),
    ) -> JSONResponse:
        return call(
            service.list_substance_history,
            str(material_uuid),
            page=page,
            page_size=page_size,
        )

    return router


__all__ = ["create_container_content_router"]
