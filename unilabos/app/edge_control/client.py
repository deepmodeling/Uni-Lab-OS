"""后端调度器与 HostNode 之间的生产 Edge 协议桥。"""

from __future__ import annotations

import asyncio
import copy
import json
import ssl as ssl_module
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse, urlunparse

import websockets

from unilabos.app.communication import BaseCommunicationClient
from unilabos.app.edge_control.http import EdgeDataPlane, websocket_url
from unilabos.app.edge_control.store import EdgeControlStore, StoredEvent, StoredJob
from unilabos.config.config import BasicConfig, EdgeControlConfig, HTTPConfig
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

    @classmethod
    def from_config(cls) -> "EdgeControlSettings":
        scheduler_address = str(
            EdgeControlConfig.scheduler_addr
            or HTTPConfig.schedule_addr
            or _derive_scheduler_address(HTTPConfig.remote_addr)
        ).strip()
        backend_address = str(
            EdgeControlConfig.backend_addr or HTTPConfig.remote_addr
        ).strip()
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
        )


@dataclass
class EdgeJobContext:
    """HostNode 回调需要的最小 Job 上下文。"""

    job_id: str
    task_id: str
    node_id: str
    command_uuid: str
    device_id: str
    action_name: str
    action_type: str
    action_args: Dict[str, Any]
    trace_context: Dict[str, str]
    task_type: str = "job_call_back_status"
    notebook_id: str = ""

    @property
    def device_action_key(self) -> str:
        return f"/devices/{self.device_id}/{self.action_name}"


class EdgeControlClient(BaseCommunicationClient):
    """HTTP 传事实、WebSocket 传短通知的生产协议客户端。"""

    def __init__(
        self,
        settings: Optional[EdgeControlSettings] = None,
        *,
        store: Optional[EdgeControlStore] = None,
        data_plane: Optional[EdgeDataPlane] = None,
        host_node_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__()
        self.settings = settings or EdgeControlSettings.from_config()
        self.store = store or EdgeControlStore(self.settings.state_db)
        self.instance_uuid = self.store.get_or_create_instance_uuid(
            self.settings.instance_uuid
        )
        self.data_plane = data_plane or EdgeDataPlane(
            self.settings.backend_address,
            self.settings.scheduler_address,
            self.settings.api_key,
            timeout=self.settings.request_timeout,
        )
        self._host_node_provider = host_node_provider or _host_node
        self.client_id = self.instance_uuid
        self.is_disabled = False
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._connected = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket: Any = None
        self._edge_uuid = ""
        self._session_uuid = ""
        self._active_jobs: Set[str] = set()
        self._scheduled_jobs: Set[str] = set()
        self._active_jobs_lock = threading.RLock()
        self._terminal_jobs: Set[str] = set()
        self._tasks: Set[asyncio.Task[Any]] = set()

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
        job = self.store.get_job(str(item.job_id))
        if job is None:
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
        return_info: Optional[dict] = None,
    ) -> None:
        job_uuid = str(item.job_id)
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
        # 设备属性属于事实数据，不通过生产 WebSocket 控制面传播。
        return

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
                self._edge_uuid = str(registration["edge_uuid"])
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

    def _register(self) -> Dict[str, Any]:
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

    def _registration_devices(self) -> List[Dict[str, str]]:
        host_node = self._host_node_provider()
        if host_node is None:
            raise RuntimeError("HostNode is not ready")
        nodes: Dict[str, Dict[str, Any]] = {}
        for tree in host_node.resources_config.dump():
            for resource in tree:
                resource_id = str(resource.get("id") or "").strip()
                if resource_id:
                    nodes[resource_id] = resource
        devices: List[Dict[str, str]] = []
        for local_id in sorted(host_node.devices_names):
            resource = nodes.get(str(local_id), {})
            barcode = str(resource.get("barcode") or "").strip()
            if not barcode:
                continue
            devices.append(
                {
                    "local_id": str(local_id),
                    "name": str(resource.get("name") or local_id),
                    "barcode": barcode,
                }
            )
        if not devices:
            raise RuntimeError("Edge production registration requires a device barcode")
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
            await self._resume_pending_outcomes()
            await self._resume_received_jobs()
            try:
                async for encoded in websocket:
                    envelope = json.loads(encoded)
                    await self._handle_envelope(envelope)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    def _hello_envelope(self) -> Dict[str, Any]:
        running_job_uuids = {
            job.job_uuid
            for job in self.store.list_jobs({"running", "cancel_requested"})
        }
        running_jobs: List[Dict[str, Any]] = []
        for job_uuid in sorted(running_job_uuids):
            job = self.store.get_job(job_uuid)
            if job is not None:
                running_job: Dict[str, Any] = {
                    "job_uuid": job.job_uuid,
                    "command_uuid": job.command_uuid,
                    "state": "running",
                }
                running_jobs.append(running_job)
        return _envelope(
            "hello",
            {
                "edge_uuid": self._edge_uuid,
                "session_uuid": self._session_uuid,
                "last_ack_command_sequence": self.store.last_ack_command_sequence(),
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

    async def _handle_envelope(self, envelope: Dict[str, Any]) -> None:
        message_type = str(envelope.get("type") or "")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("control payload must be an object")
        if message_type == "event.ack":
            event_uuid = str(payload.get("event_uuid") or "")
            if event_uuid:
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
        }:
            raise ValueError(f"unsupported Edge command {message_type!r}")
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
            self.store.record_command(envelope)
            if message_type == "job.start":
                await self._accept_job_start(command_uuid, payload, command_trace)
            elif message_type == "job.cancel":
                await self._accept_job_cancel(command_uuid, payload, command_trace)

    async def _send_pong(
        self, ping_uuid: str, parent_carrier: Dict[str, str]
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
            for key in ("traceparent", "tracestate"):
                if trace_context.get(key):
                    envelope[key] = trace_context[key]
            await websocket.send(json.dumps(envelope, ensure_ascii=False))

    async def _accept_job_start(
        self,
        command_uuid: str,
        payload: Dict[str, Any],
        command_trace: Dict[str, str],
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

    async def _accept_job_cancel(
        self,
        command_uuid: str,
        payload: Dict[str, Any],
        command_trace: Dict[str, str],
    ) -> None:
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
        self.store.set_job_status(job_uuid, "cancel_requested")
        host_node = self._host_node_provider()
        if host_node is None or not host_node.cancel_goal(job_uuid):
            await self._commit_terminal_status(
                job_uuid,
                "canceled",
                {},
                {"message": "Job canceled before a running ROS goal was found"},
            )

    async def _resume_received_jobs(self) -> None:
        for job in self.store.list_jobs({"received", "fetch_retry"}):
            self._spawn(self._execute_job(job.job_uuid))

    async def _resume_pending_outcomes(self) -> None:
        for outcome in self.store.list_pending_outcomes():
            self._spawn(self._commit_pending_outcome(outcome.job_uuid))

    async def _execute_job(self, job_uuid: str) -> None:
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
                    try:
                        payload = await asyncio.to_thread(
                            self.data_plane.fetch_job, job
                        )
                        break
                    except Exception as exc:
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
                    device_id=str(payload["local_device_id"]),
                    action_name=str(payload["action_name"]),
                    action_type=str(payload.get("action_type") or ""),
                    action_args=action_args,
                    trace_context=action_trace_context,
                )
                self.store.set_job_status(job_uuid, "dispatching")
                host_node.send_goal(
                    context,
                    action_type=context.action_type,
                    action_kwargs=context.action_args,
                    sample_material={},
                    server_info=None,
                )
        except Exception as exc:
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
        self, job_uuid: str, feedback: Dict[str, Any]
    ) -> None:
        job = self.store.get_job(job_uuid)
        if job is None or job.status in {"outcome_committed", "completed"}:
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
        result_data: Dict[str, Any],
        return_info: Any,
    ) -> None:
        if not self._persist_terminal_status(
            job_uuid, status, result_data, return_info
        ):
            return
        await self._commit_pending_outcome(job_uuid)

    def _persist_terminal_status(
        self,
        job_uuid: str,
        status: str,
        result_data: Dict[str, Any],
        return_info: Any,
    ) -> bool:
        job = self.store.get_job(job_uuid)
        if job is None:
            return False
        outcome = {
            "success": "succeeded",
            "failed": "failed",
            "canceled": "canceled",
            "timeout": "timeout",
        }.get(status, "failed")
        if job.status == "cancel_requested" and outcome == "failed":
            outcome = "canceled"
        normalized_return = _return_info(return_info, result_data)
        error_info: List[Dict[str, Any]] = []
        if outcome != "succeeded":
            error_info.append(_error_info(return_info, outcome))
        inserted = self.store.save_pending_outcome(
            job_uuid, outcome, normalized_return, error_info
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
                    committed = await asyncio.to_thread(
                        self.data_plane.commit_outcome,
                        job,
                        pending.outcome,
                        pending.return_info,
                        pending.error_info,
                    )
                    result_uuid = str(committed.get("uuid") or "")
                    event_payload: Dict[str, Any] = {"job_uuid": job_uuid}
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
            except Exception as exc:
                logger.warning(
                    f"[EdgeControl] 提交 Job {job_uuid[:8]} outcome 失败，稍后重试: {exc}"
                )
                await asyncio.sleep(max(self.settings.reconnect_interval, 0.5))

    def _enqueue_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        parent_carrier: Optional[Dict[str, str]] = None,
        fallback_carrier: Optional[Dict[str, str]] = None,
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


def _derive_scheduler_address(backend_address: str) -> str:
    parsed = urlparse(str(backend_address or ""))
    if not parsed.scheme or not parsed.netloc:
        return str(backend_address or "")
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port + 1}"
    else:
        netloc = parsed.netloc
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def _envelope(
    message_type: str,
    payload: Dict[str, Any],
    *,
    message_uuid: Optional[str] = None,
    sent_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "protocol_version": 1,
        "message_uuid": message_uuid or str(uuid.uuid4()),
        "type": message_type,
        "sent_at": sent_at or _utc_now(),
        "payload": payload,
    }


def _stored_event_envelope(
    event: StoredEvent, trace_context: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    envelope = _envelope(
        event.event_type,
        event.payload,
        message_uuid=event.event_uuid,
        sent_at=event.created_at,
    )
    effective = trace_context or _event_trace_carrier(event)
    for key in ("traceparent", "tracestate"):
        if effective.get(key):
            envelope[key] = effective[key]
    return envelope


def _job_trace_carrier(job: StoredJob) -> Dict[str, str]:
    return {
        "traceparent": job.traceparent,
        "tracestate": job.tracestate,
    }


def _event_trace_carrier(event: StoredEvent) -> Dict[str, str]:
    return {
        "traceparent": event.traceparent,
        "tracestate": event.tracestate,
    }


def _message_trace_carrier(message: Dict[str, Any]) -> Dict[str, str]:
    return {
        "traceparent": str(message.get("traceparent") or ""),
        "tracestate": str(message.get("tracestate") or ""),
    }


def _current_trace_carrier(
    *, fallback: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    carrier: Dict[str, Any] = {}
    inject_trace_context(carrier)
    result = {
        key: str(carrier.get(key) or "")
        for key in ("traceparent", "tracestate")
    }
    fallback = fallback or {}
    for key in ("traceparent", "tracestate"):
        if not result[key] and fallback.get(key):
            result[key] = str(fallback[key])
    return result


def _validate_job_payload(job: StoredJob, payload: Dict[str, Any]) -> None:
    expected = {
        "job_uuid": job.job_uuid,
        "task_uuid": job.task_uuid,
        "node_uuid": job.node_uuid,
        "command_uuid": job.command_uuid,
    }
    for field, value in expected.items():
        if str(payload.get(field) or "") != value:
            raise ValueError(f"HTTP Job {field} does not match job.start")
    if not str(payload.get("local_device_id") or ""):
        raise ValueError("HTTP Job local_device_id is required")
    if not str(payload.get("action_name") or ""):
        raise ValueError("HTTP Job action_name is required")
    if not isinstance(payload.get("param"), dict):
        raise ValueError("HTTP Job param must be an object")


def _return_info(return_info: Any, result_data: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(return_info, dict):
        normalized = copy.deepcopy(return_info)
    elif return_info is None:
        normalized = {}
    else:
        normalized = {"raw": str(return_info)}
    if result_data and "result" not in normalized:
        normalized["result"] = result_data
    return normalized


def _error_info(return_info: Any, outcome: str) -> Dict[str, Any]:
    if isinstance(return_info, dict):
        message = return_info.get("error") or return_info.get("message")
        if message:
            return {"message": str(message), "outcome": outcome}
    if return_info:
        return {"message": str(return_info), "outcome": outcome}
    return {"message": f"Device action {outcome}", "outcome": outcome}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000Z"
