"""Backend 下发 Job 的进程内执行登记。

本模块不是调度队列。资源等待和先后选择只允许发生在
``server.backend.scheduler``；这里仅登记已经获准执行的动作，并在收到冲突命令时
返回拒绝，避免 Edge 再形成第二套待调度队列。
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


class JobStatus(str, Enum):
    STARTED = "started"
    ENDED = "ended"


#: Job 生命周期 owner（origin）。执行面只按 origin 把生命周期回调路由给声明了
#: ``job_origins`` 的 bridge；未声明的 bridge 是观察者，收到全部 job。
JOB_ORIGIN_LOCAL_SCHEDULER = "local_scheduler"
JOB_ORIGIN_BACKEND_CONTROL = "backend_control"


@dataclass
class QueueItem:
    """HostLink/ROS2 Adapter 使用的执行引用；名称保留线侧兼容语义。"""

    task_type: str
    device_id: str
    action_name: str
    task_id: str
    job_id: str
    notebook_id: str
    device_action_key: str
    node_id: str = ""
    node_run_uuid: str = ""
    origin: str = ""
    next_run_time: float = 0
    retry_count: int = 0
    trace_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobInfo:
    """一个已被微后端接受并准备立即执行的设备动作。"""

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
    node_run_uuid: str = ""
    origin: str = ""
    retry_count: int = 0
    action_type: str = ""
    action_args: dict[str, Any] = field(default_factory=dict)
    sample_material: dict[str, Any] = field(default_factory=dict)
    server_info: Optional[dict[str, Any]] = None
    trace_context: Any = None
    # 调度权威随 execute_job 下发的超时（秒）；None 时执行面按本地注册表副本解析
    timeout_seconds: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None
    execution_payload: dict[str, Any] = field(default_factory=dict)

    def update_timestamp(self) -> None:
        self.last_update_time = time.time()


class DeviceActionManager:
    """执行期动作登记表，不保存任何等待 Job。"""

    def __init__(self) -> None:
        self.active_jobs: dict[str, JobInfo] = {}
        self.all_jobs: dict[str, JobInfo] = {}
        self.lock = threading.RLock()

    def accept_job(self, job_info: JobInfo) -> str:
        """原子接纳 Job，返回 ``accepted/duplicate/conflict``。"""

        with self.lock:
            existing = self.all_jobs.get(job_info.job_id)
            if existing is not None:
                if existing.task_id != job_info.task_id:
                    logger.warning(
                        "[DeviceActionManager] duplicate job %s has another task",
                        job_info.job_id[:8],
                    )
                    return "conflict"
                existing.update_timestamp()
                return "duplicate"

            if not job_info.always_free and job_info.device_action_key in self.active_jobs:
                return "conflict"

            job_info.status = JobStatus.STARTED
            job_info.update_timestamp()
            self.all_jobs[job_info.job_id] = job_info
            if not job_info.always_free:
                self.active_jobs[job_info.device_action_key] = job_info
            return "accepted"

    def end_job(self, job_id: str) -> Optional[JobInfo]:
        """结束并移除 Job；不会提升另一个本地等待者。"""

        with self.lock:
            job = self.all_jobs.pop(job_id, None)
            if job is None:
                return None
            if self.active_jobs.get(job.device_action_key) is job:
                self.active_jobs.pop(job.device_action_key, None)
            job.status = JobStatus.ENDED
            job.update_timestamp()
            return job

    def get_active_jobs(self) -> list[JobInfo]:
        with self.lock:
            return [
                job
                for job in self.all_jobs.values()
                if job.status is JobStatus.STARTED
            ]

    def get_job_info(self, job_id: str) -> Optional[JobInfo]:
        with self.lock:
            return self.all_jobs.get(job_id)

    def is_action_busy(self, device_action_key: str) -> bool:
        with self.lock:
            return device_action_key in self.active_jobs

    def cancel_job(self, job_id: str) -> bool:
        return self.end_job(job_id) is not None

    def cancel_jobs_by_task_id(self, task_id: str) -> list[JobInfo]:
        with self.lock:
            job_ids = [
                job.job_id for job in self.all_jobs.values() if job.task_id == task_id
            ]
        canceled: list[JobInfo] = []
        for job_id in job_ids:
            job = self.end_job(job_id)
            if job is not None:
                canceled.append(job)
        return canceled

    def busy_keys(self) -> set[str]:
        with self.lock:
            return set(self.active_jobs)


__all__ = [
    "DeviceActionManager",
    "JOB_ORIGIN_BACKEND_CONTROL",
    "JOB_ORIGIN_LOCAL_SCHEDULER",
    "JobInfo",
    "JobStatus",
    "QueueItem",
    "format_job_log",
]
