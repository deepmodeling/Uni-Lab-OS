"""V1 工站错误处置路由（``/api/v1/stations/{station_id}/*``，17 条 + 资源处置 / 恢复）。

写操作都要 ``Idempotency-Key``（V1 必填头）；操作员身份取请求上下文，本地部署无多用户身份时记
``local-workspace``。失败按统一信封：不存在 4001、版本 / 阶段 / 幂等冲突 3003、请求不合法 1000、
依赖不可用 5001（V1 原来用 HTTP 404 / 409 / 422 / 503）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from unilabos.server.api.envelope import GENERIC, RESOURCE_NOT_FOUND, ApiError, envelope_router, ok_response
from unilabos.server.api.runtime.tasks_v1 import operator_identity


class StationVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_station_version: int = Field(ge=1, strict=True, description="当前读取的工站控制版本")


class SessionVersion(StationVersion):
    expected_session_version: int = Field(ge=1, strict=True, description="当前读取的人工处置会话版本")


class DecisionRequest(StationVersion):
    action: Literal["cancel_task", "retry_current_node", "enter_manual_handling"]
    expected_decision_version: int = Field(ge=1, strict=True)
    observed_error_epoch: int = Field(ge=0, strict=True)
    resume_requested: StrictBool = False
    operator_confirmed: StrictBool = False
    confirm_snapshot_id: Optional[str] = None
    session_id: Optional[str] = None
    expected_session_version: Optional[int] = Field(default=None, ge=1, strict=True)
    reason: str = ""


class CompleteRequest(SessionVersion):
    summary: str = Field(default="", max_length=20000)


class ResumeRequest(StationVersion):
    observed_error_epoch: int = Field(ge=0, strict=True)
    operator_confirmed: StrictBool
    confirm_snapshot_id: str


class ManualActionRequest(SessionVersion):
    device_id: str
    action_id: str
    action_version: str
    parameters: Dict[str, Any]


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = "用户停止手动动作"


class ManualReviewRequest(SessionVersion):
    session_id: str
    expected_review_version: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=20000)
    operator_confirmed: StrictBool


class HistoricalManualReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_review_version: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=20000)
    operator_confirmed: StrictBool


class InventoryChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_uuid: str
    parent_uuid: str = ""
    site_uuid: str = ""


class InventoryCorrectionRequest(SessionVersion):
    changes: List[InventoryChange] = Field(min_length=1, max_length=100)
    expected_inventory_versions: Dict[str, int | str]
    reason: str = Field(min_length=1, max_length=20000)
    related_manual_command_id: Optional[str] = None


class ResourceDispositionRequest(StationVersion):
    decision_id: str
    session_id: Optional[str] = None
    expected_session_version: Optional[int] = Field(default=None, ge=1, strict=True)
    occupancy_ids: List[str] = Field(min_length=1, max_length=100)
    expected_versions: Dict[str, str]
    operation: Literal["release"]
    reason: str = Field(min_length=1, max_length=20000)


def endpoint_action_catalog(device_id: str) -> List[Dict[str, Any]]:
    """设备动作目录：读 Host 上报的 endpoint 能力快照（分进程时经控制面读 Host）。"""

    from unilabos.server.api.edge_proxy import edge_http, edge_proxy_enabled, edge_response_data
    from unilabos.server.composition import get_server_services
    from unilabos.server.services.runtime.station_handling import StationUnavailable

    if edge_proxy_enabled():
        response = edge_http("GET", "/api/v1/runtime/endpoints?state=online&limit=1000", timeout=5.0)
        if response is None:
            raise StationUnavailable("Host 动作能力暂不可用")
        endpoints = edge_response_data(response)
    else:
        services = get_server_services()
        endpoints = (
            [item.model_dump(mode="json") if hasattr(item, "model_dump") else item
             for item in services.runtime.list_endpoint_snapshots(state="online", limit=1000)]
            if services is not None else []
        )
    actions: List[Dict[str, Any]] = []
    for endpoint in endpoints or []:
        routes = {route.get("device_uuid"): route for route in endpoint.get("device_routes") or []}
        for capability in endpoint.get("action_capabilities") or []:
            descriptor = capability.get("descriptor") or {}
            device_key = descriptor.get("device_id") or capability.get("device_uuid")
            if device_id not in {device_key, capability.get("device_uuid")} or capability.get("state") == "retired":
                continue
            name = capability["action_name"]
            actions.append({
                "action_id": name,
                "action_version": capability.get("descriptor_hash") or "",
                "name": name,
                "display_name": descriptor.get("display_name") or name,
                "action_type": capability.get("action_type") or "",
                "parameter_schema": descriptor.get("schema") or descriptor.get("goal_schema") or {},
                "defaults": descriptor.get("goal_default") or {},
                "device_binding": {
                    "material_uuid": capability.get("device_uuid"),
                    "edge_local_id": device_key,
                    "route": routes.get(capability.get("device_uuid"), {}).get("route_uuid"),
                },
            })
    return actions


def create_station_router(
    service: Any,
    *,
    station_id: str = "local",
    action_catalog: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    materials: Optional[Callable[[], Any]] = None,
) -> APIRouter:
    from unilabos.server.services.runtime.station_handling import StationHandling

    def deliver(decision_id: str, payload: Dict[str, Any]) -> bool:
        deliverer = getattr(service, "_intervention_delivery", None)
        if deliverer is None:
            from unilabos.server.services.runtime.error_decisions import default_desk

            deliverer = default_desk().deliver
        return bool(deliverer(decision_id, payload))

    def default_materials() -> Any:
        from unilabos.server.backend.composition import get_materials_service

        return get_materials_service()

    handling = StationHandling(
        service, station_id=station_id, deliver=deliver,
        action_catalog=action_catalog or endpoint_action_catalog, materials=materials or default_materials,
    )
    service.station_handling = handling

    expected_station = station_id

    def station_scope(station_id: str) -> None:
        if station_id != expected_station:
            raise ApiError(RESOURCE_NOT_FOUND, "工站不存在")

    router = envelope_router(
        profile=GENERIC, prefix="/api/v1/stations/{station_id}", tags=["工站错误处置"],
        dependencies=[Depends(station_scope)],
    )

    def accepted(result: Dict[str, Any]) -> JSONResponse:
        return ok_response(result, status_code=202)

    @router.get("/error-handling", summary="读取工站错误与处置状态")
    def snapshot() -> Dict[str, Any]:
        return handling.snapshot()

    @router.post("/errors/{decision_id}/decision", summary="提交节点错误处理选择")
    def decide(decision_id: str, body: DecisionRequest, request: Request,
               idempotency_key: str = Header(alias="Idempotency-Key")) -> JSONResponse:
        return accepted(handling.decide(decision_id, body.model_dump(), idempotency_key=idempotency_key,
                                        operator_id=operator_identity(request)))

    @router.get("/sessions/{session_id}", summary="读取人工处置会话")
    def session(session_id: str) -> Dict[str, Any]:
        return handling.get_session(session_id)

    @router.post("/sessions/{session_id}/complete", summary="完成手动处理并继续等待决定")
    def complete(session_id: str, body: CompleteRequest, request: Request,
                 idempotency_key: str = Header(alias="Idempotency-Key")) -> Dict[str, Any]:
        return handling.complete(session_id, body.model_dump(), idempotency_key=idempotency_key,
                                 operator_id=operator_identity(request))

    @router.post("/sessions/{session_id}/return", summary="返回同一会话继续手动处理")
    def return_manual(session_id: str, body: SessionVersion, request: Request,
                      idempotency_key: str = Header(alias="Idempotency-Key")) -> Dict[str, Any]:
        return handling.return_to_manual(session_id, body.model_dump(), idempotency_key=idempotency_key,
                                         operator_id=operator_identity(request))

    @router.get("/sessions/{session_id}/devices/{device_id}/actions", summary="读取设备全部可用动作")
    def actions(session_id: str, device_id: str) -> List[Dict[str, Any]]:
        return handling.manual_actions(session_id, device_id)

    @router.post("/sessions/{session_id}/manual-actions", summary="提交手动设备动作")
    def execute(session_id: str, body: ManualActionRequest, request: Request,
                idempotency_key: str = Header(alias="Idempotency-Key")) -> JSONResponse:
        return accepted(handling.execute_manual(session_id, body.model_dump(), idempotency_key=idempotency_key,
                                                operator_id=operator_identity(request)))

    @router.get("/manual-actions/{command_id}", summary="读取手动动作执行记录")
    def action(command_id: str) -> Dict[str, Any]:
        return handling.manual_action(command_id)

    @router.post("/manual-actions/{command_id}/stop", summary="请求停止指定手动动作")
    def stop(command_id: str, body: StopRequest, request: Request,
             idempotency_key: str = Header(alias="Idempotency-Key")) -> JSONResponse:
        return accepted(handling.stop_manual(command_id, body.model_dump(), idempotency_key=idempotency_key,
                                             operator_id=operator_identity(request)))

    @router.post("/manual-actions/{command_id}/review", summary="确认已结束手动动作的现场核对结果")
    def review(command_id: str, body: ManualReviewRequest, request: Request,
               idempotency_key: str = Header(alias="Idempotency-Key")) -> Dict[str, Any]:
        return handling.review_manual(command_id, body.model_dump(), idempotency_key=idempotency_key,
                                      operator_id=operator_identity(request))

    @router.get("/manual-action-jobs/{job_uuid}/review", summary="读取历史手动动作的核对上下文")
    def historical_review_context(job_uuid: str) -> Dict[str, Any]:
        return handling.manual_job_review_context(job_uuid)

    @router.post("/manual-action-jobs/{job_uuid}/review", summary="保存历史手动动作的现场核对记录")
    def historical_review(job_uuid: str, body: HistoricalManualReviewRequest, request: Request,
                          idempotency_key: str = Header(alias="Idempotency-Key")) -> Dict[str, Any]:
        return handling.review_manual_job(job_uuid, body.model_dump(), idempotency_key=idempotency_key,
                                          operator_id=operator_identity(request))

    @router.get("/control-commands/{command_uuid}", summary="读取已受理控制命令")
    def command(command_uuid: str) -> Dict[str, Any]:
        return handling.get_command(command_uuid)

    @router.post("/control-commands/{command_uuid}/apply", summary="继续应用已受理控制命令")
    def apply_command(command_uuid: str) -> JSONResponse:
        return accepted(handling.apply_command(command_uuid))

    @router.get("/sessions/{session_id}/inventory", summary="读取手动处理所需的库存事实")
    def inventory(session_id: str) -> Dict[str, Any]:
        return handling.inventory(session_id)

    @router.post("/sessions/{session_id}/inventory-corrections", summary="登记人工确认的上下料与库位更正")
    def correction(session_id: str, body: InventoryCorrectionRequest, request: Request,
                   idempotency_key: str = Header(alias="Idempotency-Key")) -> JSONResponse:
        return accepted(handling.inventory_correction(session_id, body.model_dump(), idempotency_key=idempotency_key,
                                                      operator_id=operator_identity(request)))

    @router.get("/resource-usage", summary="读取设备、库位与任务物料使用关系")
    def resource_usage() -> Dict[str, Any]:
        return handling.resource_usage()

    @router.get("/resource-dispositions", summary="读取当前逻辑资源占用")
    def occupancies() -> List[Dict[str, Any]]:
        return handling.occupancies()

    @router.post("/resource-dispositions", summary="释放用户选中的逻辑资源占用")
    def disposition(body: ResourceDispositionRequest, request: Request,
                    idempotency_key: str = Header(alias="Idempotency-Key")) -> JSONResponse:
        return accepted(handling.resource_disposition(body.model_dump(), idempotency_key=idempotency_key,
                                                      operator_id=operator_identity(request)))

    @router.get("/events", summary="读取工站控制操作记录")
    def events(after: int = Query(default=0, ge=0)) -> Dict[str, Any]:
        return handling.events(after)

    @router.post("/resume", summary="按用户确认恢复工站普通调度")
    def resume(body: ResumeRequest, request: Request,
               idempotency_key: str = Header(alias="Idempotency-Key")) -> Dict[str, Any]:
        return handling.resume(body.model_dump(), idempotency_key=idempotency_key,
                               operator_id=operator_identity(request))

    return router


__all__ = ["create_station_router", "endpoint_action_catalog"]
