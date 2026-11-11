"""调度权威、运行控制服务与 HostLink/ROS2 执行适配器的装配层。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from unilabos.config.config import BasicConfig
from unilabos.server.composition import (
    configure_server_services,
    shutdown_server_services,
)
from unilabos.server.database import ServerDatabasePaths
from unilabos.server.backend.execution import (
    JobExecutionBackend,
    make_device_materials_need_lock_resolver,
    make_device_status_policy_resolver,
)
from unilabos.server.backend.coordinator import WorkflowBusinessCoordinator
from unilabos.server.backend.scheduler.service import BackendScheduler
from unilabos.server.backend.telemetry import TelemetryDeviceStateProjection
from unilabos.protocol.base import canonical_json
from unilabos.protocol.history import HistoryEventAppend, InlinePayloadWrite
from unilabos.server.services.history import HistoryService
from unilabos.server.services.materials import MaterialsService

logger = logging.getLogger(__name__)

_backend: Optional[JobExecutionBackend] = None
_coordinator: Optional[WorkflowBusinessCoordinator] = None
_scheduler: Optional[BackendScheduler] = None
_materials: Optional[MaterialsService] = None
_materials_gateway: Any = None
_device_state_projection: Optional[TelemetryDeviceStateProjection] = None
_workflow_service: Any = None
_release_log_notices: Any = None


def _status_incident_history_listener(
    history: HistoryService,
    *,
    endpoint_uuid: str,
):
    """把实时状态联锁事实追加到 ``history_event``。"""

    def append(event: dict[str, Any]) -> None:
        incident = event.get("incident") or {}
        data = event.get("data") or {}
        event_type = str(event.get("type") or "status_incident")
        updated_at = float(
            incident.get("updated_at")
            or incident.get("created_at")
            or 0
        )
        payload = history.store_payload(
            InlinePayloadWrite(
                media_type="application/json",
                encoding="utf-8",
                inline_payload=canonical_json(event).encode("utf-8"),
            )
        )
        history.append_event(
            HistoryEventAppend(
                event_uuid=str(uuid4()),
                event_type="action_availability",
                endpoint_uuid=endpoint_uuid,
                device_uuid=str(incident.get("device_id") or "") or None,
                event_key=str(incident.get("policy_id") or event_type),
                payload_uuid=payload.payload_uuid,
                summary={
                    "event": event_type,
                    "incident_id": str(incident.get("incident_id") or ""),
                    "property_name": str(
                        incident.get("property_name") or ""
                    ),
                    "state": str(data.get("state") or incident.get("state") or ""),
                    "hold": bool(
                        (incident.get("hold") or {}).get("new_dispatch")
                    ),
                    "selected_action": str(
                        data.get("selected_action")
                        or incident.get("selected_action")
                        or ""
                    ),
                },
                severity=str(incident.get("severity") or "info"),
                actor_type=(
                    "operator"
                    if event_type == "status_incident_resolved"
                    else "device"
                ),
                actor_uuid=(
                    None
                    if event_type == "status_incident_resolved"
                    else str(incident.get("device_id") or "") or None
                ),
                occurred_at_ms=(
                    int(updated_at * 1000) if updated_at > 0 else None
                ),
            )
        )

    return append


def get_scheduler() -> Optional[BackendScheduler]:
    """返回本进程的 Backend Scheduler；接入云端（Backend-controlled）时为 ``None``。"""

    return _scheduler


def get_execution_backend() -> Optional[JobExecutionBackend]:
    return _backend


def get_business_coordinator() -> Optional[WorkflowBusinessCoordinator]:
    return _coordinator


def get_materials_service() -> Optional[MaterialsService]:
    """返回当前进程持有的 MaterialsService。"""

    return _materials


def get_workflow_service() -> Any:
    """返回本机 Workflow Authority；接入云端（Backend-controlled）时为 ``None``。"""

    return _workflow_service


def setup_local_scheduler(
    *,
    backend: Any = None,
) -> Any:
    """装配本机默认的 Workflow Authority（``local_scheduler`` profile）。

    本地调度模式由当前进程的 ``BackendScheduler`` 持有 WorkflowTask 权威，
    并复用 ``JobExecutionBackend`` 向执行适配器派发任务。Backend-controlled
    模式由远端 Backend 持有调度权威，不装配该服务。

    Workflow 表落 ``runtime.db``：WorkflowService 复用 RuntimeService 的
    connection 与 write_lock，保持单库单连接单写者。
    """

    global _workflow_service, _scheduler
    if _workflow_service is not None:
        return _workflow_service
    execution_backend = backend or _backend
    if execution_backend is None:
        raise RuntimeError("job execution backend must be ready first")

    from unilabos.server.backend.scheduler.authority import SchedulerAuthorityProfile
    from unilabos.server.composition import get_server_services
    from unilabos.server.services.runtime.workflow.service import WorkflowService

    services = get_server_services()
    if services is None:
        raise RuntimeError("server services must be configured first")

    service = WorkflowService(
        services.runtime,
        authority_profile=SchedulerAuthorityProfile.LOCAL_SCHEDULER,
    )
    scheduler = BackendScheduler(
        service,
        execution_backend,
        materials_gateway=_materials_gateway,
        materials_need_lock_resolver=(
            execution_backend.resolve_material_lock_parameters
        ),
        device_state_reader=make_device_state_reader(execution_backend),
    )
    service.configure_preflight(
        materials=_materials_gateway, endpoints=read_preflight_endpoints,
        action_schema_resolver=getattr(execution_backend, "resolve_action_schema", None),
    )
    service.set_task_submitter(scheduler.submit)
    # 本机调度器是其派发 job 的生命周期 owner：失败 attempt 挂起等待决策时由它
    # 把 attempt/节点运行置为 intervention_required（执行面按 origin 路由）。
    execution_backend.result_bridges.append(scheduler)
    scheduler.start(recover=True)
    _start_event_retention(service)
    _workflow_service = service
    _scheduler = scheduler
    _setup_registry_authority(services.runtime)
    logger.info(
        "[WorkflowIntegration] local Workflow Authority ready (runtime.db shared)",
    )
    return service


_event_retention: Any = None


def _start_event_retention(service: Any) -> None:
    """本机 Workflow Authority 拥有 frontend_event 的保留清理（v1.0 ADR 0006）。"""

    global _event_retention
    if _event_retention is not None:
        return
    from unilabos.server.services.runtime.workflow.event_retention import FrontendEventRetention

    try:
        retention = FrontendEventRetention(service)
    except ValueError as exc:
        logger.error("[WorkflowIntegration] 前端事件保留配置无效，未启动清理: %s", exc)
        return
    retention.start()
    _event_retention = retention


def _stop_event_retention() -> None:
    global _event_retention
    if _event_retention is not None:
        _event_retention.stop()
        _event_retention = None


def read_preflight_endpoints() -> list[dict[str, Any]]:
    """与浏览器共享当前 Host 的在线能力快照，避免读取权威进程的空遥测镜像。"""
    import json
    from unilabos.server.api.edge_proxy import edge_http, edge_proxy_enabled
    from unilabos.server.composition import get_server_services

    if edge_proxy_enabled():
        response = edge_http("GET", "/api/v1/runtime/endpoints?limit=1000", timeout=5.0)
        if response is None or response.status_code != 200:
            raise RuntimeError("Host 在线能力暂不可用")
        result = json.loads(response.body_bytes())
    else:
        services = get_server_services()
        if services is None:
            raise RuntimeError("设备运行服务尚未启动")
        result = services.runtime.list_endpoint_snapshots(limit=1000)
    if not isinstance(result, list):
        raise ValueError("设备能力快照格式无效")
    return [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item) for item in result]


def make_device_state_reader(execution_backend: Any):
    """工作流 while 循环读设备状态字段的入口：``device_id -> {field: {"value", "updated_at"}}``。

    Host 与调度权威同进程时直接读执行面的设备状态投影（telemetry 最新快照）；默认拓扑下
    Host 是不监听端口的子进程，遥测库在它那边，权威经控制面让 Host 执行
    ``GET /api/v1/telemetry/states`` 再按 device_uuid 取最新一条。
    """

    import json

    projection = getattr(execution_backend, "device_state", None)
    latest_for = getattr(projection, "latest_for", None)
    if callable(latest_for):
        return latest_for

    def read_via_host(device_id: str) -> dict[str, Any]:
        from unilabos.server.api.edge_proxy import edge_http

        response = edge_http("GET", "/api/v1/telemetry/states", timeout=5.0)
        if response is None:
            raise RuntimeError("Host 未接入控制面，读不到设备状态")
        if response.status_code != 200:
            raise RuntimeError(
                f"Host 遥测接口返回 {response.status_code}，读不到设备状态"
            )
        states = json.loads(response.body_bytes().decode("utf-8") or "[]")
        latest: Optional[dict[str, Any]] = None
        for state in states if isinstance(states, list) else []:
            if not isinstance(state, dict) or str(state.get("device_uuid")) != str(device_id):
                continue
            if latest is None or int(state.get("observed_at_ms") or 0) > int(latest.get("observed_at_ms") or 0):
                latest = state
        if latest is None:
            return {}
        observed = latest.get("observed_at_ms")
        return {
            str(name): {"value": value, "updated_at": observed}
            for name, value in (latest.get("properties") or {}).items()
        }

    return read_via_host


def _setup_registry_authority(runtime_service: Any) -> None:
    """Registry Authority 与 Workflow Authority 同生命周期：谁持有调度权威，谁维护
    注册表条目版本。三个注册表表与 RuntimeService 共用 runtime.db 的连接和写锁；
    影响活跃 workflow 的动作变更进入待确认状态，其余变更直接生效。"""

    from unilabos.server.services.runtime.registry import (
        RegistryService,
        get_registry_service,
        set_registry_service,
    )

    if get_registry_service() is not None:
        return

    def _workflow_action_reference_rows():
        workflow_service = get_workflow_service()
        if workflow_service is None:
            return []
        return workflow_service.list_template_action_references()

    set_registry_service(
        RegistryService(
            runtime_service,
            reference_rows_resolver=_workflow_action_reference_rows,
        )
    )


def setup_materials_service(
    *,
    database_paths: Optional[ServerDatabasePaths] = None,
) -> MaterialsService:
    """从统一 ServerServices 装配 materials writer。"""

    global _materials
    if _materials is not None:
        return _materials

    paths = database_paths or BasicConfig.server_database_paths
    if not isinstance(paths, ServerDatabasePaths):
        raise RuntimeError("ServerDatabasePaths must be configured before startup")
    _materials = configure_server_services(paths).materials

    logger.info("[MaterialsIntegration] materials.v1 writer ready")
    return _materials


def set_materials_gateway(gateway: Any) -> None:
    """Publish the Host-selected embedded/external materials authority."""

    global _materials_gateway
    _materials_gateway = gateway


def get_materials_gateway() -> Any:
    return _materials_gateway


def setup_execution_backend(
    control_client: Any = None,
    *,
    host_node_getter: Any = None,
    database_paths: Optional[ServerDatabasePaths] = None,
    materials_gateway: Any = None,
) -> JobExecutionBackend:
    """装配 Job 执行面、运行时协调器及设备状态投影。"""

    global _backend, _coordinator, _device_state_projection, _release_log_notices
    if _backend is not None:
        return _backend

    paths = database_paths or BasicConfig.server_database_paths
    if not isinstance(paths, ServerDatabasePaths):
        raise RuntimeError("ServerDatabasePaths must be configured before startup")
    services = configure_server_services(paths)
    endpoint_uuid = ":".join(
        (
            BasicConfig.backend,
            BasicConfig.machine_name or BasicConfig.host_node_name or "host",
        )
    )
    _device_state_projection = TelemetryDeviceStateProjection(
        services.telemetry,
        endpoint_uuid=endpoint_uuid,
    )

    from unilabos.server.backend.incidents import StatusIncidentManager

    status_incidents = StatusIncidentManager()
    status_incidents.add_listener(
        _status_incident_history_listener(
            services.history,
            endpoint_uuid=endpoint_uuid,
        )
    )
    backend = JobExecutionBackend(
        host_node_getter=host_node_getter,
        device_state_store=_device_state_projection,
        status_policy_resolver=make_device_status_policy_resolver(host_node_getter),
        status_incidents=status_incidents,
        result_bridges=[],
        materials_need_lock_resolver=make_device_materials_need_lock_resolver(
            host_node_getter
        ),
        materials_gateway=(
            materials_gateway
            if materials_gateway is not None
            else _materials_gateway
        ),
    )
    from unilabos.config.config import HTTPConfig
    if HTTPConfig.remote_addr:
        from unilabos.client.runtime.workflow import HTTPWorkflowClient
        backend.configure_execution_authority(HTTPWorkflowClient(HTTPConfig.remote_addr, timeout=5.0))
    coordinator = WorkflowBusinessCoordinator(
        services.runtime,
        services.history,
        backend,
        endpoint_uuid=endpoint_uuid,
        transport=BasicConfig.backend,
        host_uuid=BasicConfig.machine_name or BasicConfig.host_node_name or "host",
        instance_name=BasicConfig.host_node_name or "host",
        notice_callback=(
            getattr(control_client, "publish_runtime_events", None)
            if control_client is not None
            else None
        ),
    )
    backend.result_bridges.append(coordinator)
    # 旧协议 Backend 客户端不是生命周期 owner，但要把微后端释放的 job 结果
    # 镜像成 job_status 回旧后端，因此也挂为 result bridge。
    if control_client is not None and getattr(control_client, "mirrors_job_results", False):
        backend.result_bridges.append(control_client)
    elif control_client is not None:
        from unilabos.utils.log_notices import log_notices

        # 复用 runtime.v1 控制连接；旧后端的 legacy result bridge 不订阅此通知。
        # 不落 durable outbox，避免日志噪声占用四库和调度事件序号。
        _release_log_notices = log_notices.subscribe(
            lambda notice: control_client._queue_message({
                "action": "runtime_logs_changed", "data": notice.model_dump(),
            })
        )
    _coordinator = coordinator
    backend.start()
    backend.rebuild_status_incidents()
    coordinator.restore()
    _backend = backend
    logger.info(
        "[JobExecutionIntegration] backend-controlled microbackend ready (%s)",
        endpoint_uuid,
    )
    return backend


def shutdown_backend_services() -> None:
    """关闭执行 bridge 和四库组合根。"""

    global _backend, _coordinator, _materials, _materials_gateway
    global _device_state_projection, _workflow_service, _scheduler, _release_log_notices

    if _release_log_notices is not None:
        _release_log_notices()
        _release_log_notices = None

    if BasicConfig.backend == "ros2":
        from unilabos.backend.hostlink.network import shutdown_network_services

        shutdown_network_services()
    if _scheduler is not None:
        _scheduler.stop()
        if _backend is not None and _scheduler in _backend.result_bridges:
            _backend.result_bridges.remove(_scheduler)
    # 先等清理线程结束当前事务，再关闭共享的 runtime.db 连接。
    _stop_event_retention()
    if _workflow_service is not None:
        _workflow_service.set_task_submitter(None)
        set_resolver = getattr(
            _workflow_service, "set_manual_confirmation_resolver", None
        )
        if callable(set_resolver):
            set_resolver(None)
        _workflow_service.close()
    if _backend is not None:
        _backend.stop()
    # RegistryService 共享 RuntimeService 的连接，随组合根一起关闭。
    from unilabos.server.services.runtime.registry import set_registry_service

    set_registry_service(None)
    shutdown_server_services()

    _backend = None
    _coordinator = None
    _materials = None
    _materials_gateway = None
    _device_state_projection = None
    _workflow_service = None
    _scheduler = None


def reset_for_test() -> None:
    shutdown_backend_services()


__all__ = [
    "get_execution_backend",
    "get_business_coordinator",
    "get_scheduler",
    "get_materials_service",
    "get_materials_gateway",
    "get_workflow_service",
    "reset_for_test",
    "setup_execution_backend",
    "setup_local_scheduler",
    "setup_materials_service",
    "set_materials_gateway",
    "shutdown_backend_services",
]
