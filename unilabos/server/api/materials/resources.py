"""V1.0 资源合同路由：资源模板、物料、库位、物料状态历史、物料外形与模型资产。

路径与 DTO 以 V1（``app/scheduler/inventory/backend_api.py``）为准，统一信封（资源口径）。
``/materials/{material_uuid}`` 等 UUID 路由必须在 dev 的字面量路由（``/materials/events``、
``/materials/executions`` …）之后挂载：:func:`install_v1_resource_api` 由 ``install_materials_api``
在 dev 路由之后调用。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unilabos.server.api.envelope import (
    RESOURCE,
    TEMPLATE_DEFINITION_INVALID,
    ApiError,
    envelope_router,
    raw_response,
)
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.materials.core import MaterialValidationError
from unilabos.server.services.materials.resource_contract import (
    MAX_SITE_IDENTITY_RESOLUTION_IDS,
    ResourceContract,
)


class V1Model(BaseModel):
    """V1 请求体：认识的字段做类型校验，未知扩展字段忽略（Gin binding 同口径）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ResourceTemplateSyncRequest(V1Model):
    resources: List[Dict[str, Any]] = Field(default_factory=list)


class ResourceTemplateUpdateRequest(V1Model):
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
    available_sites: List[Dict[str, Any]] = Field(default_factory=list)
    cover: Optional[str] = None
    scene: List[Any] = Field(default_factory=list)
    device_params: Dict[str, Any] = Field(default_factory=dict)


class SitePlacementRequest(V1Model):
    action: Literal["place", "remove"]
    site_uuid: Optional[str] = None

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class RelativePositionRequest(V1Model):
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


class MaterialReagentRequest(V1Model):
    """创建容器时内联装入的试剂（不含容器身份）。"""

    reagent_info_uuid: Optional[str] = None
    cas: str = ""
    physical_state: str = "unknown"
    density_g_per_ml: Optional[float] = None
    concentration_value: Optional[float] = None
    concentration_unit: Optional[str] = None
    quantity: float
    quantity_unit: str
    source: Optional[str] = None
    observed_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    container_capacity: Optional[Dict[str, Any]] = None
    expected_material_revision: Optional[int] = None


class MaterialCreateRequest(V1Model):
    resource_template_uuid: str
    parent_uuid: Optional[str] = None
    barcode: str = ""
    name: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    relative_position: Optional[RelativePositionRequest] = None
    site_placement: Optional[SitePlacementRequest] = None
    reagent: Optional[MaterialReagentRequest] = None


class MaterialUpdateRequest(V1Model):
    """三态更新：未出现或显式 ``null`` 的字段保持原值。"""

    resource_template_uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    barcode: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    relative_position: Optional[RelativePositionRequest] = None
    site_placement: Optional[SitePlacementRequest] = None
    expected_revision: Optional[int] = Field(default=None, ge=0)


class MaterialStateRequest(V1Model):
    status: Optional[str] = None
    state_data: Dict[str, Any] = Field(default_factory=dict)
    source: Optional[str] = None
    observed_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class SiteIdentityResolveRequest(V1Model):
    source_node_ids: List[str] = Field(max_length=MAX_SITE_IDENTITY_RESOLUTION_IDS)

    @field_validator("source_node_ids", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        result: list[Any] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                result.append(item)
                continue
            text = item.strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result


def create_v1_resource_router(
    service: MaterialsService,
    *,
    material_shapes: Sequence[Mapping[str, Any]] = (),
    material_model_catalog: Any = None,
) -> APIRouter:
    contract = ResourceContract(service)
    router = envelope_router(prefix="/api/v1", tags=["resource-contract"], profile=RESOURCE)
    frozen_shapes = tuple(deepcopy(dict(shape)) for shape in material_shapes)

    # ── 资源模板 ──────────────────────────────────────────────

    @router.post("/resource-templates")
    def sync_resource_templates(body: ResourceTemplateSyncRequest) -> Any:
        """同步一批注册表资源定义，返回 ``{templates:[{uuid,name}]}`` 稳定身份回执。"""

        try:
            return contract.sync_resource_templates(body.resources)
        except MaterialValidationError as exc:
            raise ApiError(TEMPLATE_DEFINITION_INVALID, str(exc)) from None

    @router.get("/resource-templates")
    def list_resource_templates(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        keyword: str = Query(default=""),
        resource_type: str = Query(default=""),
        name: Optional[str] = Query(default=None, description="dev 扩展：按模板 name 精确筛选"),
        detail: bool = Query(default=False, description="dev 扩展：条目返回完整模板详情（Host 同步用）"),
        include_definition: bool = Query(default=False, description="dev 扩展：详情附带 registry 全量 definition"),
    ) -> Any:
        return contract.list_resource_templates(
            page=page, page_size=page_size, keyword=keyword, resource_type=resource_type,
            name=name, include_definition=include_definition, detail=detail or include_definition,
        )

    @router.get("/resource-templates/{template_uuid}")
    def get_resource_template(template_uuid: str) -> Any:
        return contract.get_resource_template(template_uuid)

    @router.put("/resource-templates/{template_uuid}")
    def update_resource_template(template_uuid: str, body: ResourceTemplateUpdateRequest) -> Any:
        values = body.model_dump(by_alias=True, mode="json")
        specified = {("class" if key == "class_" else key) for key in body.model_fields_set}
        try:
            return contract.update_resource_template(template_uuid, values, specified)
        except MaterialValidationError as exc:
            raise ApiError(TEMPLATE_DEFINITION_INVALID, str(exc)) from None

    @router.delete("/resource-templates/{template_uuid}")
    def delete_resource_template(template_uuid: str) -> None:
        contract.delete_resource_template(template_uuid)

    # ── 物料 ──────────────────────────────────────────────────

    @router.post("/materials", status_code=201)
    def create_material(body: MaterialCreateRequest) -> Any:
        return contract.create_material(body.model_dump(mode="json"))

    @router.get("/materials")
    def list_materials(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        name: str = Query(default=""),
        barcode: str = Query(default=""),
        resource_template_uuid: Optional[str] = Query(default=None),
    ) -> Any:
        return contract.list_materials(page=page, page_size=page_size, name=name, barcode=barcode,
                                       resource_template_uuid=resource_template_uuid or None)

    @router.get("/materials/graph")
    def material_graph() -> Any:
        return contract.material_graph()

    @router.post("/materials/sites/resolve-identities")
    def resolve_site_identities(body: SiteIdentityResolveRequest) -> Any:
        return contract.resolve_site_identities(body.source_node_ids)

    @router.get("/materials/{material_uuid}")
    def get_material(material_uuid: str) -> Any:
        return contract.material_detail(material_uuid)

    @router.put("/materials/{material_uuid}")
    def update_material(material_uuid: str, body: MaterialUpdateRequest) -> Any:
        return contract.update_material(material_uuid, body.model_dump(mode="json", exclude_unset=True), body.model_fields_set)

    @router.delete("/materials/{material_uuid}")
    def delete_material(
        material_uuid: str,
        scope: Literal["material_only", "material_with_contents"] = Query(default="material_only"),
    ) -> None:
        contract.delete_material(material_uuid, scope=scope)

    @router.get("/materials/{material_uuid}/ledger")
    def material_ledger(material_uuid: str, page: int = Query(default=1), page_size: int = Query(default=50)) -> Any:
        return contract.list_material_ledger(material_uuid, page=page, page_size=page_size)

    @router.get("/materials/{material_uuid}/sites")
    def material_sites(material_uuid: str) -> Any:
        return contract.list_sites(material_uuid)

    @router.post("/materials/{material_uuid}/states", status_code=201)
    def append_material_state(material_uuid: str, body: MaterialStateRequest) -> Any:
        return contract.append_material_state(material_uuid, body.model_dump(mode="json"))

    @router.get("/materials/{material_uuid}/states")
    def list_material_states(
        material_uuid: str,
        before_time: Optional[str] = Query(default=None),
        before_uuid: Optional[str] = Query(default=None),
        limit: int = Query(default=0),
    ) -> Any:
        return contract.list_material_states(material_uuid, before_time=before_time, before_uuid=before_uuid, limit=limit)

    @router.get("/materials/{material_uuid}/states/latest")
    def latest_material_state(material_uuid: str) -> Any:
        return contract.latest_material_state(material_uuid)

    @router.get("/material-states/{state_uuid}")
    def get_material_state(state_uuid: str) -> Any:
        return contract.get_material_state(state_uuid)

    @router.get("/sites/{site_uuid}")
    def get_site(site_uuid: str) -> Any:
        return contract.get_site(site_uuid)

    # ── 物料外形与模型资产（工作区包声明，只读） ───────────────────

    @router.get("/material-shapes")
    def list_material_shapes() -> Any:
        return {"items": deepcopy(list(frozen_shapes))}

    @router.get("/material-models/{asset_path:path}", response_class=Response)
    @raw_response
    def read_material_model(asset_path: str) -> Response:
        """工作区 3D 模型资产（字节流，不包信封）；目录未装配或资产未授权时 404。"""

        if material_model_catalog is None:
            raise HTTPException(status_code=404, detail="模型资产目录未安装")
        try:
            asset = material_model_catalog.read_asset(f"/api/v1/material-models/{asset_path}")
        except KeyError as error:
            raise HTTPException(status_code=404, detail="模型资产未找到") from error
        return Response(
            content=asset.content,
            media_type=asset.media_type,
            headers={"Cache-Control": "private, max-age=0, must-revalidate", "ETag": f'"{asset.etag}"'},
        )

    return router


def install_v1_resource_api(app: FastAPI, service: MaterialsService, **kwargs: Any) -> None:
    app.include_router(create_v1_resource_router(service, **kwargs))


__all__ = ["create_v1_resource_router", "install_v1_resource_api"]
