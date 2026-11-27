"""Local Edge 会话事实、在线设备投影与就绪判定。"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypeVar

from unilabos.app.scheduler.device_state import validate_device_properties

_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_edge_session (
    session_uuid TEXT PRIMARY KEY,
    edge_uuid TEXT NOT NULL,
    instance_uuid TEXT NOT NULL,
    edge_key TEXT NOT NULL,
    devices_json TEXT NOT NULL,
    connected INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_OfflineResult = TypeVar("_OfflineResult")
_ActivationResult = TypeVar("_ActivationResult")


class LocalEdgeSessionStore:
    """独占 Local Edge 注册、连接与在线设备事实的 SQLite 深模块。

    参数：构造时接收 Authority 共用连接与重入锁。返回：提供会话生命周期、
    在线投影和原子离线后果接口的 Store 实例。异常：初始化建表或清理陈旧连接
    失败时原样传播，不产生半初始化实例。
    """

    def __init__(self, connection: sqlite3.Connection, lock: Any) -> None:
        """绑定 Authority 共用的数据库连接并初始化会话事实。

        参数：``connection`` 是已启用 ``sqlite3.Row`` 的 Authority 连接；``lock``
        是保护该连接且支持重入的上下文锁。返回无。异常：建表、清理陈旧连接或
        提交失败时原样传播；初始化完成后不会保留跨 Backend 进程的在线状态。
        """

        self._connection = connection
        self._lock = lock
        self._property_history_enabled = True
        self._initialize()

    def _initialize(self) -> None:
        """创建会话表并关闭上一 Backend 进程遗留的连接事实。

        参数：无。返回无。异常：SQLite 建表、更新或提交错误原样传播；更新与
        Authority 其他初始化共享同一重入锁，避免并发访问同一连接。
        """

        with self._lock:
            self._connection.execute(_SESSION_SCHEMA)
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS local_device_property_report (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL, properties_json TEXT NOT NULL, recorded_at INTEGER NOT NULL
                )
            """)
            self._connection.execute(
                """
                UPDATE local_edge_session SET connected = 0, updated_at = ?
                WHERE connected != 0
                """,
                (time.time(),),
            )
            self._connection.commit()

    def register_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """持久化一份尚未完成 WebSocket hello 的 Edge 注册。

        参数：``payload`` 必须包含非空 ``edge_key``、规范实例 UUID 和对象形式
        的设备数组。返回稳定 ``edge_uuid`` 与新 ``session_uuid``。异常：身份或
        设备形状非法时抛 ``ValueError``，SQLite 写入错误原样传播。
        """

        edge_key = _required_text(payload, "edge_key")
        instance_uuid = str(uuid.UUID(_required_text(payload, "instance_uuid")))
        devices = payload.get("devices")
        if not isinstance(devices, list) or any(
            not isinstance(device, dict) for device in devices
        ):
            raise ValueError("devices must be a list of objects")
        edge_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"unilab:{edge_key}"))
        session_uuid = str(uuid.uuid4())
        now = time.time()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO local_edge_session(
                    session_uuid, edge_uuid, instance_uuid, edge_key,
                    devices_json, connected, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    session_uuid,
                    edge_uuid,
                    instance_uuid,
                    edge_key,
                    json.dumps(devices, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            self._connection.commit()
        return {"edge_uuid": edge_uuid, "session_uuid": session_uuid}

    def set_session_connected(self, session_uuid: str, connected: bool) -> None:
        """原子切换唯一当前 Edge 会话的连接状态。

        参数：``session_uuid`` 是已注册会话，``connected`` 表示 WebSocket 当前
        是否已完成 hello。返回无。异常：会话不存在时抛 ``ValueError``；数据库
        错误回滚。连接新会话时先关闭所有旧连接事实，确保至多一个在线会话。
        """

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                if connected:
                    self._connection.execute(
                        """
                        UPDATE local_edge_session SET connected = 0, updated_at = ?
                        WHERE session_uuid != ? AND connected != 0
                        """,
                        (now, session_uuid),
                    )
                changed = self._connection.execute(
                    """
                    UPDATE local_edge_session SET connected = ?, updated_at = ?
                    WHERE session_uuid = ?
                    """,
                    (1 if connected else 0, now, session_uuid),
                ).rowcount
                if changed != 1:
                    raise ValueError("unknown Edge session")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def update_device_status(
        self,
        session_uuid: str,
        local_device_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """更新当前在线会话中一个设备的实时可派发事实。

        参数：会话、设备本地身份和已校验的在线/健康/未知命令及属性增量。
        返回更新后的脱离设备快照。异常：会话不在线、设备不存在或持久声明损坏
        时失败关闭；身份、动作能力和物料 UUID 不允许由状态接口修改。
        """

        normalized_session = str(uuid.UUID(str(session_uuid)))
        normalized_device = str(local_device_id or "").strip()
        if not normalized_device:
            raise ValueError("local device id is required")
        properties = dict(payload.get("status") or {})
        validate_device_properties(normalized_device, properties)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT devices_json,connected FROM local_edge_session "
                    "WHERE session_uuid=?",
                    (normalized_session,),
                ).fetchone()
                if row is None or not bool(row["connected"]):
                    raise ValueError("Edge session is not connected")
                decoded = json.loads(str(row["devices_json"]))
                if not isinstance(decoded, list):
                    raise ValueError("registered devices are invalid")
                selected: dict[str, Any] | None = None
                for item in decoded:
                    if (
                        isinstance(item, dict)
                        and str(item.get("local_id") or "").strip()
                        == normalized_device
                    ):
                        selected = item
                        break
                if selected is None:
                    raise ValueError("registered device does not exist")
                selected["online"] = payload["online"]
                selected["dispatch_block_reason"] = payload[
                    "dispatch_block_reason"
                ]
                selected["unknown_command_ids"] = list(
                    payload["unknown_command_ids"]
                )
                status = selected.get("status")
                merged_status = dict(status) if isinstance(status, Mapping) else {}
                changed = {
                    key: value for key, value in properties.items()
                    if key not in merged_status or type(merged_status[key]) is not type(value)
                    or merged_status[key] != value
                }
                merged_status.update(dict(payload.get("status") or {}))
                selected["status"] = merged_status
                now = time.time()
                self._connection.execute(
                    "UPDATE local_edge_session SET devices_json=?,updated_at=? "
                    "WHERE session_uuid=? AND connected=1",
                    (
                        json.dumps(decoded, ensure_ascii=False, separators=(",", ":")),
                        now,
                        normalized_session,
                    ),
                )
                if changed and self._property_history_enabled:
                    # 与会话快照同事务接受属性，跨库投影失败仍可恢复。
                    self._connection.execute(
                        "INSERT INTO local_device_property_report(device_id,properties_json,recorded_at) VALUES (?,?,?)",
                        (normalized_device, json.dumps(changed, ensure_ascii=False, allow_nan=False), int(now * 1000)),
                    )
                self._connection.commit()
                return dict(selected)
            except BaseException:
                self._connection.rollback()
                raise

    def pending_property_reports(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM local_device_property_report ORDER BY id LIMIT ?", (limit,),
            ).fetchall()
            return [{"id": row["id"], "device_id": row["device_id"],
                     "properties": json.loads(row["properties_json"]), "recorded_at": row["recorded_at"]}
                    for row in rows]

    def set_property_history_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._property_history_enabled = enabled
            if not enabled:
                # 显式关闭历史库时不积压永远无法消费的遥测。
                self._connection.execute("DELETE FROM local_device_property_report")
                self._connection.commit()

    def acknowledge_property_report(self, report_id: int) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM local_device_property_report WHERE id=?", (report_id,))
            self._connection.commit()

    @contextmanager
    def activation_transaction(
        self,
        session_uuid: str,
        edge_uuid: str,
        reconcile: Callable[[sqlite3.Connection], _ActivationResult],
    ) -> Iterator[_ActivationResult]:
        """在一个写事务内验证注册、对账 hello 并激活新会话。

        参数：``session_uuid`` 与 ``edge_uuid`` 必须匹配一份尚未连接的注册；
        ``reconcile`` 在同一连接上写入进程、游标与 Job 对账事实且不得提交。
        返回：上下文中产出对账结果；调用方须在 yield 内同步切换内存 generation，
        严禁跨 ``await``。异常：未知、Edge 不匹配或已 active 的会话抛
        ``ValueError``；对账、调用方切换或提交失败时回滚全部 SQLite 变更。
        """

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_registered_session_locked(
                    session_uuid,
                    edge_uuid,
                    require_disconnected=True,
                )
                result = reconcile(self._connection)
                now = time.time()
                self._connection.execute(
                    """
                    UPDATE local_edge_session SET connected = 0, updated_at = ?
                    WHERE session_uuid != ? AND connected != 0
                    """,
                    (now, session_uuid),
                )
                changed = self._connection.execute(
                    """
                    UPDATE local_edge_session SET connected = 1, updated_at = ?
                    WHERE session_uuid = ? AND connected = 0
                    """,
                    (now, session_uuid),
                ).rowcount
                if changed != 1:
                    raise ValueError("Edge session activation changed concurrently")
                yield result
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def reconcile_registered_session(
        self,
        session_uuid: str,
        edge_uuid: str,
        reconcile: Callable[[sqlite3.Connection], _ActivationResult],
    ) -> _ActivationResult:
        """只为身份匹配的已注册会话提交 hello 对账事实。

        参数：``session_uuid`` 与 ``edge_uuid`` 共同证明注册身份；``reconcile``
        在同一写事务内执行对账且不得提交。返回回调的对账结果。异常：未知或
        Edge 不匹配时抛 ``ValueError``；回调或 SQLite 失败时回滚全部写入。
        """

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_registered_session_locked(
                    session_uuid,
                    edge_uuid,
                    require_disconnected=False,
                )
                result = reconcile(self._connection)
                self._connection.commit()
                return result
            except BaseException:
                self._connection.rollback()
                raise

    def _validate_registered_session_locked(
        self,
        session_uuid: str,
        edge_uuid: str,
        *,
        require_disconnected: bool,
    ) -> sqlite3.Row:
        """在调用方写事务中校验 Session 与 Edge 注册绑定。

        参数：两个 UUID 是 hello 声明身份；``require_disconnected`` 为真时拒绝
        复用已 active 的 Session。返回匹配的 SQLite 行。异常：注册不存在、
        Edge 不匹配或违反 active 前置条件时抛 ``ValueError``；本函数不提交。
        """

        row = self._connection.execute(
            """
            SELECT edge_uuid, connected FROM local_edge_session
            WHERE session_uuid = ?
            """,
            (session_uuid,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown Edge session")
        if str(row["edge_uuid"]) != edge_uuid:
            raise ValueError("Edge session identity changed")
        if require_disconnected and bool(row["connected"]):
            raise ValueError("Edge session is already connected")
        return row

    def disconnect_session(
        self,
        session_uuid: str,
        on_edge_offline: Callable[[sqlite3.Connection], _OfflineResult],
    ) -> tuple[bool, _OfflineResult | None]:
        """关闭指定会话并原子执行真正离线后的跨聚合后果。

        参数：``session_uuid`` 是正在结束的 WebSocket 会话；``on_edge_offline``
        在同一写事务内接收 Authority 连接，仅当该会话在事务开始时确为全局
        active 且已无任何在线接管代际时调用。返回：Authority 是否真正离线及
        回调结果；失败 hello、陈旧会话或已有接管者时结果为 ``None``。异常：
        会话不存在时抛 ``ValueError``；回调或 SQLite 错误导致整个事务回滚，
        禁止回调自行提交或开启事务。
        """

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                session = self._connection.execute(
                    """
                    SELECT connected FROM local_edge_session
                    WHERE session_uuid = ?
                    """,
                    (session_uuid,),
                ).fetchone()
                if session is None:
                    raise ValueError("unknown Edge session")
                self._connection.execute(
                    """
                    UPDATE local_edge_session SET connected = 0, updated_at = ?
                    WHERE session_uuid = ?
                    """,
                    (time.time(), session_uuid),
                )
                if not bool(session["connected"]):
                    self._connection.commit()
                    return False, None
                replacement = self._connection.execute(
                    """
                    SELECT 1 FROM local_edge_session
                    WHERE session_uuid != ? AND connected != 0
                    LIMIT 1
                    """,
                    (session_uuid,),
                ).fetchone()
                if replacement is not None:
                    self._connection.commit()
                    return False, None
                result = on_edge_offline(self._connection)
                self._connection.commit()
                return True, result
            except BaseException:
                self._connection.rollback()
                raise

    def online_devices(self) -> dict[str, dict[str, Any]]:
        """把唯一在线会话的设备注册投影为 Backend 在线设备事实。

        参数：无。返回：无在线会话时为空字典，否则按 ``local_id`` 索引并保持
        既有设备键、命名空间和传输字段。异常：持久 JSON 损坏时解析错误原样传播。
        """

        with self._lock:
            row = self._connection.execute(
                """
                SELECT devices_json FROM local_edge_session
                WHERE connected = 1 ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return {}
        devices = json.loads(str(row["devices_json"]))
        return {
            str(device["local_id"]): {
                "device_key": f"/devices/{device['local_id']}/{device['local_id']}",
                "namespace": f"/devices/{device['local_id']}",
                "machine_name": "managed-local-edge",
                "uuid": str(device.get("material_uuid") or ""),
                "node_name": str(device["local_id"]),
                "transport": "edge-control",
            }
            for device in devices
            if isinstance(device, dict) and str(device.get("local_id") or "")
        }

    def latest_registration(self) -> dict[str, Any] | None:
        """返回当前优先、否则最近一次 Edge 注册的脱离副本。

        参数：无。返回：尚未注册时为 ``None``，否则包含 Edge/实例身份、连接
        状态、时间戳和设备声明。异常：持久的设备声明不是对象数组时按空数组
        失败关闭，JSON 或数据库错误原样传播。
        """

        with self._lock:
            row = self._connection.execute(
                """
                SELECT edge_uuid,instance_uuid,edge_key,devices_json,connected,
                       created_at,updated_at
                FROM local_edge_session
                ORDER BY connected DESC,updated_at DESC,created_at DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        decoded = json.loads(str(row["devices_json"]))
        devices = (
            [dict(device) for device in decoded if isinstance(device, dict)]
            if isinstance(decoded, list)
            else []
        )
        return {
            "edge_uuid": str(row["edge_uuid"]),
            "instance_uuid": str(row["instance_uuid"]),
            "edge_key": str(row["edge_key"]),
            "connected": bool(row["connected"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "devices": devices,
        }


def project_local_edge_readiness(
    registration: Mapping[str, Any] | None,
) -> tuple[bool, dict[str, Any]]:
    """从最近注册快照生成关闭式实时就绪摘要。

    参数：``registration`` 是 ``latest_registration`` 返回的脱离副本或 ``None``。
    返回：就绪布尔值与不含令牌、完整设备清单及动作参数的摘要。异常：无；缺失、
    非规范身份、空设备集或非法动作声明均转换为 ``not_ready``，不伪造目录指纹。
    """

    edge_key = ""
    instance_uuid = ""
    connected = False
    devices: Any = None
    if isinstance(registration, Mapping):
        raw_edge_key = registration.get("edge_key")
        edge_key = raw_edge_key.strip() if isinstance(raw_edge_key, str) else ""
        raw_instance_uuid = registration.get("instance_uuid")
        instance_uuid = (
            raw_instance_uuid.strip() if isinstance(raw_instance_uuid, str) else ""
        )
        connected = registration.get("connected") is True
        devices = registration.get("devices")

    valid_devices = (
        isinstance(devices, list)
        and bool(devices)
        and all(is_readiness_device_valid(device) for device in devices)
    )
    ready = bool(
        connected
        and edge_key
        and _is_canonical_uuid(instance_uuid)
        and valid_devices
    )
    return ready, {
        "status": "ready" if ready else "not_ready",
        "edge_key": edge_key,
        "instance_uuid": instance_uuid,
        "connected": connected,
        "device_count": len(devices) if isinstance(devices, list) else 0,
    }


def is_readiness_device_valid(device: Any) -> bool:
    """校验单个设备是否足以证明 Local Edge 可接受动作。

    参数：``device`` 是注册快照中的一个设备声明。返回：本地身份、物料 UUID
    及至少一个具名动作均合法时为 ``True``，否则为 ``False``。异常：无；未知
    字段被忽略，动作参数既不读取也不返回。
    """

    if not isinstance(device, Mapping):
        return False
    local_id = device.get("local_id")
    material_uuid = device.get("material_uuid")
    actions = device.get("actions")
    return bool(
        isinstance(local_id, str)
        and local_id.strip()
        and _is_canonical_uuid(material_uuid)
        and isinstance(actions, list)
        and actions
        and all(
            isinstance(action, Mapping)
            and isinstance(action.get("name"), str)
            and bool(action["name"].strip())
            and isinstance(action.get("type"), str)
            and bool(action["type"].strip())
            for action in actions
        )
    )


def _is_canonical_uuid(value: Any) -> bool:
    """判断值是否为小写连字符形式的规范 UUID 字符串。

    参数：``value`` 是待校验身份。返回：仅当字符串可解析且等于规范 UUID 文本
    时为 ``True``。异常：无；类型或格式错误均返回 ``False``。
    """

    if not isinstance(value, str):
        return False
    try:
        return value == str(uuid.UUID(value))
    except ValueError:
        return False


def _required_text(payload: dict[str, Any], field: str) -> str:
    """读取请求对象中的非空文本字段。

    参数：``payload`` 是注册请求；``field`` 是待读取字段名。返回去除首尾空白的
    文本。异常：字段缺失、类型错误或为空时抛 ``ValueError``。
    """

    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()
