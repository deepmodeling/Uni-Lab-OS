"""旧 EdgeScheduler 历史合同与规范只读投影适配器。

三库分立（读写频率与生命周期不同，互不阻塞）：

- ``inventory.db``       物料/仓储（事务重、账本语义）
- ``device_state.db``    设备遥测（高频 upsert）
- ``workflow_history.db``规范 Workflow Authority；旧对象只用于兼容查询

两张表：

- ``workflow_runs``：每次提交一行（workflow_id 主键，同 ID 重提覆盖旧记录），
  存完整 spec_json（提交的整图，可回放/审计）、状态流转与起止时间。
- ``job_runs``：每个 job 完结一行 append-only（含实际/预估时长、预估来源、
  suc_type、截断后的返回值 JSON）。

规范组合根以 ``read_only=True`` 打开本适配器，读取 Workflow schema 创建的只读
View；恢复只认 ``workflow_task/workflow_node_job``。可写模式仅保留给隔离的旧测试或
迁移工具，生产组合根不得调用 ``mark_interrupted`` 或 record 方法。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Edge 旧历史终态集合（与 service._TERMINAL_STATES 口径一致，字符串解耦避免循环依赖）。
# 本地保留 success/failed/canceled/timeout；Backend canonical 成功值是 succeeded，
# 上行 Adapter 必须转换。interrupted 是 Edge 历史库独有，向 Backend 上报时映射为 failed。
_TERMINAL = ("success", "failed", "canceled", "timeout", "interrupted")

# ret_value JSON 截断上限（历史库不是数据湖，只留调试线索）
_RET_JSON_LIMIT = 4096

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_id   TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL DEFAULT '',
    lab_id        TEXT NOT NULL DEFAULT '',
    priority      TEXT NOT NULL DEFAULT '',
    node_count    INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL,
    submitted_at  REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    duration_s    REAL,
    spec_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_wr_submitted ON workflow_runs (submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_wr_state ON workflow_runs (state);

CREATE TABLE IF NOT EXISTS job_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT NOT NULL,
    workflow_id        TEXT NOT NULL,
    node_id            TEXT NOT NULL,
    device_id          TEXT NOT NULL DEFAULT '',
    action_name        TEXT NOT NULL DEFAULT '',
    device_action_key  TEXT NOT NULL DEFAULT '',
    started_at         REAL NOT NULL,
    ended_at           REAL NOT NULL,
    actual_s           REAL NOT NULL DEFAULT 0,
    estimated_s        REAL NOT NULL DEFAULT 0,
    estimate_source    TEXT NOT NULL DEFAULT '',
    state              TEXT NOT NULL,
    suc_type           TEXT NOT NULL DEFAULT 'normal',
    ret_json           TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jr_workflow ON job_runs (workflow_id, id);
CREATE INDEX IF NOT EXISTS idx_jr_device ON job_runs (device_id, id);
"""


def _spec_to_dict(spec: Any) -> Dict[str, Any]:
    """WorkflowSpec → 可回放 dict（与 API WorkflowSubmitIn 同形状）。"""
    return {
        "workflow_id": spec.workflow_id,
        "priority": spec.priority if isinstance(spec.priority, (int, float, str)) else str(spec.priority),
        "lab_id": spec.lab_id,
        "task_id": spec.task_id,
        "nodes": [
            {
                "id": n.id,
                "device_id": n.device_id,
                "action_name": n.action_name,
                "action_type": n.action_type,
                "param": n.param,
                "node_type": n.node_type,
                "disabled": n.disabled,
                "material_requirements": [
                    {
                        "template_id": r.template_id,
                        "lot_id": r.lot_id,
                        "quantity": r.quantity,
                        "unit": r.unit,
                        "instance_uuid": r.instance_uuid,
                        "barcode": r.barcode,
                    }
                    for r in n.material_requirements
                ],
            }
            for n in spec.nodes
        ],
        "edges": [
            {
                "uuid": e.uuid,
                "source_node_id": e.source_node_id,
                "target_node_id": e.target_node_id,
                "source_handle_uuid": e.source_handle_uuid,
                "target_handle_uuid": e.target_handle_uuid,
                "source_handle_key": e.source_handle_key,
                "target_handle_key": e.target_handle_key,
            }
            for e in spec.edges
        ],
        "handles": [
            {
                "uuid": h.uuid,
                "data_source": h.data_source,
                "handle_key": h.handle_key,
                "data_key": h.data_key,
                "node_id": h.node_id,
                "io_type": h.io_type,
            }
            for h in spec.handles
        ],
    }


def _truncate_ret(ret_value: Any) -> str:
    if ret_value is None:
        return ""
    try:
        text = json.dumps(ret_value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(ret_value)
    return text[:_RET_JSON_LIMIT]


class WorkflowHistoryStore:
    """旧历史合同适配器；规范组合根只以 ``read_only=True`` 打开。"""

    def __init__(
        self,
        db_path: str = ":memory:",
        max_runs: int = 2000,
        *,
        read_only: bool = False,
    ):
        self._lock = threading.Lock()
        self.max_runs = max_runs
        self.read_only = read_only
        if read_only:
            if db_path == ":memory:":
                raise ValueError("read-only workflow history requires a database path")
            database_uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(
                database_uri,
                uri=True,
                check_same_thread=False,
            )
        else:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if read_only:
                self._conn.execute("PRAGMA query_only = ON")
                return
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.executescript(_SCHEMA)
            # 旧库原地升级：补 started_at（对齐云端 workflow_task.started_at，
            # 首次进入 running 的时间）
            try:
                self._conn.execute("ALTER TABLE workflow_runs ADD COLUMN started_at REAL")
            except sqlite3.OperationalError:
                pass  # 列已存在
            self._conn.commit()

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise RuntimeError("legacy workflow history projection is read-only")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 写入（EdgeScheduler 生命周期挂钩） ────────────────────

    def mark_interrupted(self) -> int:
        """进程启动恢复：上一世代残留的非终态 run 统一标 interrupted。"""
        self._ensure_writable()
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE workflow_runs SET state = 'interrupted', finished_at = ?, "
                "duration_s = ? - submitted_at "
                f"WHERE state NOT IN ({','.join('?' * len(_TERMINAL))})",
                (now, now, *_TERMINAL),
            )
            self._conn.commit()
            return cursor.rowcount

    def record_submitted(self, spec: Any, state: str) -> None:
        """提交时建档；同 workflow_id 重提（跨进程世代）覆盖旧运行及其 job。"""
        self._ensure_writable()
        spec_json = json.dumps(_spec_to_dict(spec), ensure_ascii=False)
        # started_at 对齐云端 workflow_task：首次进入 running 的时间；
        # 提交即 running 时等于 submitted_at，等料（waiting_for_material）时留空。
        started_at = spec.submitted_at if state == "running" else None
        with self._lock:
            self._conn.execute("DELETE FROM job_runs WHERE workflow_id = ?", (spec.workflow_id,))
            self._conn.execute(
                "INSERT OR REPLACE INTO workflow_runs"
                "(workflow_id, task_id, lab_id, priority, node_count, state, submitted_at, "
                "started_at, finished_at, duration_s, spec_json) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?)",
                (
                    spec.workflow_id,
                    spec.task_id,
                    spec.lab_id,
                    str(spec.priority),
                    len(spec.nodes),
                    state,
                    spec.submitted_at,
                    started_at,
                    spec_json,
                ),
            )
            self._prune_locked()
            self._conn.commit()

    def record_state(self, workflow_id: str, state: str) -> None:
        """状态流转；首次 running 补 started_at，终态时补 finished_at / duration_s。"""
        self._ensure_writable()
        with self._lock:
            if state in _TERMINAL:
                now = time.time()
                self._conn.execute(
                    "UPDATE workflow_runs SET state = ?, finished_at = ?, "
                    "duration_s = ? - submitted_at WHERE workflow_id = ?",
                    (state, now, now, workflow_id),
                )
            elif state == "running":
                self._conn.execute(
                    "UPDATE workflow_runs SET state = ?, "
                    "started_at = COALESCE(started_at, ?) WHERE workflow_id = ?",
                    (state, time.time(), workflow_id),
                )
            else:
                self._conn.execute(
                    "UPDATE workflow_runs SET state = ? WHERE workflow_id = ?",
                    (state, workflow_id),
                )
            self._conn.commit()

    def record_job(self, entry: Dict[str, Any], ret_value: Any = None) -> None:
        """job 完结 append（entry 与调度器泳道时间线同形状）。"""
        self._ensure_writable()
        with self._lock:
            self._conn.execute(
                "INSERT INTO job_runs(job_id, workflow_id, node_id, device_id, action_name, "
                "device_action_key, started_at, ended_at, actual_s, estimated_s, "
                "estimate_source, state, suc_type, ret_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["job_id"],
                    entry["workflow_id"],
                    entry["node_id"],
                    entry.get("device_id", ""),
                    entry.get("action_name", ""),
                    entry.get("device_action_key", ""),
                    entry["started_at"],
                    entry["ended_at"],
                    entry.get("actual_s", 0.0),
                    entry.get("estimated_s", 0.0),
                    entry.get("estimate_source", ""),
                    entry["state"],
                    entry.get("suc_type", "normal"),
                    _truncate_ret(ret_value),
                ),
            )
            self._conn.commit()

    def _prune_locked(self) -> None:
        """总量控制：只保留最近 max_runs 次运行（连带清理其 job 行）。"""
        rows = self._conn.execute(
            "SELECT workflow_id FROM workflow_runs ORDER BY submitted_at DESC "
            "LIMIT -1 OFFSET ?",
            (self.max_runs,),
        ).fetchall()
        if not rows:
            return
        stale = [r["workflow_id"] for r in rows]
        marks = ",".join("?" * len(stale))
        self._conn.execute(f"DELETE FROM job_runs WHERE workflow_id IN ({marks})", stale)
        self._conn.execute(f"DELETE FROM workflow_runs WHERE workflow_id IN ({marks})", stale)

    # ── 查询（REST 面） ───────────────────────────────────────

    @staticmethod
    def _run_row(row: sqlite3.Row, with_spec: bool = False) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "workflow_id": row["workflow_id"],
            "task_id": row["task_id"],
            "lab_id": row["lab_id"],
            "priority": row["priority"],
            "node_count": row["node_count"],
            "state": row["state"],
            "submitted_at": row["submitted_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration_s": row["duration_s"],
        }
        if with_spec:
            try:
                result["spec"] = json.loads(row["spec_json"])
            except ValueError:
                result["spec"] = {}
        return result

    def list_runs(
        self,
        state: str = "",
        since: float = 0.0,
        limit: int = 100,
        with_spec: bool = False,
    ) -> List[Dict[str, Any]]:
        """运行列表（新→旧）；state 可过滤，since 是 submitted_at 下界。"""
        sql = "SELECT * FROM workflow_runs WHERE submitted_at >= ?"
        params: List[Any] = [since]
        if state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY submitted_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._run_row(r, with_spec=with_spec) for r in rows]

    def get_run(self, workflow_id: str, with_spec: bool = False) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            return None
        out = self._run_row(row)
        if with_spec:
            try:
                out["spec"] = json.loads(row["spec_json"])
            except ValueError:
                out["spec"] = {}
        return out

    def list_jobs(
        self, workflow_id: str = "", device_id: str = "", limit: int = 200
    ) -> List[Dict[str, Any]]:
        """job 历史（新→旧）；按 workflow / device 过滤。"""
        sql = "SELECT * FROM job_runs WHERE 1=1"
        params: List[Any] = []
        if workflow_id:
            sql += " AND workflow_id = ?"
            params.append(workflow_id)
        if device_id:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            ret_json = entry.pop("ret_json", "")
            if ret_json:
                try:
                    entry["ret_value"] = json.loads(ret_json)
                except ValueError:
                    entry["ret_value"] = ret_json
            out.append(entry)
        return out

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            by_state = {
                r["state"]: r["n"]
                for r in self._conn.execute(
                    "SELECT state, COUNT(*) AS n FROM workflow_runs GROUP BY state"
                ).fetchall()
            }
            jobs = self._conn.execute("SELECT COUNT(*) AS n FROM job_runs").fetchone()["n"]
        return {"runs_by_state": by_state, "total_runs": sum(by_state.values()), "total_jobs": jobs}


__all__ = ["WorkflowHistoryStore"]
