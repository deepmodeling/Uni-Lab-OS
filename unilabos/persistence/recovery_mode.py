"""持久灾备运行门；进入后禁止创建任务和恢复动作，历史事实仍可读取。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from time import time_ns
from typing import Any
from uuid import uuid4

from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.material import canonical_uuid
from unilabos.persistence.database import Database, PostgreSQLDatabase
from unilabos.persistence.inspection import table_exists, table_names

Connection = Database | sqlite3.Connection


class EvidenceOnlyError(RuntimeError):
    code = "EVIDENCE_ONLY_RECOVERY"


DDL = """CREATE TABLE IF NOT EXISTS center_recovery_mode (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    mode TEXT NOT NULL CHECK(mode IN ('normal','evidence_only')),
    revision BIGINT NOT NULL CHECK(revision>=0),
    reason TEXT NOT NULL, changed_at_ms BIGINT NOT NULL, recovery_uuid TEXT)"""


def status(connection: Connection) -> dict[str, Any]:
    if not table_exists(connection, "center_recovery_mode"):
        return {"mode": "normal", "revision": 0, "reason": "", "changed_at_ms": 0, "recovery_uuid": None}
    rows = connection.execute("SELECT * FROM center_recovery_mode").fetchall()
    if len(rows) != 1 or rows[0]["singleton"] != 1 or rows[0]["mode"] not in {"normal", "evidence_only"}:
        raise EvidenceOnlyError("灾备运行状态不完整，拒绝执行")
    nonce = rows[0]["recovery_uuid"] if "recovery_uuid" in rows[0].keys() else None
    if rows[0]["mode"] == "evidence_only":
        canonical_uuid(nonce)
    return {**{key: rows[0][key] for key in ("mode", "revision", "reason", "changed_at_ms")}, "recovery_uuid": nonce}


def evidence_only(connection: Connection) -> bool:
    return status(connection)["mode"] == "evidence_only"


def require_execution(connection: Connection, *, task_uuid: str | None = None,
                      invocation_uuid: str | None = None, backend_task_uuid: str | None = None,
                      invocation_key: str | None = None, device_action_key: str | None = None) -> None:
    if isinstance(connection, PostgreSQLDatabase) and connection.in_transaction and table_exists(connection, "center_recovery_mode"):
        connection.execute("SELECT singleton FROM center_recovery_mode WHERE singleton=1 FOR SHARE NOWAIT")
    if evidence_only(connection):
        raise EvidenceOnlyError("灾备模式只允许补报原证据，执行资格尚未核实")
    identities = [("task", task_uuid), ("invocation", invocation_uuid), ("device_action", device_action_key)]
    if backend_task_uuid and invocation_key:
        identities.append(("submission", digest([backend_task_uuid, invocation_key])))
    if any(value and held(connection, kind, value) for kind, value in identities):
        raise EvidenceOnlyError("原执行身份已持久隔离，禁止重建或继续物理执行")


def enter(connection: Connection, reason: str) -> dict[str, Any]:
    """由离线恢复工具在启动执行服务前调用；不创建纪元、Task、Job 或事件。"""
    if not connection.in_transaction:
        raise EvidenceOnlyError("进入灾备模式必须使用写事务")
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 2000:
        raise ValueError("灾备说明须为 1 至 2000 字节的非空文本")
    connection.execute(DDL)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(center_recovery_mode)")}
    if "recovery_uuid" not in columns:
        connection.execute("ALTER TABLE center_recovery_mode ADD COLUMN recovery_uuid TEXT")
    connection.execute("""INSERT INTO center_recovery_mode VALUES (1,'normal',0,'',0,NULL)
                          ON CONFLICT(singleton) DO NOTHING""")
    # 与同一库的写事务互斥；PG 也在这里取得状态行的写锁。
    connection.execute("UPDATE center_recovery_mode SET revision=revision WHERE singleton=1")
    connection.execute("UPDATE center_recovery_mode SET recovery_uuid=? WHERE mode='evidence_only' AND recovery_uuid IS NULL", (str(uuid4()),))
    before = status(connection)
    if before["mode"] == "evidence_only":
        return before
    connection.execute("""CREATE TABLE IF NOT EXISTS center_recovery_mode_history (
        revision BIGINT PRIMARY KEY, mode TEXT NOT NULL, reason TEXT NOT NULL,
        changed_at_ms BIGINT NOT NULL, verification_json TEXT)""")
    revision, changed_at = before["revision"] + 1, time_ns() // 1_000_000
    connection.execute("""UPDATE center_recovery_mode SET mode='evidence_only',revision=?,
                          reason=?,changed_at_ms=?,recovery_uuid=? WHERE singleton=1""", (revision, reason, changed_at, str(uuid4())))
    connection.execute("INSERT INTO center_recovery_mode_history(revision,mode,reason,changed_at_ms) VALUES (?,'evidence_only',?,?)",
                       (revision, reason, changed_at))
    _hold_existing(connection, revision)
    return status(connection)


def held(connection: Connection, kind: str, identity: str) -> bool:
    if not table_exists(connection, "center_recovery_holds"):
        return False
    return connection.execute("SELECT 1 FROM center_recovery_holds WHERE subject_kind=? AND subject_uuid=?",
                              (kind, identity)).fetchone() is not None


def _hold(connection: Connection, kind: str, identity: str, revision: int) -> None:
    connection.execute("INSERT INTO center_recovery_holds VALUES (?,?,?) ON CONFLICT DO NOTHING", (kind, identity, revision))


def _hold_existing(connection: Connection, revision: int) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS center_recovery_holds (
        subject_kind TEXT NOT NULL,subject_uuid TEXT NOT NULL,revision BIGINT NOT NULL,
        PRIMARY KEY(subject_kind,subject_uuid))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS center_recovery_baseline (
        checkpoint_uuid TEXT PRIMARY KEY,checkpoint_json TEXT NOT NULL,command_high_watermark BIGINT NOT NULL)""")
    if table_exists(connection, "workflow_task"):
        for row in connection.execute("SELECT uuid,backend_task_uuid,invocation_key,execution_kind,idempotency_key FROM workflow_task").fetchall():
            _hold(connection, "task", row["uuid"], revision)
            if row["execution_kind"] == "ad_hoc_device_action" and row["idempotency_key"]:
                _hold(connection, "device_action", row["idempotency_key"], revision)
            if row["backend_task_uuid"] and row["invocation_key"]:
                _hold(connection, "submission", digest([row["backend_task_uuid"], row["invocation_key"]]), revision)
    if table_exists(connection, "center_invocation_inbox"):
        for row in connection.execute("SELECT invocation_uuid FROM center_invocation_inbox").fetchall():
            _hold(connection, "invocation", row["invocation_uuid"], revision)
    if table_exists(connection, "center_boundary_outbox"):
        for row in connection.execute("SELECT invocation_uuid FROM center_boundary_outbox").fetchall():
            _hold(connection, "invocation", row["invocation_uuid"], revision)
    if table_exists(connection, "center_command_inbox"):
        for row in connection.execute("SELECT command_uuid FROM center_command_inbox").fetchall():
            _hold(connection, "command", row["command_uuid"], revision)


def command_held(connection: Connection, command: dict[str, Any]) -> bool:
    if held(connection, "command", command["command_uuid"]):
        return True
    if table_exists(connection, "center_recovery_baseline"):
        high = connection.execute("SELECT MAX(command_high_watermark) FROM center_recovery_baseline").fetchone()[0]
        if high is not None and command["command_sequence"] <= high:
            return True
    invocation = command["payload"].get("station_invocation_uuid")
    return bool(invocation and held(connection, "invocation", invocation))


def review(connection: Connection) -> dict[str, Any]:
    """在写事务中逐行生成核实摘要；不输出原任务输入或凭据内容。"""
    if not connection.in_transaction:
        raise EvidenceOnlyError("恢复核实必须使用写事务")
    tables = sorted(name for name in table_names(connection) if not name.startswith("sqlite_"))
    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'
    if isinstance(connection, PostgreSQLDatabase):
        connection.execute("LOCK TABLE " + ",".join(quote(name) for name in tables) + " IN SHARE MODE NOWAIT")
    hashed, counts = hashlib.sha256(), {}
    for table in tables:
        columns = [row["name"] for row in connection.execute(f"PRAGMA table_info({quote(table)})")]
        hashed.update(json.dumps([table, columns], ensure_ascii=False).encode())
        expressions = ",".join(f"CAST({quote(name)} AS TEXT)" for name in columns)
        ordering = ",".join(str(i) for i in range(1, len(columns) + 1))
        count = 0
        for row in connection.execute(f"SELECT {expressions} FROM {quote(table)} ORDER BY {ordering}"):
            hashed.update(json.dumps(list(row), ensure_ascii=False).encode())
            hashed.update(b"\n")
            count += 1
        counts[table] = count
    return {"status": status(connection), "evidence_digest": "sha256:" + hashed.hexdigest(), "record_counts": counts}


def resume(connection: Connection, request: dict[str, Any], *, checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    """checkpoint 必须由调用方先用原 mTLS 会话从中心读取，不能接受用户上传的正文。"""
    fields = {"expected_revision", "expected_evidence_digest", "reviewed_by", "reason", "evidence_refs", "previous_work", "checkpoint_uuid"}
    if (not isinstance(request, dict) or set(request) != fields or request["previous_work"] != "keep_isolated"
            or type(request["expected_revision"]) is not int
            or not isinstance(request["reviewed_by"], str) or not request["reviewed_by"].strip() or len(request["reviewed_by"]) > 200
            or not isinstance(request["reason"], str) or not request["reason"].strip() or len(request["reason"].encode()) > 2000
            or not isinstance(request["evidence_refs"], list) or not 1 <= len(request["evidence_refs"]) <= 32
            or any(not isinstance(item, str) or not item.strip() or len(item.encode()) > 2048 for item in request["evidence_refs"])):
        raise EvidenceOnlyError("退出须提供核实人、证据引用、当前快照及保留旧执行隔离的决定")
    current = review(connection)
    state = current["status"]
    if (state["mode"] != "evidence_only" or request["expected_revision"] != state["revision"]
            or request["expected_evidence_digest"] != current["evidence_digest"]):
        raise EvidenceOnlyError("核实快照已变化，请重新检查")
    if table_exists(connection, "center_runtime_state"):
        from unilabos.center_protocol.recovery_checkpoint import validate_checkpoint

        runtime = connection.execute("SELECT * FROM center_runtime_state WHERE singleton='runtime'").fetchone()
        if runtime is None:
            raise EvidenceOnlyError("原实例与纪元账本缺失，不能恢复新执行资格")
        checkpoint = validate_checkpoint(checkpoint, checkpoint_uuid=request["checkpoint_uuid"],
                                         station_uuid=runtime["station_uuid"], instance_uuid=runtime["instance_uuid"],
                                         event_epoch_uuid=runtime["event_epoch_uuid"], recovery_uuid=state["recovery_uuid"])
        command_high = connection.execute("SELECT MAX(command_sequence) FROM center_command_inbox").fetchone()[0] or 0
        baseline_high = connection.execute("SELECT MAX(command_high_watermark) FROM center_recovery_baseline").fetchone()[0] or 0
        if checkpoint["command_high_watermark"] < max(command_high, baseline_high):
            raise EvidenceOnlyError("中心命令高水位落后于本地事实，须先恢复中心命令账本")
        high = runtime["last_event_sequence"]
        if checkpoint["event_high_watermark"] != high:
            raise EvidenceOnlyError("中心与本地原事件高水位不一致，须先恢复缺失证据")
        sequences = connection.execute("""SELECT event_sequence FROM center_boundary_outbox WHERE event_epoch_uuid=?
            UNION SELECT event_sequence FROM center_handoff_outbox WHERE event_epoch_uuid=? ORDER BY event_sequence""", (runtime["event_epoch_uuid"], runtime["event_epoch_uuid"])).fetchall()
        if len(sequences) != high or any(row[0] != i for i, row in enumerate(sequences, 1)):
            raise EvidenceOnlyError("原事件仍有缺口，不能恢复新执行资格")
        verified = connection.execute("""SELECT COUNT(*) FROM center_event_history_recovery
            WHERE event_epoch_uuid=? AND recovery_revision=? AND event_sequence BETWEEN 1 AND ?
              AND state IN ('recovered','retained_needs_review')""", (runtime["event_epoch_uuid"], state["revision"], high)).fetchone()[0]
        if verified != high:
            raise EvidenceOnlyError("本轮尚未逐项核对全部原事件的精确回执")
        for item in checkpoint["invocations"]:
            _hold(connection, "invocation", item["invocation_uuid"], state["revision"])
            _hold(connection, "submission", digest([item["global_task_uuid"], item["invocation_key"]]), state["revision"])
        connection.execute("INSERT INTO center_recovery_baseline VALUES (?,?,?) ON CONFLICT DO NOTHING",
                           (checkpoint["checkpoint_uuid"], json.dumps(checkpoint, ensure_ascii=False), checkpoint["command_high_watermark"]))
    elif checkpoint is not None or request["checkpoint_uuid"] is not None:
        raise EvidenceOnlyError("没有原中心账本，不能导入其他实例的恢复基线")
    else:
        from unilabos.persistence.center_schema import TABLES

        if any(table_exists(connection, name) for name in TABLES):
            raise EvidenceOnlyError("中心相关账本存在但原实例账本缺失，不能按独立工站恢复执行")
    _hold_existing(connection, state["revision"])
    if table_exists(connection, "center_runtime_state"):
        from unilabos.persistence.center_epochs import rotate_after_review

        rotate_after_review(connection, state["revision"])
    revision, changed_at = state["revision"] + 1, time_ns() // 1_000_000
    connection.execute("UPDATE center_recovery_mode SET mode='normal',revision=?,reason=?,changed_at_ms=? WHERE singleton=1",
                       (revision, request["reason"], changed_at))
    connection.execute("INSERT INTO center_recovery_mode_history(revision,mode,reason,changed_at_ms,verification_json) VALUES (?,'normal',?,?,?)",
                       (revision, request["reason"], changed_at, json.dumps(request, ensure_ascii=False)))
    return status(connection)
