"""按需容量诊断；固定大小连接计数，不参与执行或精确 ACK 决策。"""

from __future__ import annotations

import os
import threading
import time
from typing import Any


def workload_snapshot(database: Any) -> dict[str, Any]:
    """同一条聚合查询读取 Task/Job；不解析完整执行图，不修改历史事实。"""
    if not all(database.table_exists(table) for table in ("workflow_task", "workflow_node_job")):
        return {"available": False, "reason": "WORKFLOW_SCHEMA_UNAVAILABLE"}
    required = {"workflow_task": {"backend_task_uuid", "invocation_key"},
                "workflow_node_job": {"current_attempt", "control_data", "attempt"}}
    for table, fields in required.items():
        columns = {row["name"] for row in database.execute(f"PRAGMA table_info({table})")}
        if not fields <= columns:
            return {"available": False, "reason": "WORKFLOW_SCHEMA_UNAVAILABLE"}
    row = database.execute("""WITH center_tasks AS (
        SELECT uuid,backend_task_uuid,invocation_key FROM workflow_task
        WHERE backend_task_uuid IS NOT NULL AND invocation_key IS NOT NULL
    ), center_jobs AS (
        SELECT j.workflow_task_uuid,j.attempt,j.deleted_at,j.current_attempt,
               COALESCE(json_extract(j.control_data,'$.runtime_node_id'),j.workflow_node_uuid) AS runtime_node_id
        FROM workflow_node_job j JOIN center_tasks t ON t.uuid=j.workflow_task_uuid
    ) SELECT
        (SELECT COUNT(*) FROM workflow_task) AS tasks_total,
        (SELECT COUNT(*) FROM workflow_node_job) AS jobs_total,
        (SELECT COUNT(*) FROM center_tasks) AS center_tasks_total,
        (SELECT COUNT(*) FROM center_jobs) AS center_jobs_total,
        (SELECT COUNT(*) FROM (SELECT backend_task_uuid,invocation_key FROM center_tasks
            GROUP BY backend_task_uuid,invocation_key HAVING COUNT(*)>1) d) AS duplicate_task_groups,
        (SELECT COUNT(*) FROM (SELECT workflow_task_uuid,runtime_node_id,attempt FROM center_jobs
            WHERE deleted_at IS NULL GROUP BY workflow_task_uuid,runtime_node_id,attempt HAVING COUNT(*)>1) d)
            AS duplicate_job_attempt_groups,
        (SELECT COUNT(*) FROM (SELECT workflow_task_uuid,runtime_node_id FROM center_jobs
            WHERE deleted_at IS NULL AND current_attempt=1 GROUP BY workflow_task_uuid,runtime_node_id HAVING COUNT(*)>1) d)
            AS duplicate_current_job_groups""").fetchone()
    return {"available": True, "observed_at_ms": time.time_ns() // 1_000_000,
            "scope": "original_station_database", **dict(row)}


class ConnectionStatistics:
    """一个接收器实例的固定计数；首次成功之前是初连，之后的尝试才是重连。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._counts = {name: 0 for name in (
            "connect_attempts", "connect_successes", "connect_failures", "connect_cancellations",
            "reconnect_attempts", "reconnect_successes", "reconnect_failures", "reconnect_cancellations",
            "connections_closed",
        )}
        self._connected = False
        self._started_attempt: float | None = None
        self._reconnect = False
        self._established = False
        self._last_attempt_at_ms: int | None = None
        self._last_established_at_ms: int | None = None
        self._last_connect_ms: float | None = None

    def begin_attempt(self) -> None:
        with self._lock:
            if self._started_attempt is not None:
                raise RuntimeError("同一 WSS 接收器不能并行建连")
            self._reconnect = self._counts["connect_successes"] > 0
            self._counts["connect_attempts"] += 1
            if self._reconnect:
                self._counts["reconnect_attempts"] += 1
            self._started_attempt = time.monotonic()
            self._last_attempt_at_ms = time.time_ns() // 1_000_000
            self._established = False

    def established(self) -> None:
        with self._lock:
            if self._started_attempt is None or self._established:
                return
            self._established = self._connected = True
            self._counts["connect_successes"] += 1
            if self._reconnect:
                self._counts["reconnect_successes"] += 1
            self._last_established_at_ms = time.time_ns() // 1_000_000
            self._last_connect_ms = (time.monotonic() - self._started_attempt) * 1000

    def finish_attempt(self, *, cancelled: bool) -> bool:
        with self._lock:
            if self._started_attempt is None:
                return False
            if self._established:
                self._counts["connections_closed"] += 1
            else:
                suffix = "cancellations" if cancelled else "failures"
                self._counts["connect_" + suffix] += 1
                if self._reconnect:
                    self._counts["reconnect_" + suffix] += 1
            self._started_attempt = None
            self._connected = False
            return self._established

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            completed = self._counts["reconnect_successes"] + self._counts["reconnect_failures"]
            return {"available": True, "scope": "receiver_lifetime", "process_id": os.getpid(),
                    "observed_at_ms": time.time_ns() // 1_000_000,
                    "uptime_seconds": time.monotonic() - self._started, **self._counts,
                    "connected": self._connected,
                    "connect_in_flight": self._started_attempt is not None and not self._established,
                    "last_attempt_at_ms": self._last_attempt_at_ms,
                    "last_established_at_ms": self._last_established_at_ms,
                    "last_connect_ms": self._last_connect_ms,
                    "reconnect_completed_success_ratio": self._counts["reconnect_successes"] / completed if completed else None}
