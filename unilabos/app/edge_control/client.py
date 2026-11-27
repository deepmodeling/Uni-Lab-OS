"""本地 Scheduler 与 HostNode 之间的 Edge 协议桥。"""

from __future__ import annotations

import asyncio
import copy
import json
import ssl as ssl_module
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from unilabos.app.communication import BaseCommunicationClient
from unilabos.app.device_action_capabilities import (
    project_device_action_capabilities,
)
from unilabos.app.edge_control.http import (
    BACKEND_UNAUTHORIZED_BUSINESS_CODE,
    EdgeDataPlane,
    EdgeProtocolHTTPError,
    websocket_url,
)
from unilabos.app.edge_control.store import EdgeControlStore, StoredEvent, StoredJob
from unilabos.app.scheduler.execution_outcome import normalize_executor_outcome
from unilabos.config.config import BasicConfig, EdgeControlConfig, HTTPConfig
from unilabos.resources.instance_identity import normalize_resource_instance_barcode
from unilabos.resources.resource_graph_node_kind import is_site_resource_node
from unilabos.utils.log import get_comm_logger
from unilabos.utils.tracing import (
    extract_trace_context,
    inject_trace_context,
    span,
)

logger = get_comm_logger()
_CONTROL_ACTION_ARGUMENTS = frozenset(
    {
        "unilabos_device_id",
        # manual_confirm 将审批配置和设备动作参数混合存储；
        # 它们用于调度阶段，不是驱动 Goal 字段。
        "timeout_seconds",
        "assignee_user_ids",
    }
)


def _device_dispatch_state(host_node: Any, device_id: str) -> tuple[str, list[str]]:
    """读取设备阻断展示文案和结构化 UNKNOWN 命令身份。"""

    if host_node is None or not device_id:
        return "", []
    block_reason_reader = getattr(host_node, "device_dispatch_block_reason", None)
    block_reason = (
        str(block_reason_reader(device_id) or "").strip()
        if callable(block_reason_reader)
        else ""
    )
    command_ids_reader = getattr(host_node, "device_unknown_command_ids", None)
    command_ids = (
        [str(command_id).strip() for command_id in command_ids_reader(device_id) if str(command_id).strip()]
        if callable(command_ids_reader)
        else []
    )
    return block_reason, command_ids


def _device_command_belongs_to_job(device_command_id: str, job_uuid: str) -> bool:
    """校验设备命令属于目标工作流节点作业（WorkflowNodeJob）。

    ``device_command_id`` 是设备命令身份，``job_uuid`` 是作业身份；
    根命令或仅追加一层受控 ASCII 子命令时返回 ``True``，否则返回
    ``False``，不抛出异常。
    """

    root_command_id = f"workflow-node-job:{job_uuid}"
    if device_command_id == root_command_id:
        return True
    prefix = root_command_id + ":"
    if not device_command_id.startswith(prefix):
        return False
    child_identity = device_command_id[len(prefix) :]
    return 0 < len(child_identity) <= 128 and all(
        character.isascii()
        and (character.isalnum() or character in {"-", "_", "."})
        for character in child_identity
    )


def _unknown_command_ids_for_job(
    command_ids: list[str],
    job_uuid: str,
) -> list[str]:
    """从设备 UNKNOWN 账本中筛选属于一个工作流作业的命令。"""

    return [
        command_id
        for command_id in command_ids
        if _device_command_belongs_to_job(command_id, job_uuid)
    ]


_SETTLED_JOB_STATUSES = frozenset(
    {
        "outcome_pending",
        "outcome_committed",
        "outcome_committed_unknown",
        "outcome_retired",
        "completed",
    }
)


def _job_is_settled(status: str) -> bool:
    """判断一个作业镜像是否已经在设备执行账本上收口。

    参数：``status`` 是 ``edge_job_runtime`` 中的作业状态。
    返回：作业已提交结果、已物理结算或已退役时返回 ``True``。
    语义：与 ``EdgeControlStore.save_pending_outcome`` 拒绝迟到回调的判据
    保持一致；已收口的作业不再接受任何改变执行走向的下行命令。
    """

    return status in _SETTLED_JOB_STATUSES or status.startswith(
        "outcome_resolution_pending_final:"
    )


def _registration_action_mappings(
    host_node: Any,
    device_id: str,
    resource: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Resolve logical actions even before ROS discovery has mirrored them."""

    mappings_by_device = getattr(host_node, "_action_value_mappings", {})
    runtime_mappings = (
        mappings_by_device.get(device_id, {})
        if isinstance(mappings_by_device, Mapping)
        else {}
    )
    if isinstance(runtime_mappings, Mapping) and runtime_mappings:
        return runtime_mappings

    devices = getattr(host_node, "devices_instances", {})
    wrapper = devices.get(device_id) if isinstance(devices, Mapping) else None
    base_node = getattr(wrapper, "_ros_node", None)
    instance_mappings = getattr(base_node, "_action_value_mappings", {})
    if isinstance(instance_mappings, Mapping) and instance_mappings:
        return instance_mappings

    registry_name = str(
        resource.get("class") or resource.get("klass") or ""
    ).strip()
    if not registry_name:
        return {}
    from unilabos.registry.registry import lab_registry

    registry_entry = lab_registry.device_type_registry.get(registry_name, {})
    class_entry = (
        registry_entry.get("class", {})
        if isinstance(registry_entry, Mapping)
        else {}
    )
    registry_mappings = (
        class_entry.get("action_value_mappings", {})
        if isinstance(class_entry, Mapping)
        else {}
    )
    return registry_mappings if isinstance(registry_mappings, Mapping) else {}


@dataclass(frozen=True)
class EdgeControlSettings:
    scheduler_address: str
    backend_address: str
    api_key: str
    edge_key: str
    capability_revision: str
    instance_uuid: str
    state_db: str
    reconnect_interval: float
    request_timeout: float
    event_retry_interval: float
    backend_api_key: str = ""
    device_status_interval: float = 5.0

    @classmethod
    def from_config(cls) -> EdgeControlSettings:
        """从进程配置冻结动作协议与本地调度参数。

        参数：无。返回不可变客户端设置。异常：数值配置转换失败时原样传播；
        地址与凭据的完整性在 HTTP/WS 建连边界继续关闭式校验。
        """

        scheduler_address = str(
            EdgeControlConfig.scheduler_addr
            or HTTPConfig.schedule_addr
            or "http://127.0.0.1:8002"
        ).strip()
        backend_address = scheduler_address
        edge_key = str(EdgeControlConfig.edge_key or BasicConfig.machine_name).strip()
        state_db = str(EdgeControlConfig.state_db or "").strip()
        if not state_db:
            working_dir = BasicConfig.working_dir or "~/.unilabos"
            state_db = str(Path(working_dir).expanduser() / "edge_control.db")
        return cls(
            scheduler_address=scheduler_address,
            backend_address=backend_address,
            api_key=str(EdgeControlConfig.api_key or "").strip(),
            edge_key=edge_key,
            capability_revision=str(
                EdgeControlConfig.capability_revision or "unilabos-edge-v1"
            ).strip(),
            instance_uuid=str(EdgeControlConfig.instance_uuid or "").strip(),
            state_db=state_db,
            reconnect_interval=float(EdgeControlConfig.reconnect_interval),
            request_timeout=float(EdgeControlConfig.request_timeout),
            event_retry_interval=float(EdgeControlConfig.event_retry_interval),
            backend_api_key=str(EdgeControlConfig.api_key or "").strip(),
        )


@dataclass
class EdgeJobContext:
    """HostNode 回调需要的最小 Job 上下文。"""

    job_id: str
    task_id: str
    node_id: str
    command_uuid: str
    claim_uuid: str
    attempt: int
    fences: tuple[tuple[str, int], ...]
    device_id: str
    action_name: str
    action_type: str
    action_args: dict[str, Any]
    trace_context: dict[str, str]
    task_type: str = "job_call_back_status"
    notebook_id: str = ""
    execution_source: str = "workflow"
    start_guard: Callable[[], str] | None = None

    @property
    def device_action_key(self) -> str:
        return f"/devices/{self.device_id}/{self.action_name}"


class EdgeControlClient(BaseCommunicationClient):
    """HTTP 传事实、WebSocket 传短通知的生产协议客户端。"""

    def __init__(
        self,
        settings: EdgeControlSettings | None = None,
        *,
        store: EdgeControlStore | None = None,
        data_plane: EdgeDataPlane | None = None,
        host_node_provider: Callable[[], Any] | None = None,
    ) -> None:
        """装配动作执行进程的协议镜像与设备执行适配器。

        参数：``settings`` 是双进程地址和凭据；``store`` 是动作镜像唯一账本；
        ``data_plane`` 负责 HTTP 事实；``host_node_provider`` 提供设备运行入口。
        返回无。异常：账本、配置或客户端构造失败时原样传播，不启动后台线程。
        """

        super().__init__()
        self.settings = settings or EdgeControlSettings.from_config()
        self.store = store or EdgeControlStore(self.settings.state_db)
        self.instance_uuid = self.store.get_or_create_instance_uuid(
            self.settings.instance_uuid
        )
        # 该身份只在本次动作执行进程生命周期内稳定；WebSocket 重连保持不变，
        # 进程重启后变化，使调度进程能够区分短暂断线和真实执行中断。
        self._process_uuid = str(uuid.uuid4())
        self._device_status_locks: dict[str, asyncio.Lock] = {}
        self.data_plane = data_plane or EdgeDataPlane(
            self.settings.backend_address,
            self.settings.scheduler_address,
            self.settings.api_key,
            backend_api_key=self.settings.backend_api_key,
            timeout=self.settings.request_timeout,
        )
        self._host_node_provider = host_node_provider or _host_node
        self.client_id = self.instance_uuid
        self.is_disabled = False
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._websocket: Any = None
        self._edge_uuid = ""
        self._session_uuid = ""
        self._active_jobs: set[str] = set()
        self._scheduled_jobs: set[str] = set()
        self._active_jobs_lock = threading.RLock()
        self._terminal_jobs: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._restart_pending_jobs = {
            job.job_uuid
            for job in self.store.list_jobs(
                {
                    "received",
                    "fetch_retry",
                    "dispatching",
                    "outcome_pending",
                    "outcome_committed_unknown",
                }
            )
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.settings.api_key or not self.settings.edge_key:
            raise ValueError("Edge production protocol requires api_key and edge_key")
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="EdgeControlClient",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._ready.set()
        loop = self._loop
        websocket = self._websocket
        if loop and websocket is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(websocket.close(), loop)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._connected.clear()

    def is_connected(self) -> bool:
        return self._connected.is_set() and not self.is_disabled

    def publish_host_ready(self) -> None:
        """HostNode 完成设备初始化后允许注册生产控制面。"""

        self._ready.set()

    def publish_job_started(self, item: Any) -> None:
        if getattr(item, "start_guard", None) is not None:
            return  # ROS goal 受理不代表已进入驱动调用。
        job = self.store.get_job(str(item.job_id))
        if job is None:
            return
        self._record_job_started(job)
        device_id = str(getattr(item, "device_id", "") or "").strip()
        if device_id:
            self._schedule(self._commit_device_status(device_id, {}))

    def begin_execution(self, job: StoredJob) -> str:
        """设备适配器在实际调用前获取一次开始许可，并发布真实开始事实。"""
        try:
            result = self.data_plane.begin_execution(job)
            if result == "START":
                self._record_job_started(job)
            elif result not in {"WAIT", "CANCELED"}:
                self._persist_start_unknown(job, "开始许可已经使用，不能重复调用")
            return result
        except Exception:
            self._persist_start_unknown(job, "开始授权应答未知")
            raise

    def _persist_start_unknown(self, job: StoredJob, message: str) -> None:
        """开始应答不明时先落库；后续 ROS 失败回调不能覆盖成明确失败。"""
        self.store.save_pending_outcome(job.job_uuid, "failed", {}, [{"message": message}],
            unknown_command_ids=[f"workflow-node-job:{job.job_uuid}"])
        with self._active_jobs_lock:
            self._terminal_jobs.add(job.job_uuid)
        self._schedule(self._commit_pending_outcome(job.job_uuid))

    def _record_job_started(self, job: StoredJob) -> None:
        """开始事实只推进活动镜像，重复或迟到通知不能复活终态作业。"""
        current = self.store.get_job(job.job_uuid)
        if current is None or current.status not in {"received", "fetch_retry", "dispatching"}:
            return
        self.store.set_job_status(job.job_uuid, "running")
        with self._active_jobs_lock:
            self._active_jobs.add(job.job_uuid)
        self._enqueue_event(
            "job.started",
            {"job_uuid": job.job_uuid, "command_uuid": job.command_uuid},
            parent_carrier=_job_trace_carrier(job),
        )

    def publish_job_status(
        self,
        feedback_data: dict,
        item: Any,
        status: str,
        return_info: dict | None = None,
    ) -> None:
        job_uuid = str(item.job_id)
        device_id = str(getattr(item, "device_id", "") or "").strip()
        if device_id:
            self._schedule(self._commit_device_status(device_id, {}))
        if status in {"success", "failed", "canceled", "timeout"}:
            with self._active_jobs_lock:
                if job_uuid in self._terminal_jobs:
                    return
                self._terminal_jobs.add(job_uuid)
            if not self._persist_terminal_status(
                job_uuid,
                status,
                copy.deepcopy(feedback_data or {}),
                copy.deepcopy(return_info),
                str(getattr(item, "device_id", "") or ""),
            ):
                return
            self._schedule(self._commit_pending_outcome(job_uuid))
            return
        if status == "running" and feedback_data:
            self._schedule(
                self._commit_feedback(job_uuid, copy.deepcopy(feedback_data))
            )

    def publish_device_status(
        self, device_status: dict, device_id: str, property_name: str
    ) -> None:
        value = copy.deepcopy(
            device_status.get(device_id, {}).get(property_name)
        )
        self._schedule(
            self._commit_device_status(
                str(device_id),
                ({str(property_name): value} if property_name else {}),
            )
        )

    def publish_job_error_decision_required(self, report: dict[str, Any]) -> bool:
        """把动作异常持久化到工作区后端的人工干预队列。"""

        try:
            self.data_plane.report_error_decision_required(dict(report))
        except Exception as error:  # noqa: BLE001 - 由调用方按默认策略收束
            logger.warning("[EdgeControl] 上报动作异常决策失败：%s", error)
            return False
        return True

    async def _commit_device_status(
        self, device_id: str, status: dict[str, Any],
    ) -> None:
        """同一设备按发送顺序提交，并在发送时读取最新属性快照。"""
        lock = self._device_status_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            await self._commit_device_status_locked(device_id, status)

    async def _commit_device_status_locked(
        self,
        device_id: str,
        status: dict[str, Any],
    ) -> None:
        """把 Runtime 设备健康与属性增量提交给工站调度注册权威。"""

        if not self._session_uuid or not self._connected.is_set():
            return
        host_node = self._host_node_provider()
        if host_node is None:
            return
        try:
            snapshot = getattr(host_node, "device_status", {}).get(device_id, {})
            status = {**status, **copy.deepcopy(snapshot)}
            block_reason, unknown_command_ids = _device_dispatch_state(
                host_node,
                device_id,
            )
            await asyncio.to_thread(
                self.data_plane.update_device_status,
                self._session_uuid,
                device_id,
                {
                    "online": True,
                    "dispatch_block_reason": block_reason,
                    "unknown_command_ids": unknown_command_ids,
                    "status": status,
                },
            )
        except Exception as error:
            logger.warning(
                "[EdgeControl] 提交设备 %s 实时状态失败：%s",
                device_id,
                error,
            )

    def send_ping(self, ping_id: str, timestamp: float) -> None:
        # 生产控制面的 ping 由后端发起，Edge 只回复 pong。
        return

    def _run(self) -> None:
        while not self._stopping.is_set() and not self._ready.wait(timeout=0.2):
            pass
        if self._stopping.is_set():
            return
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._connection_loop())
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()
            self._loop = None

    async def _connection_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                registration = await asyncio.to_thread(self._register)
                registered_edge_uuid = str(registration["edge_uuid"])
                if self.store.adopt_authority_edge_uuid(registered_edge_uuid):
                    logger.warning(
                        "[EdgeControl] Backend Authority 身份已变化，"
                        "已清理上一 Authority 的命令、任务与事件恢复状态"
                    )
                self._edge_uuid = registered_edge_uuid
                self._session_uuid = str(registration["session_uuid"])
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopping.is_set():
                    logger.warning(f"[EdgeControl] 生产控制面断开，准备重连: {exc}")
                    logger.debug(traceback.format_exc())
            finally:
                self._connected.clear()
                self._websocket = None
            if not self._stopping.is_set():
                await asyncio.sleep(max(self.settings.reconnect_interval, 0.1))

    def _register(self) -> dict[str, Any]:
        devices = self._registration_devices()
        registration = self.data_plane.register_session(
            {
                "edge_key": self.settings.edge_key,
                "instance_uuid": self.instance_uuid,
                "capability_revision": self.settings.capability_revision,
                "devices": devices,
            }
        )
        if not registration.get("edge_uuid") or not registration.get("session_uuid"):
            raise ValueError("Edge registration response is missing identity")
        logger.info(
            f"[EdgeControl] 已注册生产控制面，Edge={str(registration['edge_uuid'])[:8]}，"
            f"设备数={len(devices)}"
        )
        return registration

    def _registration_devices(self) -> list[dict[str, Any]]:
        host_node = self._host_node_provider()
        if host_node is None:
            raise RuntimeError("HostNode is not ready")
        resource_trees = host_node.resources_config.dump()
        nodes: dict[str, dict[str, Any]] = {}
        for tree in resource_trees:
            for resource in tree:
                resource_id = str(resource.get("id") or "").strip()
                if resource_id:
                    nodes[resource_id] = resource
        candidates: list[dict[str, Any]] = []
        system_device_id = str(getattr(host_node, "device_id", "host_node"))
        for local_id in sorted(host_node.devices_names):
            resource = nodes.get(str(local_id), {})
            if not resource and str(local_id) != system_device_id:
                continue
            barcode = normalize_resource_instance_barcode(
                resource.get("barcode"), str(local_id)
            )
            candidates.append(
                {
                    "local_id": str(local_id),
                    "name": str(resource.get("name") or (
                        "Host Node" if str(local_id) == system_device_id else local_id
                    )),
                    "barcode": barcode,
                }
            )
        if not candidates:
            raise RuntimeError("Edge production registration requires a device barcode")

        site_node_ids = {
            local_id
            for local_id, resource in nodes.items()
            if is_site_resource_node(resource)
        }
        material_barcodes = {
            normalize_resource_instance_barcode(
                resource.get("barcode"), local_id
            )
            for local_id, resource in nodes.items()
            if local_id not in site_node_ids
        }
        material_barcodes.update(candidate["barcode"] for candidate in candidates)
        material_uuids = self.data_plane.material_uuids_by_barcode(
            material_barcodes,
            current_only_barcodes=[candidate["barcode"] for candidate in candidates],
        )
        site_uuids_by_local_id = (
            self.data_plane.site_uuids_by_local_id(site_node_ids)
            if site_node_ids
            else {}
        )
        missing = {
            barcode
            for barcode in material_barcodes
            if barcode not in material_uuids
        }
        missing.update(
            normalize_resource_instance_barcode(
                nodes[local_id].get("barcode"), local_id
            )
            for local_id in site_node_ids
            if local_id not in site_uuids_by_local_id
        )
        if missing:
            raise RuntimeError(
                "Edge production resources have not been initialized in Backend: "
                + ", ".join(sorted(missing))
            )
        from unilabos.ros.nodes.resource_slot_hydration import (
            install_production_resource_lookup,
            install_production_resource_nodes,
        )

        install_production_resource_nodes(
            resource_trees,
            material_uuids,
            resource_uuids_by_local_id=site_uuids_by_local_id,
        )
        install_production_resource_lookup(
            self.data_plane.lookup_production_resource_nodes
        )

        devices: list[dict[str, Any]] = []
        for candidate in candidates:
            resource = nodes.get(candidate["local_id"], {})
            actions = project_device_action_capabilities(
                _registration_action_mappings(
                    host_node,
                    candidate["local_id"],
                    resource,
                )
            )
            block_reason, unknown_command_ids = _device_dispatch_state(
                host_node, candidate["local_id"]
            )
            devices.append(
                {
                    **candidate,
                    "material_uuid": material_uuids[candidate["barcode"]],
                    "actions": actions,
                    "online": True,
                    "dispatch_block_reason": block_reason,
                    "unknown_command_ids": unknown_command_ids,
                }
            )
        return devices

    async def _connect_once(self) -> None:
        url = websocket_url(self.settings.scheduler_address)
        ssl_context = (
            ssl_module.create_default_context() if url.startswith("wss://") else None
        )
        async with websockets.connect(
            url,
            ssl=ssl_context,
            open_timeout=self.settings.request_timeout,
            close_timeout=5,
            ping_interval=None,
            additional_headers={
                "Authorization": f"Bearer {self.settings.api_key}"
            },
        ) as websocket:
            self._websocket = websocket
            await websocket.send(json.dumps(self._hello_envelope(), ensure_ascii=False))
            self._connected.set()
            logger.info(f"[EdgeControl] 已连接生产控制面: {url}")
            sender = asyncio.create_task(self._event_sender(websocket))
            status_sender = asyncio.create_task(self._device_status_sender())
            try:
                await self._resume_received_jobs()
                await self._resume_pending_outcomes()
                async for encoded in websocket:
                    envelope = json.loads(encoded)
                    await self._handle_envelope(envelope)
            finally:
                sender.cancel()
                status_sender.cancel()
                await asyncio.gather(sender, status_sender, return_exceptions=True)

    async def _device_status_sender(self) -> None:
        """重连与周期性补报最新快照，修复断线期间仅变化一次的属性丢失。"""
        while self._connected.is_set() and not self._stopping.is_set():
            host = self._host_node_provider()
            statuses = getattr(host, "device_status", {})
            for device_id in list(statuses):
                await self._commit_device_status(str(device_id), {})
            await asyncio.sleep(max(self.settings.device_status_interval, 0.1))

    def _hello_envelope(self) -> dict[str, Any]:
        running_job_uuids = {
            job.job_uuid
            for job in self.store.list_jobs({"running", "cancel_requested"})
        }
        running_jobs: list[dict[str, Any]] = []
        for job_uuid in sorted(running_job_uuids):
            job = self.store.get_job(job_uuid)
            if job is not None:
                running_job: dict[str, Any] = {
                    "job_uuid": job.job_uuid,
                    "command_uuid": job.command_uuid,
                    # ``cancel_requested`` 是持久取消事实；跨 hello 对账继续传递，
                    # 避免本地权威把等待中的取消请求恢复成普通运行作业。
                    "state": (
                        "cancel_requested"
                        if job.status == "cancel_requested"
                        else "running"
                    ),
                }
                running_jobs.append(running_job)
        return _envelope(
            "hello",
            {
                "edge_uuid": self._edge_uuid,
                "session_uuid": self._session_uuid,
                "process_uuid": self._process_uuid,
                "last_ack_command_sequence": self.store.last_ack_command_sequence(),
                "individual_command_ack": True,
                "running_jobs": running_jobs,
            },
        )

    async def _event_sender(self, websocket: Any) -> None:
        while not self._stopping.is_set():
            retry_before = time.time() - max(self.settings.event_retry_interval, 0.1)
            events = self.store.pending_events(retry_before)
            for event in events:
                parent_context = extract_trace_context(_event_trace_carrier(event))
                with span(
                    "edge.control.event.send",
                    kind="producer",
                    parent_context=parent_context,
                    attributes={
                        "edge.event.uuid": event.event_uuid,
                        "edge.event.type": event.event_type,
                    },
                ):
                    send_context = _current_trace_carrier(
                        fallback=_event_trace_carrier(event)
                    )
                    await websocket.send(
                        json.dumps(
                            _stored_event_envelope(event, send_context),
                            ensure_ascii=False,
                        )
                    )
                    self.store.mark_event_sent(event.event_uuid)
            await asyncio.sleep(0.2)

    async def _handle_envelope(self, envelope: dict[str, Any]) -> None:
        """处理一个已解码的 Backend 控制信封。

        ``envelope`` 包含事件 ACK、心跳或持久 Edge 命令及其稳定身份；返回
        为空。非法载荷抛出 ``ValueError``，命令处理异常向连接循环传播以触发
        安全重连。重复 ACK 和重复命令依赖 ``EdgeControlStore``/
        设备账本保持幂等。
        """

        message_type = str(envelope.get("type") or "")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("control payload must be an object")
        if message_type == "event.ack":
            # 事件 UUID 精确定位 Edge 发件箱中被 Backend 确认的事实。
            event_uuid = str(payload.get("event_uuid") or "")
            if event_uuid:
                event = self.store.event_for_ack(event_uuid)
                if event is not None:
                    self._retire_settled_device_command(event)
                self.store.acknowledge_event(event_uuid)
            return
        if message_type == "ping":
            ping_uuid = str(payload.get("ping_uuid") or "")
            if not ping_uuid:
                raise ValueError("ping_uuid is required")
            await self._send_pong(ping_uuid, _message_trace_carrier(envelope))
            return
        if message_type not in {
            "job.start",
            "job.cancel",
            "job.error_decision",
            "job.resolve_unknown",
            "material.changed",
        }:
            raise ValueError(f"unsupported Edge command {message_type!r}")
        # 命令 UUID 是 Backend 下行投递的稳定幂等身份，不是作业身份。
        command_uuid = str(uuid.UUID(str(envelope["message_uuid"])))
        command_trace = _message_trace_carrier(envelope)
        parent_context = extract_trace_context(command_trace)
        with span(
            "edge.command.receive",
            kind="consumer",
            parent_context=parent_context,
            attributes={
                "edge.command.uuid": command_uuid,
                "edge.command.type": message_type,
                "edge.command.sequence": int(envelope.get("sequence") or 0),
            },
        ):
            inserted = self.store.record_command(envelope)
            if (
                message_type in {"job.resolve_unknown", "job.error_decision"}
                and not inserted
                and self.store.command_status(command_uuid) == "completed"
            ):
                return
            if message_type == "job.start":
                await self._accept_job_start(command_uuid, payload, command_trace)
            elif message_type == "job.cancel":
                await self._accept_job_cancel(command_uuid, payload, command_trace)
            elif message_type == "job.resolve_unknown":
                await self._accept_unknown_resolution(
                    command_uuid, payload, command_trace
                )
            elif message_type == "job.error_decision":
                await self._accept_error_decision(command_uuid, payload)
            else:
                self._accept_material_changed(command_uuid, payload, command_trace)

    def _retire_settled_device_command(self, event: StoredEvent) -> None:
        """把 Backend ACK 转成设备执行账本的显式物理结算清理信号。

        ``event`` 是尚未从 Edge 发件箱退役的事件快照；返回为空。普通工作流
        节点作业（WorkflowNodeJob）结果按稳定身份清理；携带物理不确定
        的结果必须保留到对账恢复（Reconciliation）完成。UNKNOWN 人工处置
        先清理精确设备命令，最后一条处置再清理根命令。重复 ACK 依赖驱动
        退役接口幂等；HostNode 适配器缺失时抛出 ``RuntimeError``，
        驱动异常向连接循环传播，事件仍保留并可安全重试。
        """

        job_uuid = str(event.payload.get("job_uuid") or "")
        job = self.store.get_job(job_uuid) if job_uuid else None
        if job is not None and job.status == "outcome_retired":
            # Runtime 崩溃只是软件任务终结，不能伪造设备账本的物理结算确认。
            return

        # 设备命令身份是设备执行账本的稳定物理效果键；一个最终对账 ACK
        # 可能同时结算精确子命令和工作流节点作业根命令。
        device_command_ids: list[str] = []
        if event.event_type == "job.outcome_committed":
            try:
                job_uuid = str(uuid.UUID(str(event.payload.get("job_uuid") or "")))
            except (AttributeError, TypeError, ValueError):
                return
            job = self.store.get_job(job_uuid)
            if job is not None and (
                job.status == "outcome_committed_unknown"
                or job.status.startswith("outcome_resolution_pending_final:")
            ):
                return
            device_command_ids.append(f"workflow-node-job:{job_uuid}")
        elif event.event_type == "job.unknown_resolution_committed":
            device_command_id = str(
                event.payload.get("device_command_id") or ""
            ).strip()
            if device_command_id:
                device_command_ids.append(device_command_id)
            try:
                job_uuid = str(uuid.UUID(str(event.payload.get("job_uuid") or "")))
            except (AttributeError, TypeError, ValueError):
                job_uuid = ""
            # 只有结构化 UNKNOWN 集合已在本地状态中证明为空，
            # 且 ACK 精确匹配最后一条处置事件，才结算工作流节点
            # 作业根命令；展示文本不承担安全语义。
            job = self.store.get_job(job_uuid) if job_uuid else None
            if job is not None and job.status == (
                f"outcome_resolution_pending_final:{event.event_uuid}"
            ):
                device_command_ids.append(f"workflow-node-job:{job_uuid}")
        if not device_command_ids:
            return

        host_node = self._host_node_provider()
        retire = getattr(host_node, "retire_settled_device_command", None)
        if not callable(retire):
            raise RuntimeError(
                "HostNode is not ready to retire settled device commands"
            )
        for device_command_id in dict.fromkeys(device_command_ids):
            retire(device_command_id)

    async def _send_pong(
        self, ping_uuid: str, parent_carrier: dict[str, str]
    ) -> None:
        """Reply to a heartbeat on its current connection without persistence."""

        websocket = self._websocket
        if websocket is None:
            raise RuntimeError("cannot reply to ping without an active WebSocket")
        parent_context = extract_trace_context(parent_carrier)
        with span(
            "edge.control.pong.send",
            kind="producer",
            parent_context=parent_context,
            attributes={"edge.ping.uuid": ping_uuid},
        ):
            envelope = _envelope("pong", {"ping_uuid": ping_uuid})
            trace_context = _current_trace_carrier(fallback=parent_carrier)
            for key in ("trace_id", "traceparent", "tracestate"):
                if trace_context.get(key):
                    envelope[key] = trace_context[key]
            await websocket.send(json.dumps(envelope, ensure_ascii=False))

    async def _accept_job_start(
        self,
        command_uuid: str,
        payload: dict[str, Any],
        command_trace: dict[str, str],
    ) -> None:
        if payload.get("executor_kind") != "device_action":
            raise ValueError("job.start executor_kind must be device_action")
        job_trace_context = _current_trace_carrier(fallback=command_trace)
        inserted = self.store.save_job_start(
            payload, command_uuid, job_trace_context
        )
        job = self.store.get_job(str(payload["job_uuid"]))
        if job is None:
            raise RuntimeError("persisted job.start is missing")
        if not inserted and (
            job.task_uuid != str(payload["task_uuid"])
            or job.node_uuid != str(payload["node_uuid"])
            or job.command_uuid != command_uuid
        ):
            raise ValueError("duplicate job.start identity changed")
        self._enqueue_event(
            "command.ack",
            {"command_uuid": command_uuid},
            fallback_carrier=command_trace,
        )
        self.store.mark_command_completed(command_uuid)
        if job.status in {"received", "fetch_retry"}:
            self._spawn(self._execute_job(job.job_uuid))

    def _accept_material_changed(
        self,
        command_uuid: str,
        payload: dict[str, Any],
        command_trace: dict[str, str],
    ) -> None:
        """确认 Backend 仅用于缓存失效提示的物料短通知。"""

        expected_keys = {"device_material_uuid", "material_uuid", "action"}
        if set(payload) != expected_keys:
            raise ValueError("material.changed payload has invalid fields")
        uuid.UUID(str(payload["device_material_uuid"]))
        uuid.UUID(str(payload["material_uuid"]))
        if payload["action"] not in {"add", "update", "remove"}:
            raise ValueError("material.changed action is invalid")
        self._enqueue_event(
            "command.ack",
            {"command_uuid": command_uuid},
            fallback_carrier=command_trace,
        )
        self.store.mark_command_completed(command_uuid)

    async def _accept_error_decision(
        self, command_uuid: str, payload: dict[str, Any]
    ) -> None:
        """把后端已选择的 retry/skip/abort 精确交给挂起的设备动作。"""

        decision_id = str(payload.get("decision_id") or "")
        job_id = str(payload.get("job_id") or "")
        device_id = str(payload.get("device_id") or "")
        if not decision_id or not job_id or not device_id:
            raise ValueError("job.error_decision identity is required")
        # 人工决定只能改变仍然在设备边界内等待的动作走向。作业镜像已经
        # 收口或根本不在本 Authority 的账本里时，这条命令永远不可能被设备
        # 接受；继续抛错会让它永远得不到 ACK，而 Backend 每次重连都重投同
        # 一条命令，生产控制面因此永久无法就绪，也无法再派发任何新作业。
        # 这种命令必须就地退役，并且不改变任何物理状态。
        job = self.store.get_job(job_id)
        if job is None or _job_is_settled(job.status):
            logger.warning(
                f"[EdgeControl] 忽略已收口作业 {job_id[:8]} 的人工决定 "
                f"{decision_id[:8]}：作业状态="
                f"{job.status if job is not None else 'absent'}"
            )
            self._enqueue_event("command.ack", {"command_uuid": command_uuid})
            self.store.mark_command_completed(command_uuid)
            return
        host = self._host_node_provider()
        wrapper = getattr(host, "devices_instances", {}).get(device_id) if host else None
        node = getattr(wrapper, "_ros_node", None)
        handle = getattr(node, "handle_action_error_decision", None)
        if not callable(handle) or not handle(decision_id, job_id, dict(payload)):
            raise RuntimeError("pending action error decision is unavailable")
        self._enqueue_event("command.ack", {"command_uuid": command_uuid})
        self.store.mark_command_completed(command_uuid)

    async def _accept_job_cancel(
        self,
        command_uuid: str,
        payload: dict[str, Any],
        command_trace: dict[str, str],
    ) -> None:
        """接收工作流节点作业（WorkflowNodeJob）取消命令。

        ``command_uuid`` 是取消命令身份，``payload`` 必须携带作业身份，
        ``command_trace`` 传递追踪上下文；返回为空。未下发作业可直接结算，
        已进入设备边界但找不到运行中 ROS goal 时保持待核实，由
        Backend 超时后让作业保持 ``running`` 并进入物理对账等待。
        """

        job_uuid = str(payload.get("job_uuid") or "")
        if not job_uuid:
            raise ValueError("job.cancel job_uuid is required")
        self._enqueue_event(
            "command.ack",
            {"command_uuid": command_uuid},
            fallback_carrier=command_trace,
        )
        self.store.mark_command_completed(command_uuid)
        job = self.store.get_job(job_uuid)
        if job is None:
            return
        # 取消前状态用于区分“确定未下发”与“可能已产生物理效果”。
        previous_status = job.status
        if previous_status not in {
            "received",
            "fetch_retry",
            "dispatching",
            "running",
            "cancel_requested",
        }:
            return
        self.store.set_job_status(job_uuid, "cancel_requested")
        if previous_status in {"received", "fetch_retry"}:
            await self._commit_terminal_status(
                job_uuid,
                "canceled",
                {},
                {"message": "工作流节点作业在设备下发前已取消"},
            )
            return

        host_node = self._host_node_provider()
        # 返回 False 仅说明内存中无可取消 goal，不能证明设备未执行。
        if host_node is not None:
            host_node.cancel_goal(job_uuid)

    async def _accept_unknown_resolution(
        self,
        command_uuid: str,
        payload: dict[str, Any],
        command_trace: dict[str, str],
    ) -> None:
        """在设备执行账本提交一条 UNKNOWN 命令的人工处置。

        ``command_uuid`` 是幂等处置命令，``payload`` 定位作业、设备和子命令，
        ``command_trace`` 传递追踪上下文；驱动未返回经验证的物理结算证据时
        抛出异常，成功时原子持久化对账事件与命令 ACK；返回为空。
        """

        expected_fields = {
            "job_uuid",
            "local_device_id",
            "device_command_id",
            "resolution",
            "reason",
        }
        if set(payload) != expected_fields:
            raise ValueError("job.resolve_unknown payload has invalid fields")
        job_uuid = str(uuid.UUID(str(payload["job_uuid"])))
        local_device_id = str(payload["local_device_id"] or "").strip()
        device_command_id = str(payload["device_command_id"] or "").strip()
        reason = str(payload["reason"] or "").strip()
        if (
            not local_device_id
            or not _device_command_belongs_to_job(device_command_id, job_uuid)
            or payload["resolution"] != "canceled"
            or not reason
        ):
            raise ValueError("job.resolve_unknown payload is invalid")
        host_node = self._host_node_provider()
        if host_node is None:
            raise RuntimeError("HostNode is not ready")
        # Backend 在执行进程断线时只能保守地持久化 Job 根命令；原子设备动作
        # 实际写入的则可能是带受控阶段后缀的子命令。重启后若设备账本只报告
        # 唯一一个属于该 Job 的 UNKNOWN 子命令，可安全地把根命令解析到该精确
        # 身份；零个或多个候选都继续失败关闭，避免误取消别的物理动作。
        root_device_command_id = f"workflow-node-job:{job_uuid}"
        if device_command_id == root_device_command_id:
            _, device_unknown_command_ids = _device_dispatch_state(
                host_node,
                local_device_id,
            )
            job_unknown_command_ids = _unknown_command_ids_for_job(
                device_unknown_command_ids,
                job_uuid,
            )
            if (
                root_device_command_id not in job_unknown_command_ids
                and len(job_unknown_command_ids) == 1
            ):
                device_command_id = job_unknown_command_ids[0]
        result = await asyncio.to_thread(
            host_node.resolve_unknown_device_command,
            local_device_id,
            device_command_id,
            command_uuid,
            reason,
        )
        if (
            not isinstance(result, dict)
            or result.get("command_id") != device_command_id
            or result.get("resolution_committed") is not True
            or result.get("previous_state") != "UNKNOWN"
            or result.get("state") != "CANCELED"
            or result.get("resolution_command_uuid") != command_uuid
        ):
            raise RuntimeError(
                f"device rejected UNKNOWN resolution: {result!r}"
            )
        if (self.store.get_meta(f"recovery_policy:{job_uuid}") == "reconcile"
                and result.get("durable_journal") is False):
            # 通用驱动的“当前未报告 UNKNOWN”不证明掉电前的物理动作已停止。
            raise RuntimeError("重启核实需要驱动命令账或明确物理证据，不能按空状态解除占用")
        # 结构化 UNKNOWN 集合是物理结算事实；设备可能同时保留
        # 其他作业的 UNKNOWN，因此必须先按当前作业稳定身份过滤。
        _, remaining_unknown_command_ids = _device_dispatch_state(
            host_node, local_device_id
        )
        remaining_job_unknown_command_ids = _unknown_command_ids_for_job(
            remaining_unknown_command_ids,
            job_uuid,
        )
        self.store.complete_unknown_resolution(
            command_uuid,
            {
                "job_uuid": job_uuid,
                "command_uuid": command_uuid,
                "device_command_id": device_command_id,
                "resolution": "canceled",
                "previous_state": "UNKNOWN",
                "current_state": "CANCELED",
                "unknown_command_ids": remaining_unknown_command_ids,
                "dispatch_block_reason": str(
                    host_node.device_dispatch_block_reason(local_device_id) or ""
                ).strip(),
            },
            _current_trace_carrier(fallback=command_trace),
            remaining_job_unknown_command_ids,
        )
        await self._commit_device_status(local_device_id, {})

    async def _resume_received_jobs(self) -> None:
        # 新路径由中心显式声明核实策略，保留原回执与可能启动的命令。
        # 未声明该策略的旧工站仍按兼容协议退役镜像。
        previous_jobs = set(self._restart_pending_jobs)
        previous_jobs.update(
            job.job_uuid for job in self.store.list_jobs({"running", "cancel_requested"})
            if job.job_uuid not in self._active_jobs
        )
        for job_uuid in tuple(previous_jobs):
            job = self.store.get_job(job_uuid)
            if job is None:
                self._restart_pending_jobs.discard(job_uuid)
                continue
            if job.status == "outcome_committed_unknown" and not job.job_access_token:
                # 已提交结果的令牌已退役；仅等待设备物理处置，不能重新取参或执行。
                self._restart_pending_jobs.discard(job_uuid)
                continue
            payload = await asyncio.to_thread(self.data_plane.fetch_job, job)
            if payload.get("station_controlled") and payload.get("recovery_policy") == "reconcile":
                self.store.preserve_restarted_job(
                    job_uuid, not_started=payload.get("execution_start_state") == "NOT_STARTED"
                )
            elif payload.get("station_controlled") and job_uuid in previous_jobs:
                self.store.abort_restarted_job(job_uuid)
            self._restart_pending_jobs.discard(job_uuid)
        for job in self.store.list_jobs({"received", "fetch_retry"}):
            self._spawn(self._execute_job(job.job_uuid))

    async def _resume_pending_outcomes(self) -> None:
        for outcome in self.store.list_pending_outcomes():
            self._spawn(self._commit_pending_outcome(outcome.job_uuid))

    async def _execute_job(self, job_uuid: str) -> None:
        """拉取作业参数并将工作流节点作业下发到本地设备。

        ``job_uuid`` 是作业稳定身份；返回为空。方法保证同一作业仅有一个
        调度协程，并在设备下发前再次核对取消状态；拉取可重试，其他异常
        被转换为待提交的失败结果。
        """

        with self._active_jobs_lock:
            if job_uuid in self._scheduled_jobs or job_uuid in self._active_jobs:
                return
            self._scheduled_jobs.add(job_uuid)
        try:
            job = self.store.get_job(job_uuid)
            if job is None:
                return
            parent_context = extract_trace_context(_job_trace_carrier(job))
            with span(
                "edge.job.dispatch",
                parent_context=parent_context,
                attributes={
                    "edge.job.uuid": job.job_uuid,
                    "workflow.task.uuid": job.task_uuid,
                    "workflow.node.uuid": job.node_uuid,
                },
            ):
                while self._connected.is_set() and not self._stopping.is_set():
                    current_job = self.store.get_job(job_uuid)
                    if current_job is None or current_job.status not in {
                        "received",
                        "fetch_retry",
                    }:
                        return
                    try:
                        payload = await asyncio.to_thread(
                            self.data_plane.fetch_job, job
                        )
                        break
                    except Exception as exc:
                        # 取参异常可与取消并发；不得用 fetch_retry 覆盖
                        # 已持久化的取消结果，否则下一轮会误下发已取消作业。
                        current_job = self.store.get_job(job_uuid)
                        if current_job is None or current_job.status not in {
                            "received",
                            "fetch_retry",
                        }:
                            return
                        self.store.set_job_status(job_uuid, "fetch_retry")
                        logger.warning(
                            f"[EdgeControl] 拉取 Job {job_uuid[:8]} 运行参数失败，"
                            f"稍后重试: {exc}"
                        )
                        await asyncio.sleep(
                            max(self.settings.reconnect_interval, 0.5)
                        )
                else:
                    return
                _validate_job_payload(job, payload)
                if payload.get("recovery_policy") == "reconcile":
                    self.store.set_meta(f"recovery_policy:{job_uuid}", "reconcile")
                # 拉取参数期间可能收到取消；只有仍处于可下发状态才进入设备边界。
                latest_job = self.store.get_job(job_uuid)
                if latest_job is None or latest_job.status not in {
                    "received",
                    "fetch_retry",
                }:
                    return
                host_node = self._host_node_provider()
                if host_node is None:
                    raise RuntimeError("HostNode is not ready")
                action_trace_context = _current_trace_carrier(
                    fallback=_job_trace_carrier(job)
                )
                action_args = dict(payload.get("param") or {})
                # 正式协议已由 material_uuid -> Edge binding -> local_device_id
                # 唯一确定驱动。旧微后端 Schema 中的选择字段不能泄漏为驱动 kwargs。
                for name in _CONTROL_ACTION_ARGUMENTS:
                    action_args.pop(name, None)
                context = EdgeJobContext(
                    job_id=job.job_uuid,
                    task_id=job.task_uuid,
                    node_id=job.node_uuid,
                    command_uuid=job.command_uuid,
                    claim_uuid=job.claim_uuid,
                    attempt=job.attempt,
                    fences=job.fences,
                    device_id=str(payload["local_device_id"]),
                    action_name=str(payload["action_name"]),
                    action_type=str(payload.get("action_type") or ""),
                    action_args=action_args,
                    trace_context=action_trace_context,
                    execution_source=job.execution_source,
                    start_guard=(lambda: self.begin_execution(job)) if payload.get("station_controlled") else None,
                )
                from unilabos.app.edge_control.execution_start import ExecutionStartDeferred
                while self._connected.is_set() and not self._stopping.is_set():
                    latest = self.store.get_job(job_uuid)
                    if latest is None or latest.status not in {"received", "fetch_retry", "dispatching"}:
                        return
                    self.store.set_job_status(job_uuid, "dispatching")
                    try:
                        host_node.send_goal(context, action_type=context.action_type,
                            action_kwargs=context.action_args, sample_material={}, server_info=None)
                        break
                    except ExecutionStartDeferred:
                        self.store.defer_not_started_job(job_uuid)
                        await asyncio.sleep(0.5)
        except Exception as exc:
            from unilabos.app.edge_control.execution_start import ExecutionStartCanceled, ExecutionStartUnknown
            if isinstance(exc, ExecutionStartCanceled):
                await self._commit_terminal_status(job_uuid, "canceled", {"cancel_reason": "local_no_send_proof"}, {})
                return
            if isinstance(exc, ExecutionStartUnknown):
                self.store.save_pending_outcome(job_uuid, "failed", {}, [{"message": str(exc)}],
                    unknown_command_ids=[f"workflow-node-job:{job_uuid}"])
                await self._commit_pending_outcome(job_uuid)
                return
            logger.error(f"[EdgeControl] 启动 Job {job_uuid[:8]} 失败: {exc}")
            logger.debug(traceback.format_exc())
            await self._commit_terminal_status(
                job_uuid,
                "failed",
                {},
                {"message": str(exc), "phase": "dispatch"},
            )
        finally:
            with self._active_jobs_lock:
                self._scheduled_jobs.discard(job_uuid)
            job = self.store.get_job(job_uuid)
            if job is None or job.status not in {
                "dispatching",
                "running",
                "cancel_requested",
            }:
                with self._active_jobs_lock:
                    self._active_jobs.discard(job_uuid)

    async def _commit_feedback(
        self, job_uuid: str, feedback: dict[str, Any]
    ) -> None:
        """持久化一条工作流节点作业运行反馈。

        ``job_uuid`` 是作业身份，``feedback`` 是 Edge 观测样本；返回为空。
        已进入结果提交或物理结算阶段的作业忽略迟到反馈；其他
        传输异常按原有间隔重试，进程停止时结束。
        """

        job = self.store.get_job(job_uuid)
        if (
            job is None
            or job.status in {
                "outcome_pending",
                "outcome_committed",
                "outcome_committed_unknown",
                "completed",
            }
            or job.status.startswith("outcome_resolution_pending_final:")
        ):
            return
        sequence = self.store.next_feedback_sequence(job_uuid)
        observed_at = _utc_now()
        parent_context = extract_trace_context(_job_trace_carrier(job))
        while not self._stopping.is_set():
            try:
                with span(
                    "edge.job.feedback.publish",
                    kind="producer",
                    parent_context=parent_context,
                    attributes={"edge.job.uuid": job.job_uuid},
                ):
                    result = await asyncio.to_thread(
                        self.data_plane.commit_feedback,
                        job,
                        sequence,
                        "action_feedback",
                        feedback,
                        observed_at,
                    )
                    through_sequence = int(
                        result.get("through_sequence") or sequence
                    )
                    self._enqueue_event(
                        "job.feedback_committed",
                        {
                            "job_uuid": job_uuid,
                            "through_sequence": through_sequence,
                        },
                    )
                return
            except Exception as exc:
                logger.warning(
                    f"[EdgeControl] 提交 Job {job_uuid[:8]} feedback 失败，稍后重试: {exc}"
                )
                await asyncio.sleep(max(self.settings.reconnect_interval, 0.5))

    async def _commit_terminal_status(
        self,
        job_uuid: str,
        status: str,
        result_data: dict[str, Any],
        return_info: Any,
        device_id: str = "",
    ) -> None:
        if not self._persist_terminal_status(
            job_uuid, status, result_data, return_info, device_id
        ):
            return
        await self._commit_pending_outcome(job_uuid)

    def _persist_terminal_status(
        self,
        job_uuid: str,
        status: str,
        result_data: dict[str, Any],
        return_info: Any,
        device_id: str = "",
    ) -> bool:
        job = self.store.get_job(job_uuid)
        if job is None:
            return False
        outcome = normalize_executor_outcome(
            status,
            return_info,
            cancel_requested=job.status == "cancel_requested",
        )
        normalized_return = _return_info(return_info, result_data)
        error_info: list[dict[str, Any]] = []
        if outcome != "succeeded":
            error_info.append(_error_info(return_info, outcome))
        _, unknown_command_ids = _device_dispatch_state(
            self._host_node_provider(), device_id
        )
        inserted = self.store.save_pending_outcome(
            job_uuid,
            outcome,
            normalized_return,
            error_info,
            unknown_command_ids,
        )
        return inserted or self.store.get_pending_outcome(job_uuid) is not None

    async def _commit_pending_outcome(self, job_uuid: str) -> None:
        job = self.store.get_job(job_uuid)
        pending = self.store.get_pending_outcome(job_uuid)
        if job is None or pending is None:
            return
        parent_context = extract_trace_context(_job_trace_carrier(job))
        while not self._stopping.is_set():
            try:
                with span(
                    "edge.job.outcome.publish",
                    kind="producer",
                    parent_context=parent_context,
                    attributes={"edge.job.uuid": job.job_uuid},
                ):
                    inventory_consumptions = (
                        pending.return_info.get("inventory_consumptions", [])
                        if isinstance(pending.return_info, dict)
                        else []
                    )
                    aliquot_receipts = _aliquot_receipts(pending.return_info)
                    if inventory_consumptions or aliquot_receipts:
                        committed = await asyncio.to_thread(
                            self.data_plane.commit_outcome,
                            job,
                            pending.outcome,
                            pending.return_info,
                            pending.error_info,
                            pending.unknown_command_ids,
                            inventory_consumptions=inventory_consumptions,
                            material_aliquot_receipts=aliquot_receipts,
                        )
                    else:
                        # 保持旧 EdgeDataPlane 测试/扩展的五参数协议兼容。
                        committed = await asyncio.to_thread(
                            self.data_plane.commit_outcome,
                            job,
                            pending.outcome,
                            pending.return_info,
                            pending.error_info,
                            pending.unknown_command_ids,
                        )
                    result_uuid = str(committed.get("uuid") or "")
                    event_payload: dict[str, Any] = {"job_uuid": job_uuid}
                    if result_uuid:
                        event_payload["result_uuid"] = result_uuid
                    event_trace_context = _current_trace_carrier(
                        fallback=_job_trace_carrier(job)
                    )
                    self.store.complete_pending_outcome(
                        job_uuid, event_payload, event_trace_context
                    )
                with self._active_jobs_lock:
                    self._active_jobs.discard(job_uuid)
                return
            except EdgeProtocolHTTPError as exc:
                if exc.business_code == BACKEND_UNAUTHORIZED_BUSINESS_CODE:
                    self.store.retire_pending_outcome(job_uuid)
                    with self._active_jobs_lock:
                        self._active_jobs.discard(job_uuid)
                    logger.warning(
                        f"[EdgeControl] Job {job_uuid[:8]} 凭证已由 Backend 终结，"
                        f"退役本地 pending outcome: {exc}"
                    )
                    return
                logger.warning(
                    f"[EdgeControl] 提交 Job {job_uuid[:8]} outcome 失败，稍后重试: {exc}"
                )
                await asyncio.sleep(max(self.settings.reconnect_interval, 0.5))
            except Exception as exc:
                logger.warning(
                    f"[EdgeControl] 提交 Job {job_uuid[:8]} outcome 失败，稍后重试: {exc}"
                )
                await asyncio.sleep(max(self.settings.reconnect_interval, 0.5))

    def _enqueue_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        parent_carrier: dict[str, str] | None = None,
        fallback_carrier: dict[str, str] | None = None,
    ) -> str:
        parent_context = (
            extract_trace_context(parent_carrier) if parent_carrier else None
        )
        with span(
            "edge.control.event.enqueue",
            kind="producer",
            parent_context=parent_context,
            attributes={"edge.event.type": event_type},
        ):
            trace_context = _current_trace_carrier(
                fallback=parent_carrier or fallback_carrier
            )
            return self.store.enqueue_event(event_type, payload, trace_context)

    def _schedule(self, coroutine: Any) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            logger.warning("[EdgeControl] 协议事件循环未运行，无法处理设备回调")
            if hasattr(coroutine, "close"):
                coroutine.close()
            return
        asyncio.run_coroutine_threadsafe(coroutine, loop)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _host_node() -> Any:
    from unilabos.ros.nodes.presets.host_node import HostNode

    return HostNode.get_instance(0)


def _envelope(
    message_type: str,
    payload: dict[str, Any],
    *,
    message_uuid: str | None = None,
    sent_at: str | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "message_uuid": message_uuid or str(uuid.uuid4()),
        "type": message_type,
        "sent_at": sent_at or _utc_now(),
        "payload": payload,
    }


def _stored_event_envelope(
    event: StoredEvent, trace_context: dict[str, str] | None = None
) -> dict[str, Any]:
    envelope = _envelope(
        event.event_type,
        event.payload,
        message_uuid=event.event_uuid,
        sent_at=event.created_at,
    )
    effective = trace_context or _event_trace_carrier(event)
    for key in ("trace_id", "traceparent", "tracestate"):
        if effective.get(key):
            envelope[key] = effective[key]
    return envelope


def _job_trace_carrier(job: StoredJob) -> dict[str, str]:
    return {
        "trace_id": job.trace_id,
        "traceparent": job.traceparent,
        "tracestate": job.tracestate,
    }


def _event_trace_carrier(event: StoredEvent) -> dict[str, str]:
    return {
        "trace_id": event.trace_id,
        "traceparent": event.traceparent,
        "tracestate": event.tracestate,
    }


def _message_trace_carrier(message: dict[str, Any]) -> dict[str, str]:
    # 保留控制信封实际携带的字段边界。尤其不能把缺失的 ``trace_id``
    # 扩写为空字符串，否则重放路径会把“没有该只读投影”误表示为“收到一个
    # 无效投影”，并让下游载体合同与原始持久命令不一致。
    carrier = {
        "traceparent": str(message.get("traceparent") or ""),
        "tracestate": str(message.get("tracestate") or ""),
    }
    if message.get("trace_id"):
        carrier["trace_id"] = str(message["trace_id"])
    return carrier


def _current_trace_carrier(
    *, fallback: dict[str, str] | None = None
) -> dict[str, str]:
    carrier: dict[str, Any] = {}
    inject_trace_context(carrier)
    result = {
        key: str(carrier.get(key) or "")
        for key in ("trace_id", "traceparent", "tracestate")
    }
    fallback = fallback or {}
    for key in ("trace_id", "traceparent", "tracestate"):
        if not result[key] and fallback.get(key):
            result[key] = str(fallback[key])
    return result


def _validate_job_payload(job: StoredJob, payload: dict[str, Any]) -> None:
    """证明 HTTP 载荷与 WebSocket 持久 Job/Claim/Fence 身份完全一致。"""

    expected = {
        "job_uuid": job.job_uuid,
        "task_uuid": job.task_uuid,
        "node_uuid": job.node_uuid,
        "command_uuid": job.command_uuid,
        "claim_uuid": job.claim_uuid,
    }
    if payload.get("execution_source", "workflow") != job.execution_source:
        raise ValueError("HTTP Job execution_source does not match job.start")
    for field, value in expected.items():
        if str(payload.get(field) or "") != value:
            raise ValueError(f"HTTP Job {field} does not match job.start")
    if payload.get("attempt") != job.attempt:
        raise ValueError("HTTP Job attempt does not match job.start")
    payload_fences = _normalize_job_payload_fences(payload.get("fences"))
    if payload_fences != job.fences:
        raise ValueError("HTTP Job fences do not match job.start")
    if not str(payload.get("local_device_id") or ""):
        raise ValueError("HTTP Job local_device_id is required")
    if not str(payload.get("action_name") or ""):
        raise ValueError("HTTP Job action_name is required")
    if not isinstance(payload.get("param"), dict):
        raise ValueError("HTTP Job param must be an object")


def _normalize_job_payload_fences(value: Any) -> tuple[tuple[str, int], ...]:
    """把 HTTP Job Fence 列表规范为可与 Edge 镜像比较的元组。"""

    if not isinstance(value, list):
        raise ValueError("HTTP Job fences must be a list")
    result: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("HTTP Job fence must be an object")
        lock_key = str(item.get("lock_key") or "").strip()
        token = item.get("fencing_token")
        if (
            not lock_key
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token < 1
        ):
            raise ValueError("HTTP Job fence is invalid")
        result.append((lock_key, token))
    ordered = tuple(sorted(result))
    if len({lock_key for lock_key, _ in ordered}) != len(ordered):
        raise ValueError("HTTP Job fence lock_key is duplicated")
    return ordered


def _aliquot_receipts(return_info: Any) -> list[dict[str, Any]]:
    """读取原始动作或 ROS UniLabJsonCommand 封装中的分装回执。

    只解开协议规定的一层 JSON 和 return_value，不递归搜索任意业务数据。
    每次投递都从持久结果解析，因此旧版待重投递结果也可恢复，不重发动作。
    """
    if not isinstance(return_info, dict):
        return []
    candidates = [return_info]
    direct = return_info.get("return_value")
    if isinstance(direct, dict):
        candidates.append(direct)
    result = return_info.get("result")
    encoded = result.get("return_info") if isinstance(result, dict) else None
    if isinstance(encoded, str):
        try:
            envelope = json.loads(encoded)
        except (ValueError, TypeError):
            envelope = None
        if isinstance(envelope, dict) and envelope.get("suc") is True:
            value = envelope.get("return_value")
            if isinstance(value, dict):
                candidates.append(value)
    receipts = None
    for candidate in candidates:
        if "material_aliquot_receipts" not in candidate:
            continue
        value = candidate["material_aliquot_receipts"]
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError("分装回执必须是对象数组")
        if receipts is not None and receipts != value:
            raise ValueError("动作结果封装中的分装回执冲突")
        receipts = value
    return copy.deepcopy(receipts or [])


def _return_info(return_info: Any, result_data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(return_info, dict):
        normalized = copy.deepcopy(return_info)
    elif return_info is None:
        normalized = {}
    else:
        normalized = {"raw": str(return_info)}
    if result_data and "result" not in normalized:
        normalized["result"] = result_data
    return normalized


def _error_info(return_info: Any, outcome: str) -> dict[str, Any]:
    if isinstance(return_info, dict):
        message = return_info.get("error") or return_info.get("message")
        if message:
            return {"message": str(message), "outcome": outcome}
    if return_info:
        return {"message": str(return_info), "outcome": outcome}
    return {"message": f"Device action {outcome}", "outcome": outcome}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"
