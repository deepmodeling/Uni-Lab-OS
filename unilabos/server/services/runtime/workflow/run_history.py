"""V1 ``/api/v1/history/*``：工作流任务与作业的运行历史（读 runtime.db 的投影视图）。

V1 把运行历史放在独立的 history SQLite（``workflow_runs`` / ``job_runs`` 两张表）；统一分支
沿用 dev 的 runtime.db，这两张同名视图由 ``tables/runtime/workflow.py`` 从 ``workflow_task`` /
``workflow_node_job`` 投影而来，字段与 V1 的历史表一致（时间为 epoch 秒浮点数，状态 ``success``）。
本模块只读，不写库。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

_RUN_FIELDS = (
    "workflow_id",
    "task_id",
    "lab_id",
    "priority",
    "node_count",
    "state",
    "submitted_at",
    "started_at",
    "finished_at",
    "duration_s",
)


def _run_row(row: Any, *, with_spec: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {field: row[field] for field in _RUN_FIELDS}
    if with_spec:
        try:
            result["spec"] = json.loads(row["spec_json"])
        except (TypeError, ValueError):
            result["spec"] = {}
    return result


def _job_row(row: Any) -> Dict[str, Any]:
    entry = dict(row)
    ret_json = entry.pop("ret_json", "")
    if ret_json:
        try:
            entry["ret_value"] = json.loads(ret_json)
        except ValueError:
            entry["ret_value"] = ret_json
    return entry


class RunHistory:
    """V1 ``WorkflowHistoryStore`` 的只读等价物：同一连接、同一把锁。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    def list_runs(
        self,
        *,
        state: str = "",
        since: float = 0.0,
        limit: int = 100,
        with_spec: bool = False,
    ) -> List[Dict[str, Any]]:
        """运行列表（新→旧）；``state`` 过滤，``since`` 是 submitted_at（epoch 秒）下界。"""

        sql = "SELECT * FROM workflow_runs WHERE submitted_at >= ?"
        params: List[Any] = [since]
        if state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY submitted_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._store.write_lock:
            rows = self._store.connection.execute(sql, params).fetchall()
        return [_run_row(row, with_spec=with_spec) for row in rows]

    def get_run(self, workflow_id: str, *, with_spec: bool = True) -> Optional[Dict[str, Any]]:
        with self._store.write_lock:
            row = self._store.connection.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        return None if row is None else _run_row(row, with_spec=with_spec)

    def list_jobs(
        self,
        *,
        workflow_id: str = "",
        device_id: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """作业历史（新→旧）；按任务（V1 的 workflow_id）或设备过滤。"""

        sql = "SELECT * FROM job_runs WHERE 1=1"
        params: List[Any] = []
        if workflow_id:
            sql += " AND workflow_id = ?"
            params.append(workflow_id)
        if device_id:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with self._store.write_lock:
            rows = self._store.connection.execute(sql, params).fetchall()
        return [_job_row(row) for row in rows]

    def stats(self) -> Dict[str, Any]:
        with self._store.write_lock:
            conn = self._store.connection
            by_state = {
                row["state"]: row["n"]
                for row in conn.execute(
                    "SELECT state, COUNT(*) AS n FROM workflow_runs GROUP BY state"
                ).fetchall()
            }
            jobs = conn.execute("SELECT COUNT(*) AS n FROM job_runs").fetchone()["n"]
        return {
            "runs_by_state": by_state,
            "total_runs": sum(by_state.values()),
            "total_jobs": jobs,
        }


__all__ = ["RunHistory"]
