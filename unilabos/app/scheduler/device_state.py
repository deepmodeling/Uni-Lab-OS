"""设备状态存储：标量变化历史和连续观测，共用 OS 的数据库适配。

设计要点：

- SQLite 使用独立 WAL 文件；PostgreSQL 使用 OS 的 device 存储作用域。
  标量历史与连续观测共享同一连接和线程锁，不新建仿真数据库。
- **EAV 三元组 + 显式类型标记**：值只允许标量（str / int / float / bool），
  统一存 TEXT，``value_type`` 列记录原始类型，读出无损还原；拒绝复杂类型
  （与 HostNode.property_callback 的过滤口径一致）。
- **当前值就是历史最后一行**：按 ``(device_id, property, id)`` 取最大 id。
  值不变不追加。每个键环形保留 ``max_history_per_key`` 条。
- 由工站后端持有；使用锁保证 REST 上报等写入入口的线程安全。
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from unilabos.persistence import open_database

if TYPE_CHECKING:
    from unilabos.app.scheduler.monitor import MonitorBus

# 允许的标量类型 → 类型标记（注意 bool 是 int 子类，必须先判）
_TYPE_TAGS: Tuple[Tuple[type, str], ...] = (
    (bool, "bool"),
    (int, "int"),
    (float, "float"),
    (str, "str"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_property_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    property    TEXT NOT NULL,
    value       TEXT NOT NULL,
    value_type  TEXT NOT NULL,
    recorded_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dph_key_time
    ON device_property_history (device_id, property, id);
"""


def _encode(value: Any) -> Tuple[str, str]:
    """标量 → (TEXT 值, 类型标记)；复杂类型直接拒绝。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("设备属性不允许 NaN 或无穷数")
    for py_type, tag in _TYPE_TAGS:
        if isinstance(value, py_type):
            if tag == "bool":
                return ("1" if value else "0", tag)
            return (str(value), tag)
    raise TypeError(f"device property value must be str/int/float/bool, got {type(value).__name__}")


def _decode(text: str, tag: str) -> Any:
    if tag == "int":
        return int(text)
    if tag == "float":
        return float(text)
    if tag == "bool":
        return text == "1"
    return text


def validate_device_properties(device_id: str, properties: Dict[str, Any]) -> None:
    """所有属性入口共用的标量合同；非法整批不得改变状态。"""
    if not device_id or not device_id.strip():
        raise ValueError("device_id must be non-empty")
    for prop, value in properties.items():
        if not isinstance(prop, str) or not prop.strip():
            raise ValueError("property must be non-empty")
        _encode(value)


def report_device_properties(
    store: DeviceStateStore, device_id: str, properties: Dict[str, Any], *,
    now_ms: int | None = None, monitor: MonitorBus | None = None,
) -> Dict[str, bool]:
    """先提交属性变化历史，再通知已有 SSE 监控总线。"""
    from unilabos.app.scheduler.monitor import monitor_bus

    monitor = monitor if monitor is not None else monitor_bus
    changed = store.set_many(device_id, properties, now_ms=now_ms)
    for prop, was_changed in changed.items():
        if was_changed:
            monitor.emit(
                "device", "device_property",
                {"device_id": device_id, "property": prop, "value": properties[prop]},
            )
    return changed


class DeviceStateStore:
    """设备状态存储；标量按变化保留，连续观测保留来源和完整采样。"""

    def __init__(self, db_path: str = ":memory:", max_history_per_key: int = 1000):
        self._lock = threading.RLock()
        self.max_history_per_key = max_history_per_key
        self.db = open_database(db_path, "device")
        self._conn = self.db
        self._conn.row_factory = sqlite3.Row
        self.db.initialize(self._initialize_sqlite)
        from unilabos.persistence.observations import migrate_legacy_observations
        try:
            migrate_legacy_observations(self.db)
        except BaseException:
            self.db.close()
            raise

    def _initialize_sqlite(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.executescript(_SCHEMA)
            from unilabos.persistence.observations import SCHEMA
            self._conn.executescript(SCHEMA)
            self._fold_latest_into_history()
            self._conn.commit()

    def _fold_latest_into_history(self) -> None:
        """把旧的当前值表并进历史最后一行，值相同则不重复插入。"""

        names = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "device_property_latest" not in names:
            return
        self._conn.execute(
            """
            INSERT INTO device_property_history(
                device_id, property, value, value_type, recorded_at
            )
            SELECT latest.device_id, latest.property, latest.value,
                   latest.value_type, latest.updated_at
            FROM device_property_latest AS latest
            WHERE NOT EXISTS (
                SELECT 1 FROM device_property_history AS history
                WHERE history.device_id = latest.device_id
                  AND history.property = latest.property
                  AND history.id = (
                      SELECT MAX(id) FROM device_property_history
                      WHERE device_id = latest.device_id
                        AND property = latest.property
                  )
                  AND history.value = latest.value
                  AND history.value_type = latest.value_type
            )
            """
        )
        self._conn.execute("DROP TABLE device_property_latest")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 写入 ─────────────────────────────────────────────────

    def set(self, device_id: str, prop: str, value: Any, now_ms: Optional[int] = None) -> bool:
        """写入变化点；同值同类型不刷新时间戳。"""
        return self.set_many(device_id, {prop: value}, now_ms=now_ms)[prop]

    def set_many(self, device_id: str, properties: Dict[str, Any],
                 now_ms: Optional[int] = None) -> Dict[str, bool]:
        """整批先校验再原子写入，返回逐属性变化标记。"""
        validate_device_properties(device_id, properties)
        encoded = {prop: _encode(value) for prop, value in properties.items()}
        ts = now_ms if now_ms is not None else int(time.time() * 1000)
        changed = {}
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for prop, (text, tag) in encoded.items():
                    row = self._conn.execute(
                        "SELECT value,value_type FROM device_property_history "
                        "WHERE device_id=? AND property=? ORDER BY id DESC LIMIT 1", (device_id, prop),
                    ).fetchone()
                    changed[prop] = row is None or row["value"] != text or row["value_type"] != tag
                    if changed[prop]:
                        self._conn.execute(
                            "INSERT INTO device_property_history(device_id,property,value,value_type,recorded_at) "
                            "VALUES (?,?,?,?,?)", (device_id, prop, text, tag, ts),
                        )
                        if self.max_history_per_key > 0:  # 0 表示不裁（录制轨迹的部署）
                            self._conn.execute(
                                "DELETE FROM device_property_history WHERE device_id=? AND property=? "
                                "AND id NOT IN (SELECT id FROM device_property_history "
                                "WHERE device_id=? AND property=? ORDER BY id DESC LIMIT ?)",
                                (device_id, prop, device_id, prop, self.max_history_per_key),
                            )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return changed

    # ── 查询 ─────────────────────────────────────────────────

    def latest_all(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """全量当前状态：{device_id: {property: {value, value_type, updated_at}}}。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT history.* FROM device_property_history AS history
                JOIN (
                    SELECT device_id, property, MAX(id) AS id
                    FROM device_property_history
                    GROUP BY device_id, property
                ) AS latest ON latest.id = history.id
                ORDER BY history.device_id, history.property
                """
            ).fetchall()
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row["device_id"], {})[row["property"]] = {
                "value": _decode(row["value"], row["value_type"]),
                "value_type": row["value_type"],
                "updated_at": row["recorded_at"],
            }
        return out

    def latest_for(self, device_id: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT history.* FROM device_property_history AS history
                JOIN (
                    SELECT property, MAX(id) AS id
                    FROM device_property_history
                    WHERE device_id = ?
                    GROUP BY property
                ) AS latest ON latest.id = history.id
                ORDER BY history.property
                """,
                (device_id,),
            ).fetchall()
        return {
            row["property"]: {
                "value": _decode(row["value"], row["value_type"]),
                "value_type": row["value_type"],
                "updated_at": row["recorded_at"],
            }
            for row in rows
        }

    def history(
        self, device_id: str, prop: str, since_ms: int = 0, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """某属性的变化轨迹（新→旧）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_property_history "
                "WHERE device_id = ? AND property = ? AND recorded_at >= ? "
                "ORDER BY id DESC LIMIT ?",
                (device_id, prop, since_ms, max(1, min(limit, 5000))),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "device_id": row["device_id"],
                "property": row["property"],
                "value": _decode(row["value"], row["value_type"]),
                "value_type": row["value_type"],
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    def history_all(self, since_ms: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        """跨设备/属性的最近变化点（新→旧），供本地实体检查器使用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_property_history WHERE recorded_at >= ? "
                "ORDER BY id DESC LIMIT ?",
                (since_ms, max(1, min(limit, 5000))),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "device_id": row["device_id"],
                "property": row["property"],
                "value": _decode(row["value"], row["value_type"]),
                "value_type": row["value_type"],
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            devices = self._conn.execute(
                "SELECT COUNT(DISTINCT device_id) AS n FROM ("
                "SELECT device_id, property FROM device_property_history "
                "GROUP BY device_id, property) AS latest"
            ).fetchone()["n"]
            props = self._conn.execute(
                "SELECT COUNT(*) AS n FROM ("
                "SELECT device_id, property FROM device_property_history "
                "GROUP BY device_id, property) AS latest"
            ).fetchone()["n"]
            hist = self._conn.execute(
                "SELECT COUNT(*) AS n FROM device_property_history"
            ).fetchone()["n"]
        return {"devices": devices, "properties": props, "history_rows": hist}


__all__ = ["DeviceStateStore"]
