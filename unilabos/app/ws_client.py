#!/usr/bin/env python
# coding=utf-8
"""旧后端完整载荷 WebSocket 协议的兼容实现。

该模块保留旧后端使用的 ``job_start``、设备状态、物料变更和完整结果
payload。新微后端的 ``control.v1`` 轻通知协议位于
``unilabos.app.backend_protocol.control``，不要在这里继续扩展新协议消息。
"""

import json
import logging
import time
import uuid
import threading
import asyncio
import traceback
import websockets
import ssl as ssl_module
import copy
from queue import Queue, Empty
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum

from typing_extensions import TypedDict

from unilabos.app.model import JobAddReq
from unilabos.resources.objects.resource import ResourceDictType
from unilabos.app.execution_adapter import get_execution_adapter
from unilabos.utils.type_check import serialize_result_info
from unilabos.app.communication import BaseCommunicationClient
from unilabos.config.config import WSConfig, BasicConfig
from unilabos.utils.log import get_comm_logger
from unilabos.utils.tracing import wrap_with_current_context
from unilabos.app.backend_protocol.common import build_schedule_websocket_url


def _get_job_execution_backend():
    """Resolve the process-owned microbackend without creating an import cycle."""

    try:
        from unilabos.server.scheduler.integration import get_edge_backend

        return get_edge_backend()
    except ImportError:
        return None


# 服务端通信专用 logger：独立成文件(unilabos_data/logs/ws_comm_*.log)，
# 全量 TRACE 落本地、微秒级时间戳 + 线程名，便于排查通信/queue 时序问题。
# 未调用 configure_comm_logger 时安全回退到根 logger。
logger = get_comm_logger()


def format_job_log(job_id: str, task_id: str = "", device_id: str = "", action_name: str = "") -> str:
    """格式化job日志信息：jobid[:4]-taskid[:4] device_id/action_name"""
    job_part = f"{job_id[:4]}-{task_id[:4]}" if task_id else job_id[:4]
    device_part = f"{device_id}/{action_name}" if device_id and action_name else ""
    return f"{job_part} {device_part}".strip()


class JobStatus(Enum):
    """任务状态枚举"""

    QUEUE = "queue"  # 排队中
    STARTED = "started"  # 执行中
    ENDED = "ended"  # 已结束


@dataclass
class QueueItem:
    """队列项数据结构"""

    task_type: str  # "query_action_status" 或 "job_call_back_status"
    device_id: str
    action_name: str
    task_id: str
    job_id: str
    notebook_id: str
    device_action_key: str
    node_id: str = ""  # 调度图逻辑节点；retry 的新 job 仍关联同一 node_id
    next_run_time: float = 0  # 下次执行时间戳
    retry_count: int = 0  # 重试次数


@dataclass
class JobInfo:
    """任务信息数据结构"""

    job_id: str
    task_id: str
    device_id: str
    notebook_id: str
    action_name: str
    device_action_key: str
    status: JobStatus
    start_time: float
    last_update_time: float = field(default_factory=time.time)
    always_free: bool = False  # 是否为永久闲置动作(不受排队限制)
    node_id: str = ""
    retry_count: int = 0
    # 执行载荷：排队的 job 在出队时由客户端自行启动，需保存原始 job_start 参数
    action_type: str = ""
    action_args: Dict[str, Any] = field(default_factory=dict)
    sample_material: Dict[str, Any] = field(default_factory=dict)
    server_info: Optional[Dict[str, Any]] = None

    def update_timestamp(self):
        """更新最后更新时间"""
        self.last_update_time = time.time()


@dataclass
class WebSocketMessage:
    """WebSocket消息数据结构"""

    action: str
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)


@dataclass
class JobStartCacheEntry:
    """job_start幂等缓存项"""

    request_data: Dict[str, Any]
    response_message: Optional[Dict[str, Any]] = None
    response_status: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class WSResourceChatData(TypedDict):
    uuid: str
    device_uuid: str
    device_id: str
    device_old_uuid: str
    device_old_id: str


class DeviceActionManager:
    """设备动作管理器 - 管理每个device_action_key的任务队列"""

    def __init__(self):
        self.device_queues: Dict[str, List[JobInfo]] = {}  # device_action_key -> job queue
        self.active_jobs: Dict[str, JobInfo] = {}  # device_action_key -> active job
        self.all_jobs: Dict[str, JobInfo] = {}  # job_id -> job_info
        self.lock = threading.RLock()

    def enqueue_job(self, job_info: JobInfo) -> Tuple[bool, bool]:
        """
        服务端直接下发的 job 入队/直发。

        返回 (should_start_now, lock_became_busy):
          should_start_now: 该 job 是否应立即启动(调用方负责 send_goal)
          lock_became_busy: 该 device+action 是否发生 free->busy 翻转(需上报 busy 锁)
        """
        with self.lock:
            device_key = job_info.device_action_key
            existing_job = self.all_jobs.get(job_info.job_id)
            if existing_job is not None:
                if job_info.task_id != existing_job.task_id:
                    logger.warning(
                        "[DeviceActionManager] Duplicate job_id has different task_id: "
                        f"{job_info.job_id[:8]} old={existing_job.task_id[:8]} new={job_info.task_id[:8]}"
                    )
                    return False, False
                if job_info.notebook_id and not existing_job.notebook_id:
                    existing_job.notebook_id = job_info.notebook_id
                existing_job.update_timestamp()
                job_log = format_job_log(
                    existing_job.job_id,
                    existing_job.task_id,
                    existing_job.device_id,
                    existing_job.action_name,
                )
                logger.info(
                    f"[DeviceActionManager] Duplicate job request ignored for job {job_log}, "
                    f"status={existing_job.status}"
                )
                return False, False

            # 总是将job添加到all_jobs中
            self.all_jobs[job_info.job_id] = job_info

            # always_free的动作不受排队限制，直接并发执行，不占用设备锁
            if job_info.always_free:
                job_info.status = JobStatus.STARTED
                job_info.update_timestamp()
                job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
                logger.trace(f"[DeviceActionManager] Job {job_log} always_free, start immediately")
                return True, False

            # 设备上已有占用(正在执行)或已有排队任务 -> 入队
            if device_key in self.active_jobs or self.device_queues.get(device_key):
                if device_key not in self.device_queues:
                    self.device_queues[device_key] = []
                job_info.status = JobStatus.QUEUE
                self.device_queues[device_key].append(job_info)
                job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
                logger.info(f"[DeviceActionManager] Job {job_log} queued for {device_key}")
                return False, False

            # 设备空闲 -> 立即执行并占用设备锁(free->busy 翻转)
            job_info.status = JobStatus.STARTED
            job_info.update_timestamp()
            self.active_jobs[device_key] = job_info
            job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
            logger.trace(f"[DeviceActionManager] Job {job_log} start immediately for {device_key}")
            return True, True

    def end_job(self, job_id: str) -> Tuple[Optional[JobInfo], bool]:
        """
        结束任务。

        返回 (next_job, lock_became_free):
          next_job: 下一个应启动的任务(调用方负责 send_goal)，无则 None
          lock_became_free: 该 device+action 是否发生 busy->free 翻转(需上报 free 锁)
        """
        with self.lock:
            if job_id not in self.all_jobs:
                logger.warning(f"[DeviceActionManager] Job {job_id[:4]} not found for end")
                return None, False

            job_info = self.all_jobs[job_id]
            device_key = job_info.device_action_key

            # always_free的job直接清理，不影响队列/锁
            if job_info.always_free:
                job_info.status = JobStatus.ENDED
                job_info.update_timestamp()
                del self.all_jobs[job_id]
                return None, False

            # 移除活跃任务
            was_active = device_key in self.active_jobs and self.active_jobs[device_key].job_id == job_id
            if was_active:
                del self.active_jobs[device_key]
            else:
                job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
                logger.warning(f"[DeviceActionManager] Job {job_log} was not active for {device_key}")
            job_info.status = JobStatus.ENDED
            job_info.update_timestamp()
            del self.all_jobs[job_id]

            # 检查队列中是否有等待的任务 -> 直接置 STARTED 并占用，锁保持 busy
            if device_key in self.device_queues and self.device_queues[device_key]:
                next_job = self.device_queues[device_key].pop(0)  # FIFO
                next_job.status = JobStatus.STARTED
                next_job.update_timestamp()
                self.active_jobs[device_key] = next_job
                next_job_log = format_job_log(
                    next_job.job_id, next_job.task_id, next_job.device_id, next_job.action_name
                )
                logger.trace(f"[DeviceActionManager] Next job {next_job_log} starts for {device_key}")
                return next_job, False

            # 队列已空：若刚释放了活跃任务，则 busy->free 翻转
            return None, was_active

    def get_active_jobs(self) -> List[JobInfo]:
        """获取所有正在执行的任务(含active_jobs和always_free的STARTED job)"""
        with self.lock:
            jobs = list(self.active_jobs.values())
            # 补充 always_free 的 STARTED job(它们不在 active_jobs 中)
            for job in self.all_jobs.values():
                if job.always_free and job.status == JobStatus.STARTED and job not in jobs:
                    jobs.append(job)
            return jobs

    def get_queued_jobs(self) -> List[JobInfo]:
        """获取所有排队中的任务"""
        with self.lock:
            queued = []
            for queue in self.device_queues.values():
                queued.extend(queue)
            return queued

    def get_job_info(self, job_id: str) -> Optional[JobInfo]:
        """获取任务信息"""
        with self.lock:
            return self.all_jobs.get(job_id)

    def is_action_busy(self, device_action_key: str) -> bool:
        """该 device+action 是否被占用(有正在执行或排队的非 always_free 任务)。"""
        with self.lock:
            if device_action_key in self.active_jobs:
                return True
            return bool(self.device_queues.get(device_action_key))

    def cancel_job(self, job_id: str) -> Tuple[bool, Optional[JobInfo], bool]:
        """
        取消单个任务。

        返回 (success, next_job, lock_became_free):
          success: 是否成功取消
          next_job: 取消活跃任务后被提升应启动的下一个任务(调用方负责 send_goal)，无则 None
          lock_became_free: 该 device+action 是否发生 busy->free 翻转(需上报 free 锁)
        """
        with self.lock:
            if job_id not in self.all_jobs:
                logger.warning(f"[DeviceActionManager] Job {job_id[:4]} not found for cancel")
                return False, None, False

            job_info = self.all_jobs[job_id]
            device_key = job_info.device_action_key

            # always_free的job直接清理，不影响锁
            if job_info.always_free:
                job_info.status = JobStatus.ENDED
                del self.all_jobs[job_id]
                job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
                logger.trace(f"[DeviceActionManager] Always-free job {job_log} cancelled")
                return True, None, False

            # 如果是正在执行的任务
            if device_key in self.active_jobs and self.active_jobs[device_key].job_id == job_id:
                del self.active_jobs[device_key]
                job_info.status = JobStatus.ENDED
                del self.all_jobs[job_id]
                job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
                logger.trace(f"[DeviceActionManager] Active job {job_log} cancelled for {device_key}")

                # 队列中有等待任务 -> 提升并占用，锁保持 busy
                if device_key in self.device_queues and self.device_queues[device_key]:
                    next_job = self.device_queues[device_key].pop(0)
                    next_job.status = JobStatus.STARTED
                    next_job.update_timestamp()
                    self.active_jobs[device_key] = next_job
                    next_job_log = format_job_log(
                        next_job.job_id, next_job.task_id, next_job.device_id, next_job.action_name
                    )
                    logger.trace(f"[DeviceActionManager] Next job {next_job_log} starts after cancel")
                    return True, next_job, False
                # 队列已空 -> busy->free 翻转
                return True, None, True

            # 如果是排队中的任务(取消不影响锁)
            elif device_key in self.device_queues:
                original_length = len(self.device_queues[device_key])
                self.device_queues[device_key] = [j for j in self.device_queues[device_key] if j.job_id != job_id]
                if len(self.device_queues[device_key]) < original_length:
                    job_info.status = JobStatus.ENDED
                    del self.all_jobs[job_id]
                    job_log = format_job_log(
                        job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name
                    )
                    logger.trace(f"[DeviceActionManager] Queued job {job_log} cancelled for {device_key}")
                    return True, None, False

            job_log = format_job_log(job_info.job_id, job_info.task_id, job_info.device_id, job_info.action_name)
            logger.warning(f"[DeviceActionManager] Job {job_log} not found in active or queued jobs")
            return False, None, False

    def cancel_jobs_by_task_id(self, task_id: str) -> Tuple[List[str], List[JobInfo], List[Tuple[str, str]]]:
        """
        按task_id取消所有相关任务。

        返回 (cancelled_job_ids, next_jobs_to_start, freed_locks):
          cancelled_job_ids: 被取消的 job_id 列表
          next_jobs_to_start: 因取消而被提升应启动的任务列表(调用方负责 send_goal)
          freed_locks: 发生 busy->free 翻转的 (device_id, action_name) 列表(需上报 free 锁)
        """
        cancelled_job_ids: List[str] = []
        next_jobs_to_start: List[JobInfo] = []
        freed_locks: List[Tuple[str, str]] = []

        # 首先找到所有属于该task_id的job
        with self.lock:
            jobs_to_cancel = [job_info for job_info in self.all_jobs.values() if job_info.task_id == task_id]

        if not jobs_to_cancel:
            logger.warning(f"[DeviceActionManager] No jobs found for task_id: {task_id}")
            return cancelled_job_ids, next_jobs_to_start, freed_locks

        logger.info(f"[DeviceActionManager] Found {len(jobs_to_cancel)} jobs to cancel for task_id: {task_id}")

        # 逐个取消job
        for job_info in jobs_to_cancel:
            success, next_job, lock_became_free = self.cancel_job(job_info.job_id)
            if success:
                cancelled_job_ids.append(job_info.job_id)
            # 被提升的下一个任务若也属于本 task_id，会在后续循环中被取消，无需启动
            if next_job is not None and next_job.task_id != task_id:
                next_jobs_to_start.append(next_job)
            if lock_became_free:
                freed_locks.append((job_info.device_id, job_info.action_name))

        logger.info(
            f"[DeviceActionManager] Successfully cancelled {len(cancelled_job_ids)} " f"jobs for task_id: {task_id}"
        )

        return cancelled_job_ids, next_jobs_to_start, freed_locks


class MessageProcessor:
    """消息处理线程 - 处理WebSocket消息，划分任务执行和任务队列"""

    def __init__(self, websocket_url: str, send_queue: Queue, device_manager: DeviceActionManager):
        self.websocket_url = websocket_url
        self.send_queue = send_queue
        self.device_manager = device_manager
        self.queue_processor = None  # 延迟设置
        self.websocket_client = None  # 延迟设置
        self.inventory_service = None  # 由 scheduler.integration 延迟注入
        self.session_id = str(uuid.uuid4())[:6]  # 产生一个随机的session_id

        # WebSocket连接
        self.websocket = None
        self.connected = False

        # 线程控制
        self.is_running = False
        self.thread = None
        self._loop = None  # asyncio event loop引用，用于外部关闭websocket
        self.reconnect_count = 0

        logger.info(f"[MessageProcessor] Initialized for URL: {websocket_url}")

    def set_queue_processor(self, queue_processor: "QueueProcessor"):
        """设置队列处理器引用"""
        self.queue_processor = queue_processor

    def set_websocket_client(self, websocket_client: "WebSocketClient"):
        """设置WebSocket客户端引用"""
        self.websocket_client = websocket_client

    def start(self) -> None:
        """启动消息处理线程"""
        if self.is_running:
            logger.warning("[MessageProcessor] Already running")
            return

        self.is_running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="MessageProcessor")
        self.thread.start()
        logger.trace("[MessageProcessor] Started")

    def stop(self) -> None:
        """停止消息处理线程"""
        self.is_running = False
        # 主动关闭websocket以快速中断消息接收循环
        ws = self.websocket
        loop = self._loop
        if ws and loop and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        logger.info("[MessageProcessor] Stopped")

    def _run(self):
        """运行消息处理主循环"""
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._connection_handler())
        except Exception as e:
            logger.error(f"[MessageProcessor] Thread error: {str(e)}")
            logger.error(traceback.format_exc())
        finally:
            if self._loop:
                self._loop.close()
            self._loop = None

    async def _connection_handler(self):
        """处理WebSocket连接和重连逻辑"""
        while self.is_running:
            try:
                # 构建SSL上下文
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
                        "EdgeSession": f"{self.session_id}",
                    },
                    logger=ws_logger,
                ) as websocket:
                    self.websocket = websocket
                    self.connected = True
                    self.reconnect_count = 0

                    logger.info(f"[MessageProcessor] 已连接到 {self.websocket_url}")

                    # 启动发送协程
                    send_task = asyncio.create_task(self._send_handler(), name="websocket-send_task")

                    # 每次连接（含重连）后尝试向服务端注册，
                    # 否则服务端不知道客户端已上线，不会推送消息。
                    # 注意：publish_host_ready 内部带就绪门禁——HostNode 未初始化完成时会自动延后，
                    # 首连若设备尚未就绪则不会在此发送，待 HostNode 初始化完成后由其回调补发。
                    if self.websocket_client:
                        self.websocket_client.publish_host_ready()

                    try:
                        # 接收消息循环
                        await self._message_handler()
                    finally:
                        # 必须在 async with __aexit__ 之前停止 send_task，
                        # 否则 send_task 会在关闭握手期间继续发送数据，
                        # 干扰 websockets 库的内部清理，导致 task 泄漏。
                        self.connected = False
                        send_task.cancel()
                        try:
                            await send_task
                        except asyncio.CancelledError:
                            pass

            except websockets.exceptions.ConnectionClosed:
                logger.warning("[MessageProcessor] 与服务端连接中断")
            except TimeoutError:
                logger.warning(
                    f"[MessageProcessor] 与服务端连接通信超时 (已尝试 {self.reconnect_count + 1} 次)，请检查您的网络状况"
                )
            except websockets.exceptions.InvalidStatus as e:
                logger.warning(
                    f"[MessageProcessor] 收到服务端注册码 {e.response.status_code}, 上一进程可能还未退出"
                )
            except Exception as e:
                logger.error(traceback.format_exc())
                logger.error(f"[MessageProcessor] 尝试重连时出错 {str(e)}")
            finally:
                self.connected = False
                self.websocket = None

            # 重连逻辑
            if not self.is_running:
                break
            if self.reconnect_count < WSConfig.max_reconnect_attempts:
                self.reconnect_count += 1
                backoff = WSConfig.reconnect_interval
                logger.info(
                    "[MessageProcessor] 即将在 %s 秒后重连 (已尝试 %s/%s)",
                    backoff,
                    self.reconnect_count,
                    WSConfig.max_reconnect_attempts,
                )
                await asyncio.sleep(backoff)
            else:
                logger.error("[MessageProcessor] Max reconnection attempts reached")
                break

    async def _message_handler(self):
        """处理接收到的消息。

        ConnectionClosed 不在此处捕获，让其向上传播到 _connection_handler，
        以便 async with websockets.connect() 的 __aexit__ 能感知连接已断，
        正确清理内部 task，避免 task 泄漏。
        """
        if not self.websocket:
            logger.error("[MessageProcessor] WebSocket connection is None")
            return

        async for message in self.websocket:
            try:
                logger.trace(f"[WS_RECV] {message}")
                data = json.loads(message)
                message_type = data.get("action", "")
                message_data = data.get("data")
                if self.session_id and self.session_id == data.get("edge_session"):
                    await self._process_message(message_type, message_data)
                else:
                    if message_type.endswith("_material"):
                        logger.trace(
                            f"[MessageProcessor] 收到一条归属 {data.get('edge_session')} 的旧消息：{data}"
                        )
                        logger.debug(
                            f"[MessageProcessor] 跳过了一条归属 {data.get('edge_session')} 的旧消息: {data.get('action')}"
                        )
                    else:
                        await self._process_message(message_type, message_data)
            except json.JSONDecodeError:
                logger.error(f"[MessageProcessor] Invalid JSON received: {message}")
            except Exception as e:
                logger.error(f"[MessageProcessor] Error processing message: {str(e)}")
                logger.error(traceback.format_exc())

    async def _send_handler(self):
        """处理发送队列中的消息"""
        logger.trace("[MessageProcessor] Send handler started")

        try:
            while self.connected and self.websocket:
                try:
                    # 从发送队列获取消息（非阻塞）
                    messages_to_send = []
                    max_batch = 10

                    while len(messages_to_send) < max_batch:
                        try:
                            message = self.send_queue.get_nowait()
                            messages_to_send.append(message)
                        except Empty:
                            break

                    if not messages_to_send:
                        await asyncio.sleep(0.1)
                        continue

                    # 批量发送消息
                    for msg in messages_to_send:
                        if not self.connected or not self.websocket:
                            break

                        try:
                            message_str = json.dumps(msg, ensure_ascii=False)
                            await self.websocket.send(message_str)
                            logger.trace(f"[WS_SEND] {message_str}")
                        except Exception as e:
                            logger.error(f"[MessageProcessor] Failed to send message: {str(e)}")
                            logger.error(f"[WS_SEND_FAILED] {msg}")
                            logger.error(traceback.format_exc())
                            break

                    # 批量发送后短暂等待
                    if len(messages_to_send) > 5:
                        await asyncio.sleep(0.001)

                except Exception as e:
                    logger.error(f"[MessageProcessor] Error in send handler: {str(e)}")
                    logger.error(traceback.format_exc())
                    await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.debug("[MessageProcessor] Send handler cancelled")
            raise
        except Exception as e:
            logger.error(f"[MessageProcessor] Fatal error in send handler: {str(e)}")
            logger.error(traceback.format_exc())
        finally:
            logger.debug("[MessageProcessor] Send handler stopped")

    async def _process_message(self, message_type: str, message_data: Dict[str, Any]):
        """处理收到的消息"""
        logger.trace(f"[MessageProcessor] Processing message: {message_type}")

        try:
            if message_type == "pong":
                self._handle_pong(message_data)
            elif message_type == "query_action_state":
                await self._handle_query_action_state(message_data)
            elif message_type == "query_action_lock":
                await self._handle_query_action_lock(message_data)
            elif message_type == "job_start":
                await self._handle_job_start(message_data)
            elif message_type == "inventory_command":
                await self._handle_inventory_command(message_data)
            elif message_type == "cancel_action" or message_type == "cancel_task":
                await self._handle_cancel_action(message_data)
            elif message_type == "add_material":
                # noinspection PyTypeChecker
                await self._handle_resource_tree_update(message_data, "add")
            elif message_type == "update_material":
                # noinspection PyTypeChecker
                await self._handle_resource_tree_update(message_data, "update")
            elif message_type == "remove_material":
                # noinspection PyTypeChecker
                await self._handle_resource_tree_update(message_data, "remove")
            # elif message_type == "session_id":
            #     self.session_id = message_data.get("session_id")
            #     logger.info(f"[MessageProcessor] Session ID: {self.session_id}")
            elif message_type == "add_device":
                await self._handle_device_manage(message_data, "add")
            elif message_type == "remove_device":
                await self._handle_device_manage(message_data, "remove")
            elif message_type == "request_restart":
                await self._handle_request_restart(message_data)
            elif message_type == "job_error_decision":
                await self._handle_job_error_decision(message_data)
            else:
                logger.debug(f"[MessageProcessor] Unknown message type: {message_type}")

        except Exception as e:
            logger.error(f"[MessageProcessor] Error processing message {message_type}: {str(e)}")
            logger.error(traceback.format_exc())

    async def _handle_inventory_command(self, data: Dict[str, Any]):
        """幂等执行云端下发的库存命令，并用统一 wire schema 回报结果。"""

        command_id = str(data.get("command_id", "") or "")
        if self.inventory_service is None:
            logger.error(
                f"[MessageProcessor] inventory_command {command_id} ignored: "
                "inventory not attached"
            )
            self._send_inventory_command_result(
                {
                    "command_id": command_id,
                    "status": "rejected",
                    "error": "inventory service not attached",
                }
            )
            return

        try:
            from unilabos.server.scheduler.inventory.commands import (
                backend_command_actor,
                execute_command,
            )

            response = execute_command(
                self.inventory_service,
                data,
                trusted_actor=backend_command_actor(data.get("actor")),
            )
        except Exception as exc:
            logger.error(
                f"[MessageProcessor] inventory_command {command_id} failed: {exc}"
            )
            logger.error(traceback.format_exc())
            response = {
                "command_id": command_id,
                "status": "rejected",
                "error": str(exc),
            }
        self._send_inventory_command_result(response)

    def _send_inventory_command_result(self, response: Dict[str, Any]) -> None:
        """经 WebSocket 与 HTTP 回调双路上报库存命令结果。"""

        from unilabos.server.scheduler.inventory.schemas import (
            CloudInventoryCommandResult,
        )

        wire_result = CloudInventoryCommandResult.model_validate(
            {**response, "timestamp": int(time.time() * 1000)}
        ).model_dump(mode="json", exclude_none=True)
        self.send_message(
            {"action": "inventory_command_result", "data": wire_result}
        )

        def _http_callback():
            try:
                from unilabos.server.scheduler.integration import (
                    report_http_inventory_command_result,
                )

                report_http_inventory_command_result(response)
            except Exception as exc:
                logger.warning(
                    "[MessageProcessor] inventory_command_result http callback "
                    f"failed: {exc}"
                )

        threading.Thread(
            target=wrap_with_current_context(_http_callback),
            daemon=True,
            name="inv-cmd-result",
        ).start()

    def _handle_pong(self, pong_data: Dict[str, Any]):
        """处理pong响应"""
        host_node = get_execution_adapter(0)
        if host_node:
            host_node.handle_pong_response(pong_data)

    def _check_action_always_free(self, device_id: str, action_name: str) -> bool:
        """检查该action是否标记为always_free，通过HostNode统一的_action_value_mappings查找"""
        try:
            host_node = get_execution_adapter(0)
            if not host_node:
                return False
            # noinspection PyProtectedMember
            action_mappings = host_node._action_value_mappings.get(device_id)
            if not action_mappings:
                return False
            # 尝试直接匹配或 auto- 前缀匹配
            for key in [action_name, f"auto-{action_name}"]:
                if key in action_mappings:
                    return action_mappings[key].get("always_free", False)
            return False
        except Exception:
            return False

    async def _handle_query_action_state(self, data: Dict[str, Any]):
        """处理query_action_state消息：纯被动查询，只回复当前状态，不入队、无副作用。"""
        device_id = data.get("device_id", "")
        action_name = data.get("action_name", "")
        task_id = data.get("task_id", "")
        job_id = data.get("job_id", "")
        notebook_id = data.get("notebook_id", "")

        if not all([device_id, action_name, task_id, job_id]):
            logger.error("[MessageProcessor] Missing required fields in query_action_state")
            return

        job_log = format_job_log(job_id, task_id, device_id, action_name)

        # 该 (task_id, job_id) 仍在设备管理器中(QUEUE/STARTED) -> 正在处理，回复 busy。
        microbackend = _get_job_execution_backend()
        device_manager = (
            microbackend.device_manager
            if microbackend is not None
            else self.device_manager
        )
        existing_job = device_manager.get_job_info(job_id)
        if existing_job and existing_job.task_id == task_id and existing_job.status in (
            JobStatus.QUEUE,
            JobStatus.STARTED,
        ):
            await self._send_action_state_response(
                existing_job.device_id,
                existing_job.action_name,
                existing_job.task_id,
                existing_job.job_id,
                "query_action_status",
                False,
                10,
                notebook_id=existing_job.notebook_id or notebook_id,
            )
            logger.trace(
                f"[MessageProcessor] query_action_state {job_log} 返回当前状态 {existing_job.status} (busy)"
            )
            return

        # 不在管理器中 -> 已完成/未知，回复 free（仅状态回报，不触发任何执行/入队）。
        if self.websocket_client and self.websocket_client.is_job_cached(job_id, task_id):
            self.websocket_client.log_cached_job(job_id, task_id, source="query_action_state")
        await self._send_action_state_response(
            device_id,
            action_name,
            task_id,
            job_id,
            "query_action_status",
            True,
            0,
            notebook_id=notebook_id,
        )
        logger.trace(f"[MessageProcessor] query_action_state {job_log} 返回当前状态 free")

    async def _handle_query_action_lock(self, data: Dict[str, Any]):
        """处理 query_action_lock：服务端要求客户端重新上报当前全量锁(每个 device+action 的忙闲)。

        与 query_action_state(查询单个 job) 不同，这里是全量锁快照重传，用于服务端侧状态重新对齐。
        """
        if not self.websocket_client:
            logger.warning("[MessageProcessor] query_action_lock received but websocket_client unavailable")
            return
        self.websocket_client.report_all_action_locks()
        logger.trace("[MessageProcessor] query_action_lock: re-reported all action locks")

    async def _handle_job_error_decision(self, data: Dict[str, Any]) -> None:
        """后端完成前端询问和调度更新后，释放 Host 暂存的设备失败。"""

        decision_id = str(data.get("decision_id") or "")
        job_id = str(data.get("job_id") or "")
        device_id = str(data.get("device_id") or "")
        if not decision_id or not job_id or not device_id:
            logger.warning(
                "[MessageProcessor] job_error_decision requires "
                "decision_id, job_id and device_id"
            )
            return
        if data.get("scheduler_updated") is not True:
            logger.warning(
                "[MessageProcessor] Ignore error decision before scheduler update: "
                f"decision={decision_id} job={job_id[:8]}"
            )
            return

        microbackend = _get_job_execution_backend()
        if microbackend is None:
            logger.warning(
                f"[MessageProcessor] Microbackend unavailable, keep backend decision "
                f"pending job={job_id[:8]}"
            )
            return
        if not microbackend.handle_action_error_decision(
            decision_id,
            job_id,
            dict(data),
        ):
            replayed = microbackend.get_resolved_action_error_decision(
                decision_id,
                job_id,
                device_id,
            )
            if replayed is not None:
                logger.info(
                    f"[MessageProcessor] Error decision already released "
                    f"decision={decision_id} job={job_id[:8]}"
                )
                return
            logger.warning(
                f"[MessageProcessor] No pending error decision matched "
                f"decision={decision_id} job={job_id[:8]} device={device_id}"
            )

    async def _handle_job_start(self, data: Dict[str, Any]):
        """处理后端 job_start：统一交给微后端入队和下发执行。"""
        try:
            data = dict(data or {})
            if not data.get("sample_material"):
                data["sample_material"] = {}
            req = JobAddReq(**data)

            job_log = format_job_log(req.job_id, req.task_id, req.device_id, req.action)

            if self.websocket_client:
                # 幂等缓存：首次 job_start 登记缓存并真正执行；
                # 重复的 (task_id, job_id) 则假装执行——直接回放之前缓存的结果，不再下发设备动作。
                is_new_request = self.websocket_client.register_job_start_request(data)
                if not is_new_request:
                    self.websocket_client.log_cached_job(req.job_id, req.task_id, source="job_start")
                    replayed = self.websocket_client.replay_cached_job_start_response(req.job_id, req.task_id)
                    if replayed:
                        logger.info(
                            f"[MessageProcessor] [缓存复用] job_start {job_log} 命中缓存，假装执行并回放缓存结果"
                        )
                    else:
                        logger.info(
                            f"[MessageProcessor] [缓存复用] job_start {job_log} 命中缓存但暂无结果"
                            f"(原任务仍在执行)，跳过重复执行"
                        )
                    return

            action_always_free = self._check_action_always_free(req.device_id, req.action)
            microbackend = _get_job_execution_backend()
            if microbackend is None:
                raise RuntimeError("Job execution microbackend is not available")
            microbackend.dispatch(
                {
                    "job_id": req.job_id,
                    "task_id": req.task_id,
                    "node_id": req.node_id,
                    "device_id": req.device_id,
                    "action": req.action,
                    "action_type": req.action_type,
                    "action_args": req.action_args,
                    "sample_material": req.sample_material,
                    "server_info": req.server_info,
                    "notebook_id": req.notebook_id or "",
                    "retry_count": req.retry_count,
                    "always_free": action_always_free,
                }
            )
            logger.info(f"[MessageProcessor] Submitted job {job_log} to microbackend")

        except Exception as e:
            logger.error(f"[MessageProcessor] Error handling job start: {str(e)}")
            traceback.print_exc()

            # 微后端尚未接管成功时，回报本次 command 的启动失败。
            if "req" in locals() and self.websocket_client:
                job_log = format_job_log(req.job_id, req.task_id, req.device_id, req.action)
                logger.info(f"[MessageProcessor] Publishing failed status for job {job_log}")
                queue_item = QueueItem(
                    task_type="job_call_back_status",
                    device_id=req.device_id,
                    action_name=req.action,
                    task_id=req.task_id,
                    job_id=req.job_id,
                    notebook_id=req.notebook_id or "",
                    device_action_key=f"/devices/{req.device_id}/{req.action}",
                    node_id=req.node_id,
                    retry_count=req.retry_count,
                )
                self.websocket_client.publish_job_status(
                    {},
                    queue_item,
                    "failed",
                    serialize_result_info(traceback.format_exc(), False, {}),
                )
            else:
                logger.warning(
                    "[MessageProcessor] Failed to publish job error status - missing request/client"
                )

    async def _handle_cancel_action(self, data: Dict[str, Any]):
        """处理cancel_action/cancel_task消息"""
        task_id = data.get("task_id")
        job_id = data.get("job_id")

        logger.info(f"[MessageProcessor] Cancel request - task_id: {task_id}, job_id: {job_id}")

        microbackend = _get_job_execution_backend()
        if microbackend is None:
            logger.warning("[MessageProcessor] Cannot cancel: microbackend unavailable")
            return
        if job_id:
            if not microbackend.cancel_job(str(job_id)):
                logger.warning(
                    f"[MessageProcessor] Job {str(job_id)[:8]} was not active in microbackend"
                )
            return
        if task_id:
            cancelled = microbackend.cancel_task(str(task_id))
            logger.info(
                f"[MessageProcessor] Microbackend canceled {len(cancelled)} jobs for task {task_id}"
            )
            return
        logger.warning("[MessageProcessor] Cancel request missing job_id/task_id")
        return

    async def _handle_resource_tree_update(self, resource_uuid_list: List[WSResourceChatData], action: str):
        """处理资源树更新消息（add_material/update_material/remove_material）"""
        if not resource_uuid_list:
            return

        # 按device_id和action分组
        # device_action_groups: {(device_id, action): [uuid_list]}
        device_action_groups = {}

        for item in resource_uuid_list:
            device_id = item["device_id"]
            if not device_id:
                device_id = "host_node"

            # 特殊处理update action: 检查是否设备迁移
            if action == "update":
                device_old_id = item.get("device_old_id", "")
                if not device_old_id:
                    device_old_id = "host_node"

                # 设备迁移：device_id != device_old_id
                if device_id != device_old_id:
                    # 给旧设备发送remove
                    key_remove = (device_old_id, "remove")
                    if key_remove not in device_action_groups:
                        device_action_groups[key_remove] = []
                    device_action_groups[key_remove].append(item["uuid"])

                    # 给新设备发送add
                    key_add = (device_id, "add")
                    if key_add not in device_action_groups:
                        device_action_groups[key_add] = []
                    device_action_groups[key_add].append(item["uuid"])

                    logger.info(f"[资源同步] 跨站Transfer: {item['uuid'][:8]} from {device_old_id} to {device_id}")
                else:
                    # 正常update
                    key = (device_id, "update")
                    if key not in device_action_groups:
                        device_action_groups[key] = []
                    device_action_groups[key].append(item["uuid"])
            else:
                # add或remove action，直接分组
                key = (device_id, action)
                if key not in device_action_groups:
                    device_action_groups[key] = []
                device_action_groups[key].append(item["uuid"])

        logger.trace(
            f"[资源同步] 动作 {action} 分组数量: {len(device_action_groups)}, 总数量: {len(resource_uuid_list)}"
        )

        # 为每个(device_id, action)创建独立的更新线程
        for (device_id, actual_action), items in device_action_groups.items():
            logger.trace(f"[资源同步] {device_id} 物料动作 {actual_action} 数量: {len(items)}")

            def _notify_resource_tree(dev_id, act, item_list):
                try:
                    host_node = get_execution_adapter(timeout=5)
                    if not host_node:
                        logger.error(f"[MessageProcessor] HostNode instance not available for {act}")
                        return

                    success = host_node.notify_resource_tree_update(dev_id, act, item_list)

                    if success is True:
                        logger.info(
                            f"[MessageProcessor] Resource tree {act} completed for device {dev_id}, "
                            f"items: {len(item_list)}"
                        )
                    elif success is None:
                        logger.info(
                            f"[MessageProcessor] Resource tree {act} skipped for device {dev_id}: "
                            "在线增加设备暂不支持"
                        )
                    else:
                        logger.warning(f"[MessageProcessor] Resource tree {act} failed for device {dev_id}")

                except Exception as e:
                    logger.error(f"[MessageProcessor] Error in resource tree {act} for device {dev_id}: {str(e)}")
                    logger.error(traceback.format_exc())

            # 在新线程中执行通知
            thread = threading.Thread(
                target=_notify_resource_tree,
                args=(device_id, actual_action, items),
                daemon=True,
                name=f"ResourceTreeUpdate-{actual_action}-{device_id}",
            )
            thread.start()

    async def _handle_device_manage(self, device_list: list[ResourceDictType], action: str):
        """Handle add_device / remove_device from LabGo server."""
        if not device_list:
            return

        for item in device_list:
            target_node_id = item.get("target_node_id", "host_node")
            if action == "add":
                logger.info(
                    f"[DeviceManage] 在线增加设备暂不支持，跳过 add_device: {item.get('id', '')}"
                )
                continue

            def _notify(target_id: str, act: str, cfg: ResourceDictType):
                try:
                    host_node = get_execution_adapter(timeout=5)
                    if not host_node:
                        logger.error(f"[DeviceManage] HostNode not available for {act}_device")
                        return
                    success = host_node.notify_device_manage(target_id, act, cfg)
                    if success:
                        logger.info(f"[DeviceManage] {act}_device completed on {target_id}")
                    else:
                        logger.warning(f"[DeviceManage] {act}_device failed on {target_id}")
                except Exception as e:
                    logger.error(f"[DeviceManage] Error in {act}_device: {e}")
                    logger.error(traceback.format_exc())

            thread = threading.Thread(
                target=_notify,
                args=(target_node_id, action, item),
                daemon=True,
                name=f"DeviceManage-{action}-{item.get('id', '')}",
            )
            thread.start()

    async def _handle_request_restart(self, data: Dict[str, Any]):
        """
        处理重启请求

        当LabGo发送request_restart时，执行清理并触发重启
        """
        reason = data.get("reason", "unknown")
        delay = data.get("delay", 2)  # 默认延迟2秒
        logger.info(f"[MessageProcessor] Received restart request, reason: {reason}, delay: {delay}s")

        # 发送确认消息
        self.send_message(
            {"action": "restart_acknowledged", "data": {"reason": reason, "delay": delay}}
        )

        # 设置全局重启标志
        import unilabos.app.main as main_module

        main_module._restart_requested = True
        main_module._restart_reason = reason

        # 延迟后执行清理
        await asyncio.sleep(delay)

        # 在新线程中执行清理，避免阻塞当前事件循环
        def do_cleanup():
            import time

            time.sleep(0.5)  # 给当前消息处理完成的时间
            logger.info(f"[MessageProcessor] Starting cleanup for restart, reason: {reason}")
            try:
                from unilabos.app.utils import cleanup_for_restart

                if cleanup_for_restart():
                    logger.info("[MessageProcessor] Cleanup successful, main() will restart")
                else:
                    logger.error("[MessageProcessor] Cleanup failed")
            except Exception as e:
                logger.error(f"[MessageProcessor] Error during cleanup: {e}")

        cleanup_thread = threading.Thread(target=do_cleanup, name="RestartCleanupThread", daemon=True)
        cleanup_thread.start()
        logger.info("[MessageProcessor] Restart cleanup scheduled")

    async def _send_action_state_response(
        self,
        device_id: str,
        action_name: str,
        task_id: str,
        job_id: str,
        typ: str,
        free: bool,
        need_more: int,
        notebook_id: str = "",
    ):
        """发送动作状态响应"""
        message = {
            "action": "report_action_state",
            "data": {
                "type": typ,
                "device_id": device_id,
                "action_name": action_name,
                "task_id": task_id,
                "job_id": job_id,
                "notebook_id": notebook_id,
                "free": free,
                "need_more": need_more + 1,
            },
        }

        try:
            self.send_queue.put_nowait(message)
        except Exception:
            logger.warning("[MessageProcessor] Send queue full, dropping message")

    def send_message(self, message: Dict[str, Any]) -> bool:
        """发送消息到队列"""
        try:
            self.send_queue.put_nowait(message)
            return True
        except Exception:
            logger.warning(f"[MessageProcessor] Failed to queue message: {message.get('action', 'unknown')}")
            return False

    def is_connected(self) -> bool:
        """检查连接状态"""
        return self.connected


class QueueProcessor:
    """队列处理线程 - 定时给发送队列推送消息，管理任务状态"""

    def __init__(self, device_manager: DeviceActionManager, message_processor: MessageProcessor):
        self.device_manager = device_manager
        self.message_processor = message_processor
        self.websocket_client = None  # 延迟设置

        # 线程控制
        self.is_running = False
        self.thread = None

        # 事件通知机制
        self.queue_update_event = threading.Event()

        # 待启动队列：出队/取消提升的任务由本线程统一调用 send_goal，
        # 避免在 ROS 结果回调线程里直接 send_goal(wait_for_server) 阻塞 executor。
        self.pending_starts: "Queue[JobInfo]" = Queue()

        logger.trace("[QueueProcessor] Initialized")

    def set_websocket_client(self, websocket_client: "WebSocketClient"):
        """设置WebSocket客户端引用"""
        self.websocket_client = websocket_client

    def start(self) -> None:
        """启动队列处理线程"""
        if self.is_running:
            logger.warning("[QueueProcessor] Already running")
            return

        self.is_running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="QueueProcessor")
        self.thread.start()
        logger.trace("[QueueProcessor] Started")

    def stop(self) -> None:
        """停止队列处理线程"""
        self.is_running = False
        self.queue_update_event.set()  # 立即唤醒等待中的线程
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        logger.info("[QueueProcessor] Stopped")

    def _run(self):
        """运行队列处理主循环：消费待启动队列，由本线程统一启动出队/取消提升的任务。"""
        logger.trace("[QueueProcessor] Queue processor started")

        while self.is_running:
            try:
                self._drain_pending_starts()

                # 无 READY 超时/周期上报负担，事件驱动为主，10s 兜底唤醒
                self.queue_update_event.wait(timeout=10)
                self.queue_update_event.clear()  # 清除事件

            except Exception as e:
                logger.error(f"[QueueProcessor] Error in queue processor: {str(e)}")
                logger.error(traceback.format_exc())
                time.sleep(1)

        logger.debug("[QueueProcessor] Queue processor stopped")

    def notify_queue_update(self):
        """通知队列有更新，触发立即检查"""
        self.queue_update_event.set()

    def enqueue_pending_start(self, job_info: JobInfo) -> None:
        """登记一个待启动任务(出队/取消提升)，由 QueueProcessor 线程统一 send_goal。"""
        try:
            self.pending_starts.put_nowait(job_info)
        except Exception:
            logger.warning("[QueueProcessor] pending_starts queue full, dropping job")
            return
        self.notify_queue_update()

    def _drain_pending_starts(self) -> None:
        """消费待启动队列，逐个启动。"""
        while True:
            try:
                job = self.pending_starts.get_nowait()
            except Empty:
                break
            self._start_job_goal(job)

    def _start_job_goal(self, job: JobInfo) -> None:
        """用 JobInfo 保存的载荷向 HostNode 下发 goal。"""
        job_log = format_job_log(job.job_id, job.task_id, job.device_id, job.action_name)
        queue_item = QueueItem(
            task_type="job_call_back_status",
            device_id=job.device_id,
            action_name=job.action_name,
            task_id=job.task_id,
            job_id=job.job_id,
            notebook_id=job.notebook_id,
            device_action_key=job.device_action_key,
            node_id=job.node_id,
            retry_count=job.retry_count,
        )

        host_node = get_execution_adapter(0)
        if not host_node:
            logger.error(f"[QueueProcessor] HostNode unavailable, fail dequeued job {job_log}")
            if self.websocket_client:
                self.websocket_client.publish_job_status(
                    {}, queue_item, "failed", serialize_result_info("HostNode instance not available", False, {})
                )
            return

        try:
            host_node.send_goal(
                queue_item,
                action_type=job.action_type,
                action_kwargs=job.action_args,
                sample_material=job.sample_material,
                server_info=job.server_info,
            )
            logger.info(f"[QueueProcessor] Started dequeued job {job_log}")
        except Exception as e:
            logger.error(f"[QueueProcessor] Failed to start dequeued job {job_log}: {e}")
            logger.error(traceback.format_exc())
            if self.websocket_client:
                self.websocket_client.publish_job_status(
                    {}, queue_item, "failed", serialize_result_info(traceback.format_exc(), False, {})
                )

    def handle_job_completed(self, job_id: str, status: str) -> None:
        """处理任务完成：出队下一个任务由本类自行启动；队列清空则上报 free 锁。"""
        # 获取job信息用于日志（end_job 会将其移除，需提前取出 device/action）
        job_info = self.device_manager.get_job_info(job_id)

        # 如果job不存在，说明可能已被手动取消
        if not job_info:
            logger.debug(
                f"[QueueProcessor] Job {job_id[:8]} not found in manager " "(may have been cancelled manually)"
            )
            return

        device_id = job_info.device_id
        action_name = job_info.action_name
        job_log = format_job_log(job_id, job_info.task_id, device_id, action_name)

        logger.trace(f"[QueueProcessor] Job {job_log} completed with status: {status}")

        # 结束任务，获取下一个可执行的任务及锁翻转
        next_job, lock_became_free = self.device_manager.end_job(job_id)

        if next_job:
            # 锁保持 busy，客户端自行启动下一个任务
            self.enqueue_pending_start(next_job)
        elif lock_became_free and self.websocket_client:
            # busy->free 翻转，主动上报锁释放
            self.websocket_client.publish_action_lock(device_id, action_name, free=True)


class WebSocketClient(BaseCommunicationClient):
    """
    重构后的WebSocket客户端 v2

    采用两线程架构：
    - 消息处理线程：处理WebSocket消息，划分任务执行和任务队列
    - 队列处理线程：定时给发送队列推送消息，管理任务状态
    """

    def __init__(self):
        super().__init__()
        self.is_disabled = False
        self.client_id = f"{uuid.uuid4()}"

        # 核心组件
        self.device_manager = DeviceActionManager()
        self.send_queue = Queue(maxsize=1000)

        # 构建WebSocket URL
        self.websocket_url = self._build_websocket_url()
        if not self.websocket_url:
            self.websocket_url = ""  # 默认空字符串，避免None

        # 两个核心线程
        self.message_processor = MessageProcessor(self.websocket_url, self.send_queue, self.device_manager)
        self.queue_processor = QueueProcessor(self.device_manager, self.message_processor)

        # running状态debounce缓存: {job_id: (last_send_timestamp, last_feedback_data)}
        self._job_running_last_sent: Dict[str, tuple] = {}
        self._job_running_debounce_interval: float = 10.0  # 秒

        # job_start幂等缓存: {(task_id, job_id): JobStartCacheEntry}
        self._job_start_cache: Dict[Tuple[str, str], JobStartCacheEntry] = {}
        self._job_start_cache_lock = threading.RLock()
        self._job_start_cache_ttl_seconds: float = 24 * 60 * 60
        self._job_start_cache_max_entries: int = 1024

        # 设置相互引用
        self.message_processor.set_queue_processor(self.queue_processor)
        self.message_processor.set_websocket_client(self)
        self.queue_processor.set_websocket_client(self)

        logger.info(f"[WebSocketClient] Client_id: {self.client_id}")

    def _build_websocket_url(self) -> Optional[str]:
        """构建旧协议 schedule 通道的 WebSocket URL。"""

        return build_schedule_websocket_url()

    @staticmethod
    def _job_start_cache_key(job_id: str, task_id: str) -> Optional[Tuple[str, str]]:
        if not job_id or not task_id:
            return None
        return task_id, job_id

    def _prune_job_start_cache_locked(self) -> None:
        now = time.time()
        expired_keys = [
            key
            for key, entry in self._job_start_cache.items()
            if now - entry.updated_at > self._job_start_cache_ttl_seconds
        ]
        for key in expired_keys:
            self._job_start_cache.pop(key, None)

        overflow = len(self._job_start_cache) - self._job_start_cache_max_entries
        if overflow <= 0:
            return

        oldest_keys = sorted(self._job_start_cache, key=lambda key: self._job_start_cache[key].updated_at)[:overflow]
        for key in oldest_keys:
            self._job_start_cache.pop(key, None)

    def register_job_start_request(self, request_data: Dict[str, Any]) -> bool:
        """登记job_start请求；返回False表示同一(task_id, job_id)已处理过。"""
        key = self._job_start_cache_key(request_data.get("job_id", ""), request_data.get("task_id", ""))
        if key is None:
            return True

        with self._job_start_cache_lock:
            self._prune_job_start_cache_locked()
            cached = self._job_start_cache.get(key)
            if cached is not None:
                cached.updated_at = time.time()
                if cached.request_data != request_data:
                    logger.warning(
                        "[WebSocketClient] Duplicate job_start has different payload for "
                        f"job={key[1][:8]}, task={key[0][:8]}"
                    )
                return False

            self._job_start_cache[key] = JobStartCacheEntry(request_data=copy.deepcopy(request_data))
            self._prune_job_start_cache_locked()
            return True

    def is_job_cached(self, job_id: str, task_id: str) -> bool:
        """判断同一 (task_id, job_id) 是否已 job_start 过（已登记进幂等缓存）。"""
        key = self._job_start_cache_key(job_id, task_id)
        if key is None:
            return False

        with self._job_start_cache_lock:
            self._prune_job_start_cache_locked()
            cached = self._job_start_cache.get(key)
            if cached is None:
                return False
            cached.updated_at = time.time()
            return True

    def log_cached_job(self, job_id: str, task_id: str, source: str = "") -> None:
        """打印命中缓存的 job 内容（请求 + 已缓存结果），便于核对复用的数据。"""
        key = self._job_start_cache_key(job_id, task_id)
        if key is None:
            return

        with self._job_start_cache_lock:
            cached = self._job_start_cache.get(key)
            if cached is None:
                return
            request_data = copy.deepcopy(cached.request_data)
            response_message = copy.deepcopy(cached.response_message)
            response_status = cached.response_status

        result_repr = json.dumps(response_message, ensure_ascii=False) if response_message else "none"
        logger.info(
            f"[WebSocketClient] [缓存复用] 命中缓存 source={source} job={job_id[:8]} task={task_id[:8]} "
            f"status={response_status or 'none'} "
            f"request={json.dumps(request_data, ensure_ascii=False)} "
            f"result={result_repr}"
        )

    def get_cached_job_start_response_status(self, job_id: str, task_id: str) -> str:
        """获取同一job_start已缓存的回复状态。"""
        key = self._job_start_cache_key(job_id, task_id)
        if key is None:
            return ""

        with self._job_start_cache_lock:
            self._prune_job_start_cache_locked()
            cached = self._job_start_cache.get(key)
            if cached is None:
                return ""
            cached.updated_at = time.time()
            return cached.response_status

    def cache_job_start_response(self, item: QueueItem, message: Dict[str, Any], status: str) -> None:
        """缓存同一 (task_id, job_id) 的 job 结果(最新 job_status)，供重复请求复用回放。"""
        key = self._job_start_cache_key(item.job_id, item.task_id)
        if key is None:
            return

        with self._job_start_cache_lock:
            cached = self._job_start_cache.get(key)
            if cached is None:
                cached = JobStartCacheEntry(request_data={})
                self._job_start_cache[key] = cached

            cached.response_message = copy.deepcopy(message)
            cached.response_status = status
            cached.updated_at = time.time()
            self._prune_job_start_cache_locked()

    def replay_cached_job_start_response(self, job_id: str, task_id: str) -> bool:
        """回放同一 (task_id, job_id) 已缓存的最终结果。

        仅当已缓存到 success/failed/canceled 的终态结果时才回放；若原任务仍在执行
        (只缓存了 running 中间态)，返回 False，由调用方决定如何处理。
        """
        key = self._job_start_cache_key(job_id, task_id)
        if key is None:
            return False

        with self._job_start_cache_lock:
            cached = self._job_start_cache.get(key)
            if cached is None or cached.response_message is None:
                return False
            if cached.response_status not in ("success", "failed", "canceled"):
                return False
            message = copy.deepcopy(cached.response_message)
            status = cached.response_status
            cached.updated_at = time.time()

        sent = self.message_processor.send_message(message)
        if sent:
            logger.info(
                f"[WebSocketClient] [缓存复用] 回放缓存结果 job={job_id[:8]} task={task_id[:8]} "
                f"status={status} payload={json.dumps(message, ensure_ascii=False)}"
            )
        return sent

    def start(self) -> None:
        """启动WebSocket客户端"""
        if self.is_disabled:
            logger.warning("[WebSocketClient] WebSocket is disabled, skipping connection.")
            return

        if not self.websocket_url:
            logger.error("[WebSocketClient] WebSocket URL not configured")
            return

        # 启动两个核心线程
        self.message_processor.start()
        self.queue_processor.start()

        logger.trace("[WebSocketClient] All threads started")

    def stop(self) -> None:
        """停止WebSocket客户端"""
        if self.is_disabled:
            return

        logger.info("[WebSocketClient] Stopping connection")

        # 发送 normal_exit 消息
        if self.is_connected():
            try:
                session_id = self.message_processor.session_id
                message = {"action": "normal_exit", "data": {"session_id": session_id}}
                self.message_processor.send_message(message)
                logger.info(f"[WebSocketClient] Sent normal_exit message with session_id: {session_id}")
                # send_handler 每100ms检查一次队列，等300ms足以让消息发出
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"[WebSocketClient] Failed to send normal_exit message: {str(e)}")

        # 停止两个核心线程
        self.message_processor.stop()
        self.queue_processor.stop()

        logger.info("[WebSocketClient] All threads stopped")

    # BaseCommunicationClient接口实现
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.message_processor.is_connected() and not self.is_disabled

    def publish_device_status(self, device_status: dict, device_id: str, property_name: str) -> None:
        """发布设备状态"""
        if self.is_disabled or not self.is_connected():
            return

        message = {
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
        self.message_processor.send_message(message)
        # logger.trace(f"[WebSocketClient] Device status published: {device_id}.{property_name}")

    def publish_job_status(
        self, feedback_data: dict, item: QueueItem, status: str, return_info: Optional[dict] = None
    ) -> None:
        """发布微后端已经放行的 canonical 作业状态。"""
        job_log = format_job_log(item.job_id, item.task_id, item.device_id, item.action_name)

        # 拦截最终结果状态，与原版本逻辑一致
        if status in ["success", "failed", "canceled"]:
            self._job_running_last_sent.pop(item.job_id, None)
            # Queue advancement already happened in JobExecutionBackend.  The
            # legacy client-side manager is retained only for compatibility.
            if _get_job_execution_backend() is None:
                self.queue_processor.handle_job_completed(item.job_id, status)

            cached_status = self.get_cached_job_start_response_status(item.job_id, item.task_id)
            if cached_status in ["success", "failed", "canceled"]:
                # 断线重连时，旧 READY 占位可能在结果已回放后触发 timeout failed。
                # 已有终态时不允许重复终态覆盖缓存或再次发送，success 也不允许被 failed 降级。
                if cached_status == "success" or cached_status == status:
                    logger.warning(
                        f"[WebSocketClient] Skipped duplicate terminal job status for {job_log}: "
                        f"cached={cached_status}, incoming={status}"
                    )
                    return

        # running状态按job_id做debounce，内容变化时仍然上报
        if status == "running":
            now = time.time()
            cached = self._job_running_last_sent.get(item.job_id)
            if cached is not None:
                last_ts, last_data = cached
                if now - last_ts < self._job_running_debounce_interval and last_data == feedback_data:
                    logger.trace(f"[WebSocketClient] Job status debounced (skip): {job_log} - {status}")
                    return
            self._job_running_last_sent[item.job_id] = (now, feedback_data)

        message = {
            "action": "job_status",
            "data": {
                "job_id": item.job_id,
                "task_id": item.task_id,
                "device_id": item.device_id,
                "notebook_id": item.notebook_id,
                "action_name": item.action_name,
                "node_id": item.node_id,
                "retry_count": item.retry_count,
                "status": status,
                "feedback_data": feedback_data,
                "return_info": return_info,
                "timestamp": time.time(),
            },
        }
        self.cache_job_start_response(item, message, status)

        if not self.is_connected():
            logger.debug(f"[WebSocketClient] Not connected, cached job status for job {job_log} - {status}")
            return

        self.message_processor.send_message(message)

        logger.trace(f"[WebSocketClient] Job status published: {job_log} - {status}")

    def publish_job_error_decision_required(self, report: Dict[str, Any]) -> bool:
        """把暂存失败及可选策略发送给调度后端，由后端询问前端。"""

        if self.is_disabled or not self.is_connected():
            logger.warning(
                "[WebSocketClient] Cannot publish pending action failure while "
                f"disconnected: job={str(report.get('job_id', ''))[:8]}"
            )
            return False
        return bool(
            self.message_processor.send_message(
                {"action": "job_error_decision_required", "data": report}
            )
        )

    def report_action_error_decisions(self) -> None:
        """连接或重连后重放仍等待后端 release 的失败。"""

        if self.is_disabled or not self.is_connected():
            return
        microbackend = _get_job_execution_backend()
        if microbackend is None:
            return
        for report in microbackend.get_pending_action_error_decisions():
            self.publish_job_error_decision_required(report)

    def send_ping(self, ping_id: str, timestamp: float) -> None:
        """发送ping消息"""
        if self.is_disabled or not self.is_connected():
            logger.warning("[WebSocketClient] Not connected, cannot send ping")
            return

        message = {"action": "ping", "data": {"ping_id": ping_id, "client_timestamp": timestamp}}
        self.message_processor.send_message(message)
        logger.debug(f"[WebSocketClient] Ping sent: {ping_id}")

    def cancel_goal(self, job_id: str) -> None:
        """取消指定的任务"""
        # 获取job信息用于日志
        job_info = self.device_manager.get_job_info(job_id)
        device_id = job_info.device_id if job_info else ""
        action_name = job_info.action_name if job_info else ""
        job_log = format_job_log(
            job_id,
            job_info.task_id if job_info else "",
            device_id,
            action_name,
        )

        logger.debug(f"[WebSocketClient] Cancel goal request for job: {job_log}")
        success, next_job, lock_became_free = self.device_manager.cancel_job(job_id)
        if success:
            logger.info(f"[WebSocketClient] Job {job_log} cancelled successfully")
            if next_job:
                self.queue_processor.enqueue_pending_start(next_job)
            elif lock_became_free:
                self.publish_action_lock(device_id, action_name, free=True)
        else:
            logger.warning(f"[WebSocketClient] Failed to cancel job {job_log}")

    def publish_action_lock(self, device_id: str, action_name: str, free: bool) -> None:
        """主动上报单个 device+action 的锁(可用性)状态。"""
        self.publish_action_locks([{"device_id": device_id, "action_name": action_name, "free": free}])

    def publish_action_locks(self, locks: List[Dict[str, Any]]) -> None:
        """批量主动上报 device+action 的锁(可用性)状态。

        report_action_lock 不带 job_id/task_id，仅表达每个 device+action 当前是否空闲。
        单次锁翻转 locks 长度为 1，host_ready/重连时为全量快照。
        """
        if self.is_disabled or not locks:
            return
        # 未连接时不发送：重连后由 publish_host_ready 的全量快照重新对齐真实锁状态，
        # 避免把断链期间产生的中间态当作稳定状态推给服务端。
        if not self.is_connected():
            logger.debug(f"[WebSocketClient] Not connected, skip report_action_lock for {len(locks)} action(s)")
            return

        message = {
            "action": "report_action_lock",
            "data": {
                "locks": locks,
                "machine_name": BasicConfig.machine_name,
                "timestamp": time.time(),
            },
        }
        self.message_processor.send_message(message)
        logger.info(f"[WebSocketClient] report_action_lock sent for {len(locks)} action(s)")

    def report_all_action_locks(self) -> None:
        """重新上报全量锁快照：遍历所有 device+action，按 DeviceActionManager 的忙闲状态上报 free/busy。

        用于 host_ready/重连后的锁对齐，以及响应服务端主动下发的 query_action_lock。
        """
        if self.is_disabled or not self.is_connected():
            logger.debug("[WebSocketClient] Not connected, skip report_all_action_locks")
            return

        host_node = get_execution_adapter(0)
        if host_node is None:
            logger.debug("[WebSocketClient] Host node 尚未就绪，跳过全量锁上报")
            return

        microbackend = _get_job_execution_backend()
        device_manager = (
            microbackend.device_manager
            if microbackend is not None
            else self.device_manager
        )

        locks: List[Dict[str, Any]] = []
        for device_id in host_node.devices_names.keys():
            action_names = set()
            # 从全量动作映射(_action_value_mappings)取动作名，而非仅 _action_clients：
            # 它是设备“可调用动作”的权威清单，需全量上报，包含
            #   - 建独立 ROS ActionServer 的动作；
            #   - UniLabJsonCommand 动作(不建 ActionServer、经 _execute_driver_command 调用)；
            #   - @action(auto_prefix=True) 注册成的 "auto-" 动作(如 workbench 的 prepare_materials 等)。
            # 仅跳过 _execute_driver_command[_async]：它是上述动作的通用调用通道本身，并非具体业务动作。
            for action_name in host_node._action_value_mappings.get(device_id, {}).keys():
                if action_name.startswith("_execute_driver_command"):
                    continue
                action_names.add(action_name)
            for action_name in action_names:
                device_action_key = f"/devices/{device_id}/{action_name}"
                free = not device_manager.is_action_busy(device_action_key)
                locks.append({"device_id": device_id, "action_name": action_name, "free": free})
        self.publish_action_locks(locks)

    def publish_host_ready(self) -> None:
        """发布host_node ready信号，包含设备和动作信息"""
        if self.is_disabled or not self.is_connected():
            logger.debug("[WebSocketClient] Not connected, cannot publish host ready signal")
            return

        # 仅在 HostNode 初始化完成（设备已就绪）后才向服务端注册。
        # get_instance(0) 在未就绪时立即返回 None；此时必须延后发送，
        # 否则会发出 devices=[] 的空 host_ready，令服务端误判节点已就绪而过早调度，
        # 进而触发 READY 超时与启动期频繁断链重连。
        host_node = get_execution_adapter(0)
        if host_node is None:
            logger.info("[WebSocketClient] Host node 尚未就绪，延后发送 host_ready（待初始化完成后再注册）")
            return

        # 收集设备信息
        devices = []
        machine_name = BasicConfig.machine_name

        try:
            # host_node_ready 不再上报 actions：设备可调用动作以 report_action_lock 为准
            # (来源 _action_value_mappings，覆盖 auto-/UniLabJsonCommand 等全量动作，且借 FIFO
            # 在本消息之前先行发送)。host_ready 仅声明设备在线与归属，避免与锁口径不一致。
            for device_id, namespace in host_node.devices_names.items():
                device_key = (
                    f"{namespace}/{device_id}" if namespace.startswith("/") else f"/{namespace}/{device_id}"
                )
                is_online = device_key in host_node._online_devices

                devices.append(
                    {
                        "device_id": device_id,
                        "namespace": namespace,
                        "device_key": device_key,
                        "is_online": is_online,
                        "machine_name": host_node.device_machine_names.get(device_id, machine_name),
                    }
                )

            logger.info(f"[WebSocketClient] Collected {len(devices)} devices for host_ready")
        except Exception as e:
            logger.warning(f"[WebSocketClient] Error collecting device info: {e}")

        message = {
            "action": "host_node_ready",
            "data": {
                "status": "ready",
                "timestamp": time.time(),
                "machine_name": machine_name,
                "devices": devices,
            },
        }
        # 先上报全量锁快照，再发 host_ready：借助发送队列 FIFO 顺序，
        # 服务端会先收到 report_action_lock、再收到 host_node_ready。
        # 启动时全部 free，重连时按 DeviceActionManager 反映正在运行/排队的 busy，实现锁状态对齐。
        self.report_action_error_decisions()
        self.report_all_action_locks()
        self.message_processor.send_message(message)
        logger.info(f"[WebSocketClient] Host node ready signal published with {len(devices)} devices")
