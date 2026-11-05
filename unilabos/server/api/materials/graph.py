"""``/api/v1/graphs`` FastAPI 路由：materials 域设备图的 HTTP 契约。

图不是独立权威：节点/边/快照都落 materials.db，业务在
``services.graph.GraphService``。响应统一使用 envelope
（``{"code": 0, "data": ...}``），与云端 Backend 协议同构；``--remote``
时 CLI 客户端以同一契约访问云端。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from unilabos.server.api.envelope import envelope_error, install_envelope_validation
from unilabos.server.services.materials.graph import GraphError, GraphService

#: GraphError.code → 信封业务码类别（与 workflow / chemicals 同一张码表，见 api/envelope.py）。
_CATEGORIES = {
    "not_found": "not_found",
    "invalid_input": "invalid",
    "invalid_payload": "invalid",
    "identity_conflict": "conflict",
    "unsupported": "unavailable",
}


class GraphUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    payload: Dict[str, Any]
    uuid: Optional[str] = None
    tags: List[Any] = Field(default_factory=list)
    description: Optional[str] = None
    meta_data: Dict[str, Any] = Field(default_factory=dict)
    #: 调用方（Host 子进程）注册表里的设备模板 Site；缺省用本进程注册表。Host 刚装的
    #: 驱动包本进程注册表还没有，由 Host 随图带上。
    device_site_templates: Optional[Dict[str, List[Any]]] = None
    #: 权威已有该图时：replace 以上传为准（前端 / CLI 编辑）；adopt 以权威为准、只补
    #: 权威没有的节点（``unilab -g <文件>`` 启动登记）。
    on_existing: Literal["replace", "adopt"] = "replace"


def _success(data: Any = None) -> JSONResponse:
    content: Dict[str, Any] = {"code": 0}
    if data is not None:
        content["data"] = data
    return JSONResponse(status_code=200, content=content)


def _error(error: GraphError) -> JSONResponse:
    return envelope_error(_CATEGORIES.get(error.code, "internal"), error.message)


def create_graph_router(service: GraphService) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["graphs-v1"])

    @router.get("/graphs")
    async def list_graphs(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=1000),
        name: str = Query(default=""),
    ):
        try:
            return _success(
                service.list_graphs(page=page, page_size=page_size, name=name)
            )
        except GraphError as error:
            return _error(error)

    @router.post("/graphs")
    async def upsert_graph(value: GraphUpsertRequest):
        try:
            device_site_templates = value.device_site_templates
            if device_site_templates is None:
                from unilabos.registry.registry import lab_registry

                device_site_templates = {
                    device_id: (entry or {}).get("available_sites") or []
                    for device_id, entry in lab_registry.device_type_registry.items()
                }
            return _success(
                service.upsert_graph(
                    name=value.name,
                    payload=value.payload,
                    uuid=value.uuid,
                    tags=value.tags,
                    description=value.description,
                    meta_data=value.meta_data,
                    device_site_templates=device_site_templates,
                    on_existing=value.on_existing,
                )
            )
        except GraphError as error:
            return _error(error)

    @router.get("/graphs/live/payload")
    async def get_live_payload():
        """当前真实拓扑：material + material_link 实时序列化（非快照回放）。"""
        try:
            return _success(service.live_payload())
        except GraphError as error:
            return _error(error)

    @router.get("/graphs/{identity}")
    async def get_graph(identity: str):
        try:
            return _success(service.get_graph(identity))
        except GraphError as error:
            return _error(error)

    @router.get("/graphs/{identity}/payload")
    async def get_graph_payload(identity: str):
        try:
            return _success(service.get_payload(identity))
        except GraphError as error:
            return _error(error)

    @router.delete("/graphs/{identity}")
    async def delete_graph(identity: str):
        try:
            service.delete_graph(identity)
            return _success({})
        except GraphError as error:
            return _error(error)

    return router


def install_graph_api(app: FastAPI, service: GraphService) -> None:
    install_envelope_validation(app, ("/api/v1/graphs",))
    app.include_router(create_graph_router(service))


__all__ = ["GraphUpsertRequest", "create_graph_router", "install_graph_api"]
