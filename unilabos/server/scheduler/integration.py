"""后端受控微后端与 HostLink/ROS2 执行 bridge 的装配层。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from unilabos.config.config import BasicConfig
from unilabos.server.composition import (
    configure_server_services,
    shutdown_server_services,
)
from unilabos.server.database import ServerDatabasePaths
from unilabos.server.scheduler.backend import (
    JobExecutionBackend,
    make_device_status_policy_resolver,
)
from unilabos.server.scheduler.telemetry_state import TelemetryDeviceStateProjection
from unilabos.server.services.materials import MaterialsService
from unilabos.utils.tracing import inject_trace_context

logger = logging.getLogger(__name__)

_backend: Optional[JobExecutionBackend] = None
_materials: Optional[MaterialsService] = None
_materials_gateway: Any = None
_owns_materials = False
_device_state_projection: Optional[TelemetryDeviceStateProjection] = None


class CloudBusinessError(RuntimeError):
    """Cloud returned HTTP success but a non-zero ``common.Resp.code``."""

    def __init__(self, code: int, message: str, info: Optional[list[str]] = None):
        super().__init__(message)
        self.code = code
        self.info = info or []


def unwrap_cloud_response(body: object) -> Any:
    """Validate and unwrap the Go Cloud envelope without hiding business errors."""

    from unilabos.server.scheduler.inventory.schemas import CloudResponse

    envelope = CloudResponse.model_validate(body)
    if envelope.code != 0:
        message = (
            envelope.error.msg
            if envelope.error is not None
            else f"Cloud business error {envelope.code}"
        )
        info = envelope.error.info if envelope.error is not None else []
        raise CloudBusinessError(envelope.code, message, info)
    return envelope.data


def get_edge_scheduler() -> None:
    """本地不再提供 DAG scheduler。"""

    return None


def get_edge_backend() -> Optional[JobExecutionBackend]:
    return _backend


def get_materials_service() -> Optional[MaterialsService]:
    """返回当前进程持有的 MaterialsService。"""

    return _materials


def get_workflow_executor() -> None:
    """本地不再把 workflow 定义转换为可执行 DAG。"""

    return None


def bind_workflow_executor(workflow_service: Any = None) -> None:
    """明确拒绝旧的本地 workflow 执行装配。"""

    del workflow_service
    raise RuntimeError("workflow execution is owned by the backend scheduler")


def make_http_sync_sender() -> Any:
    """保留旧 Cloud 同步调用形状；新 MaterialsService 不自动启动它。"""

    from unilabos.app.web.client import http_client
    from unilabos.server.scheduler.inventory.schemas import (
        CloudInventoryEventBatch,
        CloudSyncAck,
    )

    def send(events: Any) -> int:
        if not events:
            raise ValueError("inventory event batch cannot be empty")
        batch = CloudInventoryEventBatch.model_validate(
            {"edge_id": events[0].get("edge_id", ""), "events": events}
        )
        trace_headers: dict[str, Any] = {}
        inject_trace_context(trace_headers)
        response = http_client._session.post(
            f"{http_client.remote_addr}/edge/sync/events",
            json=batch.model_dump(mode="json", exclude_none=True),
            headers=trace_headers,
            timeout=30,
        )
        response.raise_for_status()
        data = unwrap_cloud_response(response.json())
        return CloudSyncAck.model_validate(data).acked_sequence

    return send


def make_http_snapshot_sender(edge_id: str) -> Any:
    """保留旧 Cloud snapshot 调用形状，不参与四库 writer 装配。"""

    from unilabos.app.web.client import http_client
    from unilabos.server.scheduler.inventory.schemas import (
        CloudInventorySnapshotRequest,
    )

    def send(snapshot: Any) -> None:
        request = CloudInventorySnapshotRequest.from_edge_snapshot(edge_id, snapshot)
        trace_headers: dict[str, Any] = {}
        inject_trace_context(trace_headers)
        response = http_client._session.post(
            f"{http_client.remote_addr}/edge/sync/snapshot",
            json=request.model_dump(mode="json", exclude_none=True),
            headers=trace_headers,
            timeout=30,
        )
        response.raise_for_status()
        unwrap_cloud_response(response.json())

    return send


def report_http_inventory_command_result(response: object) -> None:
    """旧 inventory command 的拒绝/结果回调；不打开旧库存数据库。"""

    from unilabos.app.web.client import http_client
    from unilabos.server.scheduler.inventory.schemas import (
        CloudInventoryCommandResultRequest,
        InventoryCommandResult,
    )

    local = InventoryCommandResult.model_validate(response)
    request = CloudInventoryCommandResultRequest(
        command_id=local.command_id,
        status=local.status,
        result=local.result,
        error=local.error,
    )
    trace_headers: dict[str, Any] = {}
    inject_trace_context(trace_headers)
    cloud_response = http_client._session.post(
        f"{http_client.remote_addr}/edge/inventory/command_result",
        json=request.model_dump(mode="json", exclude_none=True),
        headers=trace_headers,
        timeout=15,
    )
    cloud_response.raise_for_status()
    unwrap_cloud_response(cloud_response.json())


def setup_materials_service(
    *,
    database_paths: Optional[ServerDatabasePaths] = None,
    database_path: str | Path | None = None,
    ws_client: Any = None,
) -> MaterialsService:
    """装配新的 materials writer；不构造旧 InventoryStore。"""

    global _materials, _owns_materials
    if _materials is not None:
        return _materials

    paths = database_paths or BasicConfig.server_database_paths
    if paths is not None:
        _materials = configure_server_services(paths).materials
        _owns_materials = False
    else:
        if database_path is None or not str(database_path).strip():
            raise ValueError("database_paths or database_path is required")
        _materials = MaterialsService(database_path)
        _owns_materials = True

    message_processor = getattr(ws_client, "message_processor", None)
    if message_processor is not None:
        message_processor.materials_service = _materials
    logger.info("[MaterialsIntegration] materials.v1 writer ready")
    return _materials


def set_materials_gateway(gateway: Any) -> None:
    """Publish the Host-selected embedded/external materials authority."""

    global _materials_gateway
    _materials_gateway = gateway


def get_materials_gateway() -> Any:
    return _materials_gateway


def setup_job_execution_backend(
    ws_client: Any = None,
    *,
    host_node_getter: Any = None,
    database_paths: Optional[ServerDatabasePaths] = None,
) -> JobExecutionBackend:
    """启动只消费后端命令的微后端，不创建本地 DAG 或旧 Store。"""

    global _backend, _device_state_projection
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

    from unilabos.server.scheduler.monitor import monitor_bus
    from unilabos.server.scheduler.status_incidents import StatusIncidentManager

    status_incidents = StatusIncidentManager(monitor=monitor_bus)
    backend = JobExecutionBackend(
        host_node_getter=host_node_getter,
        device_state_store=_device_state_projection,
        monitor=monitor_bus,
        status_policy_resolver=make_device_status_policy_resolver(host_node_getter),
        status_incidents=status_incidents,
        result_bridges=[ws_client] if ws_client is not None else [],
    )
    backend.start()
    backend.rebuild_status_incidents()
    _backend = backend
    logger.info(
        "[JobExecutionIntegration] backend-controlled microbackend ready (%s)",
        endpoint_uuid,
    )
    return backend


def shutdown_edge_services() -> None:
    """关闭执行 bridge 和四库组合根。"""

    global _backend, _materials, _materials_gateway, _owns_materials
    global _device_state_projection

    if BasicConfig.backend == "ros2":
        from unilabos.server.scheduler.host_network import shutdown_network_services

        shutdown_network_services()
    if _backend is not None:
        _backend.stop()
    if _owns_materials and _materials is not None:
        _materials.close()
    shutdown_server_services()

    _backend = None
    _materials = None
    _materials_gateway = None
    _owns_materials = False
    _device_state_projection = None


def reset_for_test() -> None:
    shutdown_edge_services()


__all__ = [
    "CloudBusinessError",
    "bind_workflow_executor",
    "get_edge_backend",
    "get_edge_scheduler",
    "get_materials_service",
    "get_materials_gateway",
    "get_workflow_executor",
    "make_http_snapshot_sender",
    "make_http_sync_sender",
    "report_http_inventory_command_result",
    "reset_for_test",
    "setup_job_execution_backend",
    "setup_materials_service",
    "set_materials_gateway",
    "shutdown_edge_services",
    "unwrap_cloud_response",
]
