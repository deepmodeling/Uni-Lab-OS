"""FastAPI adapter matching Backend c35d821 resource routes and envelope."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unilabos.app.scheduler.inventory.backend_contract import (
    MAX_SITE_IDENTITY_RESOLUTION_IDS,
    TEMPLATE_DATA_CONFLICT,
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.backend_response import (
    call as _call,
)
from unilabos.app.scheduler.inventory.backend_response import (
    error_response as _error,
)
from unilabos.app.scheduler.inventory.backend_response import (
    success as _success,
)
from unilabos.app.scheduler.inventory.batch_api import (
    MAX_BATCH_ROWS,
    create_batch_response,
)
from unilabos.app.scheduler.inventory.content_api import (
    create_container_content_router,
)
from unilabos.app.scheduler.inventory.content_contract import (
    BackendContainerContentService,
)
from unilabos.app.scheduler.inventory.reagent_api import (
    MaterialReagentRequest,
    create_reagent_router,
)
from unilabos.app.scheduler.inventory.reagent_contract import BackendReagentService
from unilabos.package_manager.package_catalog.material_models import model_asset_media_type


class BackendModel(BaseModel):
    """Gin binding-compatible model: recognized fields are typed, extras ignored."""

    model_config = ConfigDict(extra="ignore")


class SiteListItem(BaseModel):
    """全库位列表中的稳定身份、组成归属与物理占位摘要。"""

    uuid: UUID = Field(description="库位 UUID")
    name: str = Field(description="库位名称")
    material_uuid: UUID = Field(description="直接承载该库位的物料或设备 UUID")
    material_name: str = Field(description="直接载体名称")
    device_uuid: Optional[UUID] = Field(description="组成父链中最近设备的 UUID；无设备祖先时为空")
    device_name: Optional[str] = Field(description="最近设备名称；无设备祖先时为空")
    occupied_material_uuid: Optional[UUID] = Field(description="当前占位物料 UUID；空位时为空，不表示没有任务预留")
    occupied_material_name: Optional[str] = Field(description="当前占位物料名称；空位或物料不可见时为空")
    allowed_resource_template_uuids: List[UUID] = Field(description="允许放入的物料模板 UUID；空列表表示不限制模板")


class SiteListData(BaseModel):
    """与物料模板列表一致的后续页分页合同。"""

    items: List[SiteListItem]
    has_more: bool
    page: int
    page_size: int


class SiteListResponse(BaseModel):
    """全库位列表的成功响应信封。"""

    code: Literal[0] = 0
    data: SiteListData


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
    available_sites: List[Dict[str, Any]] = Field(default_factory=list)
    cover: Optional[str] = None
    scene: List[Any] = Field(default_factory=list)
    device_params: Dict[str, Any] = Field(default_factory=dict)


class SitePlacementRequest(BackendModel):
    action: Literal["place", "remove"]
    site_uuid: Optional[UUID] = None

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, value: Any) -> Any:
        """把 Backend 接受的大小写动作规范为小写枚举值。

        参数：``value`` 是未信任 JSON 动作。返回：字符串时去空白并转小写，其余
        类型交给 Pydantic 拒绝。异常：非法枚举由字段校验统一报告。
        """

        return value.strip().lower() if isinstance(value, str) else value


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
    barcode: Optional[str] = None
    name: str
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    relative_position: Optional[RelativePositionRequest] = None
    site_placement: Optional[SitePlacementRequest] = None


class MaterialCreateRequest(MaterialRequest):
    """创建物料时可选地在同一事务装入试剂。"""

    reagent: Optional[MaterialReagentRequest] = None


class MaterialBatchCreateRequest(BackendModel):
    """批量创建物料；每行沿用单条创建字段，可同时携带试剂内容。"""

    # 保留原始行，由批量适配器逐行校验，便于部分成功模式定位失败字段。
    items: List[Dict[str, Any]] = Field(
        min_length=1,
        max_length=MAX_BATCH_ROWS,
        description="物料创建列表，每行包含模板 UUID、名称及可选的 reagent 试剂信息。",
    )
    atomic: bool = Field(default=True, description="是否要求全部成功；为 false 时保留成功行。")


class MaterialUpdateRequest(BackendModel):
    """按字段更新物料；未出现或显式为 ``null`` 的字段保持原值。"""

    resource_template_uuid: Optional[UUID] = None
    parent_uuid: Optional[UUID] = None
    barcode: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    relative_position: Optional[RelativePositionRequest] = None
    site_placement: Optional[SitePlacementRequest] = None
    expected_revision: Optional[int] = Field(default=None, ge=0)


class MaterialStateRequest(BackendModel):
    status: Optional[str] = None
    state_data: Dict[str, Any] = Field(default_factory=dict)
    source: Optional[str] = None
    observed_at: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)


class SiteIdentityResolveRequest(BackendModel):
    """Edge 注册阶段批量解析库位业务身份的窄请求。"""

    source_node_ids: List[str] = Field(
        max_length=MAX_SITE_IDENTITY_RESOLUTION_IDS
    )

    @field_validator("source_node_ids", mode="before")
    @classmethod
    def normalize_source_node_ids(cls, value: Any) -> Any:
        """在批量上限校验前去除空白和重复字符串。"""

        if not isinstance(value, list):
            return value
        normalized: List[Any] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            identity = item.strip()
            if identity and identity not in seen:
                seen.add(identity)
                normalized.append(identity)
        return normalized


def create_backend_resource_router(
    service: BackendResourceService,
    *,
    material_shapes: Sequence[Mapping[str, Any]] = (),
    material_model_catalog: Any = None,
    compound_source: Any = None,
) -> APIRouter:
    """创建前端资源合同路由并附带静态物料外形。

    参数：``service`` 提供公共资源读写；``material_shapes`` 是已由包资产编译器
    校验的静态外形投影；``material_model_catalog`` 只读取本启动代际授权的模型
    资产。返回：不包含 Edge 私有库存路由的 ``APIRouter``。异常：外形项目或模型
    目录形状非法时原样抛出。
    """

    router = APIRouter(prefix="/api/v1", tags=["backend-resource-contract"])
    router.include_router(
        create_material_asset_router(
            material_shapes=material_shapes,
            material_model_catalog=material_model_catalog,
        )
    )
    router.include_router(
        create_reagent_router(
            BackendReagentService(
                service.store,
                edge_id=service.edge_id,
                lab_id=service.lab_id,
                compound_source=compound_source,
            )
        )
    )
    router.include_router(
        create_container_content_router(
            BackendContainerContentService(
                service.store,
                edge_id=service.edge_id,
                lab_id=service.lab_id,
            )
        )
    )

    @router.post("/resource-templates")
    def sync_resource_templates(body: ResourceTemplateSyncRequest) -> JSONResponse:
        return _call(service.sync_resource_templates, body.resources)

    @router.get("/resource-templates")
    def list_resource_templates(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        keyword: str = Query(default=""),
        resource_type: str = Query(default=""),
    ) -> JSONResponse:
        return _call(
            service.list_resource_templates,
            page=page,
            page_size=page_size,
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
        """更新一项本地资源模板（ResourceTemplate）定义。

        参数：``template_uuid`` 是既有模板稳定身份，``body`` 是调用方显式更新
        内容。返回：公共 Backend 信封中的最新模板详情；模板不存在、字段非法或
        持久化冲突时返回对应业务错误，未传 ``available_sites`` 时保留既有定义。
        """

        try:
            template_identity = str(template_uuid)
            current = service.get_resource_template(template_identity)
            if (
                current.get("resource_type") == "device"
                and service.store.runtime_device_template_catalog_required
            ):
                raise BackendContractError(
                    TEMPLATE_DATA_CONFLICT,
                    "runtime device templates can only be changed by the domain package",
                )
            definition = body.model_dump(by_alias=True, mode="json")
            if "registry_type" not in body.model_fields_set:
                definition["registry_type"] = current.get("resource_type") or "resource"
            if "handles" not in body.model_fields_set:
                definition.pop("handles", None)
            if "available_sites" not in body.model_fields_set:
                # 旧调用方更新展示字段时未携带新字段，不得清空既有库位模板定义。
                definition["available_sites"] = current.get("available_sites") or []
            definition["id"] = current["name"]
            service.sync_resource_templates([definition])
            return _success(service.get_resource_template(template_identity))
        except BackendContractError as error:
            return _error(error)

    @router.delete("/resource-templates/{template_uuid}")
    def delete_resource_template(template_uuid: UUID) -> JSONResponse:
        return _call(service.delete_resource_template, str(template_uuid))

    @router.post("/materials")
    def create_material(body: MaterialCreateRequest) -> JSONResponse:
        return _call(
            service.create_material,
            body.model_dump(mode="json"),
            status_code=201,
        )

    @router.post("/materials/batch", summary="批量创建物料和试剂")
    def create_materials(body: MaterialBatchCreateRequest) -> JSONResponse:
        return create_batch_response(
            body.items,
            model=MaterialCreateRequest,
            batch_create=service.create_materials,
            single_create=service.create_material,
            atomic=body.atomic,
        )

    @router.get("/materials")
    def list_materials(
        page: int = Query(default=0),
        page_size: int = Query(default=0),
        name: str = Query(default=""),
        barcode: str = Query(default=""),
        resource_template_uuid: Optional[UUID] = Query(default=None),
        category: Optional[Literal["material", "device"]] = Query(
            default=None,
            description="分类：material 为非设备物料，device 为设备；不传查询全部。",
        ),
        inventory_scope: Literal["current", "history"] = Query(
            default="current", description="当前库存或只读离站历史。"
        ),
        empty_containers_only: bool = Query(
            default=False, description="只返回模板标记为容器且没有活动内容登记的物料。"
        ),
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
            category=category,
            inventory_scope=inventory_scope,
            empty_containers_only=empty_containers_only,
        )

    @router.get("/materials/graph")
    def get_material_graph(
        inventory_scope: Literal["current", "history"] = Query(default="current"),
        material_uuid: list[UUID] | None = Query(default=None, max_length=100),
    ) -> JSONResponse:
        def read() -> dict:
            graph = service.material_graph(
                inventory_scope=inventory_scope,
                material_uuids=None if material_uuid is None else [str(value) for value in material_uuid],
            )
            publication = getattr(material_model_catalog, "publication", None)
            if publication is None:
                return graph
            graph["modelAssets"] = {publication.revision: {"basePath": publication.base, "files": publication.files}}
            templates = {}
            for node in graph["nodes"]:
                material = node["material"]
                config = material.get("config") or {}
                rendering = config.get("rendering", config) if isinstance(config, dict) else {}
                model = rendering.get("model") if isinstance(rendering, dict) else None
                if isinstance(model, dict):
                    updated = {**rendering, "model": publication.describe(model)}
                    material["config"] = {**config, "rendering": updated} if "rendering" in config else updated
                    if any(model.get(key) for key in ("path", "mesh", "instances", "attachPoints", "attach_points")):
                        continue
                # 把模板回退也放入同一份图，按模板复用查询，保留物料自身配置。
                summary = node.get("resource_template") or {}
                template_id = summary.get("uuid")
                if not template_id:
                    continue
                if template_id not in templates:
                    detail = service.get_resource_template(template_id)
                    declaration = material_model_catalog.models_by_template.get(detail.get("name"), {})
                    templates[template_id] = publication.describe_template(detail, declaration).get("model") or {}
                fallback = templates[template_id]
                if isinstance(fallback.get("path"), str) and fallback["path"].startswith("/api/v1/material-models/"):
                    node["resource_template"] = {**summary, "model": fallback}
            return graph

        response = _call(read)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @router.get("/materials/events")
    async def material_events(request: Request):
        from unilabos.app.resource_change_api import resource_change_stream

        return await resource_change_stream(request, service.store, scope="inventory")

    @router.post("/materials/sites/resolve-identities", summary="批量解析库位标识")
    def resolve_site_identities(body: SiteIdentityResolveRequest) -> JSONResponse:
        """把资源图 Site 业务 ID 批量解析为 Backend 稳定 UUID。"""

        return _call(service.resolve_site_identities, body.source_node_ids)

    @router.get("/materials/movements", summary="查询出入站记录")
    def list_material_movements(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        direction: Literal["all", "inbound", "outbound"] = Query(default="all"),
        keyword: str = Query(default="", max_length=200),
        material_uuid: Optional[UUID] = Query(default=None),
    ) -> JSONResponse:
        """按交接明细查询真实出入站和取消记录，支持物料、方向筛选及分页。"""
        return _call(service.list_material_movements, page=page, page_size=page_size,
                     direction=direction, keyword=keyword,
                     material_uuid=str(material_uuid) if material_uuid else None)

    @router.get("/materials/{material_uuid}")
    def get_material(material_uuid: UUID) -> JSONResponse:
        return _call(service.get_material, str(material_uuid))

    @router.put("/materials/{material_uuid}")
    def update_material(
        material_uuid: UUID, body: MaterialUpdateRequest
    ) -> JSONResponse:
        """按字段修改物料，或通过 ``site_placement`` 执行人工上下料。

        参数：路径 UUID 是物料稳定身份，``body`` 仅更新明确给出且非空的字段；
        ``place`` 放入或换到目标库位，``remove`` 解除当前精确库位占用。返回：统一
        数值信封中的最新物料详情。异常：参数、修订、模板、父级或库位
        冲突由资源服务映射为稳定 Backend 错误码。
        """

        values = body.model_dump(mode="json", exclude_unset=True)
        values["_specified_fields"] = set(body.model_fields_set)
        values["_relative_position_specified"] = (
            "relative_position" in body.model_fields_set
            and body.relative_position is not None
        )
        return _call(
            service.update_material,
            str(material_uuid),
            values,
        )

    @router.delete("/materials/{material_uuid}")
    def delete_material(
        material_uuid: UUID,
        scope: Literal["material_only", "material_with_contents"] = Query(
            default="material_only"
        ),
    ) -> JSONResponse:
        """删除空闲物料；带内容的容器需 ``scope=material_with_contents``。"""

        return _call(service.delete_material, str(material_uuid), scope=scope)

    @router.get("/materials/{material_uuid}/ledger")
    def list_material_ledger(
        material_uuid: UUID,
        page: int = Query(default=1),
        page_size: int = Query(default=50),
    ) -> JSONResponse:
        """读取一只物料的台账，按发生时间倒序。"""

        return _call(
            service.list_material_ledger,
            str(material_uuid),
            page=page,
            page_size=page_size,
        )

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

    @router.get("/sites", response_model=SiteListResponse)
    def list_all_sites(
        page: int = Query(default=1, description="页码；小于 1 时按 1 处理"),
        page_size: int = Query(default=20, description="每页数量；非正数按 20 处理，最多 100"),
        keyword: str = Query(default="", description="库位、直接载体或设备名称中的关键词"),
        device_uuid: Optional[UUID] = Query(default=None, description="按最近设备祖先筛选"),
        material_uuid: Optional[UUID] = Query(default=None, description="按库位直接载体筛选"),
        occupied: Optional[bool] = Query(default=None, description="仅按物理占位筛选，不表示任务预留或可用性"),
    ) -> JSONResponse:
        """分页列出当前工站库位；排除已删除及已离站载体的库位。"""

        return _call(
            service.list_all_sites,
            page=page,
            page_size=page_size,
            keyword=keyword,
            device_uuid=str(device_uuid) if device_uuid is not None else None,
            material_uuid=str(material_uuid) if material_uuid is not None else None,
            occupied=occupied,
        )

    @router.get("/sites/{site_uuid}")
    def get_site(site_uuid: UUID) -> JSONResponse:
        return _call(service.get_site, str(site_uuid))

    return router


def create_material_asset_router(
    *,
    material_shapes: Sequence[Mapping[str, Any]] = (),
    material_model_catalog: Any = None,
) -> APIRouter:
    """创建不依赖 Inventory 的只读物料外形与模型资产路由。

    参数：``material_shapes`` 和 ``material_model_catalog`` 必须来自同一工作区
    编译代。返回：可挂载到正式资源合同或纯 Authoring Web 进程的 Router。
    异常：外形项目形状非法时原样抛出。
    """

    router = APIRouter()
    # ``frozen_material_shapes`` 与调用者容器隔离，保证启动代际不会随请求漂移。
    frozen_material_shapes = tuple(deepcopy(dict(shape)) for shape in material_shapes)

    @router.get("/material-shapes")
    def list_material_shapes() -> JSONResponse:
        """返回工作区包声明的静态物料外形。

        参数：无。返回：Backend 公共信封中的外形项目列表。异常：无。
        """

        return _success({"items": deepcopy(list(frozen_material_shapes))})

    @router.get("/material-models/{asset_path:path}")
    def read_material_model(asset_path: str, request: Request, v: str | None = None) -> Response:
        """返回一项工作区 3D 模型资产。

        参数：``asset_path`` 是公共模型路由内的相对路径，``request`` 提供实体标签。
        返回：资产字节或校验命中的无正文 304。异常：目录未安装或资产未授权时返回 404。
        """

        if material_model_catalog is None:
            raise HTTPException(status_code=404, detail="模型资产目录未安装")
        if v is not None:
            distribution, _, path = asset_path.partition("/")
            publication = getattr(material_model_catalog, "publication", None)
            if publication is None or distribution != material_model_catalog.startup_plan.distribution_name:
                raise HTTPException(status_code=404, detail="模型资产版本未找到")
            try:
                content = publication.read(v, path)
            except KeyError:
                raise HTTPException(status_code=404, detail="模型资产版本未找到") from None
            return Response(
                content, media_type=model_asset_media_type(path),
                headers={"Cache-Control": "private, max-age=2592000, immutable", "Vary": "Authorization, Cookie"},
            )
        public_path = f"/api/v1/material-models/{asset_path}"
        try:
            # ``asset`` 是当前工作区启动代际完成边界校验后的不可变读取结果。
            asset = material_model_catalog.read_asset(public_path)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="模型资产未找到") from error
        headers = {
            "Cache-Control": "private, max-age=0, must-revalidate",
            "ETag": f'"{asset.etag}"',
        }
        validator = request.headers.get("if-none-match", "")
        # GET 使用弱比较；先读取受限目录，不能用缓存校验绕过资产存在性与授权检查。
        if validator.strip() == "*" or any(
            tag.strip().removeprefix("W/") == headers["ETag"]
            for tag in validator.split(",")
        ):
            return Response(status_code=304, headers=headers)
        return Response(
            content=asset.content,
            media_type=asset.media_type,
            headers=headers,
        )

    return router


def install_backend_resource_api(
    app: FastAPI,
    service: BackendResourceService,
    *,
    material_shapes: Sequence[Mapping[str, Any]] = (),
    material_model_catalog: Any = None,
    compound_source: Any = None,
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
            "/api/v1/compounds",
            "/api/v1/reagent-infos",
            "/api/v1/reagents",
            "/api/v1/reagent-history",
            "/api/v1/samples",
            "/api/v1/current-substances",
            "/api/v1/substance-history",
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
            compound_source=compound_source,
        )
    )


__all__ = [
    "create_backend_resource_router",
    "create_material_asset_router",
    "install_backend_resource_api",
]
