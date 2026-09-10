"""设备状态存储：``(device_id, property, value)`` 三元组，独立 SQLite。

设计要点：

- **独立 db 文件**（与 inventory.db、工作流状态分开），WAL 模式——设备遥测
  读写频繁，不与物料/工作流事务互相阻塞。
- **EAV 三元组 + 显式类型标记**：值只允许标量（str / int / float / bool），
  统一存 TEXT，``value_type`` 列记录原始类型，读出无损还原；拒绝复杂类型
  （与 HostNode.property_callback 的过滤口径一致）。
- **latest / history 分表**：
  - ``device_property_latest``：主键 ``(device_id, property)`` upsert，
    永远只有"设备数 × 属性数"行，当前状态查询零扫描；
  - ``device_property_history``：append-only，仅在**值变化**时写入，
    每个 (device, property) 环形保留 ``max_history_per_key`` 条。
- 写入方由微后端（JobExecutionBackend）worker 线程串行驱动；本模块自身
  也用锁保证任意线程直调安全（REST 上报路径）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# 允许的标量类型 → 类型标记（注意 bool 是 int 子类，必须先判）
_TYPE_TAGS: Tuple[Tuple[type, str], ...] = (
    (bool, "bool"),
    (int, "int"),
    (float, "float"),
    (str, "str"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_property_latest (
    device_id   TEXT NOT NULL,
    property    TEXT NOT NULL,
    value       TEXT NOT NULL,
    value_type  TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (device_id, property)
);

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


class DeviceStateStore:
    """设备状态 SQLite 存储（latest upsert + history 环形）。"""

    def __init__(self, db_path: str = ":memory:", max_history_per_key: int = 1000):
        self._lock = threading.Lock()
        self.max_history_per_key = max_history_per_key
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 写入 ─────────────────────────────────────────────────

    def set(self, device_id: str, prop: str, value: Any, now_ms: Optional[int] = None) -> bool:
        """写入一个属性值；返回值是否发生变化（新建视为变化）。

        latest 无条件 upsert（时间戳始终推进）；history 仅在值变化时追加，
        并按 key 环形裁剪。
        """
        if not device_id or not prop:
            raise ValueError("device_id and property must be non-empty")
        text, tag = _encode(value)
        ts = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._lock:
            row = self._conn.execute(
                "SELECT value, value_type FROM device_property_latest "
                "WHERE device_id = ? AND property = ?",
                (device_id, prop),
            ).fetchone()
            changed = row is None or row["value"] != text or row["value_type"] != tag
            self._conn.execute(
                "INSERT INTO device_property_latest(device_id, property, value, value_type, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(device_id, property) DO UPDATE SET "
                "value = excluded.value, value_type = excluded.value_type, "
                "updated_at = excluded.updated_at",
                (device_id, prop, text, tag, ts),
            )
            if changed:
                self._conn.execute(
                    "INSERT INTO device_property_history(device_id, property, value, value_type, recorded_at) "
                    "VALUES (?,?,?,?,?)",
                    (device_id, prop, text, tag, ts),
                )
                # 环形裁剪：只保留该 key 最新 max_history_per_key 条
                self._conn.execute(
                    "DELETE FROM device_property_history WHERE device_id = ? AND property = ? "
                    "AND id NOT IN (SELECT id FROM device_property_history "
                    "WHERE device_id = ? AND property = ? ORDER BY id DESC LIMIT ?)",
                    (device_id, prop, device_id, prop, self.max_history_per_key),
                )
            self._conn.commit()
            return changed

    def set_many(self, device_id: str, properties: Dict[str, Any]) -> Dict[str, bool]:
        """批量写入（REST 上报入口）；返回 {property: changed}。"""
        return {prop: self.set(device_id, prop, value) for prop, value in properties.items()}

    # ── 查询 ─────────────────────────────────────────────────

    def latest_all(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """全量当前状态：{device_id: {property: {value, value_type, updated_at}}}。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_property_latest ORDER BY device_id, property"
            ).fetchall()
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row["device_id"], {})[row["property"]] = {
                "value": _decode(row["value"], row["value_type"]),
                "value_type": row["value_type"],
                "updated_at": row["updated_at"],
            }
        return out

    def latest_for(self, device_id: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_property_latest WHERE device_id = ? ORDER BY property",
                (device_id,),
            ).fetchall()
        return {
            row["property"]: {
                "value": _decode(row["value"], row["value_type"]),
                "value_type": row["value_type"],
                "updated_at": row["updated_at"],
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
                "SELECT COUNT(DISTINCT device_id) AS n FROM device_property_latest"
            ).fetchone()["n"]
            props = self._conn.execute(
                "SELECT COUNT(*) AS n FROM device_property_latest"
            ).fetchone()["n"]
            hist = self._conn.execute(
                "SELECT COUNT(*) AS n FROM device_property_history"
            ).fetchone()["n"]
        return {"devices": devices, "properties": props, "history_rows": hist}


__all__ = ["DeviceStateStore"]
