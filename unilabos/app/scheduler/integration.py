"""边缘调度器（EdgeScheduler）与 OS 主进程、旧云端 WS 的装配层。

装配关系：

    云端 ──ws──▶ ws_client.MessageProcessor
                    │ workflow_start / workflow_cancel（整图下发，本模块注入 edge_scheduler）
                    ▼
                EdgeScheduler ──dispatch──▶ JobExecutionBackend ──send_goal──▶ HostNode
                    ▲                            │（注册进 HostNode.bridges 收执行回报）
                    └────── on_job_finished ─────┘
                    │
                    └ workflow_status 终态上报 ──▶ ws_client.send_message ──▶ 云端

main.py 在组装 bridges 时调用 ``setup_edge_scheduler``，把返回的 backend 追加进
bridges 列表即可（backend 的 ``publish_job_status`` 是 bridge 形状）。
"""

from __future__ import annotations

import logging
import os
import time
from copy import deepcopy
from typing import Any, Optional, Tuple

from unilabos.app.scheduler.backend import JobExecutionBackend, create_edge_stack
from unilabos.app.scheduler.ordering import StableLocalOrderer
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.utils.tracing import inject_trace_context

logger = logging.getLogger(__name__)

# 进程内单例（主进程装配一次，ws_client/api 层共享）
_scheduler: Optional[EdgeScheduler] = None
_backend: Optional[JobExecutionBackend] = None
_inventory: Optional[Any] = None
_outbox_worker: Optional[Any] = None
# 工作区物料外形是包资产编译结果，不属于库存数据库私有事实。
_material_shapes: tuple[dict[str, Any], ...] = ()
# 工作区模型目录仅授权 OS 公开 HTTP 路由，不经过 local_bridge。
_material_model_catalog: Optional[Any] = None


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


def get_material_shapes() -> list[dict[str, Any]]:
    """返回工作区编译后的静态物料外形副本。

    参数：无。返回：容器不与组合根共享的公共外形列表。异常：无。
    """

    return deepcopy(list(_material_shapes))


def get_material_model_catalog() -> Optional[Any]:
    """返回当前启动代际的 OS 公开物料模型目录。

    参数：无。返回：尚未组装时为 ``None``，否则返回受限目录同一
    对象，供 HTTP 资产路由读取。异常：无；目录自身是不可变启动快照。
    """

    return _material_model_catalog


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
    resource_tree_set: Any = None,
    registry_snapshot: Any = None,
    resource_graph_source_id: str = "",
    material_shapes: Any = None,
    material_model_catalog: Any = None,
) -> Any:
    """启动主机库存服务并可选建立资源图投影（Resource Graph Projection）。

    参数：``inventory_db_path`` 是主机私有 SQLite 路径；``edge_id`` 与
    ``lab_id`` 是库存事件身份；``ws_client`` 和 ``sync_sender`` 分别接入主机链路
    与发件箱（Outbox）；资源树、注册表快照和来源身份共同提供资源图投影；
    ``material_shapes`` 是工作区包资产编译后的静态公共投影；
    ``material_model_catalog`` 是只通过 OS 公开 HTTP 路由读取的同代模型目录。
    返回：进程内唯一库存服务。异常：路径切换、外形形状、模型目录换代、
    投影参数不完整或资源图不安全时关闭式失败；
    从节点不调用本函数，因此不会打开此数据库。
    """

    global _inventory, _material_model_catalog, _material_shapes, _outbox_worker
    path = str(inventory_db_path or "").strip()
    if not path:
        raise ValueError("inventory_db_path is required")
    if material_shapes is not None:
        if not isinstance(material_shapes, (list, tuple)) or any(
            not isinstance(shape, dict) for shape in material_shapes
        ):
            raise TypeError("material_shapes 必须是对象列表或 tuple")
        # ``_material_shapes`` 固定本进程启动代际，读取端只获得深复制副本。
        compiled_shapes = tuple(deepcopy(material_shapes))
        if _inventory is not None and _material_shapes != compiled_shapes:
            raise RuntimeError("库存服务启动后不得切换工作区物料外形代际")
        _material_shapes = compiled_shapes
    if material_model_catalog is not None:
        models_by_template = getattr(
            material_model_catalog,
            "models_by_template",
            None,
        )
        if not isinstance(models_by_template, dict) and not hasattr(
            models_by_template,
            "items",
        ):
            raise TypeError("物料模型目录必须提供 models_by_template 映射")
        if (
            _inventory is not None
            and _material_model_catalog is not material_model_catalog
        ):
            raise RuntimeError("库存服务启动后不得切换工作区物料模型目录代际")
        # ``_material_model_catalog`` 固定资产授权根和模板模型的同一启动快照。
        _material_model_catalog = material_model_catalog
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

    # 三项启动输入必须全有或全无，防止调用者绕过稳定模板身份或来源指纹。
    resource_graph_source = str(resource_graph_source_id or "").strip()
    has_bootstrap_input = (
        resource_tree_set is not None
        or registry_snapshot is not None
        or bool(resource_graph_source)
    )
    if has_bootstrap_input:
        if (
            resource_tree_set is None
            or registry_snapshot is None
            or not resource_graph_source
        ):
            raise ValueError("资源图启动投影参数必须同时提供")
        from unilabos.app.scheduler.inventory.resource_graph_bootstrap import (
            bootstrap_local_resource_graph,
        )

        bootstrap_local_resource_graph(
            store=_inventory.store,
            resource_tree_set=resource_tree_set,
            registry_snapshot=registry_snapshot,
            source_id=resource_graph_source,
            material_rendering_by_template=(
                _material_model_catalog.models_by_template
                if _material_model_catalog is not None
                else None
            ),
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


def setup_edge_scheduler(
    ws_client: Any = None,
    lab_id: str = "edge-lab",
    host_node_getter: Any = None,
    inventory_db_path: str = "",
    edge_id: str = "edge-default",
    sync_sender: Any = None,
    device_state_db_path: str = "",
    workflow_history_db_path: str = "",
) -> Tuple[EdgeScheduler, JobExecutionBackend]:
    """装配本地调度器（Scheduler）与执行后端，并可接通旧云端 WS。

    参数：
        ws_client: 旧云端 ``WebSocketClient`` 实例。传入时：
            - 注入 message_processor.edge_scheduler（workflow_start/cancel 转发目标）
            - 注入 message_processor.inventory_service（inventory_command 执行目标）
            - 注册工作流终态上报（workflow_status 消息）
        lab_id: 当前实验室标识。
        host_node_getter: 获取 ``HostNode`` 的可调用对象。
        inventory_db_path: Edge 仓储 SQLite 路径（空 = 不启用仓储/物料衔接）
        edge_id: 当前边缘实例标识。
        sync_sender: outbox 上报 callable（events → acked_sequence）；
            传入时启动 OutboxWorker，不传则事件保留在 outbox（云端端点就绪后再挂）
        device_state_db_path: 设备状态 SQLite 路径（独立于仓储/工作流库；
            空则用 ULAB_DEVICE_STATE_DB，默认 ~/.unilabos/device_state.db，
            "off" 关闭落盘）。微后端经 publish_device_status bridge 收
            HostNode 属性更新并串行写入。
        workflow_history_db_path: 工作流执行历史 SQLite 路径（第三个独立库；
            空则用 ULAB_WORKFLOW_HISTORY_DB，默认
            ~/.unilabos/workflow_history.db，"off" 关闭）。启动时把上一
            世代残留的非终态 run 标记 interrupted。
    返回：
        ``(scheduler, backend)``；``backend`` 需由调用方追加进
        ``HostNode.bridges``。

    异常：
        本函数不吞掉数据库、调度器（Scheduler）或执行后端初始化异常。
    """
    global _scheduler, _backend, _inventory, _outbox_worker
    if _scheduler is not None and _backend is not None:
        logger.warning(
            "[EdgeSchedulerIntegration] already set up, reusing existing stack"
        )
        return _scheduler, _backend

    # 时长预估器：orderer（排序 duration）与 scheduler（泳道图/历史样本）共享
    from unilabos.app.scheduler.estimation import DurationEstimator

    estimator = DurationEstimator(
        mode=os.environ.get("ULAB_ESTIMATE_MODE", "auto").strip() or "auto",
        default_s=float(os.environ.get("ULAB_ESTIMATE_DEFAULT_S", "60")),
    )

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
    state_db = device_state_db_path or os.environ.get(
        "ULAB_DEVICE_STATE_DB", "~/.unilabos/device_state.db"
    )
    state_db = state_db.strip()
    if state_db and state_db.lower() != "off":
        from unilabos.app.scheduler.device_state import DeviceStateStore

        state_db = os.path.abspath(os.path.expanduser(state_db))
        os.makedirs(os.path.dirname(state_db) or ".", exist_ok=True)
        device_state_store = DeviceStateStore(state_db)
        logger.info("[EdgeSchedulerIntegration] device state store: %s", state_db)

    # 工作流执行历史：第三个独立 SQLite（低频 append，审计/回放/跨重启）
    history_store = None
    history_db = workflow_history_db_path or os.environ.get(
        "ULAB_WORKFLOW_HISTORY_DB", "~/.unilabos/workflow_history.db"
    )
    history_db = history_db.strip()
    if history_db and history_db.lower() != "off":
        from unilabos.app.scheduler.history import WorkflowHistoryStore

        history_db = os.path.abspath(os.path.expanduser(history_db))
        os.makedirs(os.path.dirname(history_db) or ".", exist_ok=True)
        history_store = WorkflowHistoryStore(history_db)
        interrupted = history_store.mark_interrupted()
        if interrupted:
            logger.info(
                "[EdgeSchedulerIntegration] marked %d stale workflow runs interrupted",
                interrupted,
            )
        logger.info("[EdgeSchedulerIntegration] workflow history store: %s", history_db)

    scheduler, backend = create_edge_stack(
        orderer=orderer,
        host_node_getter=host_node_getter,
        inventory=inventory,
        estimator=estimator,
        monitor=monitor_bus,
        device_state_store=device_state_store,
        history=history_store,
    )
    _scheduler, _backend = scheduler, backend

    if ws_client is not None:
        _wire_ws_client(scheduler, ws_client)

    logger.info(
        "[EdgeSchedulerIntegration] edge scheduler ready (ordering=%s)",
        "local-stable",
    )
    return scheduler, backend


def _wire_ws_client(scheduler: EdgeScheduler, ws_client: Any) -> None:
    """把调度器接到 ws 链路：收整图消息 + 回报工作流终态。"""
    message_processor = getattr(ws_client, "message_processor", None)
    if message_processor is not None:
        message_processor.edge_scheduler = scheduler
        if _inventory is not None:
            message_processor.inventory_service = _inventory
    else:
        logger.warning("[EdgeSchedulerIntegration] ws_client has no message_processor")

    def _report_workflow_state(workflow_id: str, state: str) -> None:
        run_snapshot = scheduler.workflow_snapshot(workflow_id) or {}
        message = {
            "action": "workflow_status",
            "data": {
                "workflow_id": workflow_id,
                "task_id": run_snapshot.get("task_id", workflow_id),
                "status": state,
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
    """关闭进程拥有的全部 Edge 服务并清除启动代际。

    参数：无。返回：无。异常：底层关闭错误原样抛出，避免遗留半关闭单例。
    """

    global _scheduler, _backend, _inventory
    global _material_model_catalog, _material_shapes, _outbox_worker
    from unilabos.app.scheduler.host_network import shutdown_network_services

    # 先拒绝新 Slave/物料请求，再关闭请求会触达的调度与存储组件。
    shutdown_network_services()
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
    _scheduler = None
    _backend = None
    _inventory = None
    _material_model_catalog = None
    _material_shapes = ()
    _outbox_worker = None


def reset_for_test() -> None:
    """测试用：清掉进程内 Edge 微后端单例。"""

    shutdown_edge_services()


__all__ = [
    "CloudBusinessError",
    "get_edge_backend",
    "get_edge_scheduler",
    "get_inventory_service",
    "get_material_model_catalog",
    "get_material_shapes",
    "make_http_snapshot_sender",
    "make_http_sync_sender",
    "report_http_inventory_command_result",
    "reset_for_test",
    "shutdown_edge_services",
    "setup_edge_inventory",
    "setup_edge_scheduler",
    "unwrap_cloud_response",
]
