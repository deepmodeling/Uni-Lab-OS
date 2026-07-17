"""Task 编排服务的最小 HTTP 路由。"""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ..models import (
    AdvanceRequest,
    GenerateInstancesRequest,
    MoveInstanceRequest,
    OpcPushRequest,
    ScheduleRequest,
    ScheduledTemplatesUpdateRequest,
    TemplateCreateRequest,
    TemplateUpdateRequest,
    VersionedWorkspaceResponse,
    WorkspaceUpdateRequest,
)
from ..service import WorkspaceService, WorkspaceServiceError
from ..store import (
    SidecarCorruptionError,
    VersionConflictError,
    WorkflowPathError,
    WorkspaceStore,
)


def public_workspace_response(response: VersionedWorkspaceResponse) -> dict:
    """唯一的公网工作区投影：绝不序列化 OPC 原始值。"""
    payload = response.model_dump(mode="json")
    payload["workspace"]["opc_snapshots"] = [
        {
            "provider_id": snapshot.provider_id,
            "sequence": snapshot.sequence,
            "updated_at_by_variable": snapshot.updated_at_by_variable,
            "variable_count": len(snapshot.values),
        }
        for snapshot in response.workspace.opc_snapshots
    ]
    return payload


def create_router(store: WorkspaceStore, service: WorkspaceService | None = None) -> APIRouter:
    """创建绑定到指定存储实例的路由。"""
    router = APIRouter()
    workspace_service = service or WorkspaceService(store)
    admin_token = os.getenv("TASK_ORCHESTRATION_ADMIN_TOKEN")
    gateway_token = os.getenv("TASK_ORCHESTRATION_GATEWAY_TOKEN")
    ui_token = os.getenv("TASK_ORCHESTRATION_UI_TOKEN")

    def require_token(configured_token: str | None, authorization: str | None) -> None:
        if not configured_token:
            raise HTTPException(status_code=403, detail="administrative write is disabled")
        expected = f"Bearer {configured_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=403, detail="invalid bearer token")

    def require_ui_token(authorization: str | None) -> None:
        if not ui_token:
            raise HTTPException(status_code=403, detail="UI write is disabled")
        require_token(ui_token, authorization)

    def business_error(exc: WorkspaceServiceError) -> HTTPException:
        return HTTPException(
            status_code=404
            if exc.code in {"template_not_found", "instance_not_found"}
            else 409,
            detail={"code": exc.code, "message": str(exc)},
        )

    def mutation_error(exc: Exception) -> HTTPException:
        if isinstance(exc, VersionConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, WorkspaceServiceError):
            return business_error(exc)
        if isinstance(exc, SidecarCorruptionError):
            return HTTPException(status_code=422, detail="invalid task workspace sidecar")
        if isinstance(exc, WorkflowPathError):
            return HTTPException(status_code=422, detail=str(exc))
        raise exc

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/workspaces")
    def get_workspace(
        workflow_path: str = Query(min_length=1),
    ) -> dict:
        try:
            response = store.get(workflow_path)
            return public_workspace_response(response)
        except SidecarCorruptionError as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid task workspace sidecar",
            ) from exc
        except WorkflowPathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.put("/workspaces")
    def put_workspace(
        request: WorkspaceUpdateRequest,
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_token(admin_token, authorization)
        try:
            return public_workspace_response(store.put(
                request.workspace,
                expected_version=request.expected_version,
            ))
        except VersionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SidecarCorruptionError as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid task workspace sidecar",
            ) from exc
        except WorkflowPathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/templates")
    def create_template(request: TemplateCreateRequest, authorization: str | None = Header(default=None)) -> dict:
        require_ui_token(authorization)
        try:
            return public_workspace_response(workspace_service.create_template(
                request.workflow_path, request.expected_version, request.template
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.patch("/templates/{template_id}")
    def update_template(
        template_id: str, request: TemplateUpdateRequest, authorization: str | None = Header(default=None)
    ) -> dict:
        require_ui_token(authorization)
        try:
            return public_workspace_response(workspace_service.update_template(
                request.workflow_path,
                request.expected_version,
                template_id,
                name=request.name,
                trigger=request.trigger,
                input_triggers=request.input_triggers,
                output_triggers=request.output_triggers,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.delete("/templates/{template_id}")
    def delete_template(
        template_id: str, workflow_path: str = Query(min_length=1), expected_version: int = Query(ge=0),
        authorization: str | None = Header(default=None),
    ) -> dict:
        require_ui_token(authorization)
        try:
            return public_workspace_response(workspace_service.delete_template(
                workflow_path, expected_version, template_id
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.put("/workspaces/scheduled-templates")
    def update_scheduled_templates(request: ScheduledTemplatesUpdateRequest, authorization: str | None = Header(default=None)) -> dict:
        require_ui_token(authorization)
        try:
            return public_workspace_response(
                workspace_service.update_scheduled_templates(
                    request.workflow_path,
                    request.expected_version,
                    request.template_ids,
                )
            )
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/instances:generate")
    def generate_instances(request: GenerateInstancesRequest, authorization: str | None = Header(default=None)) -> dict:
        require_ui_token(authorization)
        try:
            return public_workspace_response(workspace_service.generate_instances(
                request.workflow_path,
                request.expected_version,
                request.template_ids,
                request.sample_ids,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/instances/{instance_id}:move")
    def move_instance(
        instance_id: str, request: MoveInstanceRequest, authorization: str | None = Header(default=None)
    ) -> dict:
        require_ui_token(authorization)
        try:
            return public_workspace_response(workspace_service.move_instance(
                request.workflow_path, request.expected_version, instance_id, request.order
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/opc/snapshots")
    async def push_opc_snapshot(
        request: OpcPushRequest,
        raw_request: Request,
        authorization: str | None = Header(default=None),
        content_length: int | None = Header(default=None, alias="Content-Length"),
    ) -> dict:
        if content_length is not None and content_length > 16 * 1024:
            raise HTTPException(status_code=422, detail="OPC snapshot body exceeds 16 KiB")
        if len(await raw_request.body()) > 16 * 1024:
            raise HTTPException(status_code=422, detail="OPC snapshot body exceeds 16 KiB")
        require_token(gateway_token, authorization)
        try:
            response, accepted = workspace_service.push_opc_snapshot(
                request.workflow_path,
                request.expected_version,
                request.provider_id,
                request.sequence,
                request.values,
            )
            return {"accepted": accepted, **public_workspace_response(response)}
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/schedule:plan")
    def plan(request: ScheduleRequest, authorization: str | None = Header(default=None)) -> dict:
        require_ui_token(authorization)
        try:
            response, schedule = workspace_service.plan(
                request.workflow_path, request.expected_version, paused=request.paused
            )
            return {
                **public_workspace_response(response),
                "schedule": schedule.model_dump(mode="json"),
            }
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/schedule:advance")
    def advance(request: AdvanceRequest, authorization: str | None = Header(default=None)) -> dict:
        require_ui_token(authorization)
        try:
            response, schedule = workspace_service.advance(
                request.workflow_path,
                request.expected_version,
                request.completed_instance_ids,
            )
            return {
                **public_workspace_response(response),
                "schedule": schedule.model_dump(mode="json"),
            }
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    return router
