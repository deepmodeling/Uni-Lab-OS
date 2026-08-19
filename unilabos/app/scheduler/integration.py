"""Edge scheduler 与 unilab 主进程 / 云端 ws 链路的装配层（composition root）。

装配关系：

    WorkflowService / 兼容 Adapter
                    │
                    ▼
                EdgeScheduler ──dispatch──▶ JobExecutionBackend ──send_goal──▶ adapter
                    ▲                            │（注册进 adapter bridges 收执行回报）
                    └────── on_job_finished ─────┘
                    │
                    └ workflow_status 兼容回报 ──▶ ws_client.send_message ──▶ 云端

旧 WebSocket ``workflow_start/workflow_cancel`` handler 不在组合根挂接，避免与
WorkflowService 或 edge_control 形成第二个任务权威。

main.py 在组装 bridges 时调用 ``setup_edge_scheduler``，把返回的 backend 追加进
bridges 列表即可（backend 的 ``publish_job_status`` 是 bridge 形状）。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional, Tuple

from unilabos.app.scheduler.backend import (
    JobExecutionBackend,
    create_edge_stack,
    make_device_status_policy_resolver,
)
from unilabos.app.scheduler.models import to_backend_workflow_status
from unilabos.app.scheduler.ordering import HttpSchedulerOrderer, StableLocalOrderer
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.config.config import BasicConfig
from unilabos.storage.paths import RuntimeStoragePaths
from unilabos.storage.profiles import SchedulerAuthorityProfile
from unilabos.utils.tracing import inject_trace_context

logger = logging.getLogger(__name__)

# 进程内单例（主进程装配一次，ws_client/api 层共享）
_scheduler: Optional[EdgeScheduler] = None
_backend: Optional[JobExecutionBackend] = None
_inventory: Optional[Any] = None
_outbox_worker: Optional[Any] = None
_workflow_executor: Optional[Any] = None


class CloudBusinessError(RuntimeError):
    """Cloud returned HTTP success but a non-zero ``common.Resp.code``."""

    def __init__(self, code: int, message: str, info: Optional[list[str]] = None):
        super().__init__(message)
        self.code = code
        self.info = info or []


def unwrap_cloud_response(body: object) -> Any:
    """Validate and unwrap the Go Cloud envelope without hiding business errors."""
    from unilabos.app.scheduler.inventory.schemas import CloudResponse

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


def get_edge_scheduler() -> Optional[EdgeScheduler]:
    return _scheduler


def get_edge_backend() -> Optional[JobExecutionBackend]:
    return _backend


def get_inventory_service() -> Optional[Any]:
    return _inventory


def get_workflow_executor() -> Optional[Any]:
    return _workflow_executor


def bind_workflow_executor(workflow_service: Any = None) -> Optional[Any]:
    """把 canonical Workflow Authority 接到唯一设备执行 backend。"""

    global _workflow_executor
    if workflow_service is None:
        from unilabos.workflow.composition import get_workflow_service

        workflow_service = get_workflow_service()
    if workflow_service is None or _backend is None:
        return None
    if not workflow_service.authority_profile.can_recover_local_workflow_task:
        return None
    if _workflow_executor is not None:
        if (
            _workflow_executor.service is not workflow_service
            or _workflow_executor.backend is not _backend
        ):
            raise RuntimeError("workflow executor is already bound to another authority")
        return _workflow_executor
    from unilabos.workflow.execution import WorkflowTaskExecutor

    _workflow_executor = WorkflowTaskExecutor(workflow_service, _backend)
    workflow_service.set_task_submitter(_workflow_executor.submit)
    _workflow_executor.start(recover=True)
    logger.info("[WorkflowExecution] canonical task adapter ready")
    return _workflow_executor


def make_http_sync_sender() -> Any:
    """生产 outbox sender：批量 POST 云端 /edge/sync/events，返回 acked_sequence。

    复用 HTTPClient 的 remote_addr + Lab auth 会话；云端未部署该端点时请求会
    失败，OutboxWorker 按指数退避保留事件重试（不丢数据、自愈）。
    """
    from unilabos.app.scheduler.inventory.schemas import (
        CloudInventoryEventBatch,
        CloudSyncAck,
    )
    from unilabos.app.web.client import http_client

    def send(events: Any) -> int:
        if not events:
            raise ValueError("inventory event batch cannot be empty")
        batch = CloudInventoryEventBatch.model_validate(
            {"edge_id": events[0].get("edge_id", ""), "events": events}
        )
        trace_headers: dict[str, Any] = {}
        inject_trace_context(trace_headers)
        resp = http_client._session.post(
            f"{http_client.remote_addr}/edge/sync/events",
            json=batch.model_dump(mode="json", exclude_none=True),
            headers=trace_headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = unwrap_cloud_response(resp.json())
        return CloudSyncAck.model_validate(data).acked_sequence

    return send


def make_http_snapshot_sender(edge_id: str) -> Any:
    """Build a sender for the Cloud snapshot envelope (not the Local REST DTO)."""
    from unilabos.app.scheduler.inventory.schemas import (
        CloudInventorySnapshotRequest,
    )
    from unilabos.app.web.client import http_client

    def send(snapshot: Any) -> None:
        request = CloudInventorySnapshotRequest.from_edge_snapshot(edge_id, snapshot)
        trace_headers: dict[str, Any] = {}
        inject_trace_context(trace_headers)
        resp = http_client._session.post(
            f"{http_client.remote_addr}/edge/sync/snapshot",
            json=request.model_dump(mode="json", exclude_none=True),
            headers=trace_headers,
            timeout=30,
        )
        resp.raise_for_status()
        unwrap_cloud_response(resp.json())

    return send


def report_http_inventory_command_result(response: object) -> None:
    """POST a typed command result and validate the Cloud business envelope."""
    from unilabos.app.scheduler.inventory.schemas import (
        CloudInventoryCommandResultRequest,
        InventoryCommandResult,
    )
    from unilabos.app.web.client import http_client

    local = InventoryCommandResult.model_validate(response)
    request = CloudInventoryCommandResultRequest(
        command_id=local.command_id,
        status=local.status,
        result=local.result,
        error=local.error,
    )
    trace_headers: dict[str, Any] = {}
    inject_trace_context(trace_headers)
    resp = http_client._session.post(
        f"{http_client.remote_addr}/edge/inventory/command_result",
        json=request.model_dump(mode="json", exclude_none=True),
        headers=trace_headers,
        timeout=15,
    )
    resp.raise_for_status()
    unwrap_cloud_response(resp.json())


def _wire_inventory_ws_client(inventory: Any, ws_client: Any) -> None:
    """Expose the host-owned inventory command target without requiring scheduler."""

    message_processor = getattr(ws_client, "message_processor", None)
    if message_processor is None:
        logger.warning("[EdgeInventoryIntegration] ws_client has no message_processor")
        return
    message_processor.inventory_service = inventory


def setup_edge_inventory(
    inventory_db_path: str,
    *,
    edge_id: str = "edge-default",
    lab_id: str = "edge-lab",
    ws_client: Any = None,
    sync_sender: Any = None,
) -> Any:
    """Start the host-owned inventory service independently of EdgeScheduler.

    The SQLite connection remains private to the host process.  REST and
    HostLink expose service-level queries; slave processes never open this DB.
    """

    global _inventory, _outbox_worker
    path = str(inventory_db_path or "").strip()
    if not path:
        raise ValueError("inventory_db_path is required")
    if path != ":memory:":
        path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    if _inventory is None:
        from unilabos.app.scheduler.inventory.service import InventoryService
        from unilabos.app.scheduler.inventory.store import InventoryStore

        from unilabos.app.scheduler.monitor import monitor_bus

        _inventory = InventoryService(
            InventoryStore(path),
            edge_id=edge_id,
            lab_id=lab_id,
            monitor=monitor_bus,
        )
        logger.info("[EdgeInventoryIntegration] inventory ready: %s", path)
    else:
        active_path = str(getattr(_inventory.store, "path", ""))
        if active_path and active_path != path:
            raise RuntimeError(
                f"inventory already initialized at {active_path}, cannot switch to {path}"
            )

    if ws_client is not None:
        _wire_inventory_ws_client(_inventory, ws_client)

    if sync_sender is not None and _outbox_worker is None:
        from unilabos.app.scheduler.inventory.sync import OutboxWorker

        _outbox_worker = OutboxWorker(_inventory.store, sync_sender)
        _outbox_worker.start()
    elif sync_sender is None:
        logger.info(
            "[EdgeInventoryIntegration] cloud sync disabled; outbox retained locally"
        )
    return _inventory


def setup_job_execution_backend(
    ws_client: Any = None,
    *,
    host_node_getter: Any = None,
    storage_paths: Optional[RuntimeStoragePaths] = None,
) -> JobExecutionBackend:
    """启动只消费后端 job 命令的微后端，不创建本地 DAG 调度器。"""

    global _backend
    if _backend is not None:
        if _scheduler is not None:
            raise RuntimeError(
                "local EdgeScheduler already owns the job execution backend"
            )
        return _backend

    profile = SchedulerAuthorityProfile.parse(
        BasicConfig.scheduler_authority_profile
    )
    if not profile.can_execute_backend_command:
        raise RuntimeError(
            "standalone JobExecutionBackend requires backend_controlled authority"
        )
    storage_paths = storage_paths or BasicConfig.runtime_storage_paths

    from unilabos.app.scheduler.monitor import monitor_bus
    from unilabos.app.scheduler.status_incidents import StatusIncidentManager

    device_state_store = None
    if storage_paths is not None and storage_paths.device_state_db is not None:
        from unilabos.app.scheduler.device_state import DeviceStateStore

        state_db = storage_paths.device_state_db
        state_db.parent.mkdir(parents=True, exist_ok=True)
        device_state_store = DeviceStateStore(str(state_db))

    status_incidents = StatusIncidentManager(monitor=monitor_bus)
    backend = JobExecutionBackend(
        host_node_getter=host_node_getter,
        device_state_store=device_state_store,
        monitor=monitor_bus,
        status_policy_resolver=make_device_status_policy_resolver(
            host_node_getter
        ),
        status_incidents=status_incidents,
        result_bridges=[ws_client] if ws_client is not None else [],
    )
    backend.start()
    backend.rebuild_status_incidents()
    _backend = backend
    logger.info(
        "[JobExecutionIntegration] backend-controlled microbackend ready"
    )
    return backend


def setup_edge_scheduler(
    ws_client: Any = None,
    ordering_url: str = "",
    ordering_algorithm: str = "WeightedCriticalPath",
    lab_id: str = "edge-lab",
    host_node_getter: Any = None,
    inventory_db_path: str = "",
    edge_id: str = "edge-default",
    sync_sender: Any = None,
    device_state_db_path: str = "",
    workflow_history_db_path: str = "",
    storage_paths: Optional[RuntimeStoragePaths] = None,
    authority_profile: Optional[SchedulerAuthorityProfile] = None,
) -> Tuple[EdgeScheduler, JobExecutionBackend]:
    """装配 EdgeScheduler + 微后端，并接通云端 ws 链路（幂等）。

    Args:
        ws_client: WebSocketClient 实例。传入时：
            - 旧 workflow_start/cancel 不再注入本地调度权威
            - 注入 message_processor.inventory_service（inventory_command 执行目标）
            - 注册工作流终态上报（workflow_status 消息）
        ordering_url: uni-lab-scheduler 地址（空则本地稳定排序）
        inventory_db_path: Edge 仓储 SQLite 路径（空 = 不启用仓储/物料衔接）
        sync_sender: outbox 上报 callable（events → acked_sequence）；
            传入时启动 OutboxWorker，不传则事件保留在 outbox（云端端点就绪后再挂）
        device_state_db_path: 设备状态 SQLite 路径（独立于仓储/工作流库；
            空则用 ULAB_DEVICE_STATE_DB，默认 ~/.unilabos/device_state.db，
            "off" 关闭落盘）。微后端经 publish_device_status bridge 收
            HostNode 属性更新并串行写入。
        workflow_history_db_path: Workflow Authority SQLite 路径（第三个独立库；
            空则用 ULAB_WORKFLOW_HISTORY_DB，默认
            ~/.unilabos/workflow_history.db）。旧审计对象只保留只读投影，
            本组合根不会创建或推进 workflow_runs/job_runs。
        storage_paths: 主组合根解析的运行时存储路径（RuntimeStoragePaths）。
            传入后设备遥测投影（DeviceTelemetryProjection）与工作流历史只从
            该对象取路径；旧的独立路径参数仅保留测试和兼容入口。
    Returns:
        (scheduler, backend)；backend 需由调用方追加进执行适配器 bridges 列表。
    """
    global _scheduler, _backend, _inventory, _outbox_worker
    profile = SchedulerAuthorityProfile.parse(
        authority_profile or BasicConfig.scheduler_authority_profile
    )
    if not profile.can_recover_local_workflow_task:
        raise RuntimeError(
            "JobExecutionBackend/EdgeScheduler cannot start in "
            f"{profile.value}; Backend commands belong to edge_control"
        )
    if _scheduler is None and _backend is not None:
        raise RuntimeError(
            "backend-controlled JobExecutionBackend is already running"
        )
    if _scheduler is not None and _backend is not None:
        logger.warning(
            "[EdgeSchedulerIntegration] already set up, reusing existing stack"
        )
        return _scheduler, _backend

    explicit_legacy_paths = any(
        str(value or "").strip()
        for value in (
            inventory_db_path,
            device_state_db_path,
            workflow_history_db_path,
        )
    )
    if storage_paths is None and not explicit_legacy_paths:
        storage_paths = BasicConfig.runtime_storage_paths
    if storage_paths is None:
        storage_paths = RuntimeStoragePaths.resolve(
            {
                "working_dir": BasicConfig.working_dir or "~/.unilabos",
                "edge_inventory_db": inventory_db_path or "off",
                "edge_device_state_db": device_state_db_path
                or os.environ.get(
                    "ULAB_DEVICE_STATE_DB",
                    "~/.unilabos/device_state.db",
                ),
                "edge_workflow_history_db": workflow_history_db_path
                or os.environ.get(
                    "ULAB_WORKFLOW_HISTORY_DB",
                    "~/.unilabos/workflow_history.db",
                ),
            }
        )

    # 时长预估器：orderer（排序 duration）与 scheduler（泳道图/历史样本）共享
    from unilabos.app.scheduler.estimation import DurationEstimator

    estimator = DurationEstimator(
        mode=os.environ.get("ULAB_ESTIMATE_MODE", "auto").strip() or "auto",
        default_s=float(os.environ.get("ULAB_ESTIMATE_DEFAULT_S", "60")),
    )

    if ordering_url:
        orderer: Any = HttpSchedulerOrderer(
            base_url=ordering_url,
            lab_id=lab_id,
            algorithm=ordering_algorithm,
            estimator=estimator,
        )
    else:
        orderer = StableLocalOrderer()

    inventory = _inventory
    if inventory_db_path:
        inventory = setup_edge_inventory(
            inventory_db_path,
            edge_id=edge_id,
            lab_id=lab_id,
            ws_client=ws_client,
            sync_sender=sync_sender,
        )
    elif inventory is not None and ws_client is not None:
        _wire_inventory_ws_client(inventory, ws_client)

    from unilabos.app.scheduler.monitor import monitor_bus

    # 设备状态存储：独立 SQLite（与仓储/工作流库分开），归微后端管
    device_state_store = None
    state_db = storage_paths.device_state_db
    if state_db is not None:
        from unilabos.app.scheduler.device_state import DeviceStateStore

        state_db.parent.mkdir(parents=True, exist_ok=True)
        device_state_store = DeviceStateStore(str(state_db))
        logger.info("[EdgeSchedulerIntegration] device state store: %s", state_db)

    scheduler, backend = create_edge_stack(
        orderer=orderer,
        host_node_getter=host_node_getter,
        inventory=inventory,
        estimator=estimator,
        monitor=monitor_bus,
        device_state_store=device_state_store,
        # 规范 WorkflowStore 是唯一写者。旧 EdgeScheduler 不再写
        # workflow_runs/job_runs；兼容历史查询由只读 View 提供。
        history=None,
        result_bridges=[ws_client] if ws_client is not None else [],
    )
    _scheduler, _backend = scheduler, backend

    # Workflow Authority 属于调度运行时，不依赖 FastAPI 是否启用。先确保规范
    # Store 已按同一 storage_paths 装配，再绑定唯一执行适配器。
    from unilabos.workflow.composition import compose_workflow_runtime

    workflow_service = compose_workflow_runtime(
        storage_paths,
        authority_profile=profile,
    )
    bind_workflow_executor(workflow_service)

    if ws_client is not None:
        _wire_ws_client(scheduler, ws_client, profile)

    logger.info(
        "[EdgeSchedulerIntegration] edge scheduler ready (ordering=%s)",
        ordering_url or "local-stable",
    )
    return scheduler, backend


def _wire_ws_client(
    scheduler: EdgeScheduler,
    ws_client: Any,
    authority_profile: SchedulerAuthorityProfile,
) -> None:
    """只接回报链路；旧云端整图命令不再成为本地任务权威。"""
    message_processor = getattr(ws_client, "message_processor", None)
    if message_processor is not None:
        message_processor.edge_scheduler = None
        if _inventory is not None:
            message_processor.inventory_service = _inventory
        logger.info(
            "[EdgeSchedulerIntegration] legacy workflow_start disabled in %s",
            authority_profile.value,
        )
    else:
        logger.warning("[EdgeSchedulerIntegration] ws_client has no message_processor")

    def _report_workflow_state(workflow_id: str, state: str) -> None:
        run_snapshot = scheduler.workflow_snapshot(workflow_id) or {}
        message = {
            "action": "workflow_status",
            "data": {
                "workflow_id": workflow_id,
                "task_id": run_snapshot.get("task_id", workflow_id),
                "status": to_backend_workflow_status(state),
                "timestamp": time.time(),
            },
        }
        try:
            if message_processor is not None:
                message_processor.send_message(message)
        except Exception:  # noqa: BLE001 - 上报失败不影响调度
            logger.exception("[EdgeSchedulerIntegration] workflow_status report failed")

    scheduler.set_workflow_state_listener(_report_workflow_state)


def shutdown_edge_services() -> None:
    """Stop every process-owned Edge microbackend service and clear singletons."""

    global _scheduler, _backend, _inventory, _outbox_worker, _workflow_executor

    # ROS2 模式的 HostLink 组网控制面归微后端所有；direct hostlink backend
    # 则由 HostLinkBackendRuntime 自己关闭，不能在这里抢先断开其设备传输。
    if BasicConfig.backend == "ros2":
        from unilabos.app.scheduler.host_network import (
            shutdown_network_services,
        )

        shutdown_network_services()
    if _workflow_executor is not None:
        _workflow_executor.service.set_task_submitter(None)
        _workflow_executor.stop()
    if _backend is not None:
        _backend.stop()
        device_state = getattr(_backend, "device_state", None)
        if device_state is not None:
            device_state.close()
    if _scheduler is not None:
        history = getattr(_scheduler, "_history", None)
        if history is not None:
            history.close()
    if _outbox_worker is not None:
        _outbox_worker.stop()
    if _inventory is not None:
        _inventory.store.close()
    from unilabos.workflow.composition import reset_workflow_service_for_test

    reset_workflow_service_for_test()
    _scheduler = None
    _backend = None
    _inventory = None
    _outbox_worker = None
    _workflow_executor = None


def reset_for_test() -> None:
    """测试用：清掉进程内 Edge 微后端单例。"""

    shutdown_edge_services()


__all__ = [
    "CloudBusinessError",
    "bind_workflow_executor",
    "get_edge_backend",
    "get_edge_scheduler",
    "get_inventory_service",
    "get_workflow_executor",
    "make_http_snapshot_sender",
    "make_http_sync_sender",
    "report_http_inventory_command_result",
    "reset_for_test",
    "shutdown_edge_services",
    "setup_edge_inventory",
    "setup_job_execution_backend",
    "setup_edge_scheduler",
    "unwrap_cloud_response",
]
