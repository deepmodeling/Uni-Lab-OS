"""FastAPI adapter matching Backend c35d821 resource routes and envelope."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from unilabos.app.scheduler.inventory.backend_contract import (
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
    config_info: List[Any] = Field(default_factory=list)
    cover: Optional[str] = None
    scene: List[Any] = Field(default_factory=list)
    device_params: Dict[str, Any] = Field(default_factory=dict)


class SitePlacementRequest(BackendModel):
    action: str
    site_uuid: Optional[UUID] = None


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
    resource_template_uuid: UUID
    parent_uuid: Optional[UUID] = None
    barcode: str = ""
    name: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
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


def create_backend_resource_router(
    service: BackendResourceService,
    *,
    material_shapes: Sequence[Mapping[str, Any]] = (),
    material_model_catalog: Any = None,
) -> APIRouter:
    """创建前端资源合同路由并附带静态物料外形。

    参数：``service`` 提供公共资源读写；``material_shapes`` 是已由包资产编译器
    校验的静态外形投影；``material_model_catalog`` 只读取本启动代际授权的模型
    资产。返回：不包含 Edge 私有库存路由的 ``APIRouter``。异常：外形项目或模型
    目录形状非法时原样抛出。
    """

    router = APIRouter(prefix="/api/v1", tags=["backend-resource-contract"])
    # ``frozen_material_shapes`` 与调用者容器隔离，保证启动代际不会随请求漂移。
    frozen_material_shapes = tuple(deepcopy(dict(shape)) for shape in material_shapes)

    @router.get("/material-shapes")
    def list_material_shapes() -> JSONResponse:
        """返回工作区包声明的静态物料外形。

        参数：无。返回：Backend 公共信封中的外形项目列表。异常：无。
        """

        return _success({"items": deepcopy(list(frozen_material_shapes))})

    @router.get("/material-models/{asset_path:path}")
    def read_material_model(asset_path: str) -> Response:
        """返回一项工作区 3D 模型资产。

        参数：``asset_path`` 是公共模型路由内的相对路径。返回：带媒体类型、摘要
        和禁止陈旧缓存策略的资产字节。异常：目录未安装或资产未授权时返回 404。
        """

        if material_model_catalog is None:
            raise HTTPException(status_code=404, detail="模型资产目录未安装")
        public_path = f"/api/v1/material-models/{asset_path}"
        try:
            # ``asset`` 是当前工作区启动代际完成边界校验后的不可变读取结果。
            asset = material_model_catalog.read_asset(public_path)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="模型资产未找到") from error
        return Response(
            content=asset.content,
            media_type=asset.media_type,
            headers={
                "Cache-Control": "private, max-age=0, must-revalidate",
                "ETag": f'"{asset.etag}"',
            },
        )

    @router.post("/resource-templates")
    def sync_resource_templates(body: ResourceTemplateSyncRequest) -> JSONResponse:
        return _call(service.sync_resource_templates, body.resources)

    @router.get("/resource-templates")
    def list_resource_templates(
        limit: int = Query(default=0),
        cursor_uuid: Optional[UUID] = Query(default=None),
        keyword: str = Query(default=""),
        resource_type: str = Query(default=""),
    ) -> JSONResponse:
        return _call(
            service.list_resource_templates,
            limit=limit,
            cursor_uuid=str(cursor_uuid) if cursor_uuid else None,
            keyword=keyword,
            resource_type=resource_type,
        )

    @router.get("/resource-templates/{template_uuid}")
    def get_resource_template(template_uuid: UUID) -> JSONResponse:
        return _call(service.get_resource_template, str(template_uuid))

    @router.put("/resource-templates/{template_uuid}")
    def update_resource_template(
        template_uuid: UUID, body: ResourceTemplateUpdateRequest
    ) -> JSONResponse:
        try:
            template_identity = str(template_uuid)
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
    def delete_resource_template(template_uuid: UUID) -> JSONResponse:
        return _call(service.delete_resource_template, str(template_uuid))

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
        resource_template_uuid: Optional[UUID] = Query(default=None),
    ) -> JSONResponse:
        return _call(
            service.list_materials,
            page=page,
            page_size=page_size,
            name=name,
            barcode=barcode,
            resource_template_uuid=(
                str(resource_template_uuid) if resource_template_uuid else None
            ),
        )

    @router.get("/materials/graph")
    def get_material_graph() -> JSONResponse:
        return _call(service.material_graph)

    @router.get("/materials/{material_uuid}")
    def get_material(material_uuid: UUID) -> JSONResponse:
        return _call(service.get_material, str(material_uuid))

    @router.put("/materials/{material_uuid}")
    def update_material(material_uuid: UUID, body: MaterialRequest) -> JSONResponse:
        values = body.model_dump(mode="json")
        values["_relative_position_specified"] = (
            "relative_position" in body.model_fields_set
        )
        return _call(
            service.update_material,
            str(material_uuid),
            values,
        )

    @router.delete("/materials/{material_uuid}")
    def delete_material(material_uuid: UUID) -> JSONResponse:
        return _call(service.delete_material, str(material_uuid))

    @router.get("/materials/{material_uuid}/sites")
    def list_sites(material_uuid: UUID) -> JSONResponse:
        try:
            material_identity = str(material_uuid)
            service.get_material(material_identity)
            return _success(service.list_sites(material_identity))
        except BackendContractError as error:
            return _error(error)

    @router.post("/materials/{material_uuid}/states")
    def append_material_state(
        material_uuid: UUID, body: MaterialStateRequest
    ) -> JSONResponse:
        return _call(
            service.append_material_state,
            str(material_uuid),
            body.model_dump(mode="json"),
            status_code=201,
        )

    @router.get("/materials/{material_uuid}/states")
    def list_material_states(
        material_uuid: UUID,
        before_time: Optional[str] = Query(default=None),
        before_uuid: Optional[str] = Query(default=None),
        limit: int = Query(default=0),
    ) -> JSONResponse:
        return _call(
            service.list_material_states,
            str(material_uuid),
            before_time=before_time,
            before_uuid=before_uuid,
            limit=limit,
        )

    @router.get("/materials/{material_uuid}/states/latest")
    def latest_material_state(material_uuid: UUID) -> JSONResponse:
        return _call(service.latest_material_state, str(material_uuid))

    @router.get("/material-states/{state_uuid}")
    def get_material_state(state_uuid: UUID) -> JSONResponse:
        return _call(service.get_material_state, str(state_uuid))

    @router.get("/sites/{site_uuid}")
    def get_site(site_uuid: UUID) -> JSONResponse:
        return _call(service.get_site, str(site_uuid))

    return router


def install_backend_resource_api(
    app: FastAPI,
    service: BackendResourceService,
    *,
    material_shapes: Sequence[Mapping[str, Any]] = (),
    material_model_catalog: Any = None,
) -> None:
    """安装公共资源路由与 Backend 校验信封。

    参数：``app`` 是产品 FastAPI 应用；``service`` 是资源合同服务；
    ``material_shapes`` 是工作区包资产的静态公共投影；
    ``material_model_catalog`` 是同代受限模型目录。返回：无。
    异常：路由、外形或模型装配错误原样抛出。
    """

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

    app.include_router(
        create_backend_resource_router(
            service,
            material_shapes=material_shapes,
            material_model_catalog=material_model_catalog,
        )
    )


__all__ = [
    "create_backend_resource_router",
    "install_backend_resource_api",
]
