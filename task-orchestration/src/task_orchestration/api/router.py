"""Task 编排服务的最小 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ..models import (
    ActionClaimRequest,
    ActionFailRequest,
    ActionSucceedRequest,
    AdvanceRequest,
    ClearInstancesRequest,
    GenerateInstancesRequest,
    InstanceParametersUpdateRequest,
    MoveInstanceRequest,
    PlcRegistrationRequest,
    OpcPushRequest,
    ScheduleRequest,
    ResetInstancesProgressRequest,
    ScheduledTemplatesUpdateRequest,
    TemplateCreateRequest,
    TemplateUpdateRequest,
    TemplatesDeleteRequest,
    VersionedWorkspaceResponse,
    WorkspaceResetRequest,
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
    """向排程 UI 返回 PLC 已分发的快照和值。"""
    return response.model_dump(mode="json")


def create_router(store: WorkspaceStore, service: WorkspaceService | None = None) -> APIRouter:
    """创建绑定到指定存储实例的路由。"""
    router = APIRouter()
    workspace_service = service or WorkspaceService(store)

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
    def put_workspace(request: WorkspaceUpdateRequest) -> dict:
        try:
            return public_workspace_response(store.put(
                request.workspace.model_copy(
                    update={"dynamic_resource_leases": []}
                ),
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

    @router.post("/workspaces/reset")
    def reset_workspace(request: WorkspaceResetRequest) -> dict:
        try:
            return public_workspace_response(
                workspace_service.reset_workspace(request.workflow_path)
            )
        except WorkflowPathError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/templates")
    def create_template(request: TemplateCreateRequest) -> dict:
        try:
            return public_workspace_response(workspace_service.create_template(
                request.workflow_path, request.expected_version, request.template
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/templates:delete")
    def delete_templates(request: TemplatesDeleteRequest) -> dict:
        try:
            return public_workspace_response(workspace_service.delete_templates(
                request.workflow_path,
                request.expected_version,
                request.template_ids,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.patch("/templates/{template_id}")
    def update_template(
        template_id: str, request: TemplateUpdateRequest
    ) -> dict:
        try:
            return public_workspace_response(workspace_service.update_template(
                request.workflow_path,
                request.expected_version,
                template_id,
                name=request.name,
                input_triggers=request.input_triggers,
                output_triggers=request.output_triggers,
                dependencies=request.dependencies,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.delete("/templates/{template_id}")
    def delete_template(
        template_id: str, workflow_path: str = Query(min_length=1), expected_version: int = Query(ge=0),
    ) -> dict:
        try:
            return public_workspace_response(workspace_service.delete_template(
                workflow_path, expected_version, template_id
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.put("/workspaces/scheduled-templates")
    def update_scheduled_templates(request: ScheduledTemplatesUpdateRequest) -> dict:
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
    def generate_instances(request: GenerateInstancesRequest) -> dict:
        try:
            return public_workspace_response(workspace_service.generate_instances(
                request.workflow_path,
                request.expected_version,
                request.template_ids,
                request.sample_ids,
                sample_start_interval_seconds=request.sample_start_interval_seconds,
                template_node_parameters=request.template_node_parameters,
                sample_template_node_parameters=request.sample_template_node_parameters,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/instances:clear")
    def clear_instances(request: ClearInstancesRequest) -> dict:
        try:
            return public_workspace_response(workspace_service.clear_instances(
                request.workflow_path,
                request.expected_version,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/instances:reset-progress")
    def reset_instances_progress(request: ResetInstancesProgressRequest) -> dict:
        try:
            return public_workspace_response(
                workspace_service.reset_instances_progress(
                    request.workflow_path,
                    request.expected_version,
                )
            )
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/instances/{instance_id}:move")
    def move_instance(
        instance_id: str, request: MoveInstanceRequest
    ) -> dict:
        try:
            return public_workspace_response(workspace_service.move_instance(
                request.workflow_path, request.expected_version, instance_id, request.order
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.patch("/instances/{instance_id}/parameters")
    def update_instance_parameters(
        instance_id: str, request: InstanceParametersUpdateRequest
    ) -> dict:
        try:
            return public_workspace_response(
                workspace_service.update_instance_parameters(
                    request.workflow_path,
                    request.expected_version,
                    instance_id,
                    request.node_parameters,
                )
            )
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/opc/snapshots")
    async def push_opc_snapshot(
        request: OpcPushRequest,
        raw_request: Request,
        content_length: int | None = Header(default=None, alias="Content-Length"),
    ) -> dict:
        if content_length is not None and content_length > 16 * 1024:
            raise HTTPException(status_code=422, detail="OPC snapshot body exceeds 16 KiB")
        if len(await raw_request.body()) > 16 * 1024:
            raise HTTPException(status_code=422, detail="OPC snapshot body exceeds 16 KiB")
        try:
            response, accepted = workspace_service.push_opc_snapshot(
                request.workflow_path,
                request.expected_version,
                request.plc_device_id,
                request.sequence,
                request.values,
            )
            return {"accepted": accepted, **public_workspace_response(response)}
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/opc/registrations")
    def register_plc_variables(request: PlcRegistrationRequest) -> dict:
        try:
            return public_workspace_response(
                workspace_service.register_plc_variables(
                    request.workflow_path,
                    request.expected_version,
                    request.registration,
                )
            )
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/schedule:plan")
    def plan(request: ScheduleRequest) -> dict:
        try:
            response, schedule = workspace_service.plan(
                request.workflow_path,
                request.expected_version,
                paused=request.paused,
                acknowledge_peer_failure=request.acknowledge_peer_failure,
            )
            return {
                **public_workspace_response(response),
                "schedule": schedule.model_dump(mode="json"),
            }
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/schedule:advance")
    def advance(request: AdvanceRequest) -> dict:
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

    @router.post("/actions:claim")
    def claim_action(request: ActionClaimRequest) -> dict:
        try:
            return public_workspace_response(workspace_service.claim_action(
                request.workflow_path,
                request.expected_version,
                request.instance_id,
                request.node_id,
                request.execution_id,
                request.resources,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/actions:succeed")
    def succeed_action(request: ActionSucceedRequest) -> dict:
        try:
            return public_workspace_response(workspace_service.succeed_action(
                request.workflow_path,
                request.expected_version,
                request.instance_id,
                request.node_id,
                request.execution_id,
                result=request.result,
                release_resources=request.release_resources,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    @router.post("/actions:fail")
    def fail_action(request: ActionFailRequest) -> dict:
        try:
            return public_workspace_response(workspace_service.fail_action(
                request.workflow_path,
                request.expected_version,
                request.instance_id,
                request.node_id,
                request.execution_id,
                error=request.error,
            ))
        except (VersionConflictError, WorkspaceServiceError, SidecarCorruptionError, WorkflowPathError) as exc:
            raise mutation_error(exc) from exc

    return router
