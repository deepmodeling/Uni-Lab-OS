"""本地执行器与 WebSocket 共用的队列模型，不依赖 ROS 或网络连接。"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from unilabos.utils.log import get_comm_logger

logger = get_comm_logger()


def format_job_log(
    job_id: str, task_id: str = "", device_id: str = "", action_name: str = ""
) -> str:
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
    next_run_time: float = 0  # 下次执行时间戳
    retry_count: int = 0  # 重试次数
    trace_context: dict[str, str] | None = None
    execution_source: str = "workflow"
    start_guard: Callable[[], str] | None = None


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
    # 执行载荷：排队的 job 在出队时由客户端自行启动，需保存原始 job_start 参数
    action_type: str = ""
    action_args: dict[str, Any] = field(default_factory=dict)
    sample_material: dict[str, Any] = field(default_factory=dict)
    server_info: dict[str, Any] | None = None
    trace_context: Any = None
    execution_source: str = "workflow"
    command_uuid: str = ""

    def update_timestamp(self):
        """更新最后更新时间"""
        self.last_update_time = time.time()


class DeviceActionManager:
    """设备动作管理器 - 管理每个device_action_key的任务队列"""

    def __init__(self):
        self.device_queues: dict[
            str, list[JobInfo]
        ] = {}  # device_action_key -> job queue
        self.active_jobs: dict[str, JobInfo] = {}  # device_action_key -> active job
        self.all_jobs: dict[str, JobInfo] = {}  # job_id -> job_info
        self.lock = threading.RLock()

    def enqueue_job(self, job_info: JobInfo) -> tuple[bool, bool]:
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
                job_log = format_job_log(
                    job_info.job_id,
                    job_info.task_id,
                    job_info.device_id,
                    job_info.action_name,
                )
                logger.trace(
                    f"[DeviceActionManager] Job {job_log} always_free, start immediately"
                )
                return True, False

            # 设备上已有占用(正在执行)或已有排队任务 -> 入队
            if device_key in self.active_jobs or self.device_queues.get(device_key):
                if device_key not in self.device_queues:
                    self.device_queues[device_key] = []
                job_info.status = JobStatus.QUEUE
                self.device_queues[device_key].append(job_info)
                job_log = format_job_log(
                    job_info.job_id,
                    job_info.task_id,
                    job_info.device_id,
                    job_info.action_name,
                )
                logger.info(
                    f"[DeviceActionManager] Job {job_log} queued for {device_key}"
                )
                return False, False

            # 设备空闲 -> 立即执行并占用设备锁(free->busy 翻转)
            job_info.status = JobStatus.STARTED
            job_info.update_timestamp()
            self.active_jobs[device_key] = job_info
            job_log = format_job_log(
                job_info.job_id,
                job_info.task_id,
                job_info.device_id,
                job_info.action_name,
            )
            logger.trace(
                f"[DeviceActionManager] Job {job_log} start immediately for {device_key}"
            )
            return True, True

    def end_job(self, job_id: str) -> tuple[JobInfo | None, bool]:
        """
        结束任务。

        返回 (next_job, lock_became_free):
          next_job: 下一个应启动的任务(调用方负责 send_goal)，无则 None
          lock_became_free: 该 device+action 是否发生 busy->free 翻转(需上报 free 锁)
        """
        with self.lock:
            if job_id not in self.all_jobs:
                logger.warning(
                    f"[DeviceActionManager] Job {job_id[:4]} not found for end"
                )
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
            was_active = (
                device_key in self.active_jobs
                and self.active_jobs[device_key].job_id == job_id
            )
            if was_active:
                del self.active_jobs[device_key]
            else:
                job_log = format_job_log(
                    job_info.job_id,
                    job_info.task_id,
                    job_info.device_id,
                    job_info.action_name,
                )
                logger.warning(
                    f"[DeviceActionManager] Job {job_log} was not active for {device_key}"
                )
            job_info.status = JobStatus.ENDED
            job_info.update_timestamp()
            del self.all_jobs[job_id]

            # 检查队列中是否有等待的任务 -> 直接置 STARTED 并占用，锁保持 busy
            if self.device_queues.get(device_key):
                next_job = self.device_queues[device_key].pop(0)  # FIFO
                next_job.status = JobStatus.STARTED
                next_job.update_timestamp()
                self.active_jobs[device_key] = next_job
                next_job_log = format_job_log(
                    next_job.job_id,
                    next_job.task_id,
                    next_job.device_id,
                    next_job.action_name,
                )
                logger.trace(
                    f"[DeviceActionManager] Next job {next_job_log} starts for {device_key}"
                )
                return next_job, False

            # 队列已空：若刚释放了活跃任务，则 busy->free 翻转
            return None, was_active

    def get_active_jobs(self) -> list[JobInfo]:
        """获取所有正在执行的任务(含active_jobs和always_free的STARTED job)"""
        with self.lock:
            jobs = list(self.active_jobs.values())
            # 补充 always_free 的 STARTED job(它们不在 active_jobs 中)
            for job in self.all_jobs.values():
                if (
                    job.always_free
                    and job.status == JobStatus.STARTED
                    and job not in jobs
                ):
                    jobs.append(job)
            return jobs

    def get_queued_jobs(self) -> list[JobInfo]:
        """获取所有排队中的任务"""
        with self.lock:
            queued = []
            for queue in self.device_queues.values():
                queued.extend(queue)
            return queued

    def get_job_info(self, job_id: str) -> JobInfo | None:
        """获取任务信息"""
        with self.lock:
            return self.all_jobs.get(job_id)

    def is_action_busy(self, device_action_key: str) -> bool:
        """该 device+action 是否被占用(有正在执行或排队的非 always_free 任务)。"""
        with self.lock:
            if device_action_key in self.active_jobs:
                return True
            return bool(self.device_queues.get(device_action_key))

    def cancel_job(self, job_id: str) -> tuple[bool, JobInfo | None, bool]:
        """
        取消单个任务。

        返回 (success, next_job, lock_became_free):
          success: 是否成功取消
          next_job: 取消活跃任务后被提升应启动的下一个任务(调用方负责 send_goal)，无则 None
          lock_became_free: 该 device+action 是否发生 busy->free 翻转(需上报 free 锁)
        """
        with self.lock:
            if job_id not in self.all_jobs:
                logger.warning(
                    f"[DeviceActionManager] Job {job_id[:4]} not found for cancel"
                )
                return False, None, False

            job_info = self.all_jobs[job_id]
            device_key = job_info.device_action_key

            # always_free的job直接清理，不影响锁
            if job_info.always_free:
                job_info.status = JobStatus.ENDED
                del self.all_jobs[job_id]
                job_log = format_job_log(
                    job_info.job_id,
                    job_info.task_id,
                    job_info.device_id,
                    job_info.action_name,
                )
                logger.trace(
                    f"[DeviceActionManager] Always-free job {job_log} cancelled"
                )
                return True, None, False

            # 如果是正在执行的任务
            if (
                device_key in self.active_jobs
                and self.active_jobs[device_key].job_id == job_id
            ):
                del self.active_jobs[device_key]
                job_info.status = JobStatus.ENDED
                del self.all_jobs[job_id]
                job_log = format_job_log(
                    job_info.job_id,
                    job_info.task_id,
                    job_info.device_id,
                    job_info.action_name,
                )
                logger.trace(
                    f"[DeviceActionManager] Active job {job_log} cancelled for {device_key}"
                )

                # 队列中有等待任务 -> 提升并占用，锁保持 busy
                if self.device_queues.get(device_key):
                    next_job = self.device_queues[device_key].pop(0)
                    next_job.status = JobStatus.STARTED
                    next_job.update_timestamp()
                    self.active_jobs[device_key] = next_job
                    next_job_log = format_job_log(
                        next_job.job_id,
                        next_job.task_id,
                        next_job.device_id,
                        next_job.action_name,
                    )
                    logger.trace(
                        f"[DeviceActionManager] Next job {next_job_log} starts after cancel"
                    )
                    return True, next_job, False
                # 队列已空 -> busy->free 翻转
                return True, None, True

            # 如果是排队中的任务(取消不影响锁)
            elif device_key in self.device_queues:
                original_length = len(self.device_queues[device_key])
                self.device_queues[device_key] = [
                    j for j in self.device_queues[device_key] if j.job_id != job_id
                ]
                if len(self.device_queues[device_key]) < original_length:
                    job_info.status = JobStatus.ENDED
                    del self.all_jobs[job_id]
                    job_log = format_job_log(
                        job_info.job_id,
                        job_info.task_id,
                        job_info.device_id,
                        job_info.action_name,
                    )
                    logger.trace(
                        f"[DeviceActionManager] Queued job {job_log} cancelled for {device_key}"
                    )
                    return True, None, False

            job_log = format_job_log(
                job_info.job_id,
                job_info.task_id,
                job_info.device_id,
                job_info.action_name,
            )
            logger.warning(
                f"[DeviceActionManager] Job {job_log} not found in active or queued jobs"
            )
            return False, None, False

    def cancel_jobs_by_task_id(
        self, task_id: str
    ) -> tuple[list[str], list[JobInfo], list[tuple[str, str]]]:
        """
        按task_id取消所有相关任务。

        返回 (cancelled_job_ids, next_jobs_to_start, freed_locks):
          cancelled_job_ids: 被取消的 job_id 列表
          next_jobs_to_start: 因取消而被提升应启动的任务列表(调用方负责 send_goal)
          freed_locks: 发生 busy->free 翻转的 (device_id, action_name) 列表(需上报 free 锁)
        """
        cancelled_job_ids: list[str] = []
        next_jobs_to_start: list[JobInfo] = []
        freed_locks: list[tuple[str, str]] = []

        # 首先找到所有属于该task_id的job
        with self.lock:
            jobs_to_cancel = [
                job_info
                for job_info in self.all_jobs.values()
                if job_info.task_id == task_id
            ]

        if not jobs_to_cancel:
            logger.warning(
                f"[DeviceActionManager] No jobs found for task_id: {task_id}"
            )
            return cancelled_job_ids, next_jobs_to_start, freed_locks

        logger.info(
            f"[DeviceActionManager] Found {len(jobs_to_cancel)} jobs to cancel for task_id: {task_id}"
        )

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
            f"[DeviceActionManager] Successfully cancelled {len(cancelled_job_ids)} "
            f"jobs for task_id: {task_id}"
        )

        return cancelled_job_ids, next_jobs_to_start, freed_locks
