"""Backend-shaped 资源路由与响应 envelope 的 FastAPI 适配器。

共享资源表语义已复核至 Backend ``d552078``；新增物料台账等尚未闭环的能力不会在这里
伪装为已支持。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from unilabos.server.scheduler.inventory.backend_contract import (
    BackendContractError,
    BackendResourceService,
)


class BackendModel(BaseModel):
    """Gin binding-compatible model: recognized fields are typed, extras ignored."""

    model_config = ConfigDict(extra="ignore")


class ResourceTemplateSyncRequest(BackendModel):
    resources: List[Dict[str, Any]] = Field(default_factory=list)


class ResourceTemplateUpdateRequest(BackendModel):
    display_name: str = ""
    description: Optional[str] = None
    icon: Optional[str] = None
    registry_type: str = "resource"
    model: Dict[str, Any] = Field(default_factory=dict)
    class_: Dict[str, Any] = Field(default_factory=dict, alias="class")
    handles: List[Dict[str, Any]] = Field(default_factory=list)
    init_param_schema: Optional[Dict[str, Any]] = None
    category: List[Any] = Field(default_factory=list)
    tags: Optional[List[str]] = None
    config_info: List[Any] = Field(default_factory=list)
    cover: Optional[str] = None
    scene: List[Any] = Field(default_factory=list)
    device_params: Dict[str, Any] = Field(default_factory=dict)


class SitePlacementRequest(BackendModel):
    action: str
    site_uuid: Optional[str] = None


class RelativePositionRequest(BackendModel):
    position_x: float = 0
    position_y: float = 0
    position_z: float = 0
    depth: float = Field(default=0, ge=0)
    length: float = Field(default=0, ge=0)
    width: float = Field(default=0, ge=0)
    scale_x: float = Field(default=1, gt=0)
    scale_y: float = Field(default=1, gt=0)
    scale_z: float = Field(default=1, gt=0)
    rotation_x: float = 0
    rotation_y: float = 0
    rotation_z: float = 0
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class MaterialRequest(BackendModel):
    resource_template_uuid: str
    parent_uuid: Optional[str] = None
    barcode: str = ""
    name: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    data: Dict[str, Any] = Field(default_factory=dict)
    relative_position: Optional[RelativePositionRequest] = None
    site_placement: Optional[SitePlacementRequest] = None


class MaterialUpdateRequest(BackendModel):
    """Backend partial update DTO.

    ``class``/``type``/``data`` intentionally do not exist here.  Unknown
    legacy fields are ignored by ``BackendModel`` but never mutate canonical
    Material facts.
    """

    resource_template_uuid: Optional[str] = None  # legacy, immutable
    parent_uuid: Optional[str] = None
    barcode: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    relative_position: Optional[RelativePositionRequest] = None
    site_placement: Optional[SitePlacementRequest] = None


class MaterialStateRequest(BackendModel):
    status: Optional[str] = None
    state_data: Dict[str, Any] = Field(default_factory=dict)
    source: Optional[str] = None
    observed_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


def _success(data: Any = None, *, status_code: int = 200) -> JSONResponse:
    content: Dict[str, Any] = {"code": 0}
    if data is not None:
        content["data"] = data
    return JSONResponse(status_code=status_code, content=content)


def _error(error: BackendContractError) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"code": error.code, "error": {"msg": error.message}},
    )


def _call(callback, *args, status_code: int = 200, **kwargs) -> JSONResponse:
    try:
        return _success(callback(*args, **kwargs), status_code=status_code)
    except BackendContractError as error:
        return _error(error)


def create_backend_resource_router(service: BackendResourceService) -> APIRouter:
    """Create the frontend-facing Resource adapter; Edge-only routes stay separate."""

    router = APIRouter(prefix="/api/v1", tags=["backend-resource-contract"])

    @router.post("/resource-templates")
    def sync_resource_templates(body: ResourceTemplateSyncRequest) -> JSONResponse:
        return _call(service.sync_resource_templates, body.resources)

    @router.get("/resource-templates")
    def list_resource_templates(
        limit: int = Query(default=0),
        cursor_uuid: Optional[str] = Query(default=None),
        keyword: str = Query(default=""),
        resource_type: str = Query(default=""),
    ) -> JSONResponse:
        return _call(
            service.list_resource_templates,
            limit=limit,
            cursor_uuid=cursor_uuid,
            keyword=keyword,
            resource_type=resource_type,
        )

    @router.get("/resource-templates/{template_uuid}")
    def get_resource_template(template_uuid: str) -> JSONResponse:
        return _call(service.get_resource_template, template_uuid)

    @router.put("/resource-templates/{template_uuid}")
    def update_resource_template(
        template_uuid: str, body: ResourceTemplateUpdateRequest
    ) -> JSONResponse:
        try:
            template_identity = template_uuid
            current = service.get_resource_template(template_identity)
            definition = body.model_dump(by_alias=True, mode="json")
            if "handles" not in body.model_fields_set:
                definition.pop("handles", None)
            definition["id"] = current["name"]
            service.sync_resource_templates([definition])
            return _success(service.get_resource_template(template_identity))
        except BackendContractError as error:
            return _error(error)

    @router.delete("/resource-templates/{template_uuid}")
    def delete_resource_template(template_uuid: str) -> JSONResponse:
        return _call(service.delete_resource_template, template_uuid)

    @router.post("/materials")
    def create_material(body: MaterialRequest) -> JSONResponse:
        return _call(
            service.create_material,
            body.model_dump(mode="json"),
            status_code=201,
        )

    @router.get("/materials")
    def list_materials(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        name: str = Query(default=""),
        barcode: str = Query(default=""),
        resource_template_uuid: Optional[str] = Query(default=None),
        with_children: bool = Query(default=False),
    ) -> JSONResponse:
        return _call(
            service.list_materials,
            page=page,
            page_size=page_size,
            name=name,
            barcode=barcode,
            resource_template_uuid=resource_template_uuid,
            with_children=with_children,
        )

    @router.get("/materials/graph")
    def get_material_graph() -> JSONResponse:
        return _call(service.material_graph)

    @router.get("/materials/{material_uuid}")
    def get_material(material_uuid: str) -> JSONResponse:
        return _call(service.get_material, material_uuid)

    @router.put("/materials/{material_uuid}")
    def update_material(
        material_uuid: str, body: MaterialUpdateRequest
    ) -> JSONResponse:
        values = body.model_dump(mode="json", exclude_unset=True)
        values["_relative_position_specified"] = (
            "relative_position" in body.model_fields_set
        )
        values["_site_placement_specified"] = (
            "site_placement" in body.model_fields_set
            and body.site_placement is not None
        )
        return _call(
            service.update_material,
            material_uuid,
            values,
        )

    @router.delete("/materials/{material_uuid}")
    def delete_material(material_uuid: str) -> JSONResponse:
        return _call(service.delete_material, material_uuid)

    @router.get("/materials/{material_uuid}/sites")
    def list_sites(material_uuid: str) -> JSONResponse:
        try:
            material_identity = material_uuid
            service.get_material(material_identity)
            return _success(service.list_sites(material_identity))
        except BackendContractError as error:
            return _error(error)

    @router.post("/materials/{material_uuid}/states")
    def append_material_state(
        material_uuid: str, body: MaterialStateRequest
    ) -> JSONResponse:
        return _call(
            service.append_material_state,
            material_uuid,
            body.model_dump(mode="json"),
            status_code=201,
        )

    @router.get("/materials/{material_uuid}/states")
    def list_material_states(
        material_uuid: str,
        before_time: Optional[str] = Query(default=None),
        before_uuid: Optional[str] = Query(default=None),
        limit: int = Query(default=0),
    ) -> JSONResponse:
        return _call(
            service.list_material_states,
            material_uuid,
            before_time=before_time,
            before_uuid=before_uuid,
            limit=limit,
        )

    @router.get("/materials/{material_uuid}/states/latest")
    def latest_material_state(material_uuid: str) -> JSONResponse:
        return _call(service.latest_material_state, material_uuid)

    @router.get("/material-states/{state_uuid}")
    def get_material_state(state_uuid: str) -> JSONResponse:
        return _call(service.get_material_state, state_uuid)

    @router.get("/sites/{site_uuid}")
    def get_site(site_uuid: str) -> JSONResponse:
        return _call(service.get_site, site_uuid)

    return router


def install_backend_resource_api(
    app: FastAPI, service: BackendResourceService
) -> None:
    """Install the shared routes and Backend validation envelope once."""

    @app.exception_handler(RequestValidationError)
    async def backend_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        shared_prefixes = (
            "/api/v1/resource-templates",
            "/api/v1/materials",
            "/api/v1/material-states",
            "/api/v1/sites",
            "/api/v1/workflows",
            "/api/v1/workflow-tasks",
            "/api/v1/workflow-node-jobs",
        )
        if any(request.url.path.startswith(prefix) for prefix in shared_prefixes):
            return _error(BackendContractError(1000, "Invalid request parameter"))
        return await request_validation_exception_handler(request, error)

    app.include_router(create_backend_resource_router(service))


__all__ = [
    "create_backend_resource_router",
    "install_backend_resource_api",
]
