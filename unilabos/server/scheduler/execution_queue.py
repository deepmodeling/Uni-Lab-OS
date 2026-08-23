"""设备动作执行队列的进程内状态模型。

这些对象属于 Host 执行微后端，不属于 Backend 线协议。后端调度发生冲突时
默认由 :class:`JobExecutionBackend` 拒绝；本模块只负责已接受 Job 的设备动作
占用、取消和生命周期查询。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from unilabos.utils.log import get_comm_logger

logger = get_comm_logger()


def format_job_log(
    job_id: str,
    task_id: str = "",
    device_id: str = "",
    action_name: str = "",
) -> str:
    """生成紧凑且稳定的 Job 日志标识。"""

    job_part = f"{job_id[:4]}-{task_id[:4]}" if task_id else job_id[:4]
    device_part = f"{device_id}/{action_name}" if device_id and action_name else ""
    return f"{job_part} {device_part}".strip()


class JobStatus(Enum):
    QUEUE = "queue"
    STARTED = "started"
    ENDED = "ended"


@dataclass
class QueueItem:
    """HostLink/ROS2 执行适配器之间传递的本地 Job 引用。"""

    task_type: str
    device_id: str
    action_name: str
    task_id: str
    job_id: str
    notebook_id: str
    device_action_key: str
    node_id: str = ""
    next_run_time: float = 0
    retry_count: int = 0


@dataclass
class JobInfo:
    """一个已被微后端接受的设备动作 Job。"""

    job_id: str
    task_id: str
    device_id: str
    notebook_id: str
    action_name: str
    device_action_key: str
    status: JobStatus
    start_time: float
    last_update_time: float = field(default_factory=time.time)
    always_free: bool = False
    node_id: str = ""
    retry_count: int = 0
    action_type: str = ""
    action_args: dict[str, Any] = field(default_factory=dict)
    sample_material: dict[str, Any] = field(default_factory=dict)
    server_info: Optional[dict[str, Any]] = None

    def update_timestamp(self) -> None:
        self.last_update_time = time.time()


class DeviceActionManager:
    """维护 device/action 的当前占用和已接受 Job。"""

    def __init__(self) -> None:
        self.device_queues: dict[str, list[JobInfo]] = {}
        self.active_jobs: dict[str, JobInfo] = {}
        self.all_jobs: dict[str, JobInfo] = {}
        self.lock = threading.RLock()

    def enqueue_job(self, job_info: JobInfo) -> tuple[bool, bool]:
        """接收 Job，返回 ``(立即启动, 动作由空闲变为占用)``。"""

        with self.lock:
            device_key = job_info.device_action_key
            existing = self.all_jobs.get(job_info.job_id)
            if existing is not None:
                if existing.task_id != job_info.task_id:
                    logger.warning(
                        "[DeviceActionManager] duplicate job %s has another task",
                        job_info.job_id[:8],
                    )
                    return False, False
                if job_info.notebook_id and not existing.notebook_id:
                    existing.notebook_id = job_info.notebook_id
                existing.update_timestamp()
                return False, False

            self.all_jobs[job_info.job_id] = job_info
            if job_info.always_free:
                job_info.status = JobStatus.STARTED
                job_info.update_timestamp()
                return True, False

            if device_key in self.active_jobs or self.device_queues.get(device_key):
                job_info.status = JobStatus.QUEUE
                self.device_queues.setdefault(device_key, []).append(job_info)
                return False, False

            job_info.status = JobStatus.STARTED
            job_info.update_timestamp()
            self.active_jobs[device_key] = job_info
            return True, True

    def end_job(self, job_id: str) -> tuple[Optional[JobInfo], bool]:
        """结束 Job，返回 ``(下一个可启动 Job, 动作是否变为空闲)``。"""

        with self.lock:
            job = self.all_jobs.get(job_id)
            if job is None:
                logger.warning("[DeviceActionManager] job %s not found for end", job_id[:8])
                return None, False

            key = job.device_action_key
            if job.always_free:
                job.status = JobStatus.ENDED
                job.update_timestamp()
                self.all_jobs.pop(job_id, None)
                return None, False

            was_active = self.active_jobs.get(key) is job
            if was_active:
                self.active_jobs.pop(key, None)
            else:
                queue = self.device_queues.get(key, [])
                self.device_queues[key] = [item for item in queue if item.job_id != job_id]

            job.status = JobStatus.ENDED
            job.update_timestamp()
            self.all_jobs.pop(job_id, None)

            if was_active and self.device_queues.get(key):
                next_job = self.device_queues[key].pop(0)
                next_job.status = JobStatus.STARTED
                next_job.update_timestamp()
                self.active_jobs[key] = next_job
                return next_job, False
            return None, was_active

    def get_active_jobs(self) -> list[JobInfo]:
        with self.lock:
            jobs = list(self.active_jobs.values())
            jobs.extend(
                job
                for job in self.all_jobs.values()
                if job.always_free
                and job.status is JobStatus.STARTED
                and job not in jobs
            )
            return jobs

    def get_queued_jobs(self) -> list[JobInfo]:
        with self.lock:
            return [job for queue in self.device_queues.values() for job in queue]

    def get_job_info(self, job_id: str) -> Optional[JobInfo]:
        with self.lock:
            return self.all_jobs.get(job_id)

    def is_action_busy(self, device_action_key: str) -> bool:
        with self.lock:
            return device_action_key in self.active_jobs or bool(
                self.device_queues.get(device_action_key)
            )

    def cancel_job(self, job_id: str) -> tuple[bool, Optional[JobInfo], bool]:
        """取消 Job，返回 ``(成功, 下一个可启动 Job, 动作是否变为空闲)``。"""

        with self.lock:
            job = self.all_jobs.get(job_id)
            if job is None:
                return False, None, False
            key = job.device_action_key

            if job.always_free:
                job.status = JobStatus.ENDED
                self.all_jobs.pop(job_id, None)
                return True, None, False

            if self.active_jobs.get(key) is job:
                self.active_jobs.pop(key, None)
                job.status = JobStatus.ENDED
                self.all_jobs.pop(job_id, None)
                if self.device_queues.get(key):
                    next_job = self.device_queues[key].pop(0)
                    next_job.status = JobStatus.STARTED
                    next_job.update_timestamp()
                    self.active_jobs[key] = next_job
                    return True, next_job, False
                return True, None, True

            queue = self.device_queues.get(key, [])
            remaining = [item for item in queue if item.job_id != job_id]
            if len(remaining) != len(queue):
                self.device_queues[key] = remaining
                job.status = JobStatus.ENDED
                self.all_jobs.pop(job_id, None)
                return True, None, False
            return False, None, False

    def cancel_jobs_by_task_id(
        self,
        task_id: str,
    ) -> tuple[list[str], list[JobInfo], list[tuple[str, str]]]:
        """取消 Task 的全部 Job，并返回被提升 Job 和释放的动作锁。"""

        with self.lock:
            candidates = [
                job for job in self.all_jobs.values() if job.task_id == task_id
            ]
        canceled: list[str] = []
        next_jobs: list[JobInfo] = []
        freed: list[tuple[str, str]] = []
        for job in candidates:
            success, next_job, became_free = self.cancel_job(job.job_id)
            if success:
                canceled.append(job.job_id)
            if next_job is not None and next_job.task_id != task_id:
                next_jobs.append(next_job)
            if became_free:
                freed.append((job.device_id, job.action_name))
        return canceled, next_jobs, freed


__all__ = [
    "DeviceActionManager",
    "JobInfo",
    "JobStatus",
    "QueueItem",
    "format_job_log",
]
