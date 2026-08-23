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
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.scheduler.backend import (
    JobExecutionBackend,
    make_device_materials_need_lock_resolver,
    make_device_status_policy_resolver,
)
from unilabos.server.scheduler.coordinator import WorkflowBusinessCoordinator
from unilabos.server.scheduler.telemetry_state import TelemetryDeviceStateProjection
from unilabos.server.services.materials import MaterialsService

logger = logging.getLogger(__name__)

_backend: Optional[JobExecutionBackend] = None
_coordinator: Optional[WorkflowBusinessCoordinator] = None
_materials: Optional[MaterialsService] = None
_standalone_materials_repository: Optional[MaterialsRepository] = None
_materials_gateway: Any = None
_device_state_projection: Optional[TelemetryDeviceStateProjection] = None
_workflow_service: Any = None
_workflow_executor: Any = None


def get_edge_scheduler() -> None:
    """本地不再提供 DAG scheduler。"""

    return None


def get_edge_backend() -> Optional[JobExecutionBackend]:
    return _backend


def get_business_coordinator() -> Optional[WorkflowBusinessCoordinator]:
    return _coordinator


def get_materials_service() -> Optional[MaterialsService]:
    """返回当前进程持有的 MaterialsService。"""

    return _materials


def get_workflow_executor() -> Any:
    """返回 Demo 显式启用的本地 WorkflowTask executor。"""

    return _workflow_executor


def get_workflow_service() -> Any:
    """返回 Demo 的本地 Workflow Authority；普通 Host 始终为 ``None``。"""

    return _workflow_service


def bind_workflow_executor(workflow_service: Any = None) -> None:
    """明确拒绝旧的本地 workflow 执行装配。"""

    del workflow_service
    raise RuntimeError("workflow execution is owned by the backend scheduler")


def setup_demo_workflow_authority(
    *,
    database_path: str | Path,
    backend: Any = None,
) -> Any:
    """为 ``--demo-mode`` 装配唯一的本地 Workflow Authority。

    普通 Host 继续由 Backend scheduler 持有 WorkflowTask 权威。Demo 是显式
    ``local_scheduler`` profile，复用同一 JobExecutionBackend 下发到 HostLink。
    """

    global _workflow_service, _workflow_executor
    if not BasicConfig.demo_mode:
        raise RuntimeError("local workflow authority is restricted to --demo-mode")
    if _workflow_service is not None:
        return _workflow_service
    execution_backend = backend or _backend
    if execution_backend is None:
        raise RuntimeError("job execution backend must be ready first")

    from unilabos.server.scheduler.authority import SchedulerAuthorityProfile
    from unilabos.server.scheduler.workflow_execution import WorkflowTaskExecutor
    from unilabos.server.workflow.service import WorkflowService
    from unilabos.server.workflow.store import WorkflowStore

    service = WorkflowService(
        WorkflowStore(database_path),
        authority_profile=SchedulerAuthorityProfile.LOCAL_SCHEDULER,
    )
    executor = WorkflowTaskExecutor(
        service,
        execution_backend,
        materials_gateway=_materials_gateway,
    )
    service.set_task_submitter(executor.submit)
    executor.start(recover=True)
    _workflow_service = service
    _workflow_executor = executor
    logger.info(
        "[WorkflowIntegration] demo Workflow Authority ready (%s)",
        database_path,
    )
    return service


def setup_materials_service(
    *,
    database_paths: Optional[ServerDatabasePaths] = None,
    database_path: str | Path | None = None,
) -> MaterialsService:
    """装配新的 materials writer；不构造旧 InventoryStore。"""

    global _materials, _standalone_materials_repository
    if _materials is not None:
        return _materials

    paths = database_paths or BasicConfig.server_database_paths
    if paths is not None:
        _materials = configure_server_services(paths).materials
    else:
        if database_path is None or not str(database_path).strip():
            raise ValueError("database_paths or database_path is required")
        _standalone_materials_repository = MaterialsRepository(database_path)
        _materials = MaterialsService(_standalone_materials_repository)

    logger.info("[MaterialsIntegration] materials.v1 writer ready")
    return _materials


def set_materials_gateway(gateway: Any) -> None:
    """Publish the Host-selected embedded/external materials authority."""

    global _materials_gateway
    _materials_gateway = gateway


def get_materials_gateway() -> Any:
    return _materials_gateway


def setup_job_execution_backend(
    control_client: Any = None,
    *,
    host_node_getter: Any = None,
    database_paths: Optional[ServerDatabasePaths] = None,
    materials_gateway: Any = None,
) -> JobExecutionBackend:
    """启动只消费后端命令的微后端，不创建本地 DAG 或旧 Store。"""

    global _backend, _coordinator, _device_state_projection
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


def shutdown_edge_services() -> None:
    """关闭执行 bridge 和四库组合根。"""

    global _backend, _coordinator, _materials, _materials_gateway
    global _standalone_materials_repository
    global _device_state_projection, _workflow_service, _workflow_executor

    if BasicConfig.backend == "ros2":
        from unilabos.server.scheduler.host_network import shutdown_network_services

        shutdown_network_services()
    if _workflow_executor is not None:
        _workflow_executor.stop()
    if _workflow_service is not None:
        _workflow_service.set_task_submitter(None)
        _workflow_service.close()
    if _backend is not None:
        _backend.stop()
    if _standalone_materials_repository is not None:
        _standalone_materials_repository.close()
    shutdown_server_services()

    _backend = None
    _coordinator = None
    _materials = None
    _standalone_materials_repository = None
    _materials_gateway = None
    _device_state_projection = None
    _workflow_service = None
    _workflow_executor = None


def reset_for_test() -> None:
    shutdown_edge_services()


__all__ = [
    "bind_workflow_executor",
    "get_edge_backend",
    "get_business_coordinator",
    "get_edge_scheduler",
    "get_materials_service",
    "get_materials_gateway",
    "get_workflow_executor",
    "get_workflow_service",
    "reset_for_test",
    "setup_job_execution_backend",
    "setup_demo_workflow_authority",
    "setup_materials_service",
    "set_materials_gateway",
    "shutdown_edge_services",
]
