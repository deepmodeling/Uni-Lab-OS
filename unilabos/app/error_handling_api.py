"""Edge 专属错误处置接口，不走旧设备干预决定投递器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from unilabos.app.scheduler.inventory.domain import CommandRejected, VersionConflict
from unilabos.app.scheduler.inventory.station_resource import StationResourceError
from unilabos.workflow.device_action_run import (
    DeviceActionRunConflict,
    DeviceActionRunUnavailable,
)
from unilabos.workflow.error_handling import ErrorHandlingService
from unilabos.workflow.store import StoreConflict, StoreNotFound
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridgeError


class StationVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_station_version: int = Field(
        ge=1, strict=True, description="当前读取的工站控制版本"
    )


class SessionVersion(StationVersion):
    expected_session_version: int = Field(
        ge=1, strict=True, description="当前读取的人工处置会话版本"
    )


class DecisionRequest(StationVersion):
    action: Literal["cancel_task", "retry_current_node", "enter_manual_handling"]
    expected_decision_version: int = Field(
        ge=1, strict=True, description="当前读取的错误决定版本"
    )
    observed_error_epoch: int = Field(
        ge=0, strict=True, description="用户确认时已看到的错误批次编号"
    )
    resume_requested: StrictBool = Field(
        default=False, description="是否同时恢复工站普通调度"
    )
    operator_confirmed: StrictBool = Field(
        default=False, description="用户是否已人工确认现场可以恢复"
    )
    confirm_snapshot_id: str | None = Field(
        default=None, description="用户确认时看到的工站快照标识"
    )
    session_id: str | None = None
    expected_session_version: int | None = Field(
        default=None, ge=1, strict=True, description="当前读取的人工处置会话版本"
    )
    reason: str = ""


class CompleteRequest(SessionVersion):
    summary: str = Field(default="", max_length=20000)


class ResumeRequest(StationVersion):
    observed_error_epoch: int = Field(
        ge=0, strict=True, description="用户确认时已看到的错误批次编号"
    )
    operator_confirmed: StrictBool = Field(description="用户是否已人工确认现场可以恢复")
    confirm_snapshot_id: str = Field(description="用户确认时看到的工站快照标识")


class ManualActionRequest(SessionVersion):
    device_id: str
    action_id: str
    action_version: str = Field(description="动作目录返回的当前动作内容版本")
    parameters: dict[str, Any]


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
    changes: list[InventoryChange] = Field(min_length=1, max_length=100)
    expected_inventory_versions: dict[str, int | str] = Field(
        description="本次更正涉及的各库存对象当前版本"
    )
    reason: str = Field(min_length=1, max_length=20000)
    related_manual_command_id: str | None = Field(
        default=None, description="关联的手动动作命令标识，可不填写"
    )


class ResourceDispositionRequest(StationVersion):
    decision_id: str = Field(description="本次资源处置所属的错误决定标识")
    session_id: str | None = None
    expected_session_version: int | None = Field(
        default=None, ge=1, strict=True, description="当前读取的人工处置会话版本"
    )
    occupancy_ids: list[str] = Field(
        min_length=1, max_length=100, description="用户明确选择释放的逻辑占用标识列表"
    )
    expected_versions: dict[str, str] = Field(description="各选中逻辑占用的当前版本")
    operation: Literal["release"]
    reason: str = Field(min_length=1, max_length=20000)


def create_error_handling_router(
    service: ErrorHandlingService | None = None,
    *,
    service_provider: Callable[[], ErrorHandlingService | None] | None = None,
) -> APIRouter:
    def resolve_service() -> ErrorHandlingService:
        # 冷启动先挂载路由，运行时在请求阶段解析；Depends 在同一请求内复用结果。
        current = service_provider() if service_provider is not None else service
        if current is None:
            raise HTTPException(503, "工站错误处置服务尚未就绪")
        return current

    service_dependency = Depends(resolve_service)

    def station_scope(
        station_id: str,
        service: ErrorHandlingService = service_dependency,
    ) -> None:
        if not service.control.accepts_station_id(station_id):
            raise HTTPException(404, "工站不存在")

    router = APIRouter(
        prefix="/stations/{station_id}",
        tags=["工站错误处置"],
        dependencies=[Depends(station_scope)],
    )

    def actor(request: Request) -> str:
        # 本地工作区使用已认证请求上下文；没有多用户身份的本地部署记录服务端身份。
        return str(getattr(request.state, "operator_id", None) or "local-workspace")

    def invoke(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"code": 0, "data": fn(*args, **kwargs)}
        except DeviceActionRunUnavailable as error:
            raise HTTPException(503, str(error)) from error
        except DeviceActionRunConflict as error:
            raise HTTPException(409, str(error)) from error
        except StoreNotFound as error:
            raise HTTPException(404, str(error)) from error
        except (
            StoreConflict,
            CommandRejected,
            VersionConflict,
            StationResourceError,
            TaskSchedulerBridgeError,
        ) as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/error-handling", summary="读取工站错误与处置状态")
    def snapshot(service: ErrorHandlingService = service_dependency) -> dict[str, Any]:
        return invoke(service.control.snapshot)

    @router.post(
        "/errors/{decision_id}/decision",
        status_code=202,
        summary="提交节点错误处理选择",
    )
    def decide(
        decision_id: str,
        body: DecisionRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.bridge.decide_error,
            decision_id,
            body.model_dump(),
            idempotency_key=idempotency_key,
            operator_id=actor(request),
        )

    @router.get("/sessions/{session_id}", summary="读取人工处置会话")
    def session(
        session_id: str, service: ErrorHandlingService = service_dependency
    ) -> dict[str, Any]:
        return invoke(service.control.get_session, session_id)

    @router.post(
        "/sessions/{session_id}/complete", summary="完成手动处理并继续等待决定"
    )
    def complete(
        session_id: str,
        body: CompleteRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.complete,
            session_id,
            body.model_dump(),
            idempotency_key=idempotency_key,
            operator_id=actor(request),
        )

    @router.post("/sessions/{session_id}/return", summary="返回同一会话继续手动处理")
    def return_manual(
        session_id: str,
        body: SessionVersion,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.control.return_to_manual,
            session_id,
            body.model_dump(),
            idempotency_key=idempotency_key,
            operator_id=actor(request),
        )

    @router.get(
        "/sessions/{session_id}/devices/{device_id}/actions",
        summary="读取设备全部可用动作",
    )
    def actions(
        session_id: str,
        device_id: str,
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(service.manual_actions, session_id, device_id)

    @router.post(
        "/sessions/{session_id}/manual-actions",
        status_code=202,
        summary="提交手动设备动作",
    )
    def execute(
        session_id: str,
        body: ManualActionRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.execute_manual,
            session_id,
            body.model_dump(),
            idempotency_key=idempotency_key,
            operator_id=actor(request),
        )

    @router.get("/manual-actions/{command_id}", summary="读取手动动作执行记录")
    def action(
        command_id: str, service: ErrorHandlingService = service_dependency
    ) -> dict[str, Any]:
        return invoke(service.control.manual_action, command_id)

    @router.post(
        "/manual-actions/{command_id}/stop",
        status_code=202,
        summary="请求停止指定手动动作",
    )
    def stop(
        command_id: str,
        body: StopRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.stop_manual,
            command_id,
            body.model_dump(),
            idempotency_key=idempotency_key,
            operator_id=actor(request),
        )

    @router.post(
        "/manual-actions/{command_id}/review", summary="确认已结束手动动作的现场核对结果"
    )
    def review(
        command_id: str,
        body: ManualReviewRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.control.review_manual, command_id, body.model_dump(),
            idempotency_key=idempotency_key, operator_id=actor(request),
        )

    @router.get("/manual-action-jobs/{job_uuid}/review", summary="读取历史手动动作的核对上下文")
    def historical_review_context(
        job_uuid: str, service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(service.control.manual_job_review_context, job_uuid)

    @router.post("/manual-action-jobs/{job_uuid}/review", summary="保存历史手动动作的现场核对记录")
    def historical_review(
        job_uuid: str,
        body: HistoricalManualReviewRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.control.review_manual_job, job_uuid, body.model_dump(),
            idempotency_key=idempotency_key, operator_id=actor(request),
        )

    @router.get("/control-commands/{command_uuid}", summary="读取已受理控制命令")
    def command(
        command_uuid: str, service: ErrorHandlingService = service_dependency
    ) -> dict[str, Any]:
        return invoke(service.control.get_command, command_uuid)

    @router.post(
        "/control-commands/{command_uuid}/apply",
        status_code=202,
        summary="继续应用已受理控制命令",
    )
    def apply_command(
        command_uuid: str, service: ErrorHandlingService = service_dependency
    ) -> dict[str, Any]:
        return invoke(service.apply_command, command_uuid)

    @router.get(
        "/sessions/{session_id}/inventory", summary="读取手动处理所需的库存事实"
    )
    def inventory(
        session_id: str, service: ErrorHandlingService = service_dependency
    ) -> dict[str, Any]:
        invoke(service.control.get_session, session_id)
        return invoke(service.bridge.error_inventory)

    @router.post(
        "/sessions/{session_id}/inventory-corrections",
        status_code=202,
        summary="登记人工确认的上下料与库位更正",
    )
    def correction(
        session_id: str,
        body: InventoryCorrectionRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.bridge.apply_auxiliary,
            f"inventory:{session_id}",
            {**body.model_dump(), "session_id": session_id},
            idempotency_key=idempotency_key,
            operator_id=actor(request),
        )

    @router.get("/resource-usage", summary="读取设备、库位与任务物料使用关系")
    def resource_usage(
        occupancy_uuid: list[str] | None = Query(default=None, max_length=100),
        binding_uuid: list[str] | None = Query(default=None, max_length=100),
        job_uuid: list[str] | None = Query(default=None, max_length=100),
        task_uuid: list[str] | None = Query(default=None, max_length=100),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        values = (occupancy_uuid, binding_uuid, job_uuid, task_uuid)
        selection = None if all(value is None for value in values) else dict(zip(
            ("occupancy_uuids", "binding_uuids", "job_uuids", "task_uuids"),
            (value or [] for value in values),
        ))
        return invoke(lambda: service.control.resource_usage(selection=selection))

    @router.get("/resource-usage/events")
    async def resource_usage_events(request: Request, service: ErrorHandlingService = service_dependency):
        from unilabos.app.resource_change_api import resource_change_stream

        return await resource_change_stream(request, service.control.store, scope="workflow")

    @router.get("/resource-dispositions", summary="读取当前逻辑资源占用")
    def occupancies(
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(service.control.occupancies)

    @router.post(
        "/resource-dispositions", status_code=202, summary="释放用户选中的逻辑资源占用"
    )
    def disposition(
        body: ResourceDispositionRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.bridge.apply_auxiliary,
            f"resource:{body.decision_id}",
            body.model_dump(),
            idempotency_key=idempotency_key,
            operator_id=actor(request),
        )

    @router.get("/events", summary="读取工站控制操作记录")
    def events(
        after: int = 0, service: ErrorHandlingService = service_dependency
    ) -> dict[str, Any]:
        return invoke(service.control.events, after)

    @router.post("/resume", summary="按用户确认恢复工站普通调度")
    def resume(
        body: ResumeRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        service: ErrorHandlingService = service_dependency,
    ) -> dict[str, Any]:
        return invoke(
            service.resume,
            body.model_dump(),
            idempotency_key=idempotency_key,
            operator_id=actor(request),
        )

    return router
