"""旧云端 Backend 的 ``/ws/schedule`` 客户端。

旧协议由 Backend 直接在 WebSocket 上下发执行命令并期待同一连接上的回报：

下行（Backend → Edge）
    ``job_start`` / ``cancel_action`` / ``cancel_task`` / ``query_action_state`` /
    ``query_action_lock`` / ``add_material`` / ``update_material`` /
    ``remove_material`` / ``add_device`` / ``remove_device`` / ``request_restart`` /
    ``pong`` / ``ping``
上行（Edge → Backend）
    ``host_node_ready`` / ``report_action_lock`` / ``report_action_state`` /
    ``job_status`` / ``device_status`` / ``ping`` / ``pong`` /
    ``restart_acknowledged`` / ``normal_exit``

本类不重新实现调度或队列：``job_start`` 直接交给微后端
:class:`~unilabos.server.backend.execution.JobExecutionBackend`（``dispatch``），
执行结果通过 result bridge 回调（``publish_job_status`` 等）翻译成 ``job_status``。
旧后端要求 Edge 用锁上报（``report_action_lock``）表达动作可用性，Edge 侧
设备-动作互斥仍由微后端登记表保证；这里只把状态镜像出去。
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import logging
import ssl as ssl_module
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Full, PriorityQueue
from typing import Any, Dict, List, Optional, Tuple

import websockets

from unilabos.backend.hostlink.adapter_registry import get_execution_adapter
from unilabos.config.config import BasicConfig, WSConfig
from unilabos.server.backend.execution_queue import QueueItem, format_job_log
from unilabos.server.backend.legacy_adaptor.session import BaseBackendClient
from unilabos.server.backend.legacy_adaptor.url import (
    build_legacy_backend_websocket_url,
)
from unilabos.protocol.runtime.control import PingNotice, PongNotice
from unilabos.utils.log import get_comm_logger
from unilabos.utils.serialization import serialize_result_info

logger = get_comm_logger()

_TERMINAL_STATUSES = ("success", "failed")
_RUNNING_DEBOUNCE_SECONDS = 10.0
_JOB_CACHE_TTL_SECONDS = 24 * 60 * 60
_JOB_CACHE_MAX_ENTRIES = 1024
#: 旧后端给每个已下发 job 维护一个约 20 s 的存活期限，``job_status running``
#: 并不续期；只有 ``report_action_state(free=False, need_more=N)`` 才把期限往后
#: 推 N 秒。运行中的 job 因此每 10 s 续一次（need_more=11 留 1 s 余量），否则
#: 超过 20 s 的动作会在云端被判成 timeout，随后的终态被丢弃。
_JOB_KEEPALIVE_SECONDS = 10.0
_JOB_KEEPALIVE_NEED_MORE = 11
#: 旧后端把工作流节点 schema 里的设备选择器原样放进 action_args；它只用于
#: 路由（job_start.device_id 已经是解析结果），驱动函数签名里没有这个参数。
_CONTROL_ACTION_ARGS = frozenset({"unilabos_device_id"})


class _PriorityMessageQueue(PriorityQueue):
    """旧协议出站队列：心跳优先，业务通知保持 FIFO。"""

    _FAST_ACTIONS = frozenset({"ping", "pong"})

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize=maxsize)
        self._sequence = itertools.count()

    def put(
        self,
        item: dict[str, Any],
        block: bool = True,
        timeout: float | None = None,
    ) -> None:
        priority = 0 if item.get("action") in self._FAST_ACTIONS else 1
        wrapped = (priority, next(self._sequence), item)
        if priority == 0 and self.maxsize > 0:
            # 旧协议也可能在锁/状态镜像高峰期填满队列；ping/pong 属于
            # 控制面快速路径，不能被业务通知的容量限制挡住。
            with self.not_full:
                self._put(wrapped)
                self.unfinished_tasks += 1
                self.not_empty.notify()
            return
        super().put(wrapped, block=block, timeout=timeout)

    def get(
        self,
        block: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        _priority, _sequence, item = super().get(block=block, timeout=timeout)
        return item


def _execution_backend() -> Any:
    """延迟解析进程内执行微后端，避免与组合根循环导入。"""

    try:
        from unilabos.server.backend.composition import get_execution_backend

        return get_execution_backend()
    except ImportError:
        return None


@dataclass
class _JobRecord:
    """旧协议幂等缓存：同一 ``(task_id, job_id)`` 的重复 ``job_start`` 只回放结果。"""

    request: Dict[str, Any]
    terminal_message: Optional[Dict[str, Any]] = None
    terminal_status: str = ""
    updated_at: float = field(default_factory=time.time)


class LegacyBackendWebSocketClient(BaseBackendClient):
    """旧 Backend 线协议 ↔ 微后端执行权威 的双向翻译层。"""

    #: 让 ``execution_result_bridges`` 把原始终态交给微后端，再由微后端回调本类。
    owns_job_lifecycle = False
    #: 组合根据此把本类挂进 ``JobExecutionBackend.result_bridges``：微后端释放的
    #: running / 终态结果需要镜像成旧协议 ``job_status``。
    mirrors_job_results = True

    def __init__(
        self,
        websocket_url: Optional[str] = None,
        *,
        execution_backend_getter: Optional[Callable[[], Any]] = None,
        adapter_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__()
        self.is_disabled = False
        self.client_id = str(uuid.uuid4())
        self.session_id = self.client_id[:6]
        self.websocket_url = (
            websocket_url
            if websocket_url is not None
            else build_legacy_backend_websocket_url()
        ) or ""
        self._execution_backend_getter = execution_backend_getter or _execution_backend
        self._adapter_getter = adapter_getter or (lambda: get_execution_adapter(0))
        # 旧协议仍可能有大量锁/状态镜像；心跳必须能越过这些通知。
        self._send_queue: PriorityQueue = _PriorityMessageQueue(maxsize=2000)
        self._running = False
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket: Any = None
        self._reconnect_count = 0
        self._lock = threading.RLock()
        # 与 runtime.v1 客户端一致：旧协议业务处理（物料 HTTP、job dispatch）
        # 串行放到独立 worker，不能阻塞接收协程读取 pong。
        self._business_queue: Optional[
            asyncio.Queue[tuple[str, Any]]
        ] = None
        self._jobs: Dict[Tuple[str, str], _JobRecord] = {}
        self._running_last_sent: Dict[str, Tuple[float, Any]] = {}
        self._host_ready_sent_for_connection = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self.is_disabled or self._running:
            return
        if not self.websocket_url:
            logger.info("[LegacyWS] 未配置云端 Backend 地址，不建立旧协议连接")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="LegacyBackendWebSocket"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._connected:
            self._queue_message(
                {"action": "normal_exit", "data": {"session_id": self.session_id}}
            )
            time.sleep(0.3)
        websocket = self._websocket
        loop = self._loop
        if websocket is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(websocket.close(), loop)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and not self.is_disabled

    # ------------------------------------------------------------------
    # BaseBackendClient / bridge 接口（HostAdapterBase 与 JobExecutionBackend 调用）
    # ------------------------------------------------------------------

    def publish_device_status(
        self, device_status: dict, device_id: str, property_name: str
    ) -> None:
        if not self.is_connected():
            return
        self._queue_message(
            {
                "action": "device_status",
                "data": {
                    "device_id": device_id,
                    "data": {
                        "property_name": property_name,
                        "status": device_status.get(device_id, {}).get(property_name),
                        "timestamp": time.time(),
                    },
                },
            }
        )

    def publish_job_started(self, item: QueueItem) -> None:
        """旧协议没有独立的 started 消息；锁在 job_start 受理时已上报为 busy。"""

    def publish_job_status(
        self,
        feedback_data: dict,
        item: QueueItem,
        status: str,
        return_info: Optional[dict] = None,
    ) -> None:
        """把微后端释放的 job 结果翻译成旧协议 ``job_status``。"""

        if status == "canceled":
            status = "failed"
            if not isinstance(return_info, dict):
                return_info = serialize_result_info("Job was cancelled", False, {})
        job_log = format_job_log(item.job_id, item.task_id, item.device_id, item.action_name)
        key = (item.task_id, item.job_id)
        if status == "running":
            now = time.time()
            cached = self._running_last_sent.get(item.job_id)
            if (
                cached is not None
                and now - cached[0] < _RUNNING_DEBOUNCE_SECONDS
                and cached[1] == feedback_data
            ):
                return
            self._running_last_sent[item.job_id] = (now, copy.deepcopy(feedback_data))
        message = {
            "action": "job_status",
            "data": {
                "job_id": item.job_id,
                "task_id": item.task_id,
                "device_id": item.device_id,
                "notebook_id": item.notebook_id,
                "action_name": item.action_name,
                "status": status,
                "feedback_data": feedback_data or {},
                "return_info": return_info,
                "timestamp": time.time(),
            },
        }
        if status in _TERMINAL_STATUSES:
            self._running_last_sent.pop(item.job_id, None)
            with self._lock:
                record = self._jobs.get(key)
                if record is None:
                    record = _JobRecord(request={})
                    self._jobs[key] = record
                if record.terminal_status == "success" or (
                    record.terminal_status and record.terminal_status == status
                ):
                    logger.warning(
                        "[LegacyWS] 忽略重复终态 %s: cached=%s incoming=%s",
                        job_log,
                        record.terminal_status,
                        status,
                    )
                    return
                record.terminal_message = copy.deepcopy(message)
                record.terminal_status = status
                record.updated_at = time.time()
                self._prune_jobs_locked()
        if not self._queue_message(message):
            logger.debug("[LegacyWS] 未连接，job_status %s (%s) 已缓存待重放", job_log, status)
            return
        if status in _TERMINAL_STATUSES:
            # 终态释放后动作恢复空闲；旧后端靠锁事件恢复调度。
            self.publish_action_lock(item.device_id, item.action_name, free=True)
        logger.trace(f"[LegacyWS] job_status {job_log} -> {status}")

    def publish_job_error_decision_required(self, report: dict) -> bool:
        """旧后端没有失败决策闸门：以 Backend 身份立即放行 failed。

        微后端把每次失败先登记为待决策，等待调度权威（runtime.v1 Backend 或本机
        ``/error-decisions`` API）放行。旧协议里 Backend 的决策永远是「直接失败」，
        这里代它同步作出 abort 决策，让终态经标准释放路径（含 tombstone 审计）
        变成 ``job_status failed`` 回到旧后端。
        """

        backend = self._execution_backend_getter()
        resolve = getattr(backend, "resolve_error_decision", None)
        decision_id = str(report.get("decision_id") or "")
        if not callable(resolve) or not decision_id:
            return False
        options = [
            str(option.get("action") or "")
            for option in report.get("options") or []
            if isinstance(option, dict)
        ]
        selected = "abort" if "abort" in options or not options else options[0]
        resolved = resolve(
            decision_id,
            {
                "action": selected,
                "reason": "legacy backend has no error decision gate",
                "scheduler_updated": True,
            },
        )
        if not resolved:
            logger.warning(
                "[LegacyWS] 无法放行失败决策 %s (job %s)，该 job 将保持挂起",
                decision_id[:8],
                str(report.get("job_id") or "")[:8],
            )
        return bool(resolved)

    def send_ping(self, ping_id: str, timestamp: float) -> bool:
        if not self.is_connected():
            logger.warning("[LegacyWS] 未连接，无法发送 ping")
            return False
        try:
            ping = PingNotice(ping_id=ping_id, client_timestamp=timestamp)
        except Exception as exc:  # noqa: BLE001 - 坏诊断参数不能污染发送队列
            logger.warning("[LegacyWS] 无效 ping 字段: %s", exc)
            return False
        return self._queue_message(
            {"action": "ping", "data": ping.model_dump(mode="json")}
        )

    def publish_action_lock(self, device_id: str, action_name: str, free: bool) -> None:
        self.publish_action_locks(
            [{"device_id": device_id, "action_name": action_name, "free": free}]
        )

    def publish_action_locks(self, locks: list) -> None:
        if self.is_disabled or not locks or not self.is_connected():
            return
        self._queue_message(
            {
                "action": "report_action_lock",
                "data": {
                    "locks": list(locks),
                    "machine_name": BasicConfig.machine_name,
                    "timestamp": time.time(),
                },
            }
        )

    def publish_capabilities_changed(self) -> None:
        """设备/动作集合变化：旧后端靠新的 host_node_ready + 锁快照感知。"""

        self.publish_host_ready()

    def publish_runtime_events(self) -> None:
        """runtime.v1 durable outbox 在旧协议下无消费者。"""

    # ------------------------------------------------------------------
    # 上行：host_node_ready / 全量锁
    # ------------------------------------------------------------------

    def _busy_action_keys(self) -> set:
        backend = self._execution_backend_getter()
        keys = getattr(backend, "busy_device_action_keys", None)
        if callable(keys):
            try:
                return set(keys())
            except Exception:  # noqa: BLE001 - 锁快照退化为全 free
                return set()
        return set()

    def report_all_action_locks(self) -> None:
        """全量锁快照：来源是 host adapter 的 ``_action_value_mappings``。"""

        if not self.is_connected():
            return
        adapter = self._adapter_getter()
        if adapter is None:
            return
        busy = self._busy_action_keys()
        locks: List[Dict[str, Any]] = []
        mappings = getattr(adapter, "_action_value_mappings", {}) or {}
        for device_id in getattr(adapter, "devices_names", {}).keys():
            for action_name in (mappings.get(device_id) or {}).keys():
                if action_name.startswith("_execute_driver_command"):
                    continue
                key = f"/devices/{device_id}/{action_name}"
                locks.append(
                    {"device_id": device_id, "action_name": action_name, "free": key not in busy}
                )
        self.publish_action_locks(locks)

    def report_running_jobs(self) -> int:
        """给每个仍在执行、且由旧后端下发的 job 续期一次；返回续期的 job 数。"""

        if not self.is_connected():
            return 0
        backend = self._execution_backend_getter()
        manager = getattr(backend, "device_manager", None)
        if manager is None:
            return 0
        with self._lock:
            known = set(self._jobs)
        count = 0
        for job in manager.get_active_jobs():
            # 本机调度器自己发起的 job 旧后端并不认识，不必也不该替它们续期。
            if (job.task_id, job.job_id) not in known:
                continue
            sent = self._queue_message(
                {
                    "action": "report_action_state",
                    "data": {
                        "type": "job_call_back_status",
                        "device_id": job.device_id,
                        "action_name": job.action_name,
                        "task_id": job.task_id,
                        "job_id": job.job_id,
                        "notebook_id": job.notebook_id or "",
                        "free": False,
                        "need_more": _JOB_KEEPALIVE_NEED_MORE,
                    },
                }
            )
            if sent:
                count += 1
        return count

    def publish_host_ready(self) -> None:
        """旧后端只有收到 ``host_node_ready`` 才会向本连接推送任何命令。"""

        if not self.is_connected():
            return
        adapter = self._adapter_getter()
        if adapter is None:
            logger.info("[LegacyWS] Host adapter 尚未就绪，延后发送 host_node_ready")
            return
        machine_name = BasicConfig.machine_name
        devices: List[Dict[str, Any]] = []
        online = getattr(adapter, "_online_devices", set()) or set()
        machine_names = getattr(adapter, "device_machine_names", {}) or {}
        for device_id, namespace in (getattr(adapter, "devices_names", {}) or {}).items():
            namespace = str(namespace or "/devices")
            device_key = (
                f"{namespace}/{device_id}"
                if namespace.startswith("/")
                else f"/{namespace}/{device_id}"
            )
            devices.append(
                {
                    "device_id": device_id,
                    "namespace": namespace,
                    "device_key": device_key,
                    "is_online": device_key in online,
                    "machine_name": machine_names.get(device_id, machine_name),
                }
            )
        # 先发全量锁再发 ready，旧后端按 FIFO 先对齐锁再开始调度。
        self.report_all_action_locks()
        self._queue_message(
            {
                "action": "host_node_ready",
                "data": {
                    "status": "ready",
                    "timestamp": time.time(),
                    "machine_name": machine_name,
                    "devices": devices,
                },
            }
        )
        self._host_ready_sent_for_connection = True
        logger.info("[LegacyWS] host_node_ready 已发送（%d 个设备）", len(devices))

    # ------------------------------------------------------------------
    # 下行处理
    # ------------------------------------------------------------------

    async def _handle_raw_message(self, raw_message: str | bytes) -> None:
        text = raw_message.decode("utf-8", "replace") if isinstance(raw_message, bytes) else raw_message
        logger.trace(f"[LegacyWS][RECV] {text[:4000]}")
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            logger.warning("[LegacyWS] 忽略无效 JSON 消息")
            return
        if not isinstance(envelope, dict):
            return
        action = str(envelope.get("action") or "")
        data = envelope.get("data")
        edge_session = envelope.get("edge_session")
        if (
            action.endswith("_material")
            and edge_session
            and edge_session != self.session_id
        ):
            logger.debug("[LegacyWS] 跳过归属其它会话 %s 的 %s", edge_session, action)
            return

        # 心跳是控制面特殊消息，必须在接收协程内即时处理；旧协议的
        # job/material 兼容逻辑可能触发同步 HTTP 或执行器调用，放入业务
        # worker 后不会再形成 receive-side head-of-line blocking。
        if action in {"ping", "pong"}:
            try:
                await self._process_message(action, data)
            except Exception:  # noqa: BLE001 - 坏心跳只丢弃当前消息
                logger.error(
                    "[LegacyWS] 处理 %s 心跳失败，已丢弃该消息:\n%s",
                    action,
                    traceback.format_exc(),
                )
            return

        business_queue = self._business_queue
        if business_queue is not None:
            await business_queue.put((action, data))
            return
        try:
            await self._process_message(action, data)
        except Exception:  # noqa: BLE001 - 单条消息失败不能断开连接
            logger.error("[LegacyWS] 处理 %s 失败:\n%s", action, traceback.format_exc())

    async def _business_message_handler(
        self, business_queue: "asyncio.Queue[tuple[str, Any]]"
    ) -> None:
        """串行处理旧协议业务消息；失败只影响当前消息。"""

        while True:
            action, data = await business_queue.get()
            try:
                await self._process_message(action, data)
            except Exception:  # noqa: BLE001 - 旧字段转换失败可继续收心跳
                logger.error(
                    "[LegacyWS] 处理 %s 失败，已丢弃该消息:\n%s",
                    action,
                    traceback.format_exc(),
                )
            finally:
                business_queue.task_done()

    @staticmethod
    def _legacy_ping_data(data: Any) -> dict[str, Any]:
        """把旧云端的 ``id/timestamp`` 别名收敛为 canonical ping 字段。"""

        if not isinstance(data, dict):
            raise ValueError("legacy ping data must be an object")
        normalized = {
            "ping_id": data.get("ping_id") or data.get("id"),
            "client_timestamp": (
                data.get("client_timestamp")
                if data.get("client_timestamp") is not None
                else data.get("timestamp")
            ),
        }
        return PingNotice.model_validate(normalized).model_dump(mode="json")

    async def _process_message(self, action: str, data: Any) -> None:
        if action == "pong":
            normalized = self._legacy_ping_data(data)
            # 旧云端的 pong 也可能使用 ``server_time``；别名只在此
            # 适配器边界转换，runtime.v1 客户端不接受旧字段。
            if isinstance(data, dict):
                server_timestamp = data.get("server_timestamp")
                if server_timestamp is None:
                    server_timestamp = data.get("server_time")
                if server_timestamp is None:
                    # 极旧实现只回显 id/timestamp，没有服务端时钟；
                    # 用收到时刻兜底，至少让诊断完成而不阻塞后续轮次。
                    server_timestamp = time.time()
                normalized["server_timestamp"] = server_timestamp
            pong = PongNotice.model_validate(normalized)
            normalized_pong = pong.model_dump(mode="json")
            # 同时唤醒通用会话诊断和 HostAdapter.test_latency；两张簿记表
            # 都按 ping_id 过滤迟到包，不会互相消费。
            self.handle_pong(normalized_pong)
            adapter = self._adapter_getter()
            if adapter is not None:
                adapter.handle_pong_response(normalized_pong)
            return
        if action == "ping":
            ping = self._legacy_ping_data(data)
            pong = PongNotice(
                **ping,
                server_timestamp=time.time(),
            )
            self._queue_message(
                {"action": "pong", "data": pong.model_dump(mode="json")}
            )
            return
        if action == "job_start":
            await asyncio.to_thread(self._handle_job_start, dict(data or {}))
        elif action in ("cancel_action", "cancel_task"):
            await asyncio.to_thread(self._handle_cancel, dict(data or {}))
        elif action == "query_action_state":
            self._handle_query_action_state(dict(data or {}))
        elif action == "query_action_lock":
            self.report_all_action_locks()
        elif action in ("add_material", "update_material", "remove_material"):
            await asyncio.to_thread(
                self._handle_material_notice, action.split("_", 1)[0], list(data or [])
            )
        elif action in ("add_device", "remove_device"):
            await asyncio.to_thread(
                self._handle_device_manage, action.split("_", 1)[0], list(data or [])
            )
        elif action == "request_restart":
            await self._handle_request_restart(dict(data or {}))
        else:
            logger.debug("[LegacyWS] 未知消息类型: %s", action)

    # -- job_start / cancel ------------------------------------------------

    @staticmethod
    def _legacy_attempt_fields(
        data: Dict[str, Any], *, job_id: str, node_id: str
    ) -> tuple[str, int, Optional[str]]:
        """把旧 ``job_start`` 里的 attempt 别名收敛成执行面字段。

        旧 Backend 并不保证携带 runtime.v1 的 attempt 元数据；缺省时仍按
        一次初始执行处理，但只要它携带了节点运行组/重试链，就不能在适配
        边界把这些字段丢掉。这里不猜测缺失的 retry parent，避免伪造重试链。
        """

        raw_attempt = data.get("attempt_no")
        try:
            attempt_no = int(raw_attempt or 1)
        except (TypeError, ValueError):
            attempt_no = 1
        attempt_no = max(attempt_no, 1)

        raw_retry = data.get("retry_of_job_uuid") or data.get("retry_of")
        retry_of = str(raw_retry).strip() if raw_retry not in (None, "") else None
        # runtime.v1 的一致性约束：初始 attempt 不得携带 retry parent。
        if attempt_no == 1:
            retry_of = None

        group = (
            data.get("node_run_uuid")
            or data.get("attempt_group_uuid")
            or data.get("workflow_node_run_uuid")
            or node_id
            or job_id
        )
        return str(group), attempt_no, retry_of

    def _handle_job_start(self, data: Dict[str, Any]) -> None:
        job_id = str(data.get("job_id") or "")
        task_id = str(data.get("task_id") or "")
        device_id = str(data.get("device_id") or "")
        action_name = str(data.get("action") or data.get("action_name") or "")
        job_log = format_job_log(job_id, task_id, device_id, action_name)
        if not job_id or not device_id or not action_name:
            logger.error("[LegacyWS] job_start 缺少 job_id/device_id/action: %s", data)
            return
        key = (task_id, job_id)
        with self._lock:
            self._prune_jobs_locked()
            record = self._jobs.get(key)
            if record is not None:
                record.updated_at = time.time()
                replay = copy.deepcopy(record.terminal_message)
            else:
                self._jobs[key] = _JobRecord(request=copy.deepcopy(data))
                replay = None
        if record is not None:
            if replay is not None:
                self._queue_message(replay)
                logger.info("[LegacyWS] 重复 job_start %s，回放缓存终态 %s", job_log, record.terminal_status)
            else:
                logger.info("[LegacyWS] 重复 job_start %s，原任务仍在执行，忽略", job_log)
            return

        backend = self._execution_backend_getter()
        item = QueueItem(
            task_type="job_call_back_status",
            device_id=device_id,
            action_name=action_name,
            task_id=task_id,
            job_id=job_id,
            notebook_id=str(data.get("notebook_id") or ""),
            device_action_key=f"/devices/{device_id}/{action_name}",
            node_id=str(data.get("node_id") or ""),
        )
        if backend is None:
            logger.error("[LegacyWS] 执行微后端不可用，job %s 直接失败", job_log)
            self.publish_job_status(
                {}, item, "failed", serialize_result_info("execution backend unavailable", False, {})
            )
            return

        node_id = str(data.get("node_id") or "")
        node_run_uuid, attempt_no, retry_of_job_uuid = self._legacy_attempt_fields(
            data, job_id=job_id, node_id=node_id
        )
        server_info = data.get("server_info")
        if isinstance(server_info, dict):
            server_info = dict(server_info)
        else:
            server_info = None
        # ``test_latency`` 的 server_info 是控制面特殊元数据，类似
        # manual_confirm 的调度字段；普通动作不应被适配器凭空注入时间戳。
        if action_name.strip().lower() == "test_latency":
            if not isinstance(server_info, dict) or not server_info.get(
                "send_timestamp"
            ):
                server_info = dict(server_info or {})
                server_info["send_timestamp"] = time.time()
        raw_action_args = data.get("action_args")
        action_args = (
            {
                key: value
                for key, value in raw_action_args.items()
                if key not in _CONTROL_ACTION_ARGS
            }
            if isinstance(raw_action_args, dict)
            else {}
        )
        workflow_id = str(
            data.get("workflow_id") or data.get("workflow_uuid") or ""
        )
        payload = {
            "job_id": job_id,
            "task_id": task_id,
            "node_id": node_id,
            "workflow_id": workflow_id,
            "device_id": device_id,
            "action": action_name,
            "action_type": str(data.get("action_type") or ""),
            "action_args": action_args,
            "sample_material": dict(data.get("sample_material") or {})
            if isinstance(data.get("sample_material") or {}, dict)
            else {},
            "notebook_id": str(data.get("notebook_id") or ""),
            "node_run_uuid": node_run_uuid,
            "attempt_no": attempt_no,
            "retry_count": attempt_no - 1,
            "always_free": self._action_always_free(device_id, action_name),
        }
        if server_info is not None:
            payload["server_info"] = server_info
        if retry_of_job_uuid is not None:
            payload["retry_of_job_uuid"] = retry_of_job_uuid
        # 旧后端自己不持有微后端库存预占，这里不传 inventory_* 字段。
        logger.info("[LegacyWS] job_start %s -> 微后端 dispatch", job_log)
        self.publish_action_lock(device_id, action_name, free=False)
        try:
            backend.dispatch(payload)
        except Exception as exc:  # noqa: BLE001 - 派发失败必须回终态
            logger.error("[LegacyWS] dispatch %s 失败: %s", job_log, exc)
            self.publish_job_status(
                {}, item, "failed", serialize_result_info(traceback.format_exc(), False, {})
            )

    def _action_always_free(self, device_id: str, action_name: str) -> bool:
        adapter = self._adapter_getter()
        mappings = getattr(adapter, "_action_value_mappings", {}) if adapter else {}
        actions = mappings.get(device_id, {}) if isinstance(mappings, dict) else {}
        for candidate in (action_name, f"auto-{action_name}"):
            value = actions.get(candidate)
            if isinstance(value, dict):
                return bool(value.get("always_free", False))
        return False

    def _handle_cancel(self, data: Dict[str, Any]) -> None:
        backend = self._execution_backend_getter()
        if backend is None:
            return
        job_id = str(data.get("job_id") or "")
        task_id = str(data.get("task_id") or "")
        if job_id:
            if backend.cancel_job(job_id):
                logger.info("[LegacyWS] job %s 已取消", job_id[:8])
            else:
                logger.warning("[LegacyWS] job %s 不在执行中，无法取消", job_id[:8])
        elif task_id:
            cancelled = backend.cancel_task(task_id)
            logger.info("[LegacyWS] task %s 取消了 %d 个 job", task_id[:8], len(cancelled))
        else:
            logger.warning("[LegacyWS] cancel 请求缺少 job_id 与 task_id")

    def _handle_query_action_state(self, data: Dict[str, Any]) -> None:
        """纯被动查询：回复 job 当前是否仍在执行，不触发任何执行。"""

        device_id = str(data.get("device_id") or "")
        action_name = str(data.get("action_name") or "")
        task_id = str(data.get("task_id") or "")
        job_id = str(data.get("job_id") or "")
        if not all((device_id, action_name, task_id, job_id)):
            logger.error("[LegacyWS] query_action_state 缺少必填字段: %s", data)
            return
        backend = self._execution_backend_getter()
        manager = getattr(backend, "device_manager", None)
        existing = manager.get_job_info(job_id) if manager is not None else None
        busy = existing is not None and existing.task_id == task_id
        self._queue_message(
            {
                "action": "report_action_state",
                "data": {
                    "type": "query_action_status",
                    "device_id": device_id,
                    "action_name": action_name,
                    "task_id": task_id,
                    "job_id": job_id,
                    "notebook_id": str(
                        (existing.notebook_id if existing else "") or data.get("notebook_id") or ""
                    ),
                    "free": not busy,
                    "need_more": 11 if busy else 1,
                },
            }
        )

    # -- 物料 / 设备管理 ---------------------------------------------------

    def _handle_material_notice(self, action: str, items: List[Any]) -> None:
        """旧后端前端改了物料后下发 uuid 列表；把变更拉回权威并分发到设备。"""

        if not items:
            return
        from unilabos.server.backend.legacy_adaptor.legacy.materials import (
            apply_legacy_material_notice,
        )

        try:
            apply_legacy_material_notice(action, items)
        except Exception:  # noqa: BLE001 - 物料通知失败不影响连接
            logger.error("[LegacyWS] 处理 %s_material 失败:\n%s", action, traceback.format_exc())

    def _handle_device_manage(self, action: str, items: List[Any]) -> None:
        from unilabos.backend.hostlink.downlink import device_manage_to_device

        for item in items:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target_node_id") or BasicConfig.host_node_name)
            if action == "add":
                logger.info("[LegacyWS] 在线增加设备暂不支持，跳过 add_device: %s", item.get("id", ""))
                continue
            try:
                result = device_manage_to_device(target, action, item)
                logger.info("[LegacyWS] %s_device on %s: %s", action, target, result)
            except Exception as exc:  # noqa: BLE001 - 单个设备失败不影响其它
                logger.error("[LegacyWS] %s_device 失败: %s", action, exc)

    async def _handle_request_restart(self, data: Dict[str, Any]) -> None:
        reason = str(data.get("reason") or "unknown")
        delay = float(data.get("delay") or 2)
        logger.info("[LegacyWS] 收到重启请求 reason=%s delay=%ss", reason, delay)
        self._queue_message(
            {"action": "restart_acknowledged", "data": {"reason": reason, "delay": delay}}
        )
        await asyncio.sleep(delay)
        from unilabos.server.backend.restart import get_restart_coordinator

        try:
            get_restart_coordinator().request(mode="quiescent", scope="process")
        except Exception as exc:  # noqa: BLE001 - 重启失败保持在线
            logger.error("[LegacyWS] 重启登记失败: %s", exc)

    # ------------------------------------------------------------------
    # 连接循环
    # ------------------------------------------------------------------

    def _prune_jobs_locked(self) -> None:
        now = time.time()
        expired = [
            key for key, record in self._jobs.items()
            if now - record.updated_at > _JOB_CACHE_TTL_SECONDS
        ]
        for key in expired:
            self._jobs.pop(key, None)
        overflow = len(self._jobs) - _JOB_CACHE_MAX_ENTRIES
        if overflow > 0:
            for key in sorted(self._jobs, key=lambda k: self._jobs[k].updated_at)[:overflow]:
                self._jobs.pop(key, None)

    def _queue_message(self, message: Dict[str, Any]) -> bool:
        if self.is_disabled or not self.is_connected():
            return False
        try:
            self._send_queue.put_nowait(message)
            return True
        except Full:
            logger.error("[LegacyWS] 发送队列已满，丢弃 %s", message.get("action"))
            return False

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._connection_handler())
        except Exception:  # noqa: BLE001 - reconnect loop owns reporting
            logger.error(traceback.format_exc())
        finally:
            self._loop.close()
            self._loop = None

    async def _connection_handler(self) -> None:
        while self._running:
            try:
                ssl_context = None
                if self.websocket_url.startswith("wss://"):
                    ssl_context = ssl_module.create_default_context()
                ws_logger = logging.getLogger("websockets.client")
                ws_logger.setLevel(logging.INFO)
                async with websockets.connect(
                    self.websocket_url,
                    ssl=ssl_context,
                    open_timeout=20,
                    ping_interval=WSConfig.ws_ping_interval,
                    ping_timeout=WSConfig.ws_ping_timeout,
                    close_timeout=5,
                    additional_headers={
                        "Authorization": f"Lab {BasicConfig.auth_secret()}",
                        "EdgeSession": self.session_id,
                    },
                    logger=ws_logger,
                ) as websocket:
                    self._websocket = websocket
                    self._connected = True
                    self._reconnect_count = 0
                    self._host_ready_sent_for_connection = False
                    logger.info("[LegacyWS] 已连接旧协议 Backend %s", self.websocket_url)
                    sender = asyncio.create_task(self._send_handler(), name="legacy-ws-send")
                    # 每次（重）连都要重新注册，否则旧后端不会向本连接推送
                    self.publish_host_ready()
                    ready_watch = asyncio.create_task(
                        self._host_ready_watchdog(), name="legacy-ws-ready"
                    )
                    keepalive = asyncio.create_task(
                        self._job_keepalive_loop(), name="legacy-ws-keepalive"
                    )
                    business_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
                    self._business_queue = business_queue
                    business_worker = asyncio.create_task(
                        self._business_message_handler(business_queue),
                        name="legacy-ws-business",
                    )
                    background = (sender, ready_watch, keepalive, business_worker)
                    try:
                        async for raw_message in websocket:
                            await self._handle_raw_message(raw_message)
                    finally:
                        self._connected = False
                        self._business_queue = None
                        for task in background:
                            task.cancel()
                        for task in background:
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        self._discard_queued()
            except websockets.exceptions.ConnectionClosed:
                logger.warning("[LegacyWS] 与 Backend 的连接中断")
            except TimeoutError:
                logger.warning(
                    "[LegacyWS] 连接超时（已尝试 %d 次），请检查网络", self._reconnect_count + 1
                )
            except websockets.exceptions.InvalidStatus as exc:
                logger.warning(
                    "[LegacyWS] 服务端返回 %s，上一进程可能还未退出",
                    exc.response.status_code,
                )
            except Exception as exc:  # noqa: BLE001 - reconnect after reporting
                logger.error("[LegacyWS] 连接错误: %s", exc)
                logger.debug(traceback.format_exc())
            finally:
                self._connected = False
                self._websocket = None

            if not self._running:
                break
            if self._reconnect_count >= WSConfig.max_reconnect_attempts:
                logger.error("[LegacyWS] 达到最大重连次数")
                break
            self._reconnect_count += 1
            await asyncio.sleep(WSConfig.reconnect_interval)

    async def _host_ready_watchdog(self) -> None:
        """Host adapter 晚于 WS 就绪时，补发 host_node_ready。"""

        while self._connected:
            if not self._host_ready_sent_for_connection:
                self.publish_host_ready()
            await asyncio.sleep(1)

    async def _job_keepalive_loop(self) -> None:
        """周期给运行中的 job 续期，见 ``_JOB_KEEPALIVE_SECONDS``。"""

        while self._connected:
            try:
                count = self.report_running_jobs()
            except Exception:  # noqa: BLE001 - 续期失败不能拖垮连接
                logger.error("[LegacyWS] job 续期失败:\n%s", traceback.format_exc())
                count = 0
            if count:
                logger.trace(f"[LegacyWS] 已为 {count} 个运行中 job 续期")
            await asyncio.sleep(_JOB_KEEPALIVE_SECONDS)

    async def _send_handler(self) -> None:
        while self._connected and self._websocket is not None:
            try:
                message = self._send_queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.05)
                continue
            try:
                text = json.dumps(message, ensure_ascii=False)
                await self._websocket.send(text)
                logger.trace(f"[LegacyWS][SEND] {text[:6000]}")
            except Exception:  # noqa: BLE001 - closing forces reconnect
                await self._websocket.close()
                raise

    def _discard_queued(self) -> None:
        while True:
            try:
                self._send_queue.get_nowait()
            except Empty:
                return


__all__ = ["LegacyBackendWebSocketClient"]
