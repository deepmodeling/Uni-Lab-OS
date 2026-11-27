"""中心账本的纯数据库初始化及旧记录迁移；与网络配置、客户端生命周期无关。"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any
from uuid import UUID, uuid4

from unilabos.center_protocol.digest import canonical_json
from unilabos.center_protocol.events import MAX_EVENT_SEQUENCE, make_event, utc_timestamp_from_ms, validate_event
from unilabos.persistence.database import Database
from unilabos.persistence.inspection import table_exists

TABLES = (
    "center_runtime_state", "center_command_inbox", "center_invocation_inbox",
    "center_boundary_outbox",
    "center_catalog_state",
    "center_reconciliation_request",
    "center_handoff_outbox",
    "center_preparation_outbox", "center_transport_revocation",
    "center_outcome_evidence",
    "center_event_history_recovery", "center_recovery_event_holds", "center_epoch_history",
    "center_material_input", "center_material_input_issue",
    "center_request_holds",
)


class CenterSchemaError(RuntimeError):
    """中心账本缺失、身份不匹配或历史记录无法安全迁移。"""


def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CenterSchemaError("中心账本身份不是规范 UUID") from None


def ensure_center_schema(
    connection: Database | sqlite3.Connection,
    *,
    station_uuid: str | None = None,
    instance_uuid: str | None = None,
    allow_event_gaps: bool = False,
) -> str | None:
    """在现有写事务升级中心账本；仅显式提供身份时允许初始化空账本。

    工作流启动不传配置：无中心事实时零写入，有既有账本则先迁移再允许任务恢复。
    任意中心表有事实却缺纪元时一律拒绝。整个中心域显式清空后可由客户端重新
    初始化；已有的 UUID、序号、完整信封和确认状态都不重新生成。
    """
    if not connection.in_transaction:
        raise CenterSchemaError("中心账本迁移必须在调用方写事务内执行")
    if (station_uuid is None) != (instance_uuid is None):
        raise CenterSchemaError("初始化中心账本必须同时提供工站和实例身份")
    if station_uuid is not None:
        _uuid(station_uuid)
        _uuid(instance_uuid)
    present = {name for name in TABLES if table_exists(connection, name)}
    if not present and station_uuid is None:
        return None
    states = (connection.execute("SELECT * FROM center_runtime_state").fetchall()
              if "center_runtime_state" in present else [])
    if len(states) > 1 or (states and states[0]["singleton"] != "runtime"):
        raise CenterSchemaError("中心实例及纪元账本不唯一")
    if not states:
        if allow_event_gaps:
            raise CenterSchemaError("历史恢复必须保留原实例和纪元账本")
        if any(connection.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() is not None for name in present):
            raise CenterSchemaError("中心事实非空但纪元账本缺失，禁止创建新纪元规避恢复")
        if station_uuid is None:
            return None
    for sql in (
        """CREATE TABLE IF NOT EXISTS center_runtime_state (
            singleton TEXT PRIMARY KEY, station_uuid TEXT NOT NULL,
            instance_uuid TEXT NOT NULL, event_epoch_uuid TEXT NOT NULL,
            session_json TEXT)""",
        """CREATE TABLE IF NOT EXISTS center_command_inbox (
            command_uuid TEXT PRIMARY KEY, command_sequence BIGINT NOT NULL UNIQUE,
            command_digest TEXT NOT NULL, envelope_json TEXT NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0,
            handled INTEGER NOT NULL DEFAULT 0, error TEXT)""",
        """CREATE TABLE IF NOT EXISTS center_invocation_inbox (
            invocation_uuid TEXT PRIMARY KEY, package_json TEXT NOT NULL,
            claim_confirmed INTEGER NOT NULL DEFAULT 0,
            local_task_uuid TEXT, terminal_recorded INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS center_material_input (
            invocation_uuid TEXT PRIMARY KEY REFERENCES center_invocation_inbox(invocation_uuid),
            local_task_uuid TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','claimed','rejected','released')))""",
        """CREATE TABLE IF NOT EXISTS center_material_input_issue (
            invocation_uuid TEXT PRIMARY KEY REFERENCES center_invocation_inbox(invocation_uuid),
            request_json TEXT NOT NULL, receipt_json TEXT)""",
        """CREATE TABLE IF NOT EXISTS center_boundary_outbox (
            event_uuid TEXT PRIMARY KEY, invocation_uuid TEXT NOT NULL,
            phase INTEGER NOT NULL, event_json TEXT NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0,
            UNIQUE(invocation_uuid, phase))""",
        """CREATE TABLE IF NOT EXISTS center_catalog_state (
            singleton TEXT PRIMARY KEY, catalog_epoch_uuid TEXT NOT NULL,
            catalog_sequence BIGINT NOT NULL, content_digest TEXT NOT NULL,
            upload_json TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS center_reconciliation_request (
            request_uuid TEXT PRIMARY KEY, invocation_uuid TEXT NOT NULL,
            station_uuid TEXT NOT NULL, instance_uuid TEXT NOT NULL, event_epoch_uuid TEXT NOT NULL,
            body_json TEXT NOT NULL, package_json TEXT, local_task_uuid TEXT,
            attempted INTEGER NOT NULL DEFAULT 0, reconciliation_uuid TEXT UNIQUE, receipt_json TEXT,
            applied INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS center_handoff_outbox (
            event_uuid TEXT PRIMARY KEY, handoff_uuid TEXT NOT NULL,
            event_sequence BIGINT NOT NULL UNIQUE, event_json TEXT NOT NULL,
            ack_json TEXT)""",
        """CREATE TABLE IF NOT EXISTS center_preparation_outbox (
            event_uuid TEXT PRIMARY KEY, request_uuid TEXT NOT NULL,
            event_sequence BIGINT NOT NULL UNIQUE, event_epoch_uuid TEXT NOT NULL,
            event_json TEXT NOT NULL, ack_json TEXT)""",
        """CREATE TABLE IF NOT EXISTS center_transport_revocation (
            invocation_uuid TEXT PRIMARY KEY, allocation_uuid TEXT NOT NULL UNIQUE,
            snapshot_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS center_outcome_evidence (
            invocation_uuid TEXT PRIMARY KEY, local_task_uuid TEXT NOT NULL UNIQUE,
            evidence_json TEXT, pending_reason TEXT)""",
        """CREATE TABLE IF NOT EXISTS center_recovery_event_holds (
            event_uuid TEXT PRIMARY KEY, event_digest TEXT NOT NULL, receipt_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS center_event_history_recovery (
            event_epoch_uuid TEXT NOT NULL, event_sequence BIGINT NOT NULL,
            event_json TEXT, receipt_json TEXT, state TEXT NOT NULL,
            reason TEXT, PRIMARY KEY(event_epoch_uuid,event_sequence))""",
        """CREATE TABLE IF NOT EXISTS center_request_holds (
            request_digest TEXT PRIMARY KEY, method TEXT NOT NULL, path TEXT NOT NULL,
            event_uuid TEXT, status_code INTEGER NOT NULL, error_code TEXT,
            active INTEGER NOT NULL DEFAULT 1, first_failed_at_ms BIGINT NOT NULL,
            last_failed_at_ms BIGINT NOT NULL, attempts BIGINT NOT NULL DEFAULT 1,
            retry_requested_at_ms BIGINT, retry_reason TEXT)""",
    ):
        connection.execute(sql)
    for table, columns in (
        ("center_runtime_state", {"last_event_sequence": "BIGINT NOT NULL DEFAULT 0"}),
        ("center_event_history_recovery", {"retention_receipt_json": "TEXT", "recovery_revision": "BIGINT NOT NULL DEFAULT 0"}),
        ("center_boundary_outbox", {"event_sequence": "BIGINT", "application": "TEXT"}),
        ("center_outcome_evidence", {"cancel_settlement_json": "TEXT", "recorded_cancellation_json": "TEXT"}),
        ("center_invocation_inbox", {"acceptance_version": "INTEGER NOT NULL DEFAULT 0", "reconciliation_uuid": "TEXT"}),
    ):
        existing = {column["name"] for column in connection.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_center_invocation_local_task
           ON center_invocation_inbox(local_task_uuid) WHERE local_task_uuid IS NOT NULL"""
    )
    if not states:
        connection.execute(
            """INSERT INTO center_runtime_state(singleton,station_uuid,instance_uuid,event_epoch_uuid)
               VALUES ('runtime',?,?,?)""", (station_uuid, instance_uuid, str(uuid4())),
        )
    runtime = connection.execute("SELECT * FROM center_runtime_state WHERE singleton='runtime'").fetchone()
    for name in ("station_uuid", "instance_uuid", "event_epoch_uuid"):
        _uuid(runtime[name])
    if station_uuid is not None and (runtime["station_uuid"] != station_uuid or runtime["instance_uuid"] != instance_uuid):
        raise CenterSchemaError("本地中心事实属于其他工站或实例，禁止复用")
    from unilabos.persistence.center_epochs import ensure_epoch_storage

    ensure_epoch_storage(connection)
    _migrate_events(connection, runtime, allow_event_gaps=allow_event_gaps)
    for sql in (
        "CREATE INDEX IF NOT EXISTS ix_center_boundary_pending ON center_boundary_outbox(event_sequence) WHERE acknowledged=0 AND application IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_center_handoff_pending ON center_handoff_outbox(event_sequence) WHERE ack_json IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_center_request_event_hold ON center_request_holds(event_uuid) WHERE active=1",
    ):
        connection.execute(sql)
    return runtime["event_epoch_uuid"]


def _event_json(value: dict[str, Any]) -> str:
    # JCS 仅用于摘要；普通 JSON 保留 binary64 表示，避免重启读成不安全大整数。
    return json.dumps(value, allow_nan=False, ensure_ascii=False)


def _restore_legacy_output_numbers(value: Any) -> Any:
    """仅恢复旧 JCS writer 合法产生、且规范数字字面量完全不变的 binary64。"""
    if isinstance(value, dict):
        return {key: _restore_legacy_output_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_legacy_output_numbers(item) for item in value]
    if type(value) is int and abs(value) > MAX_EVENT_SEQUENCE:
        try:
            candidate = float(value)
        except OverflowError:
            candidate = math.inf
        if not math.isfinite(candidate) or canonical_json(candidate) != str(value).encode("ascii"):
            raise CenterSchemaError("旧事件输出的大整数无法等价恢复 binary64，迁移已回滚")
        return candidate
    return value


def _legacy_event(invocation_uuid: str, body: dict[str, Any], sequence: int, runtime: Any) -> dict[str, Any]:
    """只转换旧版已持久化正文；不使用当前任务状态重新生成历史证据。"""
    if set(body) != {"event_uuid", "event_epoch_uuid", "local_task_uuid", "event_type",
                     "payload_digest", "output", "occurred_at_ms"}:
        raise CenterSchemaError("旧事件正文结构无效，迁移已回滚")
    if body["event_epoch_uuid"] != runtime["event_epoch_uuid"]:
        raise CenterSchemaError("旧事件纪元与本地账本不一致，禁止更换纪元")
    kind = body["event_type"]
    occurred_at = utc_timestamp_from_ms(body["occurred_at_ms"])
    payload = {"task_payload_digest": body["payload_digest"]}
    if kind in ("succeeded", "failed", "canceled"):
        payload.update(status=kind, output=_restore_legacy_output_numbers(body["output"]))
        kind = "outcome"
    elif kind not in ("accepted", "running") or body["output"] != {}:
        raise CenterSchemaError("旧事件种类或正文无效，不能丢弃证据字段")
    if kind == "accepted":
        payload.update(accepted_at=occurred_at, local_task_uuid=body["local_task_uuid"])
    return make_event(
        event_uuid=body["event_uuid"], event_epoch_uuid=body["event_epoch_uuid"],
        event_sequence=sequence, station_uuid=runtime["station_uuid"], instance_uuid=runtime["instance_uuid"],
        station_invocation_uuid=invocation_uuid, local_task_uuid=body["local_task_uuid"],
        event_type=kind, payload=payload, occurred_at=occurred_at,
    )

def _migrate_events(connection: Database | sqlite3.Connection, runtime: Any, *, allow_event_gaps: bool = False) -> None:
    """调用者持有整个升级事务；排序、序号、信封与高水位一起提交。"""
    high_water = runtime["last_event_sequence"]
    if type(high_water) is not int or not 0 <= high_water <= MAX_EVENT_SEQUENCE:
        raise CenterSchemaError("事件序号账本无效")
    rows = connection.execute("SELECT * FROM center_boundary_outbox").fetchall()
    legacy = []
    epochs = {row["event_epoch_uuid"]: dict(row) for row in connection.execute("SELECT * FROM center_epoch_history")}
    if runtime["event_epoch_uuid"] in epochs:
        raise CenterSchemaError("当前事件纪元已退休，禁止重新启用")
    epochs[runtime["event_epoch_uuid"]] = dict(runtime)
    sequences_by_epoch = {epoch: [] for epoch in epochs}
    for row in rows:
        body = json.loads(row["event_json"])
        if "protocol_version" not in body:
            if row["event_sequence"] is not None:
                raise CenterSchemaError("旧事件已占用序号但缺少信封，禁止重新分号")
            legacy.append((row, body))
            continue
        event = validate_event(body)
        known = epochs.get(event["event_epoch_uuid"])
        if known is None:
            raise CenterSchemaError("事件属于未核实的历史纪元")
        if (event["event_uuid"] != row["event_uuid"]
                or event["station_invocation_uuid"] != row["invocation_uuid"]
                or row["event_epoch_uuid"] not in (None, event["event_epoch_uuid"])
                or event["station_uuid"] != runtime["station_uuid"] or event["instance_uuid"] != runtime["instance_uuid"]
                or row["event_sequence"] != event["event_sequence"]):
            raise CenterSchemaError("持久事件与序号账本不一致，禁止重算")
        sequences_by_epoch[event["event_epoch_uuid"]].append(event["event_sequence"])
        connection.execute("UPDATE center_boundary_outbox SET event_epoch_uuid=? WHERE event_uuid=? AND event_epoch_uuid IS NULL",
                           (event["event_epoch_uuid"], event["event_uuid"]))
    from unilabos.center_protocol.handoff import HandoffProtocolError, validate_handoff_ack, validate_handoff_event

    identities = {row["event_uuid"] for row in rows}
    for row in connection.execute("SELECT * FROM center_handoff_outbox").fetchall():
        try:
            event = validate_handoff_event(json.loads(row["event_json"]))
            if row["ack_json"] is not None:
                validate_handoff_ack(json.loads(row["ack_json"]), event)
        except (HandoffProtocolError, ValueError, TypeError) as error:
            raise CenterSchemaError("持久交接事件或确认无法验证") from error
        if event["event_epoch_uuid"] not in epochs:
            raise CenterSchemaError("交接事件属于未核实的历史纪元")
        if (row["event_epoch_uuid"] not in (None, event["event_epoch_uuid"])
                or event["event_uuid"] != row["event_uuid"] or row["event_uuid"] in identities
                or event["handoff_uuid"] != row["handoff_uuid"]
                or event["event_sequence"] != row["event_sequence"]
                or any(event[key] != runtime[key] for key in ("station_uuid", "instance_uuid"))):
            raise CenterSchemaError("交接事件与共享身份账本不一致")
        identities.add(row["event_uuid"])
        sequences_by_epoch[event["event_epoch_uuid"]].append(event["event_sequence"])
        connection.execute("UPDATE center_handoff_outbox SET event_epoch_uuid=? WHERE event_uuid=? AND event_epoch_uuid IS NULL",
                           (event["event_epoch_uuid"], event["event_uuid"]))
    from unilabos.center_protocol.events import validate_ack
    from unilabos.center_protocol.transport_preparation import validate_event as validate_preparation_event
    for row in connection.execute("SELECT * FROM center_preparation_outbox").fetchall():
        try:
            event = validate_preparation_event(json.loads(row["event_json"]))
            if row["ack_json"] is not None:
                validate_ack(json.loads(row["ack_json"]), event)
        except (ValueError, TypeError) as error:
            raise CenterSchemaError("持久车辆准备事件或确认无法验证") from error
        if (event["event_epoch_uuid"] not in epochs or event["event_uuid"] in identities
                or event["event_uuid"] != row["event_uuid"] or event["event_sequence"] != row["event_sequence"]
                or event["request_uuid"] != row["request_uuid"]
                or event["event_epoch_uuid"] != row["event_epoch_uuid"]
                or any(event[key] != runtime[key] for key in ("station_uuid", "instance_uuid"))):
            raise CenterSchemaError("车辆准备事件与共享身份账本不一致")
        identities.add(event["event_uuid"])
        sequences_by_epoch[event["event_epoch_uuid"]].append(event["event_sequence"])
    for epoch, positions in sequences_by_epoch.items():
        high = epochs[epoch]["last_event_sequence"]
        if (any(epochs[epoch][key] != runtime[key] for key in ("station_uuid", "instance_uuid"))
                or type(high) is not int or not 0 <= high <= MAX_EVENT_SEQUENCE
                or len(positions) != len(set(positions)) or max(positions, default=0) > high):
            raise CenterSchemaError("历史纪元身份或序号账本不一致")
        if not allow_event_gaps and (len(positions) != high or sorted(positions) != list(range(1, high + 1))):
            raise CenterSchemaError("持久事件序号账本出现缺口，禁止重新分配历史身份")
    sequences = sequences_by_epoch[runtime["event_epoch_uuid"]]
    has_gaps = sorted(sequences) != list(range(1, len(sequences) + 1)) or high_water != len(sequences)
    if allow_event_gaps and legacy:
        raise CenterSchemaError("历史恢复缺少已分号原信封，禁止在恢复入口迁移旧事件")
    if (has_gaps and (not allow_event_gaps or legacy)) or len(sequences) != len(set(sequences)) or max(sequences, default=0) > high_water:
        raise CenterSchemaError("持久事件序号账本出现缺口，禁止重新分配历史身份")
    legacy.sort(key=lambda pair: (pair[1].get("occurred_at_ms", -1), pair[0]["invocation_uuid"],
                                 pair[0]["phase"], pair[0]["event_uuid"]))
    for row, body in legacy:
        high_water += 1
        event = _legacy_event(row["invocation_uuid"], body, high_water, runtime)
        if event["event_uuid"] != row["event_uuid"]:
            raise CenterSchemaError("旧事件正文与收件身份不一致")
        connection.execute(
            """UPDATE center_boundary_outbox SET event_json=?,event_sequence=?,application=?,event_epoch_uuid=? WHERE event_uuid=?""",
            (_event_json(event), high_water, "legacy_acknowledged" if row["acknowledged"] else None, event["event_epoch_uuid"], row["event_uuid"]),
        )
    connection.execute("UPDATE center_runtime_state SET last_event_sequence=?", (high_water,))
