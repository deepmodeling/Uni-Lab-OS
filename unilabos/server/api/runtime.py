"""``runtime.v1`` FastAPI 路由；写接口只映射合法控制命令。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query

from unilabos.server.protocol.runtime import (
    AdapterCommandAck,
    AdapterCommandClaim,
    AdapterCommandEnqueue,
    BackendEventAck,
    BackendEventClaim,
    BackendEventEnqueue,
    BackendSessionUpsert,
    CommandEnvelope,
    EndpointSnapshotUpsert,
    ErrorGateDecision,
    ErrorGateOpen,
    ExecutionJobCancel,
    ExecutionJobCreate,
    ExecutionJobFeedback,
    ExecutionJobTransition,
)
from unilabos.server.services.runtime import (
    RuntimeConflictError,
    RuntimeNotFoundError,
    RuntimeService,
    RuntimeValidationError,
)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except RuntimeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_runtime_router(service: RuntimeService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/runtime", tags=["runtime-v1"])

    # Backend session 是状态机 upsert，不提供删除或任意字段 patch。
    @router.put("/sessions/{session_uuid}")
    async def upsert_backend_session(session_uuid: str, value: BackendSessionUpsert):
        if value.session_uuid != session_uuid:
            raise HTTPException(status_code=422, detail="session UUID path mismatch")
        return _call(service.upsert_backend_session, value)

    @router.get("/sessions")
    async def list_backend_sessions(
        edge_uuid: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return _call(
            service.list_backend_sessions,
            edge_uuid=edge_uuid,
            state=state,
            limit=limit,
        )

    @router.get("/sessions/{session_uuid}")
    async def get_backend_session(session_uuid: str):
        return _call(service.get_backend_session, session_uuid)

    # Endpoint 只接受完整快照，避免 partial update 产生混合 epoch。
    @router.put("/endpoints/{endpoint_uuid}/snapshot")
    async def upsert_endpoint_snapshot(
        endpoint_uuid: str, value: EndpointSnapshotUpsert
    ):
        if value.endpoint_uuid != endpoint_uuid:
            raise HTTPException(status_code=422, detail="endpoint UUID path mismatch")
        return _call(service.upsert_endpoint_snapshot, value)

    @router.get("/endpoints")
    async def list_endpoint_snapshots(
        transport: Optional[str] = None,
        state: Optional[str] = None,
        host_uuid: Optional[str] = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return _call(
            service.list_endpoint_snapshots,
            transport=transport,
            state=state,
            host_uuid=host_uuid,
            limit=limit,
        )

    @router.get("/endpoints/{endpoint_uuid}")
    async def get_endpoint_snapshot(endpoint_uuid: str):
        return _call(service.get_endpoint_snapshot, endpoint_uuid)

    # Command inbox 仅允许幂等 receive；终态由具体业务命令原子推进。
    @router.post("/commands")
    async def receive_command(value: CommandEnvelope):
        return _call(service.receive_command, value)

    @router.get("/commands")
    async def list_commands(
        session_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        command_type: Optional[str] = None,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return _call(
            service.list_commands,
            session_uuid=session_uuid,
            status=status,
            job_uuid=job_uuid,
            command_type=command_type,
            after_sequence=after_sequence,
            limit=limit,
        )

    @router.get("/commands/{command_uuid}")
    async def get_command(command_uuid: str):
        return _call(service.get_command, command_uuid)

    # Job 更新必须走状态转换或 terminal error gate。
    @router.post("/jobs")
    async def create_execution_job(value: ExecutionJobCreate):
        return _call(service.create_execution_job, value)

    @router.get("/jobs")
    async def list_execution_jobs(
        status: Optional[str] = None,
        device_uuid: Optional[str] = None,
        endpoint_uuid: Optional[str] = None,
        retry_of_job_uuid: Optional[str] = None,
        attempt_group_uuid: Optional[str] = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return _call(
            service.list_execution_jobs,
            status=status,
            device_uuid=device_uuid,
            endpoint_uuid=endpoint_uuid,
            retry_of_job_uuid=retry_of_job_uuid,
            attempt_group_uuid=attempt_group_uuid,
            limit=limit,
        )

    @router.get("/jobs/{job_uuid}")
    async def get_execution_job(job_uuid: str):
        return _call(service.get_execution_job, job_uuid)

    @router.post("/jobs/{job_uuid}/transitions")
    async def transition_execution_job(job_uuid: str, value: ExecutionJobTransition):
        return _call(service.transition_execution_job, job_uuid, value)

    @router.post("/jobs/{job_uuid}/feedback")
    async def record_execution_feedback(job_uuid: str, value: ExecutionJobFeedback):
        return _call(service.record_execution_feedback, job_uuid, value)

    @router.post("/jobs/{job_uuid}/cancel")
    async def request_execution_cancel(job_uuid: str, value: ExecutionJobCancel):
        return _call(service.request_execution_cancel, job_uuid, value)

    @router.post("/jobs/{job_uuid}/error-gate/open")
    async def open_error_gate(job_uuid: str, value: ErrorGateOpen):
        return _call(service.open_error_gate, job_uuid, value)

    @router.post("/jobs/{job_uuid}/error-gate/decision")
    async def decide_error_gate(job_uuid: str, value: ErrorGateDecision):
        return _call(service.decide_error_gate, job_uuid, value)

    # Adapter outbox 是 append + claim + ACK，不暴露 update/delete。
    @router.post("/adapter-commands")
    async def enqueue_adapter_command(value: AdapterCommandEnqueue):
        return _call(service.enqueue_adapter_command, value)

    @router.get("/adapter-commands")
    async def list_adapter_commands(
        endpoint_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return _call(
            service.list_adapter_commands,
            endpoint_uuid=endpoint_uuid,
            status=status,
            job_uuid=job_uuid,
            after_sequence=after_sequence,
            limit=limit,
        )

    @router.post("/adapter-commands/claim")
    async def claim_adapter_commands(value: AdapterCommandClaim):
        return _call(service.claim_adapter_commands, value)

    @router.post("/adapter-commands/ack")
    async def acknowledge_adapter_command(value: AdapterCommandAck):
        return _call(service.acknowledge_adapter_command, value)

    @router.get("/adapter-commands/{adapter_command_uuid}")
    async def get_adapter_command(adapter_command_uuid: str):
        return _call(service.get_adapter_command, adapter_command_uuid)

    # Backend event outbox 同样保持 append-only 与单调 ACK cursor。
    @router.post("/backend-events")
    async def enqueue_backend_event(value: BackendEventEnqueue):
        return _call(service.enqueue_backend_event, value)

    @router.get("/backend-events")
    async def list_backend_events(
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        aggregate_type: Optional[str] = None,
        aggregate_uuid: Optional[str] = None,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return _call(
            service.list_backend_events,
            status=status,
            job_uuid=job_uuid,
            aggregate_type=aggregate_type,
            aggregate_uuid=aggregate_uuid,
            after_sequence=after_sequence,
            limit=limit,
        )

    @router.post("/backend-events/claim")
    async def claim_backend_events(value: BackendEventClaim):
        return _call(service.claim_backend_events, value)

    @router.post("/backend-events/ack")
    async def acknowledge_backend_events(value: BackendEventAck):
        return {"acknowledged": _call(service.acknowledge_backend_events, value)}

    @router.get("/backend-events/{event_uuid}")
    async def get_backend_event(event_uuid: str):
        return _call(service.get_backend_event, event_uuid)

    return router


def install_runtime_api(app: FastAPI, service: RuntimeService) -> None:
    app.include_router(create_runtime_router(service))


__all__ = ["create_runtime_router", "install_runtime_api"]
