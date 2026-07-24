"""Task Action 运行日志的进程内缓存（不持久化）。"""

from __future__ import annotations

import threading
import time
from typing import Any

MAX_ENTRIES_PER_EXECUTION = 200


class TaskActionLogStore:
    """按 workflow 存储 Task Action 日志，供 workflow_ui 查询。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq_by_workflow: dict[str, int] = {}
        self._entries_by_workflow: dict[str, list[dict[str, Any]]] = {}

    def append(
        self,
        *,
        workflow_path: str,
        instance_id: str,
        node_id: str,
        execution_id: str,
        sample_id: str,
        level: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> int:
        if not workflow_path:
            return self.latest_seq(workflow_path)
        record = {
            "timestamp": int(time.time() * 1000),
            "instance_id": instance_id,
            "node_id": node_id,
            "execution_id": execution_id,
            "sample_id": sample_id,
            "level": level or "info",
            "message": message or "",
            "detail": dict(detail or {}),
        }
        with self._lock:
            next_seq = self._seq_by_workflow.get(workflow_path, 0) + 1
            self._seq_by_workflow[workflow_path] = next_seq
            record["seq"] = next_seq
            entries = self._entries_by_workflow.setdefault(workflow_path, [])
            entries.append(record)
            self._trim_execution_entries(entries, execution_id)
            return next_seq

    def latest_seq(self, workflow_path: str) -> int:
        with self._lock:
            return int(self._seq_by_workflow.get(workflow_path, 0))

    def list_since(
        self,
        workflow_path: str,
        *,
        after_seq: int = 0,
        instance_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        with self._lock:
            latest = int(self._seq_by_workflow.get(workflow_path, 0))
            entries = list(self._entries_by_workflow.get(workflow_path, []))
        filtered = [
            item
            for item in entries
            if int(item.get("seq", 0)) > after_seq
            and (not instance_id or item.get("instance_id") == instance_id)
        ]
        if limit > 0:
            filtered = filtered[-limit:]
        return {"latest_seq": latest, "entries": filtered}

    @staticmethod
    def _trim_execution_entries(
        entries: list[dict[str, Any]],
        execution_id: str,
    ) -> None:
        same = [item for item in entries if item.get("execution_id") == execution_id]
        overflow = len(same) - MAX_ENTRIES_PER_EXECUTION
        if overflow <= 0:
            return
        remove_ids = {id(item) for item in same[:overflow]}
        entries[:] = [item for item in entries if id(item) not in remove_ids]
