"""Task 排程的持久化耗时台账与分工站动作台账。"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_RUN_HISTORY_DIR = (
    Path.home() / "Documents" / "UniLab_test" / "run-history"
)
_STATION_PATTERN = re.compile(r"(?:^|[^a-z0-9])s(\d{1,3})(?:[^a-z0-9]|$)", re.I)
_METHOD_STATION_PATTERN = re.compile(r"(?:^|_)s(\d{2,3})(?:_|$)", re.I)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_time(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().isoformat(
        timespec="milliseconds"
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value, key=str)
    if is_dataclass(value):
        return asdict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return repr(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _normalize_station(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return f"S{value:02d}"
    text = str(value).strip()
    if not text:
        return None
    exact = re.fullmatch(r"s\s*(\d{1,3})", text, re.I)
    if exact:
        number = exact.group(1)
        return f"S{int(number):02d}" if len(number) <= 2 else f"S{number}"
    match = _STATION_PATTERN.search(text)
    if match:
        number = match.group(1)
        return f"S{int(number):02d}" if len(number) <= 2 else f"S{number}"
    return None


def _iter_mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_mappings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_mappings(nested)


def infer_station(
    *,
    device_id: str = "",
    action_name: str = "",
    node_id: str = "",
    result: Any = None,
) -> str:
    """从动作结果和命名中推导物理工站；无法识别时保留明确占位。"""
    for mapping in _iter_mappings(result):
        for key in ("station", "station_id", "target_station", "source_station"):
            if key in mapping:
                station = _normalize_station(mapping.get(key))
                if station:
                    return station

    for text in (action_name, device_id, node_id):
        match = _METHOD_STATION_PATTERN.search(str(text))
        if match:
            number = match.group(1)
            return f"S{int(number):02d}" if len(number) <= 2 else f"S{number}"
        station = _normalize_station(text)
        if station:
            return station

    if "robot" in device_id.lower() or "robot" in action_name.lower():
        return "ROBOT"
    return "UNKNOWN"


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _measurement(
    *,
    name: str,
    target: Any = None,
    actual: Any = None,
    unit: str = "",
    source: str = "",
) -> dict[str, Any] | None:
    target_number = _as_number(target)
    actual_number = _as_number(actual)
    if target_number is None and actual_number is None:
        return None
    item: dict[str, Any] = {
        "name": name,
        "target": target_number,
        "actual": actual_number,
        "unit": unit,
        "source": source,
    }
    if target_number is not None and actual_number is not None:
        error = round(actual_number - target_number, 12)
        item["error"] = error
        item["absolute_error"] = abs(error)
        item["relative_error_percent"] = (
            round(error / target_number * 100, 8) if target_number else None
        )
    else:
        item["error"] = None
        item["absolute_error"] = None
        item["relative_error_percent"] = None
    return item


def extract_measurements(params: Any, result: Any) -> list[dict[str, Any]]:
    """提取已知目标/实测对；没有实测值时绝不伪造。"""
    measurements: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append(item: dict[str, Any] | None) -> None:
        if item is None:
            return
        signature = _json_dumps(item)
        if signature not in seen:
            seen.add(signature)
            measurements.append(item)

    all_mappings = list(_iter_mappings(result)) + list(_iter_mappings(params))
    for index, mapping in enumerate(all_mappings):
        if "target_weight" in mapping:
            actual = next(
                (
                    mapping.get(key)
                    for key in ("balance_reading", "actual_weight", "final_weight")
                    if mapping.get(key) is not None
                ),
                None,
            )
            append(
                _measurement(
                    name="固体重量",
                    target=mapping.get("target_weight"),
                    actual=actual,
                    unit=str(mapping.get("weight_unit") or "g"),
                    source=f"mapping[{index}]",
                )
            )

        for target_key, target_value in mapping.items():
            if not str(target_key).startswith("target_") or target_key == "target_weight":
                continue
            suffix = str(target_key).removeprefix("target_")
            actual_key = next(
                (
                    candidate
                    for candidate in (
                        f"actual_{suffix}",
                        f"measured_{suffix}",
                        f"final_{suffix}",
                    )
                    if candidate in mapping
                ),
                None,
            )
            append(
                _measurement(
                    name=suffix,
                    target=target_value,
                    actual=mapping.get(actual_key) if actual_key else None,
                    unit=str(mapping.get(f"{suffix}_unit") or mapping.get("unit") or ""),
                    source=f"mapping[{index}]",
                )
            )

        if "average_density" in mapping:
            append(
                _measurement(
                    name="平均密度",
                    actual=mapping.get("average_density"),
                    unit=str(mapping.get("density_unit") or ""),
                    source=f"mapping[{index}]",
                )
            )
    return [
        item
        for item in measurements
        if item.get("actual") is not None
        or not any(
            candidate.get("name") == item.get("name")
            and candidate.get("target") == item.get("target")
            and candidate.get("actual") is not None
            for candidate in measurements
        )
    ]


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> int:
    normalized = sorted((start, end) for start, end in intervals if end >= start)
    if not normalized:
        return 0
    total = 0
    current_start, current_end = normalized[0]
    for start, end in normalized[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end
    return total + current_end - current_start


class RunHistoryStore:
    """线程安全、延迟连接的 SQLite 连续台账。"""

    def __init__(
        self,
        history_dir: Path | str | None = None,
        *,
        session_id: str | None = None,
        clock: Any = _now_ms,
        dispatch_warning_ms: int = 60_000,
        dispatch_critical_ms: int = 300_000,
    ) -> None:
        configured_dir = os.getenv("UNILABOS_RUN_HISTORY_DIR")
        self.history_dir = Path(history_dir or configured_dir or DEFAULT_RUN_HISTORY_DIR)
        self.database_path = self.history_dir / "history.db"
        self.exports_dir = self.history_dir / "exports"
        self.artifacts_dir = self.history_dir / "artifacts"
        self.backups_dir = self.history_dir / "backups"
        self.session_id = session_id or uuid.uuid4().hex
        self._clock = clock
        self._session_started_at = int(clock())
        self._dispatch_warning_ms = max(1, int(dispatch_warning_ms))
        self._dispatch_critical_ms = max(
            self._dispatch_warning_ms,
            int(dispatch_critical_ms),
        )
        self._dispatch_waits: dict[str, dict[str, Any]] = {}
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def initialize(self) -> dict[str, str]:
        """立即初始化持久化目录和数据库，并返回实际使用的路径。"""
        self._connect()
        return {
            "history_dir": str(self.history_dir.resolve()),
            "database_path": str(self.database_path.resolve()),
            "exports_dir": str(self.exports_dir.resolve()),
            "artifacts_dir": str(self.artifacts_dir.resolve()),
            "backups_dir": str(self.backups_dir.resolve()),
        }

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is not None:
                return self._connection
            self.history_dir.mkdir(parents=True, exist_ok=True)
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.database_path,
                timeout=10,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=10000")
            self._create_schema(connection)
            now = int(self._clock())
            connection.execute(
                """
                UPDATE run_sessions
                SET status = 'interrupted', finished_at = COALESCE(finished_at, ?)
                WHERE status = 'running' AND run_id != ?
                """,
                (now, self.session_id),
            )
            connection.execute(
                """
                UPDATE action_records
                SET status = 'interrupted',
                    finished_at = COALESCE(finished_at, ?),
                    duration_ms = COALESCE(duration_ms, MAX(0, ? - started_at)),
                    updated_at = ?
                WHERE status = 'running' AND run_id != ?
                """,
                (now, now, now, self.session_id),
            )
            connection.execute(
                """
                UPDATE scheduler_incidents
                SET status = 'interrupted', last_seen_at = ?,
                    duration_ms = MAX(0, ? - first_seen_at)
                WHERE status = 'open' AND run_id != ?
                """,
                (now, now, self.session_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO run_sessions (
                    run_id, started_at, status, process_id, created_at, updated_at
                ) VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (
                    self.session_id,
                    self._session_started_at,
                    os.getpid(),
                    self._session_started_at,
                    now,
                ),
            )
            connection.commit()
            self._connection = connection
            return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_sessions (
                run_id TEXT PRIMARY KEY,
                workflow_path TEXT NOT NULL DEFAULT '',
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                status TEXT NOT NULL,
                process_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_records (
                run_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                workflow_path TEXT NOT NULL DEFAULT '',
                instance_id TEXT NOT NULL DEFAULT '',
                sample_id TEXT NOT NULL DEFAULT '',
                node_id TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL DEFAULT '',
                action_name TEXT NOT NULL DEFAULT '',
                station TEXT NOT NULL DEFAULT 'UNKNOWN',
                status TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                duration_ms INTEGER,
                params_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                measurements_json TEXT NOT NULL DEFAULT '[]',
                error_json TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (run_id, execution_id)
            );

            CREATE TABLE IF NOT EXISTS action_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                execution_id TEXT NOT NULL DEFAULT '',
                workflow_path TEXT NOT NULL DEFAULT '',
                instance_id TEXT NOT NULL DEFAULT '',
                sample_id TEXT NOT NULL DEFAULT '',
                node_id TEXT NOT NULL DEFAULT '',
                station TEXT NOT NULL DEFAULT 'UNKNOWN',
                timestamp INTEGER NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                event_type TEXT NOT NULL DEFAULT 'log',
                message TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS scheduler_incidents (
                incident_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                category TEXT NOT NULL,
                code TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                workflow_path TEXT NOT NULL DEFAULT '',
                instance_id TEXT NOT NULL DEFAULT '',
                sample_id TEXT NOT NULL DEFAULT '',
                node_id TEXT NOT NULL DEFAULT '',
                execution_id TEXT NOT NULL DEFAULT '',
                device_id TEXT NOT NULL DEFAULT '',
                action_name TEXT NOT NULL DEFAULT '',
                station TEXT NOT NULL DEFAULT 'UNKNOWN',
                phase TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                recovered_at INTEGER,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS scheduler_timings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                workflow_path TEXT NOT NULL DEFAULT '',
                cycle_id TEXT NOT NULL DEFAULT '',
                generation_id TEXT NOT NULL DEFAULT '',
                step TEXT NOT NULL,
                phase TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                duration_ms INTEGER,
                reason TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                received_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_action_records_time
                ON action_records (started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_action_records_run_station
                ON action_records (run_id, station, started_at);
            CREATE INDEX IF NOT EXISTS idx_action_records_run_sample
                ON action_records (run_id, sample_id, started_at);
            CREATE INDEX IF NOT EXISTS idx_action_events_execution
                ON action_events (run_id, execution_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_scheduler_incidents_time
                ON scheduler_incidents (first_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_scheduler_incidents_run_status
                ON scheduler_incidents (run_id, status, workflow_path);
            CREATE INDEX IF NOT EXISTS idx_scheduler_timings_run_time
                ON scheduler_timings (run_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_scheduler_timings_cycle
                ON scheduler_timings (run_id, workflow_path, cycle_id, timestamp);
            """
        )

    def append_scheduler_timings(
        self,
        *,
        workflow_path: str,
        entries: Iterable[dict[str, Any]],
    ) -> int:
        """批量持久化浏览器排程循环耗时；无效条目直接拒绝。"""
        normalized: list[tuple[Any, ...]] = []
        received_at = int(self._clock())
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError("scheduler timing 条目必须是对象")
            step = str(entry.get("step") or "").strip()
            phase = str(entry.get("phase") or "").strip()
            if not step or phase not in {"start", "finish", "error", "skipped"}:
                raise ValueError("scheduler timing 缺少有效 step/phase")
            timestamp = int(entry.get("timestamp_ms") or received_at)
            duration = entry.get("duration_ms")
            duration_ms = None if duration is None else max(0, int(duration))
            normalized.append(
                (
                    self.session_id,
                    workflow_path,
                    str(entry.get("cycle_id") or ""),
                    str(entry.get("generation_id") or ""),
                    step,
                    phase,
                    timestamp,
                    duration_ms,
                    str(entry.get("reason") or ""),
                    _json_dumps(entry.get("detail") or {}),
                    received_at,
                )
            )
        if not normalized:
            return 0
        with self._lock:
            connection = self._connect()
            connection.executemany(
                """
                INSERT INTO scheduler_timings (
                    run_id, workflow_path, cycle_id, generation_id, step, phase,
                    timestamp, duration_ms, reason, detail_json, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized,
            )
            connection.execute(
                "UPDATE run_sessions SET updated_at = ? WHERE run_id = ?",
                (received_at, self.session_id),
            )
            connection.commit()
        return len(normalized)

    def record_action_start(
        self,
        *,
        execution_id: str,
        workflow_path: str,
        instance_id: str,
        sample_id: str,
        node_id: str,
        device_id: str,
        action_name: str,
        params: Any,
        started_at: int | None = None,
    ) -> None:
        timestamp = int(started_at if started_at is not None else self._clock())
        station = infer_station(
            device_id=device_id,
            action_name=action_name,
            node_id=node_id,
        )
        with self._lock:
            connection = self._connect()
            connection.execute(
                """
                INSERT INTO action_records (
                    run_id, execution_id, workflow_path, instance_id, sample_id,
                    node_id, device_id, action_name, station, status, started_at,
                    params_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                ON CONFLICT(run_id, execution_id) DO UPDATE SET
                    workflow_path = excluded.workflow_path,
                    instance_id = excluded.instance_id,
                    sample_id = excluded.sample_id,
                    node_id = excluded.node_id,
                    device_id = excluded.device_id,
                    action_name = excluded.action_name,
                    station = excluded.station,
                    status = 'running',
                    started_at = excluded.started_at,
                    finished_at = NULL,
                    duration_ms = NULL,
                    params_json = excluded.params_json,
                    result_json = NULL,
                    measurements_json = '[]',
                    error_json = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    self.session_id,
                    execution_id,
                    workflow_path,
                    instance_id,
                    sample_id,
                    node_id,
                    device_id,
                    action_name,
                    station,
                    timestamp,
                    _json_dumps(params),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE run_sessions
                SET workflow_path = CASE
                        WHEN workflow_path = '' THEN ? ELSE workflow_path END,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (workflow_path, timestamp, self.session_id),
            )
            connection.commit()

    def record_action_finish(
        self,
        *,
        execution_id: str,
        status: str,
        result: Any = None,
        error: Any = None,
        finished_at: int | None = None,
    ) -> None:
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError(f"不支持的动作终态: {status}")
        timestamp = int(finished_at if finished_at is not None else self._clock())
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                """
                SELECT device_id, action_name, node_id, params_json, started_at
                FROM action_records
                WHERE run_id = ? AND execution_id = ?
                """,
                (self.session_id, execution_id),
            ).fetchone()
            if row is None:
                return
            params = _json_loads(row["params_json"], {})
            station = infer_station(
                device_id=row["device_id"],
                action_name=row["action_name"],
                node_id=row["node_id"],
                result=result,
            )
            connection.execute(
                """
                UPDATE action_records
                SET station = ?, status = ?, finished_at = ?,
                    duration_ms = MAX(0, ? - started_at),
                    result_json = ?, measurements_json = ?, error_json = ?,
                    updated_at = ?
                WHERE run_id = ? AND execution_id = ?
                """,
                (
                    station,
                    status,
                    timestamp,
                    timestamp,
                    _json_dumps(result) if result is not None else None,
                    _json_dumps(extract_measurements(params, result)),
                    _json_dumps(error) if error is not None else None,
                    timestamp,
                    self.session_id,
                    execution_id,
                ),
            )
            connection.execute(
                """
                UPDATE run_sessions SET updated_at = ? WHERE run_id = ?
                """,
                (timestamp, self.session_id),
            )
            connection.commit()

    def append_event(
        self,
        *,
        execution_id: str,
        workflow_path: str,
        instance_id: str,
        sample_id: str,
        node_id: str,
        level: str,
        message: str,
        detail: Any = None,
        event_type: str = "log",
        timestamp: int | None = None,
    ) -> None:
        event_time = int(timestamp if timestamp is not None else self._clock())
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                """
                SELECT station FROM action_records
                WHERE run_id = ? AND execution_id = ?
                """,
                (self.session_id, execution_id),
            ).fetchone()
            station = str(row["station"]) if row is not None else "UNKNOWN"
            connection.execute(
                """
                INSERT INTO action_events (
                    run_id, execution_id, workflow_path, instance_id, sample_id,
                    node_id, station, timestamp, level, event_type, message,
                    detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.session_id,
                    execution_id,
                    workflow_path,
                    instance_id,
                    sample_id,
                    node_id,
                    station,
                    event_time,
                    level,
                    event_type,
                    message,
                    _json_dumps(detail or {}),
                ),
            )
            connection.commit()

    def record_incident(
        self,
        *,
        category: str,
        code: str,
        severity: str,
        message: str,
        status: str = "occurred",
        workflow_path: str = "",
        instance_id: str = "",
        sample_id: str = "",
        node_id: str = "",
        execution_id: str = "",
        device_id: str = "",
        action_name: str = "",
        phase: str = "",
        detail: Any = None,
        fingerprint: str | None = None,
        first_seen_at: int | None = None,
        timestamp: int | None = None,
    ) -> str:
        """写入报警、错误或阻塞事件；同一活动异常只更新一行。"""
        if status not in {"open", "occurred"}:
            raise ValueError(f"不支持的异常状态: {status}")
        observed_at = int(timestamp if timestamp is not None else self._clock())
        first_seen = int(first_seen_at if first_seen_at is not None else observed_at)
        with self._lock:
            connection = self._connect()
            if execution_id:
                action = connection.execute(
                    """
                    SELECT workflow_path, instance_id, sample_id, node_id,
                           device_id, action_name, station
                    FROM action_records
                    WHERE run_id = ? AND execution_id = ?
                    """,
                    (self.session_id, execution_id),
                ).fetchone()
                if action is not None:
                    workflow_path = workflow_path or str(action["workflow_path"])
                    instance_id = instance_id or str(action["instance_id"])
                    sample_id = sample_id or str(action["sample_id"])
                    node_id = node_id or str(action["node_id"])
                    device_id = device_id or str(action["device_id"])
                    action_name = action_name or str(action["action_name"])
                    station = str(action["station"])
                else:
                    station = infer_station(
                        device_id=device_id,
                        action_name=action_name,
                        node_id=node_id,
                    )
            else:
                station = infer_station(
                    device_id=device_id,
                    action_name=action_name,
                    node_id=node_id,
                )
            fingerprint = fingerprint or _json_dumps(
                [
                    category,
                    code,
                    workflow_path,
                    instance_id,
                    sample_id,
                    node_id,
                    execution_id,
                    message,
                ]
            )
            query = (
                """
                SELECT incident_id, severity, occurrence_count
                FROM scheduler_incidents
                WHERE run_id = ? AND fingerprint = ? AND status = 'open'
                ORDER BY last_seen_at DESC LIMIT 1
                """
                if status == "open"
                else """
                SELECT incident_id, severity, occurrence_count
                FROM scheduler_incidents
                WHERE run_id = ? AND fingerprint = ? AND status = 'occurred'
                      AND last_seen_at >= ?
                ORDER BY last_seen_at DESC LIMIT 1
                """
            )
            query_params: tuple[Any, ...] = (
                (self.session_id, fingerprint)
                if status == "open"
                else (self.session_id, fingerprint, observed_at - 60_000)
            )
            existing = connection.execute(query, query_params).fetchone()
            severity_order = {"info": 0, "warning": 1, "error": 2, "critical": 3}
            if existing is not None:
                current_severity = str(existing["severity"])
                resolved_severity = (
                    severity
                    if severity_order.get(severity, 0)
                    >= severity_order.get(current_severity, 0)
                    else current_severity
                )
                connection.execute(
                    """
                    UPDATE scheduler_incidents
                    SET severity = ?, last_seen_at = ?,
                        duration_ms = MAX(0, ? - first_seen_at),
                        occurrence_count = occurrence_count + 1,
                        message = ?, detail_json = ?
                    WHERE incident_id = ?
                    """,
                    (
                        resolved_severity,
                        observed_at,
                        observed_at,
                        message,
                        _json_dumps(detail or {}),
                        existing["incident_id"],
                    ),
                )
                connection.commit()
                return str(existing["incident_id"])

            incident_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO scheduler_incidents (
                    incident_id, run_id, fingerprint, category, code, severity,
                    status, workflow_path, instance_id, sample_id, node_id,
                    execution_id, device_id, action_name, station, phase,
                    message, first_seen_at, last_seen_at, duration_ms,
                    occurrence_count, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    incident_id,
                    self.session_id,
                    fingerprint,
                    category,
                    code,
                    severity,
                    status,
                    workflow_path,
                    instance_id,
                    sample_id,
                    node_id,
                    execution_id,
                    device_id,
                    action_name,
                    station,
                    phase,
                    message,
                    first_seen,
                    observed_at,
                    max(0, observed_at - first_seen),
                    _json_dumps(detail or {}),
                ),
            )
            connection.commit()
            return incident_id

    def _recover_incident(self, fingerprint: str, recovered_at: int) -> None:
        connection = self._connect()
        connection.execute(
            """
            UPDATE scheduler_incidents
            SET status = 'recovered', recovered_at = ?, last_seen_at = ?,
                duration_ms = MAX(0, ? - first_seen_at)
            WHERE run_id = ? AND fingerprint = ? AND status = 'open'
            """,
            (
                recovered_at,
                recovered_at,
                recovered_at,
                self.session_id,
                fingerprint,
            ),
        )
        connection.commit()

    def observe_scheduler_cycle(
        self,
        *,
        workflow_path: str,
        diagnostics: Any,
        cycle_stats: dict[str, Any] | None = None,
    ) -> None:
        """根据现有排程循环诊断，对长期未派发事件做计时、升级和恢复。"""
        now = int(self._clock())
        diagnostic_list = diagnostics if isinstance(diagnostics, list) else []
        active_keys: set[str] = set()
        for item in diagnostic_list:
            if not isinstance(item, dict):
                continue
            fingerprint = _json_dumps(
                [
                    "dispatch",
                    workflow_path,
                    item.get("instance_id") or "",
                    item.get("node_id") or "",
                    item.get("code") or "dispatch_wait",
                ]
            )
            active_keys.add(fingerprint)
            state = self._dispatch_waits.setdefault(
                fingerprint,
                {"first_seen_at": now, "item": dict(item)},
            )
            state["item"] = dict(item)
            elapsed = max(0, now - int(state["first_seen_at"]))
            immediate = bool(item.get("immediate"))
            if not immediate and elapsed < self._dispatch_warning_ms:
                continue
            severity = str(item.get("severity") or "warning")
            if elapsed >= self._dispatch_critical_ms and severity == "warning":
                severity = "critical"
            message = str(item.get("message") or "Task 长时间未派发")
            self.record_incident(
                category=str(item.get("category") or "dispatch_stall"),
                code=str(item.get("code") or "dispatch_wait"),
                severity=severity,
                status="open",
                message=message,
                workflow_path=workflow_path,
                instance_id=str(item.get("instance_id") or ""),
                sample_id=str(item.get("sample_id") or ""),
                node_id=str(item.get("node_id") or ""),
                execution_id=str(item.get("execution_id") or ""),
                device_id=str(item.get("device_id") or ""),
                action_name=str(item.get("action_name") or ""),
                phase=str(item.get("phase") or "等待派发"),
                detail={
                    "diagnostic": item.get("detail") or {},
                    "cycle_stats": cycle_stats or {},
                },
                fingerprint=fingerprint,
                first_seen_at=int(state["first_seen_at"]),
                timestamp=now,
            )

        with self._lock:
            for fingerprint in set(self._dispatch_waits) - active_keys:
                self._recover_incident(fingerprint, now)
                self._dispatch_waits.pop(fingerprint, None)

    def _resolve_run(self, run_id: str | None = None) -> sqlite3.Row | None:
        connection = self._connect()
        if run_id:
            return connection.execute(
                "SELECT * FROM run_sessions WHERE run_id = ?", (run_id,)
            ).fetchone()
        return connection.execute(
            """
            SELECT session.* FROM run_sessions AS session
            WHERE EXISTS (
                SELECT 1 FROM action_records AS action
                WHERE action.run_id = session.run_id
            )
               OR EXISTS (
                SELECT 1 FROM scheduler_incidents AS incident
                WHERE incident.run_id = session.run_id
            )
            ORDER BY session.started_at DESC LIMIT 1
            """
        ).fetchone()

    @staticmethod
    def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "workflow_path": row["workflow_path"],
            "started_at": row["started_at"],
            "started_at_iso": _iso_time(row["started_at"]),
            "finished_at": row["finished_at"],
            "finished_at_iso": _iso_time(row["finished_at"]),
            "status": row["status"],
        }

    def list_runs(
        self,
        *,
        limit: int = 50,
        workflow_path: str | None = None,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            connection = self._connect()
            where = "WHERE session.workflow_path = ?" if workflow_path else ""
            params: list[Any] = [workflow_path] if workflow_path else []
            params.append(safe_limit)
            rows = connection.execute(
                f"""
                SELECT session.*,
                       COUNT(action.execution_id) AS action_count,
                       SUM(CASE WHEN action.status = 'completed' THEN 1 ELSE 0 END)
                           AS completed_count,
                       SUM(CASE WHEN action.status = 'failed' THEN 1 ELSE 0 END)
                           AS failed_count,
                       (SELECT COUNT(*) FROM scheduler_incidents AS incident
                        WHERE incident.run_id = session.run_id) AS incident_count,
                       MIN(action.started_at) AS first_action_at,
                       MAX(COALESCE(action.finished_at, action.started_at)) AS last_action_at
                FROM run_sessions AS session
                LEFT JOIN action_records AS action ON action.run_id = session.run_id
                {where}
                GROUP BY session.run_id
                HAVING COUNT(action.execution_id) > 0 OR incident_count > 0
                ORDER BY session.started_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            runs = []
            for row in rows:
                item = self._session_dict(row)
                item.update(
                    {
                        "action_count": int(row["action_count"] or 0),
                        "completed_count": int(row["completed_count"] or 0),
                        "failed_count": int(row["failed_count"] or 0),
                        "incident_count": int(row["incident_count"] or 0),
                        "first_action_at": row["first_action_at"],
                        "last_action_at": row["last_action_at"],
                    }
                )
                runs.append(item)
            return {
                "database_path": str(self.database_path),
                "current_run_id": self.session_id,
                "runs": runs,
            }

    def _action_rows(self, run_id: str) -> list[sqlite3.Row]:
        return self._connect().execute(
            """
            SELECT * FROM action_records
            WHERE run_id = ? ORDER BY started_at, execution_id
            """,
            (run_id,),
        ).fetchall()

    @staticmethod
    def _duration_group(
        rows: Iterable[sqlite3.Row],
        key: str,
        *,
        now: int,
    ) -> list[dict[str, Any]]:
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            group_key = str(row[key] or "UNKNOWN")
            groups.setdefault(group_key, []).append(row)
        summaries = []
        for group_key, group_rows in groups.items():
            durations = [
                int(row["duration_ms"])
                if row["duration_ms"] is not None
                else max(0, now - int(row["started_at"]))
                for row in group_rows
            ]
            first_started_at = min(int(row["started_at"]) for row in group_rows)
            last_finished_at = max(
                int(row["finished_at"] or now) for row in group_rows
            )
            summaries.append(
                {
                    key: group_key,
                    "action_count": len(group_rows),
                    "completed_count": sum(row["status"] == "completed" for row in group_rows),
                    "failed_count": sum(row["status"] == "failed" for row in group_rows),
                    "running_count": sum(row["status"] == "running" for row in group_rows),
                    "total_action_time_ms": sum(durations),
                    "average_action_time_ms": round(sum(durations) / len(durations)),
                    "max_action_time_ms": max(durations),
                    "elapsed_time_ms": max(0, last_finished_at - first_started_at),
                    "first_started_at": first_started_at,
                    "last_finished_at": last_finished_at,
                }
            )
        return sorted(
            summaries,
            key=lambda item: (-int(item["total_action_time_ms"]), str(item[key])),
        )

    def timing_ledger(self, run_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = self._resolve_run(run_id)
            if session is None:
                return {
                    "run": None,
                    "total_time_ms": 0,
                    "robot": {"busy_time_ms": 0, "idle_time_ms": 0, "utilization_percent": 0.0},
                    "samples": [],
                    "devices": [],
                    "stations": [],
                }
            now = int(self._clock())
            rows = self._action_rows(str(session["run_id"]))
            if rows:
                first_started_at = min(int(row["started_at"]) for row in rows)
                last_finished_at = max(int(row["finished_at"] or now) for row in rows)
                total_time_ms = max(0, last_finished_at - first_started_at)
            else:
                first_started_at = None
                last_finished_at = None
                total_time_ms = 0

            robot_rows = [
                row
                for row in rows
                if "robot" in str(row["device_id"]).lower()
            ]
            robot_busy_ms = _merge_intervals(
                (
                    int(row["started_at"]),
                    int(row["finished_at"] or now),
                )
                for row in robot_rows
            )
            robot_idle_ms = max(0, total_time_ms - robot_busy_ms)
            run = self._session_dict(session)
            run.update(
                {
                    "first_action_at": first_started_at,
                    "last_action_at": last_finished_at,
                    "action_count": len(rows),
                }
            )
            return {
                "run": run,
                "total_time_ms": total_time_ms,
                "robot": {
                    "action_count": len(robot_rows),
                    "busy_time_ms": robot_busy_ms,
                    "idle_time_ms": robot_idle_ms,
                    "utilization_percent": round(
                        robot_busy_ms / total_time_ms * 100, 2
                    )
                    if total_time_ms
                    else 0.0,
                    "idle_definition": "首个动作开始至最后动作结束区间减去机械臂动作区间并集",
                },
                "samples": self._duration_group(rows, "sample_id", now=now),
                "devices": self._duration_group(rows, "device_id", now=now),
                "stations": self._duration_group(rows, "station", now=now),
            }

    @staticmethod
    def _action_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "execution_id": row["execution_id"],
            "workflow_path": row["workflow_path"],
            "instance_id": row["instance_id"],
            "sample_id": row["sample_id"],
            "node_id": row["node_id"],
            "device_id": row["device_id"],
            "action_name": row["action_name"],
            "station": row["station"],
            "status": row["status"],
            "started_at": row["started_at"],
            "started_at_iso": _iso_time(row["started_at"]),
            "finished_at": row["finished_at"],
            "finished_at_iso": _iso_time(row["finished_at"]),
            "duration_ms": row["duration_ms"],
            "params": _json_loads(row["params_json"], {}),
            "result": _json_loads(row["result_json"], None),
            "measurements": _json_loads(row["measurements_json"], []),
            "error": _json_loads(row["error_json"], None),
        }

    def station_ledger(
        self,
        run_id: str | None = None,
        *,
        station: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 10000))
        normalized_station = _normalize_station(station) if station else None
        if station and station.upper() in {"ROBOT", "UNKNOWN"}:
            normalized_station = station.upper()
        with self._lock:
            session = self._resolve_run(run_id)
            if session is None:
                return {"run": None, "stations": []}
            connection = self._connect()
            where = "AND station = ?" if normalized_station else ""
            params: list[Any] = [session["run_id"]]
            if normalized_station:
                params.append(normalized_station)
            params.append(safe_limit)
            rows = connection.execute(
                f"""
                SELECT * FROM action_records
                WHERE run_id = ? {where}
                ORDER BY started_at, execution_id LIMIT ?
                """,
                params,
            ).fetchall()
            events = connection.execute(
                """
                SELECT * FROM action_events
                WHERE run_id = ? ORDER BY timestamp, id
                """,
                (session["run_id"],),
            ).fetchall()
            events_by_execution: dict[str, list[dict[str, Any]]] = {}
            for event in events:
                events_by_execution.setdefault(event["execution_id"], []).append(
                    {
                        "timestamp": event["timestamp"],
                        "level": event["level"],
                        "event_type": event["event_type"],
                        "message": event["message"],
                        "detail": _json_loads(event["detail_json"], {}),
                    }
                )
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                action = self._action_dict(row)
                action["events"] = events_by_execution.get(row["execution_id"], [])
                grouped.setdefault(str(row["station"]), []).append(action)
            return {
                "run": self._session_dict(session),
                "stations": [
                    {
                        "station": station_name,
                        "action_count": len(actions),
                        "actions": actions,
                    }
                    for station_name, actions in sorted(grouped.items())
                ],
            }

    def incident_ledger(
        self,
        run_id: str | None = None,
        *,
        sample_id: str | None = None,
        status: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 10000))
        with self._lock:
            session = self._resolve_run(run_id)
            if session is None:
                return {"run": None, "incidents": []}
            conditions = ["run_id = ?"]
            params: list[Any] = [session["run_id"]]
            if sample_id:
                conditions.append("sample_id = ?")
                params.append(sample_id)
            if status:
                conditions.append("status = ?")
                params.append(status)
            params.append(safe_limit)
            rows = self._connect().execute(
                f"""
                SELECT * FROM scheduler_incidents
                WHERE {' AND '.join(conditions)}
                ORDER BY first_seen_at DESC, incident_id DESC LIMIT ?
                """,
                params,
            ).fetchall()
            incidents = []
            for row in rows:
                incidents.append(
                    {
                        "incident_id": row["incident_id"],
                        "category": row["category"],
                        "code": row["code"],
                        "severity": row["severity"],
                        "status": row["status"],
                        "workflow_path": row["workflow_path"],
                        "instance_id": row["instance_id"],
                        "sample_id": row["sample_id"],
                        "node_id": row["node_id"],
                        "execution_id": row["execution_id"],
                        "device_id": row["device_id"],
                        "action_name": row["action_name"],
                        "station": row["station"],
                        "phase": row["phase"],
                        "message": row["message"],
                        "first_seen_at": row["first_seen_at"],
                        "first_seen_at_iso": _iso_time(row["first_seen_at"]),
                        "last_seen_at": row["last_seen_at"],
                        "last_seen_at_iso": _iso_time(row["last_seen_at"]),
                        "recovered_at": row["recovered_at"],
                        "recovered_at_iso": _iso_time(row["recovered_at"]),
                        "duration_ms": row["duration_ms"],
                        "occurrence_count": row["occurrence_count"],
                        "detail": _json_loads(row["detail_json"], {}),
                    }
                )
            return {
                "run": self._session_dict(session),
                "incidents": incidents,
            }

    def export_ledger(
        self,
        *,
        ledger: str,
        run_id: str | None = None,
        file_format: str = "json",
    ) -> Path:
        if ledger not in {"timing", "stations", "incidents"}:
            raise ValueError("ledger 仅支持 timing、stations 或 incidents")
        if file_format not in {"json", "csv"}:
            raise ValueError("format 仅支持 json 或 csv")
        if ledger == "timing":
            payload = self.timing_ledger(run_id)
        elif ledger == "stations":
            payload = self.station_ledger(run_id)
        else:
            payload = self.incident_ledger(run_id)
        resolved_run_id = str((payload.get("run") or {}).get("run_id") or "empty")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.exports_dir / (
            f"{ledger}-ledger-{timestamp}-{resolved_run_id[:8]}.{file_format}"
        )
        if file_format == "json":
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
                encoding="utf-8",
            )
            return path

        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            if ledger == "timing":
                writer.writerow(["section", "name", "data_json"])
                writer.writerow(["run", "summary", _json_dumps(payload.get("run"))])
                writer.writerow(["run", "total_time_ms", payload.get("total_time_ms", 0)])
                writer.writerow(["robot", "summary", _json_dumps(payload.get("robot"))])
                for section in ("samples", "devices", "stations"):
                    for item in payload.get(section, []):
                        name = next(
                            (
                                item.get(key)
                                for key in ("sample_id", "device_id", "station")
                                if key in item
                            ),
                            "",
                        )
                        writer.writerow([section, name, _json_dumps(item)])
            elif ledger == "stations":
                writer.writerow(
                    [
                        "station",
                        "sample_id",
                        "node_id",
                        "device_id",
                        "action_name",
                        "status",
                        "started_at",
                        "finished_at",
                        "duration_ms",
                        "params_json",
                        "result_json",
                        "measurements_json",
                        "error_json",
                        "events_json",
                    ]
                )
                for group in payload.get("stations", []):
                    for action in group.get("actions", []):
                        writer.writerow(
                            [
                                group.get("station"),
                                action.get("sample_id"),
                                action.get("node_id"),
                                action.get("device_id"),
                                action.get("action_name"),
                                action.get("status"),
                                action.get("started_at"),
                                action.get("finished_at"),
                                action.get("duration_ms"),
                                _json_dumps(action.get("params")),
                                _json_dumps(action.get("result")),
                                _json_dumps(action.get("measurements")),
                                _json_dumps(action.get("error")),
                                _json_dumps(action.get("events")),
                            ]
                        )
            else:
                writer.writerow(
                    [
                        "severity",
                        "status",
                        "category",
                        "code",
                        "sample_id",
                        "instance_id",
                        "node_id",
                        "station",
                        "device_id",
                        "action_name",
                        "phase",
                        "first_seen_at",
                        "last_seen_at",
                        "recovered_at",
                        "duration_ms",
                        "message",
                        "detail_json",
                    ]
                )
                for incident in payload.get("incidents", []):
                    writer.writerow(
                        [
                            incident.get("severity"),
                            incident.get("status"),
                            incident.get("category"),
                            incident.get("code"),
                            incident.get("sample_id"),
                            incident.get("instance_id"),
                            incident.get("node_id"),
                            incident.get("station"),
                            incident.get("device_id"),
                            incident.get("action_name"),
                            incident.get("phase"),
                            incident.get("first_seen_at"),
                            incident.get("last_seen_at"),
                            incident.get("recovered_at"),
                            incident.get("duration_ms"),
                            incident.get("message"),
                            _json_dumps(incident.get("detail")),
                        ]
                    )
        return path

    def close(self) -> None:
        with self._lock:
            if self._connection is None:
                return
            timestamp = int(self._clock())
            self._connection.execute(
                """
                UPDATE run_sessions
                SET status = 'completed', finished_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (timestamp, timestamp, self.session_id),
            )
            self._connection.execute(
                """
                UPDATE scheduler_incidents
                SET status = 'interrupted', last_seen_at = ?,
                    duration_ms = MAX(0, ? - first_seen_at)
                WHERE run_id = ? AND status = 'open'
                """,
                (timestamp, timestamp, self.session_id),
            )
            self._connection.commit()
            self._connection.close()
            self._connection = None
