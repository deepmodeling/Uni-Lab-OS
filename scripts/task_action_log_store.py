"""Task Action 运行日志的进程内缓存（不持久化）。"""

from __future__ import annotations

import threading
import time
from typing import Any


TASK_EXECUTION_LOG_CATEGORIES = frozenset({"schedule", "action", "opc", "result"})
TASK_EXECUTION_LOG_LEVELS = frozenset(
    {"debug", "info", "warning", "error", "critical"}
)


def _normalize_contract_value(
    value: str, allowed: frozenset[str], default: str
) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


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
        category: str = "action",
        code: str = "",
        phase: str = "",
        template_id: str = "",
        device_id: str = "",
        action_name: str = "",
    ) -> int:
        if not workflow_path:
            return self.latest_seq(workflow_path)
        record = {
            "timestamp": int(time.time() * 1000),
            "instance_id": instance_id,
            "node_id": node_id,
            "execution_id": execution_id,
            "sample_id": sample_id,
            "template_id": template_id,
            "device_id": device_id,
            "action_name": action_name,
            "category": _normalize_contract_value(
                category, TASK_EXECUTION_LOG_CATEGORIES, "action"
            ),
            "level": _normalize_contract_value(
                level, TASK_EXECUTION_LOG_LEVELS, "info"
            ),
            "code": str(code or "").strip(),
            "phase": str(phase or "").strip(),
            "message": message or "",
            "detail": dict(detail or {}),
        }
        with self._lock:
            next_seq = self._seq_by_workflow.get(workflow_path, 0) + 1
            self._seq_by_workflow[workflow_path] = next_seq
            record["seq"] = next_seq
            self._entries_by_workflow.setdefault(workflow_path, []).append(record)
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
        page = filtered[:limit] if limit > 0 else filtered
        next_after_seq = (
            int(page[-1].get("seq", after_seq)) if page else int(after_seq)
        )
        return {
            "latest_seq": latest,
            "next_after_seq": next_after_seq,
            "has_more": len(page) < len(filtered),
            "entries": page,
        }
