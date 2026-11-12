"""Backend Scheduler 与执行端的轻量诊断路由（含 status-incidents /
error-decisions / restart 干预入口），不复制 Runtime 数据面。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from unilabos.server.api.envelope import envelope_router, raw_response
from unilabos.protocol.runtime.lifecycle import (
    HealthStatus,
    ReadinessStatus,
    ResetRequest,
    ResetStatus,
    RestartRequest,
    RestartStatus,
)
from unilabos.server.api.runtime.lifecycle import create_scheduler_lifecycle_router


from unilabos.protocol.runtime.resource_intervals import ResourceIntervalReleaseRequest


class PhysicalSettlementConfirmation(ResourceIntervalReleaseRequest):
    result: dict[str, Any]


class StatusIncidentDecision(BaseModel):
    action: str = ""
    option: Optional[dict[str, Any]] = None
    reason: str = ""


class ErrorDecision(BaseModel):
    """V1 ``ErrorDecisionIn``：人工审批结果（与云端 ws job_error_decision 消息同语义）。

    字段与缺省值同 V1，未知字段忽略；``operator_id`` 是 dev 的可选扩展（缺省取请求上下文）。
    """

    action: str = ""  # retry / skip / abort / 其它已配置 option
    option: Optional[dict[str, Any]] = None  # 或直接回传选中的 option 对象
    result: Any = None  # operator_intervention 的服务端结果
    reason: str = ""
    operator_id: Optional[str] = None


def _hostlink_snapshot() -> dict[str, Any]:
    from unilabos.backend.hostlink.client import get_hostlink_client
    from unilabos.backend.hostlink.server import get_hostlink_server

    link_server = get_hostlink_server()
    link_client = get_hostlink_client()
    role = "host" if link_server else ("slave" if link_client else "disabled")
    result: dict[str, Any] = {
        "role": role,
        "peers": link_server.peers() if link_server else [],
        "client": (
            {
                "online": link_client.online,
                "host": link_client.host,
                "port": link_client.port,
                "node_id": link_client.node_id,
                "device_ids": link_client.device_ids,
                "capabilities": link_client.capabilities,
            }
            if link_client
            else None
        ),
    }
    if role != "disabled":
        hello = link_server.hello_payload if link_server else link_client.hello_info
        result.update(
            {
                "owner": hello.get("owner"),
                "host_id": hello.get("host_id") or hello.get("host_name"),
                "host_node_id": hello.get("host_node_id"),
                "protocol_version": hello.get("protocol_version"),
                "ros": hello.get("ros"),
            }
        )
    return result


def create_backend_router(
    get_scheduler: Callable[[], Any],
    get_execution_backend: Callable[[], Any],
) -> APIRouter:
    """创建不复制 Runtime/History/Telemetry 数据面的诊断路由。"""

    router = envelope_router(prefix="/api/v1", tags=["backend"])
    router.include_router(create_scheduler_lifecycle_router(get_scheduler, get_execution_backend))
    from unilabos.server.api.runtime.device_state import create_device_state_router

    # V1 设备状态视图（Host 数据面；分进程时由 edge 代理按 device-state 前缀转发）
    router.include_router(create_device_state_router(get_execution_backend))

    @router.get("/reset", response_model=ResetStatus)
    def reset_preview() -> dict[str, Any]:
        from unilabos.server.backend.reset import get_reset_controller, ResetConflict

        controller = get_reset_controller()
        if controller is None:
            return {"supported": False, "pending": False, "confirmation_token": "", "backup_path": "", "detail": "全量重置仅支持默认本机分离部署；独立远端或单进程请停机后维护"}
        try:
            return controller.preview()
        except ResetConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/reset", status_code=202, response_model=ResetStatus)
    def reset_request(body: ResetRequest, background: BackgroundTasks) -> dict[str, Any]:
        from unilabos.server.backend.reset import get_reset_controller, ResetConflict
        from unilabos.server.api.app import request_server_shutdown

        controller = get_reset_controller()
        if controller is None:
            raise HTTPException(status_code=409, detail="当前部署不支持全量重置")
        try:
            result = controller.request(body.confirmation_token, body.confirmation)
        except ResetConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        background.add_task(request_server_shutdown)
        return result

    def execution() -> Any:
        value = get_execution_backend()
        if value is None:
            raise HTTPException(
                status_code=503,
                detail="backend execution service is not ready",
            )
        return value

    def _execution_state() -> str:
        """ready：本进程带执行面，或调度权威的 Host 子进程已接入控制面；
        restarting：带 Host 子进程但它此刻不在线；disabled：纯调度权威。"""

        if get_execution_backend() is not None:
            return "ready"
        from unilabos.server.api.edge_proxy import edge_proxy_enabled
        from unilabos.server.backend.edge_control import get_edge_control_service

        if not edge_proxy_enabled():
            return "disabled"
        service = get_edge_control_service()
        return "ready" if service is not None and service.connected else "restarting"

    def _readiness() -> tuple[bool, dict[str, Any]]:
        backend = get_execution_backend()
        if backend is not None:
            # 本进程带执行面：执行适配器（HostLink / ROS2 host node）注册后才能派发
            host_ready = getattr(backend, "host_ready", None)
            execution = "ready" if not callable(host_ready) or bool(host_ready()) else "starting"
        else:
            execution = _execution_state()
        reasons: list[str] = []
        if execution == "starting":
            reasons.append("执行适配器尚未注册（设备初始化中）")
        elif execution == "restarting":
            reasons.append("Host 子进程未接入控制面（启动或重启中）")
        # disabled：纯调度权威（--role backend），执行由远端 Edge 接入，不在本进程判定
        scheduler = get_scheduler()
        if bool(getattr(scheduler, "dispatch_paused", False)):
            reasons.append("调度派发已暂停（排空、重启或维护中）")
        ready = execution in {"ready", "disabled"} and not reasons
        return ready, {
            "status": "ready" if ready else "not_ready",
            "scheduler": "local" if get_scheduler() is not None else "remote",
            "execution": execution,
            "reasons": reasons,
        }

    @router.get("/readiness", response_model=ReadinessStatus, responses={503: {"model": ReadinessStatus}})
    @raw_response
    def readiness() -> JSONResponse:
        """就绪探针：能接收并执行任务时 HTTP 200，否则 503（Kubernetes readiness 合同）。

        与 ``/health``（进程存活，恒 200）不同：Host 子进程启动 / 安静点重启期间、执行适配器
        尚未注册时返回 503；全量重置期间维护闸门同样返回 503。响应只含状态摘要。
        探针按 HTTP 状态码判定，所以本端点不包信封（``raw_response``）。
        """

        ready, summary = _readiness()
        return JSONResponse(status_code=200 if ready else 503, content=summary)

    @router.get("/health", response_model=HealthStatus)
    @raw_response
    def health() -> dict[str, str]:
        """存活探针：恒 HTTP 200 的裸 JSON（与 ``/readiness`` 一样不包信封，探针只看状态码与字段）。"""

        return {
            "status": "ok",
            "scheduler": "local" if get_scheduler() is not None else "remote",
            "execution": _execution_state(),
        }

    @router.get("/ping")
    def ping(client_timestamp: Optional[float] = None) -> dict[str, Any]:
        """HTTP 版 ping-pong：回显客户端时间戳并附服务端时钟，供链路时延 / 时钟偏差诊断。

        与控制 WebSocket 的 PingNotice / PongNotice 字段同名；host_node 的 test_latency
        对它所连的 Backend（本机进程或分体部署的 --role backend / 云端）逐次调用。
        """

        import time

        return {
            "client_timestamp": client_timestamp,
            "server_timestamp": time.time(),
            "scheduler": "local" if get_scheduler() is not None else "remote",
        }

    @router.get("/hostlink/peers")
    def hostlink_peers() -> dict[str, Any]:
        return _hostlink_snapshot()

    @router.get("/scheduler/resources")
    def scheduler_resources() -> dict[str, Any]:
        scheduler = get_scheduler()
        if scheduler is None:
            raise HTTPException(
                status_code=503,
                detail="scheduler authority is remote",
            )
        return scheduler.resource_snapshot().model_dump(
            mode="json",
            exclude_none=True,
        )

    @router.get("/scheduler/inventory-recovery")
    def inventory_recovery() -> dict[str, Any]:
        scheduler = get_scheduler()
        if scheduler is None:
            raise HTTPException(status_code=503, detail="scheduler authority is remote")
        return scheduler.inventory_recovery_snapshot()

    @router.post("/scheduler/inventory-recovery/reconcile")
    def reconcile_inventory() -> dict[str, Any]:
        scheduler = get_scheduler()
        if scheduler is None:
            raise HTTPException(status_code=503, detail="scheduler authority is remote")
        scheduler._reconcile_inventory()
        return scheduler.inventory_recovery_snapshot()

    @router.get("/scheduler/physical-recovery")
    def physical_recovery():
        scheduler = get_scheduler()
        if scheduler is None:
            raise HTTPException(status_code=503, detail="scheduler authority is remote")
        return scheduler._physical_recovery.snapshot()

    @router.post("/scheduler/physical-recovery/{job_uuid}/release")
    def release_physical(job_uuid: str, body: ResourceIntervalReleaseRequest):
        scheduler = get_scheduler()
        if scheduler is None:
            raise HTTPException(status_code=503, detail="scheduler authority is remote")
        try:
            return scheduler._physical_recovery.release_verified(job_uuid, body.expected_version, body.reason)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/scheduler/physical-recovery/{job_uuid}/settle")
    def settle_physical(job_uuid: str, body: PhysicalSettlementConfirmation):
        scheduler = get_scheduler()
        if scheduler is None:
            raise HTTPException(status_code=503, detail="scheduler authority is remote")
        try:
            return scheduler._physical_recovery.confirm_terminal_settlement(job_uuid, body.expected_version, body.reason, body.result)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get("/status-incidents")
    def status_incidents(
        device_id: str = "",
        include_terminal: bool = False,
    ) -> dict[str, Any]:
        backend = execution()
        manager = getattr(backend, "status_incidents", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="status policy is disabled")
        return {
            "host_ready": bool(backend.host_ready()),
            "incidents": manager.list(
                device_id=device_id,
                include_terminal=include_terminal,
            ),
            "holds": manager.holds(),
        }

    @router.post("/status-incidents/{incident_id}")
    def decide_status_incident(
        incident_id: str,
        body: StatusIncidentDecision,
    ) -> dict[str, Any]:
        backend = execution()
        manager = getattr(backend, "status_incidents", None)
        if manager is None:
            raise HTTPException(status_code=503, detail="status policy is disabled")
        try:
            result = manager.decide(
                incident_id,
                action=body.action,
                option=body.option,
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="status incident not found")
        return result["ack"]

    from unilabos.server.services.runtime.error_decisions import ErrorDecisionDesk

    desk = ErrorDecisionDesk(get_scheduler, get_execution_backend)

    @router.get("/error-decisions")
    def error_decisions() -> dict[str, Any]:
        """待人工决策的清单：执行面挂起的失败 attempt + 调度器在重启后接管的
        执行态未知 / 决策上下文丢失的 attempt，两者报告同形（V1 清单键 ``decisions``）。"""

        return {"decisions": desk.items()}

    @router.post("/error-decisions/{decision_id}")
    def resolve_error_decision(
        decision_id: str,
        body: ErrorDecision,
        request: Request,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        """V1：放行一项决策，``{"decision_id", "status": "delivered"}``；已不在等待时按 V1 的 404。

        载荷与 V1 同组装：``reason`` 恒带，``option`` / ``action`` / ``result`` 给出才带。
        dev 扩展（可选）：``Idempotency-Key`` 同键同内容重放首次结果；操作员身份随载荷记录。
        """

        from unilabos.server.api.runtime.tasks_v1 import operator_identity
        from unilabos.server.services.runtime.error_decisions import ErrorDecisionNotPending

        payload: dict[str, Any] = {"reason": body.reason}
        if body.option is not None:
            payload["option"] = body.option
        if body.action:
            payload["action"] = body.action
        if body.result is not None:
            payload["result"] = body.result
        try:
            return desk.resolve(
                decision_id, payload, idempotency_key=idempotency_key,
                operator_id=body.operator_id or operator_identity(request),
            )
        except ErrorDecisionNotPending as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── 安静点重启（调试用） ─────────────────────────────────

    @router.post("/restart", response_model=RestartStatus)
    def request_restart(body: RestartRequest) -> dict[str, Any]:
        """登记重启：暂停新派发，active job 清空后按 scope 重启并自动恢复。"""
        from unilabos.server.backend.restart import get_restart_coordinator

        try:
            return get_restart_coordinator().request(mode=body.mode, scope=body.scope)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/restart", response_model=RestartStatus)
    def restart_status() -> dict[str, Any]:
        from unilabos.server.backend.restart import get_restart_coordinator

        return get_restart_coordinator().status()

    @router.delete("/restart", response_model=RestartStatus)
    def cancel_restart() -> dict[str, Any]:
        from unilabos.server.backend.restart import get_restart_coordinator

        return get_restart_coordinator().cancel()

    return router


def create_backend_app(
    get_scheduler: Callable[[], Any] = lambda: None,
    get_execution_backend: Callable[[], Any] = lambda: None,
) -> FastAPI:
    app = FastAPI(title="Uni-Lab-OS Backend Diagnostics")
    app.include_router(create_backend_router(get_scheduler, get_execution_backend))
    return app


__all__ = [
    "ErrorDecision",
    "StatusIncidentDecision",
    "create_backend_app",
    "create_backend_router",
]
