"""V1 时间线 / 时间计划 / 监控面板的读取实现（``/timeline``、``/scheduler/time-plan``、``/monitor/*``）。

V1 的候选时间计划来自商用 CP-SAT 引擎，开源树里已经剥离；dev 的调度器是按依赖就绪顺序派发
的 DAG 调度器，不做时间求解。这里的口径：

- ``/scheduler/time-plan``：没有配置时间计划引擎，按 V1 同情形返回 ``{"enabled": false,
  "algorithm": "stable-ready-order"}``，不伪造候选计划。
- ``/timeline``：执行中作业（持久状态 dispatched / running / cancel_requested）与窗口内已完结
  作业（runtime.db ``job_runs`` 视图），预估耗时取同一 ``设备:动作`` 的历史实测均值
  （``estimate_source=observed``，无历史时为 ``unknown``，``estimated_s`` 为 None），
  ``estimator`` 给出这份实测统计。
- ``/monitor/*``：V1 进程内四通道总线（material / device / action / scheduler）在 dev 上以持久
  前端通知（``frontend_event``）为唯一来源投影：作业变化 → action，任务 / 工站变化 → scheduler，
  资源占用变化 → device；物料变化在 materials.db 的独立事件流里，这里不复制（material 通道为空）。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from unilabos.server.services.runtime.workflow.store import _load

CHANNELS = ("material", "device", "action", "scheduler")
NO_ENGINE_TIME_PLAN = {"enabled": False, "algorithm": "stable-ready-order"}
_IN_FLIGHT = ("dispatched", "running", "cancel_requested")


def _epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ExecutionTimeline:
    def __init__(self, store: Any, *, clock=time.time) -> None:
        self._store = store
        self._clock = clock

    # 实测耗时统计 ---------------------------------------------------------

    def _observed(self, conn) -> Dict[str, Dict[str, Any]]:
        stats: Dict[str, Dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT device_action_key, COUNT(*) AS n, AVG(actual_s) AS mean_s, MIN(actual_s) AS min_s, "
            "MAX(actual_s) AS max_s FROM job_runs WHERE state='success' AND actual_s > 0 AND device_action_key <> '' "
            "GROUP BY device_action_key"
        ).fetchall():
            stats[row["device_action_key"]] = {"count": row["n"], "mean_s": round(row["mean_s"], 3),
                                               "min_s": round(row["min_s"], 3), "max_s": round(row["max_s"], 3)}
        return stats

    @staticmethod
    def _estimate(stats: Dict[str, Dict[str, Any]], key: str) -> tuple[Optional[float], str]:
        observed = stats.get(key)
        if observed is None:
            return None, "unknown"
        return observed["mean_s"], "observed"

    def _running(self, conn, stats, now: float) -> List[Dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT job.uuid, job.workflow_task_uuid, job.workflow_node_uuid, job.param, job.started_at,
                   job.create_time, job.edge_agent_uuid, task.workflow_snapshot
            FROM workflow_node_job AS job JOIN workflow_task AS task ON task.uuid = job.workflow_task_uuid
            WHERE job.deleted_at IS NULL AND job.status IN ({",".join("?" for _ in _IN_FLIGHT)})
            ORDER BY job.create_time
            """,
            _IN_FLIGHT,
        ).fetchall()
        running = []
        for row in rows:
            snapshot = _load(row["workflow_snapshot"], {})
            node = next((item for item in snapshot.get("nodes") or []
                         if isinstance(item, dict) and item.get("uuid") == row["workflow_node_uuid"]), {})
            meta = node.get("meta_data") if isinstance(node.get("meta_data"), dict) else {}
            device_id = str(meta.get("target_device_id") or row["edge_agent_uuid"] or "")
            action = str(node.get("action_name") or _load(row["param"], {}).get("action") or "")
            key = f"{device_id}:{action}" if device_id else ""
            started = _epoch(row["started_at"]) or _epoch(row["create_time"]) or now
            estimated, source = self._estimate(stats, key)
            running.append({
                "job_id": row["uuid"], "workflow_id": row["workflow_task_uuid"], "node_id": row["workflow_node_uuid"],
                "device_id": device_id, "action_name": action, "device_action_key": key, "started_at": started,
                "elapsed_s": round(max(0.0, now - started), 3), "estimated_s": estimated, "estimate_source": source,
            })
        return running

    def timeline(self, window_s: float = 3600.0) -> Dict[str, Any]:
        now = self._clock()
        cutoff = now - max(float(window_s), 0.0)
        with self._store.write_lock:
            conn = self._store.connection
            stats = self._observed(conn)
            running = self._running(conn, stats, now)
            completed = []
            for row in conn.execute(
                "SELECT * FROM job_runs WHERE ended_at >= ? AND state IN ('success', 'failed', 'canceled', "
                "'timeout', 'skipped') ORDER BY ended_at",
                (cutoff,),
            ).fetchall():
                entry = {key: row[key] for key in row.keys() if key not in {"ret_json", "id"}}
                estimated, source = self._estimate(stats, row["device_action_key"])
                entry.update({"estimated_s": estimated, "estimate_source": source})
                completed.append(entry)
        return {
            "now": now, "window_s": window_s, "running": running, "completed": completed,
            "time_schedule": self.time_plan(),
            "estimator": {"mode": "observed", "default_s": None, "stats": stats},
        }

    @staticmethod
    def time_plan() -> Dict[str, Any]:
        return dict(NO_ENGINE_TIME_PLAN)

    # 监控面板 -------------------------------------------------------------

    @staticmethod
    def monitor_event(row) -> Optional[Dict[str, Any]]:
        """持久前端通知 → V1 监控事件 ``{seq, ts, channel, type, data}``；不属于四通道的返回 None。"""

        name = row["type"]
        data = _load(row["payload"], {})
        if name == "workflow.node_job.changed":
            channel, kind = "action", f"job_{data.get('status') or 'changed'}"
        elif name == "workflow.task.changed":
            channel, kind = "scheduler", "workflow_state"
        elif name == "station.error_handling.changed":
            channel, kind = "scheduler", "station_error_handling"
        elif name == "workflow.resource.changed":
            channel, kind = "device", str(data.get("operation") or "resource_changed")
        else:
            return None
        return {"seq": row["sequence"], "ts": _epoch(row["create_time"]) or 0.0, "channel": channel,
                "type": kind, "data": data}

    def monitor_events(self, *, after: int = 0, channels: Optional[Iterable[str]] = None,
                       limit: int = 200) -> List[Dict[str, Any]]:
        wanted = set(channels or CHANNELS)
        with self._store.write_lock:
            rows = self._store.connection.execute(
                "SELECT sequence, type, payload, create_time FROM frontend_event WHERE sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (after, max(1, limit) * 4),
            ).fetchall()
        events = [event for row in rows if (event := self.monitor_event(row)) and event["channel"] in wanted]
        return events[:limit]

    def recent(self, channel: str, count: int = 40) -> List[Dict[str, Any]]:
        with self._store.write_lock:
            rows = self._store.connection.execute(
                "SELECT sequence, type, payload, create_time FROM frontend_event ORDER BY sequence DESC LIMIT ?",
                (count * 10,),
            ).fetchall()
        events = [event for row in rows if (event := self.monitor_event(row)) and event["channel"] == channel]
        return list(reversed(events[:count]))

    def backlog(self, channels: Sequence[str], count: int) -> List[Dict[str, Any]]:
        if count <= 0:
            return []
        with self._store.write_lock:
            rows = self._store.connection.execute(
                "SELECT sequence, type, payload, create_time FROM frontend_event ORDER BY sequence DESC LIMIT ?",
                (count * 10,),
            ).fetchall()
        events = [event for row in rows if (event := self.monitor_event(row)) and event["channel"] in channels]
        return list(reversed(events[:count]))

    def latest_sequence(self) -> int:
        with self._store.write_lock:
            return int(self._store.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM frontend_event").fetchone()[0])

    def device_status(self) -> List[Dict[str, Any]]:
        now = self._clock()
        timeline = self.timeline(window_s=86400.0)
        devices: Dict[str, Dict[str, Any]] = {}
        for entry in timeline["completed"]:
            key = entry.get("device_id") or entry.get("device_action_key")
            if not key:
                continue
            current = devices.get(key)
            if current is None or (entry.get("ended_at") or 0) > current.get("last_seen", 0):
                devices[key] = {"device_id": key, "status": "idle", "last_action": entry.get("action_name"),
                                "last_state": entry.get("state"), "last_seen": entry.get("ended_at")}
        for job in timeline["running"]:
            key = job["device_id"] or job["device_action_key"]
            if not key:
                continue
            devices[key] = {"device_id": key, "status": "busy", "action_name": job["action_name"],
                            "job_id": job["job_id"], "workflow_id": job["workflow_id"],
                            "started_at": job["started_at"], "elapsed_s": job["elapsed_s"],
                            "estimated_s": job["estimated_s"], "estimate_source": job["estimate_source"],
                            "last_seen": now}
        return sorted(devices.values(), key=lambda item: item["device_id"])

    def snapshot(self, *, reschedule_count: int = 0) -> Dict[str, Any]:
        with self._store.write_lock:
            conn = self._store.connection
            states = {
                ("success" if row["status"] == "succeeded" else row["status"]): row["n"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM workflow_task WHERE deleted_at IS NULL GROUP BY status"
                ).fetchall()
            }
            inflight = int(conn.execute(
                f"SELECT COUNT(*) FROM workflow_node_job WHERE deleted_at IS NULL AND status IN "
                f"({','.join('?' for _ in _IN_FLIGHT)})", _IN_FLIGHT,
            ).fetchone()[0])
        return {
            "now": self._clock(),
            "devices": self.device_status(),
            "scheduler": {"workflow_states": states, "inflight": inflight, "reschedule_count": reschedule_count},
            "recent": {channel: self.recent(channel) for channel in CHANNELS},
        }


__all__ = ["CHANNELS", "ExecutionTimeline", "NO_ENGINE_TIME_PLAN"]
