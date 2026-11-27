"""中心收件箱、固定事件及纪元的持久事实；与工作流运行事实同库。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from unilabos.app.center_client.client import CenterClientError, ControlSession, TaskPackage
from unilabos.app.center_client.digest import DigestError, canonical_json
from unilabos.persistence import center_events, recovery_mode
from unilabos.persistence.center_schema import TABLES, CenterSchemaError, ensure_center_schema
from unilabos.persistence.database import open_database

RECOVERY_BATCH_SIZE = 100


def _json(value: Any) -> str:
    return canonical_json(value).decode("utf-8")


class DurableCenterStore:
    """每个实例仅有一个轮询者；所有写入先提交，再允许网络确认。"""

    def __init__(self, path: str | Path, *, station_uuid: str, instance_uuid: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.station_uuid, self.instance_uuid = station_uuid, instance_uuid
        self._transport_pickup_cursor = ""
        self._db = open_database(str(path), "workflow", create_parent=True, private=True)
        self._db.row_factory = sqlite3.Row
        try:
            with self._db.transaction():
                try:
                    epoch = ensure_center_schema(self._db, station_uuid=station_uuid, instance_uuid=instance_uuid,
                                                 allow_event_gaps=recovery_mode.evidence_only(self._db))
                except CenterSchemaError as error:
                    raise CenterClientError(str(error)) from error
                assert epoch is not None
                self.event_epoch_uuid = epoch
                # 升级后重新处理旧客户端已收到、但因不认识取消类型而隔离的命令。
                # 保留原身份和 ACK；灾备/调用隔离仍由运行时门禁裁定。
                self._db.execute(
                    """UPDATE center_command_inbox SET handled=0,error=NULL
                       WHERE handled=1 AND error=? AND json_extract(envelope_json,'$.command.type')='task.cancel'""",
                    ("当前运行时只支持 task.available；需要人工处理",),
                )
        except BaseException:
            self._db.close()
            raise

    def recovery_status(self) -> dict[str, Any]:
        with self._lock:
            return recovery_mode.status(self._db)

    def require_execution(self) -> None:
        with self._lock:
            recovery_mode.require_execution(self._db)

    def capacity_backlog(self) -> dict[str, Any]:
        """只返回聚合数，不把整个历史账本加载进内存；保留中的永久错误仍计入积压。"""
        from unilabos.persistence.database import PostgreSQLDatabase
        from unilabos.persistence.inspection import table_exists

        size = "OCTET_LENGTH(event_json)" if isinstance(self._db, PostgreSQLDatabase) else "LENGTH(CAST(event_json AS BLOB))"
        occurred = "json_extract(event_json,'$.occurred_at')"
        # 规范允许秒或毫秒两种形式；补齐秒精度后再求最早值，避免同秒字符串次序颠倒。
        oldest_time = f"CASE WHEN LENGTH({occurred})=20 THEN SUBSTR({occurred},1,19)||'.000Z' ELSE {occurred} END"
        with self._lock:
            # 灾备退出后旧调用仍永久隔离，只统计其真实积压，不再为禁止产生的新结果预留。
            executable = ""
            if table_exists(self._db, "center_recovery_holds"):
                executable = " AND NOT EXISTS (SELECT 1 FROM center_recovery_holds h WHERE h.subject_kind='invocation' AND h.subject_uuid=center_invocation_inbox.invocation_uuid)"
            rows = [self._db.execute(
                f"SELECT COUNT(*),COALESCE(SUM({size}),0),MIN({oldest_time}) FROM {table} WHERE {condition}"
            ).fetchone() for table, condition in (
                ("center_boundary_outbox", "acknowledged=0 AND application IS NULL"),
                ("center_handoff_outbox", "ack_json IS NULL"),
                ("center_preparation_outbox", "ack_json IS NULL"),
            )]
            unfinished = self._db.execute(
                "SELECT COUNT(*) FROM center_invocation_inbox WHERE terminal_recorded=0 AND (claim_confirmed=1 OR local_task_uuid IS NOT NULL)"
                " AND NOT EXISTS (SELECT 1 FROM center_outcome_evidence e WHERE e.invocation_uuid=center_invocation_inbox.invocation_uuid AND json_extract(e.cancel_settlement_json,'$.release_receipt') IS NOT NULL)" + executable
            ).fetchone()[0]
            holds = self._db.execute("SELECT COUNT(*) FROM center_request_holds WHERE active=1").fetchone()[0]
        oldest = min((row[2] for row in rows if row[2] is not None), default=None)
        return {"pending_events": sum(row[0] for row in rows), "pending_bytes": sum(row[1] for row in rows),
                "boundary_pending_events": rows[0][0], "handoff_pending_events": rows[1][0],
                "preparation_pending_events": rows[2][0],
                "oldest_event_occurred_at": oldest, "unfinished_claims": unfinished,
                "held_requests": holds, "batch_size": RECOVERY_BATCH_SIZE}

    def workload_diagnostics(self) -> dict[str, Any]:
        from unilabos.app.center_client.diagnostics import workload_snapshot

        with self._lock:
            return workload_snapshot(self._db)

    def request_hold(self, request_digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM center_request_holds WHERE request_digest=? AND active=1",
                                   (request_digest,)).fetchone()
            return dict(row) if row is not None else None

    def event_request_held(self, event_uuid: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM center_request_holds WHERE event_uuid=? AND active=1",
                                    (event_uuid,)).fetchone() is not None

    def request_holds(self, *, after_digest: str = "") -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM center_request_holds WHERE active=1 AND request_digest>? ORDER BY request_digest LIMIT ?",
                (after_digest, RECOVERY_BATCH_SIZE),
            ).fetchall()]

    def hold_request(self, request_digest: str, method: str, path: str,
                     event_uuid: str | None, status_code: int, error_code: str | None) -> None:
        from unilabos.app.center_client.digest import is_digest

        if not is_digest(request_digest) or method not in {"GET", "PUT", "POST", "DELETE", "PATCH"} or len(path) > 2048:
            raise CenterClientError("暂停重传记录的请求身份无效")
        if status_code not in {400, 413, 415, 422, 431}:
            raise CenterClientError("只允许持久暂停不可原样修复的请求错误")
        if event_uuid is not None:
            try:
                if str(UUID(event_uuid)) != event_uuid:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                event_uuid = None
        if not isinstance(error_code, str) or not 1 <= len(error_code) <= 100 or not error_code.isascii():
            error_code = None
        now = int(time.time() * 1000)
        with self._lock, self._db.transaction():
            self._db.execute(
                """INSERT INTO center_request_holds(request_digest,method,path,event_uuid,status_code,error_code,first_failed_at_ms,last_failed_at_ms)
                   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(request_digest) DO UPDATE SET
                   active=1,status_code=excluded.status_code,error_code=excluded.error_code,
                   last_failed_at_ms=excluded.last_failed_at_ms,attempts=center_request_holds.attempts+1""",
                (request_digest, method, path, event_uuid, status_code, error_code, now, now),
            )

    def request_retry(self, request_digest: str, *, expected_attempts: int, reason: str) -> None:
        """核实当前拒绝后仅恢复原请求；不确认事件、不改正文、不生成新身份。"""
        if type(expected_attempts) is not int or expected_attempts < 1 or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise CenterClientError("重传核实须包含当前拒绝次数及 1 至 500 字符理由")
        with self._lock, self._db.transaction():
            changed = self._db.execute(
                """UPDATE center_request_holds SET active=0,retry_requested_at_ms=?,retry_reason=?
                   WHERE request_digest=? AND attempts=? AND active=1""",
                (int(time.time() * 1000), reason.strip(), request_digest, expected_attempts),
            ).rowcount
            if changed != 1:
                raise CenterClientError("暂停记录已变化，须重新读取当前拒绝后核实")

    def invocation_held(self, invocation_uuid: str) -> bool:
        with self._lock:
            return recovery_mode.held(self._db, "invocation", invocation_uuid)

    def command_held(self, envelope: dict[str, Any]) -> bool:
        with self._lock:
            return recovery_mode.command_held(self._db, envelope["command"])

    def session(self) -> ControlSession | None:
        with self._lock:
            row = self._db.execute("SELECT session_json FROM center_runtime_state").fetchone()
            return ControlSession(**json.loads(row[0])) if row[0] else None

    def catalog_upload(self, snapshot: dict[str, Any], server_receipt: dict[str, Any] | None,
                       *, force: bool = False) -> dict[str, Any] | None:
        """在网络前提交目录身份及完整正文；未确认版本必须原样重试。"""
        from unilabos.app.center_client.catalog import make_upload, validate_receipt
        from unilabos.app.center_client.digest import digest

        server_receipt = validate_receipt(server_receipt, self.station_uuid)
        content_digest = digest(snapshot)
        with self._lock, self._db.transaction():
            row = self._db.execute("SELECT * FROM center_catalog_state WHERE singleton='catalog'").fetchone()
            if row is not None:
                upload = json.loads(row["upload_json"])
                receipt = upload["receipt"]
                if server_receipt is not None:
                    if (server_receipt["catalog_epoch_uuid"] != row["catalog_epoch_uuid"]
                            or server_receipt["catalog_sequence"] > row["catalog_sequence"]
                            or (server_receipt["catalog_sequence"] == row["catalog_sequence"]
                                and server_receipt != receipt)):
                        raise CenterClientError("中心目录已超出本地持久身份，禁止覆盖或更换纪元")
                elif row["acknowledged"]:
                    raise CenterClientError("中心丢失已确认目录，必须核实恢复，不能自动重建纪元")
                if server_receipt == receipt:
                    self._db.execute("UPDATE center_catalog_state SET acknowledged=1 WHERE singleton='catalog'")
                    acknowledged = True
                else:
                    acknowledged = bool(row["acknowledged"])
                if not acknowledged:
                    return upload
                if server_receipt != receipt:
                    raise CenterClientError("中心目录回退，禁止从旧游标继续上传")
                if row["content_digest"] == content_digest:
                    return upload if force else None
                epoch, sequence = row["catalog_epoch_uuid"], row["catalog_sequence"] + 1
            else:
                # 本地目录状态丢失时只能沿用中心已确认纪元和高水位；中心仍验证
                # 新目录必须明确包含旧活动项或显式下架，不能拿缺失记录当删除。
                epoch = server_receipt["catalog_epoch_uuid"] if server_receipt else str(uuid4())
                sequence = server_receipt["catalog_sequence"] + 1 if server_receipt else 1
            if sequence > 2**53 - 1:
                raise CenterClientError("目录序号已耗尽，禁止重新编号")
            upload = make_upload(snapshot, station_uuid=self.station_uuid, epoch=epoch,
                                 sequence=sequence, version=str(uuid4()))
            self._db.execute(
                """INSERT INTO center_catalog_state(singleton,catalog_epoch_uuid,catalog_sequence,
                       content_digest,upload_json,acknowledged) VALUES ('catalog',?,?,?,?,0)
                   ON CONFLICT(singleton) DO UPDATE SET catalog_epoch_uuid=excluded.catalog_epoch_uuid,
                       catalog_sequence=excluded.catalog_sequence,content_digest=excluded.content_digest,
                       upload_json=excluded.upload_json,acknowledged=0""",
                (epoch, sequence, content_digest, json.dumps(upload, ensure_ascii=False, allow_nan=False)),
            )
            return upload

    def pending_catalog_upload(self) -> dict[str, Any] | None:
        """未确认版本优先恢复；新源码转换失败不能阻断旧冻结目录重传。"""
        with self._lock:
            row = self._db.execute(
                "SELECT upload_json FROM center_catalog_state WHERE singleton='catalog' AND acknowledged=0"
            ).fetchone()
            return None if row is None else json.loads(row[0])

    def remember_reconciliation(self, invocation_uuid: str, body: dict[str, Any],
                                *, package: dict[str, Any] | None = None) -> str:
        """网络前保存完整核实请求和幂等键；后续发送只按返回身份读取。"""
        from unilabos.center_protocol.reconciliation import validate_request
        from unilabos.center_protocol.task_package import validate_task_package

        if str(UUID(invocation_uuid)) != invocation_uuid:
            raise CenterClientError("核实调用 UUID 不规范")
        body = validate_request(body)
        local_task = body["resolution"].get("local_task_uuid")
        if body["resolution"]["kind"] == "resume_original_task":
            package = validate_task_package(package)
            if (package["payload"]["station_invocation_uuid"] != invocation_uuid
                    or package["payload"]["station_uuid"] != self.station_uuid
                    or package["payload_digest"] != body["resolution"]["payload_digest"]
                    or body["resolution"]["event_epoch_uuid"] != self.event_epoch_uuid):
                raise CenterClientError("恢复核实的冻结包或事件纪元不匹配")
        elif package is not None:
            raise CenterClientError("只有恢复现存 Task 才保存冻结任务包")
        key = str(uuid4())
        with self._lock, self._db.transaction():
            self._db.execute(
                """INSERT INTO center_reconciliation_request(request_uuid,invocation_uuid,
                   station_uuid,instance_uuid,event_epoch_uuid,body_json,package_json,local_task_uuid)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (key, invocation_uuid, self.station_uuid, self.instance_uuid, self.event_epoch_uuid,
                 json.dumps(body, ensure_ascii=False, allow_nan=False),
                 None if package is None else json.dumps(package, ensure_ascii=False, allow_nan=False), local_task),
            )
        return key

    def reconciliation(self, request_uuid: str) -> dict[str, Any]:
        """读取持久核实工作单；此读取不确认结果、不改变执行状态。"""
        with self._lock:
            row = self._db.execute("SELECT * FROM center_reconciliation_request WHERE request_uuid=?", (request_uuid,)).fetchone()
            if row is None:
                raise CenterClientError("核实请求不存在")
            result = dict(row)
            for field in ("body_json", "package_json", "receipt_json"):
                raw = result.pop(field)
                result[field[:-5]] = json.loads(raw) if raw is not None else None
            return result

    def mark_reconciliation_attempted(self, request_uuid: str) -> None:
        with self._lock, self._db.transaction():
            self._db.execute("UPDATE center_reconciliation_request SET attempted=1 WHERE request_uuid=?", (request_uuid,))

    def save_reconciliation_receipt(self, request_uuid: str, receipt: dict[str, Any]) -> None:
        from unilabos.center_protocol.reconciliation import validate_receipt

        with self._lock, self._db.transaction():
            row = self._db.execute("SELECT * FROM center_reconciliation_request WHERE request_uuid=?", (request_uuid,)).fetchone()
            if row is None:
                raise CenterClientError("核实确认没有对应的持久请求")
            body = json.loads(row["body_json"])
            receipt = validate_receipt(receipt, row["invocation_uuid"], body, actor_instance_uuid=row["instance_uuid"])
            snapshot = receipt["snapshot"]
            if (snapshot["station_uuid"] != row["station_uuid"]
                    or (body["resolution"]["kind"] == "resume_original_task"
                        and snapshot["state"]["owner_instance_uuid"] != row["instance_uuid"])):
                raise CenterClientError("核实确认不属于原工站或原 owner")
            if body["resolution"]["kind"] == "resume_original_task":
                package = json.loads(row["package_json"])
                if any(snapshot[name] != package["payload"][name]
                       for name in ("station_invocation_uuid", "global_task_uuid", "station_uuid", "workflow_uuid")):
                    raise CenterClientError("恢复回执与原冻结任务包的永久身份不匹配")
            if row["receipt_json"] is not None and canonical_json(json.loads(row["receipt_json"])) != canonical_json(receipt):
                raise CenterClientError("同一核实请求的精确回执发生漂移")
            self._db.execute(
                "UPDATE center_reconciliation_request SET receipt_json=?,reconciliation_uuid=?,applied=? WHERE request_uuid=?",
                (json.dumps(receipt, ensure_ascii=False, allow_nan=False), receipt["reconciliation_uuid"],
                 int(body["resolution"]["kind"] != "resume_original_task"), request_uuid),
            )

    def acknowledge_catalog(self, receipt: dict[str, Any]) -> None:
        """只确认与冻结 manifest 完全一致的提交回执。"""
        with self._lock, self._db.transaction():
            row = self._db.execute("SELECT upload_json FROM center_catalog_state WHERE singleton='catalog'").fetchone()
            if row is None or json.loads(row[0])["receipt"] != receipt:
                raise CenterClientError("目录提交确认与本地冻结版本不匹配")
            self._db.execute("UPDATE center_catalog_state SET acknowledged=1 WHERE singleton='catalog'")

    def save_session(self, session: ControlSession | None) -> None:
        if session is not None:
            with self._lock, self._db.transaction():
                self._db.execute("UPDATE center_runtime_state SET session_json=?", (_json(asdict(session)),))

    def cursor(self) -> int:
        with self._lock:
            return int(self._db.execute(
                "SELECT COALESCE(MIN(CASE WHEN acknowledged=0 THEN command_sequence END)-1,MAX(command_sequence),0) FROM center_command_inbox"
            ).fetchone()[0])

    def receive(self, envelope: dict[str, Any]) -> None:
        command = envelope["command"]
        with self._lock, self._db.transaction():
            previous = self._db.execute(
                "SELECT * FROM center_command_inbox WHERE command_uuid=? OR command_sequence=?",
                (command["command_uuid"], command["command_sequence"]),
            ).fetchone()
            if previous is not None:
                old = json.loads(previous["envelope_json"])
                if (old["command"] != command or old["command_digest"] != envelope["command_digest"]
                        or envelope["disposition_revision"] < old["disposition_revision"]
                        or (envelope["disposition_revision"] == old["disposition_revision"]
                            and envelope["disposition"] != old["disposition"])
                        or (old["disposition"] == "canceled" and envelope["disposition"] != "canceled")
                        or (old["disposition"] == "recovery_held" and envelope["disposition"] == "active")):
                    raise CenterClientError("同一命令身份内容漂移或取消墓碑回退")
                self._db.execute("UPDATE center_command_inbox SET envelope_json=? WHERE command_uuid=?",
                                 (_json(envelope), command["command_uuid"]))
                return
            expected = int(self._db.execute(
                "SELECT COALESCE(MAX(command_sequence),0)+1 FROM center_command_inbox"
            ).fetchone()[0])
            if command["command_sequence"] != expected:
                raise CenterClientError("命令序列不连续，不能跳过缺失记录")
            self._db.execute(
                """INSERT INTO center_command_inbox(command_uuid,command_sequence,command_digest,envelope_json)
                   VALUES (?,?,?,?)""",
                (command["command_uuid"], command["command_sequence"], envelope["command_digest"], _json(envelope)),
            )

    def commands(self, *, unacknowledged: bool = False, after_sequence: int = 0,
                 acknowledged_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            condition = "acknowledged=0" if unacknowledged else "handled=0"
            if acknowledged_only:
                condition += " AND acknowledged=1"
            rows = self._db.execute(
                f"SELECT envelope_json FROM center_command_inbox WHERE {condition} AND command_sequence>? ORDER BY command_sequence LIMIT ?",
                (after_sequence, RECOVERY_BATCH_SIZE),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def recent_handoff_ids(self) -> list[str]:
        """从持久通知读取近期交接身份，包括已 ACK 的源端待放行通知。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT envelope_json FROM center_command_inbox ORDER BY command_sequence DESC LIMIT 1000"
            ).fetchall()
        identities = []
        for row in rows:
            command = json.loads(row[0])["command"]
            if command["type"] in {"handoff.available", "handoff.changed"}:
                identity = command["payload"]["handoff_uuid"]
                if identity not in identities:
                    identities.append(identity)
        return identities[:100]

    def has_command(self, command_uuid: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM center_command_inbox WHERE command_uuid=?", (command_uuid,)).fetchone() is not None

    def acknowledge_command(self, command_uuid: str) -> None:
        with self._lock, self._db.transaction():
            self._db.execute("UPDATE center_command_inbox SET acknowledged=1 WHERE command_uuid=?", (command_uuid,))

    def finish_command(self, command_uuid: str, *, error: str | None = None) -> None:
        with self._lock, self._db.transaction():
            self._db.execute("UPDATE center_command_inbox SET handled=1,error=? WHERE command_uuid=?", (error, command_uuid))

    def local_recovery_states(self) -> list[dict[str, Any]]:
        """计数复用原命令的 error 记录；重启、重复通知不能重置同一调用的上限。"""
        with self._lock:
            rows = self._db.execute("SELECT error FROM center_command_inbox WHERE error LIKE 'LOCAL_RECOVERY:%'").fetchall()
            states: dict[str, dict[str, Any]] = {}
            for row in rows:
                try:
                    state = json.loads(row[0][len("LOCAL_RECOVERY:"):])
                    identity = state["invocation_uuid"]
                    if (set(state) != {"invocation_uuid", "command_uuid", "attempts", "limit", "status", "last_error_code", "next_attempt_at_ms"}
                            or str(UUID(identity)) != identity or str(UUID(state["command_uuid"])) != state["command_uuid"]
                            or type(state["attempts"]) is not int
                            or type(state["limit"]) is not int or not 1 <= state["attempts"] <= state["limit"] <= 100
                            or type(state["next_attempt_at_ms"]) is not int
                            or state["status"] not in {"retrying", "exhausted", "needs_review", "failed"}):
                        raise ValueError("本地恢复状态无效")
                except (ValueError, TypeError, KeyError) as error:
                    raise CenterClientError("本地恢复计数损坏，禁止自动重新尝试") from error
                old = states.get(identity)
                if old is None or state["attempts"] >= old["attempts"]:
                    states[identity] = state
            return list(states.values())

    def local_recovery_state(self, invocation_uuid: str) -> dict[str, Any] | None:
        return next((x for x in self.local_recovery_states() if x["invocation_uuid"] == invocation_uuid), None)

    def record_local_failure(self, invocation_uuid: str, *, command_uuid: str | None, limit: int,
                             max_backoff_seconds: float, code: str) -> dict[str, Any]:
        """只在实际失败后计数；原通知是持久恢复责任，不增加表或改动冻结任务包。"""
        with self._lock, self._db.transaction():
            # 与原事件写入共用现有单行锁，两个连接不能丢失持久次数更新。
            self._db.execute("UPDATE center_runtime_state SET last_event_sequence=last_event_sequence WHERE singleton='runtime'")
            state = self.local_recovery_state(invocation_uuid)
            if state is not None and state["status"] != "retrying":
                return state
            owner = None
            for row in self._db.execute("SELECT command_uuid,envelope_json FROM center_command_inbox ORDER BY command_sequence").fetchall():
                command = json.loads(row["envelope_json"])["command"]
                if command["type"] == "task.available" and command["payload"].get("station_invocation_uuid") == invocation_uuid:
                    if command_uuid is None or row["command_uuid"] == command_uuid:
                        owner = row["command_uuid"]
                        break
            if owner is None:
                raise CenterClientError("本地恢复缺少原任务通知，需人工核实")
            limit = state["limit"] if state else limit
            attempts = min((state["attempts"] if state else 0) + 1, limit)
            state = {"invocation_uuid": invocation_uuid, "command_uuid": owner, "attempts": attempts, "limit": limit,
                     "status": "exhausted" if attempts == limit else "retrying", "last_error_code": code,
                     "next_attempt_at_ms": int(time.time() * 1000) + int(min(2 ** min(attempts, 10), max_backoff_seconds) * 1000)}
            self._db.execute("UPDATE center_command_inbox SET error=? WHERE command_uuid=?", ("LOCAL_RECOVERY:" + _json(state), owner))
            return state

    def finish_local_recovery(self, state: dict[str, Any], *, failed: bool) -> dict[str, Any]:
        """停止自动恢复；原执行事实与待补报事件保持不变。"""
        with self._lock, self._db.transaction():
            state = dict(state, status="failed" if failed else "needs_review", next_attempt_at_ms=0)
            self._db.execute("UPDATE center_command_inbox SET handled=1,error=? WHERE command_uuid=?",
                             ("LOCAL_RECOVERY:" + _json(state), state["command_uuid"]))
            return state

    def resolve_local_recovery(self, invocation_uuid: str) -> None:
        """已有真实 Task 恢复成功后结束旧通知，避免同轮重复恢复。"""
        with self._lock, self._db.transaction():
            state = self.local_recovery_state(invocation_uuid)
            if state is not None and state["status"] == "retrying":
                for row in self._db.execute("SELECT command_uuid,error FROM center_command_inbox WHERE error LIKE 'LOCAL_RECOVERY:%'").fetchall():
                    if json.loads(row["error"][len("LOCAL_RECOVERY:"):])["invocation_uuid"] == invocation_uuid:
                        self._db.execute("UPDATE center_command_inbox SET handled=1,error=NULL WHERE command_uuid=?", (row["command_uuid"],))

    def recorded_task_uuid(self, invocation_uuid: str) -> str | None:
        """读取与 Task 创建同事务提交的原调用映射，不推测丢失的车辆接管。"""
        with self._lock:
            row = self._db.execute("SELECT local_task_uuid FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
            return row[0] if row else None

    def fence_transport_without_task(self, request: dict, allocation_uuid: str, snapshot: dict) -> None:
        """原调用经中心确认已撤销后，原子封禁迟到任务；不伪造 Task 或执行终态。"""
        from unilabos.center_protocol.reconciliation import validate_snapshot

        invocation = request["station_invocation_uuid"]
        snapshot = validate_snapshot(snapshot, invocation)
        state = snapshot["state"]
        if (snapshot["station_uuid"] != self.station_uuid
                or snapshot["global_task_uuid"] != request["global_task_uuid"]
                or state["dispatch_status"] not in {"canceled", "rejected", "skipped"}
                or state["execution_status"] not in {"not_started", "canceled", "failed"}
                or state["local_task_uuid"] is not None
                or state["owner_instance_uuid"] not in {None, self.instance_uuid}
                or state["attention_state"] != "clear"):
            raise CenterClientError("中心尚未证明原调用未建任务且已经撤销")
        with self._lock, self._db.transaction():
            inbox = self._db.execute("SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation,)).fetchone()
            if inbox is not None:
                package = TaskPackage.from_response(json.loads(inbox["package_json"]))
                if inbox["local_task_uuid"] is not None or package.payload_digest != snapshot["payload_digest"]:
                    raise CenterClientError("原调用已有真实任务或冻结包不一致，禁止按未建任务释放")
            old = self._db.execute("SELECT allocation_uuid FROM center_transport_revocation WHERE invocation_uuid=?", (invocation,)).fetchone()
            if old is not None and old[0] != allocation_uuid:
                raise CenterClientError("原调用已经绑定其他撤销占用")
            self._db.execute("INSERT INTO center_transport_revocation VALUES(?,?,?) ON CONFLICT DO NOTHING",
                             (invocation, allocation_uuid, json.dumps(snapshot)))

    def uncreated_terminal(self, invocation_uuid: str) -> bool:
        """无 Task 的终态禁止再次领取/创建；补报仍由原发件箱负责。"""
        with self._lock:
            if self._db.execute("SELECT 1 FROM center_transport_revocation WHERE invocation_uuid=?", (invocation_uuid,)).fetchone() is not None:
                return True
            row = self._db.execute("SELECT local_task_uuid,terminal_recorded FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
            if row is not None and row[0] is None and bool(row[1]):
                return True
            # 历史恢复可能只补回 outbox；缺失 inbox 不能成为再次执行的入口。
            terminal = self._db.execute("SELECT event_json FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=1", (invocation_uuid,)).fetchone()
            if terminal is None:
                return False
            event = json.loads(terminal[0])
            if event.get("event_type") != "admission_failed":
                return False
            from unilabos.center_protocol.events import validate_event
            validate_event(event)
            if (event["station_invocation_uuid"] != invocation_uuid or event["station_uuid"] != self.station_uuid
                    or event["instance_uuid"] != self.instance_uuid or (row is not None and row[0] is not None)):
                raise CenterClientError("受理失败原信封与本地调用身份冲突")
            return True

    def reject_submission(self, package: TaskPackage, command_uuid: str, *, code: str, message: str) -> bool:
        """在同一事务保存未建 Task 的终态和命令处置；不能覆盖并发创建的真实 Task。"""
        return self._reject_submission(package, command_uuid, code=code, message=message)

    def reject_canceled_before_creation(self, package: TaskPackage, command_uuid: str, snapshot: dict[str, Any]) -> bool:
        """只读核实已丢失回执的原领取后，沿可信取消记录未建任务终态；不授予执行权。"""
        return self._reject_submission(package, command_uuid, code="canceled_before_creation",
            message="中心已在创建本地任务前取消调用", canceled_snapshot=snapshot)

    def _reject_submission(self, package: TaskPackage, command_uuid: str, *, code: str, message: str,
                           canceled_snapshot: dict[str, Any] | None = None) -> bool:
        from unilabos.app.center_client.admission import public_reason
        from unilabos.center_protocol.events import utc_timestamp_from_ms
        from unilabos.persistence.inspection import table_exists
        message = public_reason(code, message)
        with self._lock, self._db.transaction():
            recovery_mode.require_execution(self._db, invocation_uuid=package.station_invocation_uuid)
            row = self._db.execute("SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (package.station_invocation_uuid,)).fetchone()
            if row is None or (not row["claim_confirmed"] and canceled_snapshot is None):
                raise CenterClientError("受理失败必须关联原已领取调用")
            if canceled_snapshot is not None:
                from unilabos.center_protocol.reconciliation import validate_snapshot
                snapshot = validate_snapshot(canceled_snapshot, package.station_invocation_uuid)
                saved = self._db.execute("SELECT envelope_json FROM center_command_inbox WHERE command_uuid=?",
                    (command_uuid,)).fetchone()
                command = json.loads(saved[0])["command"] if saved is not None else {}
                if (command.get("type") != "task.cancel" or command.get("station_uuid") != self.station_uuid
                        or command.get("payload", {}).get("station_invocation_uuid") != package.station_invocation_uuid
                        or command["payload"].get("global_task_uuid") != package.global_task_uuid
                        or snapshot["station_uuid"] != self.station_uuid or snapshot["global_task_uuid"] != package.global_task_uuid
                        or snapshot["workflow_uuid"] != package.raw["workflow_uuid"] or snapshot["payload_digest"] != package.payload_digest
                        or snapshot["recovery_blocked"] or snapshot["state"]["owner_instance_uuid"] != self.instance_uuid
                        or snapshot["state"]["local_task_uuid"] is not None or snapshot["state"]["dispatch_status"] != "claimed"
                        or snapshot["state"]["execution_status"] != "not_started"):
                    raise CenterClientError("创建前取消缺少原领取、当前核实快照或可信取消命令")
            body = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
            if canonical_json(json.loads(row["package_json"])) != canonical_json(body):
                raise CenterClientError("受理失败不能改写原冻结任务包")
            if row["local_task_uuid"] is not None:
                return False
            if table_exists(self._db, "workflow_task") and self._db.execute(
                "SELECT 1 FROM workflow_task WHERE backend_task_uuid=? AND invocation_key=?",
                (package.global_task_uuid, package.raw["invocation_key"]),
            ).fetchone() is not None:
                return False
            if canceled_snapshot is not None and not row["claim_confirmed"]:
                # 与终态同事务提交，恢复过程中没有重新执行或重新占用的窗口。
                self._db.execute("UPDATE center_invocation_inbox SET claim_confirmed=1 WHERE invocation_uuid=?",
                    (package.station_invocation_uuid,))
                self._db.execute("UPDATE center_material_input SET state='claimed' WHERE invocation_uuid=? AND state IN ('prepared','rejected')",
                    (package.station_invocation_uuid,))
            if not row["terminal_recorded"]:
                text = message.encode("utf-8")[:500].decode("utf-8", errors="ignore")
                center_events.append_event_in_transaction(self._db, package.station_invocation_uuid, terminal=True, body={
                    "event_uuid": str(uuid4()), "local_task_uuid": None, "event_type": "admission_failed",
                    "payload": {"task_payload_digest": package.payload_digest, "status": "failed",
                                "output": {"error_info": [{"code": code, "message": text}]}},
                    "occurred_at": utc_timestamp_from_ms(int(time.time() * 1000)),
                })
            self._db.execute("UPDATE center_command_inbox SET handled=1,error=? WHERE command_uuid=?", (code, command_uuid))
            return True

    def remember_package(self, package: TaskPackage) -> TaskPackage:
        """固定首次任务包并返回持久版本；后续提交不能采用重投数值类型覆盖原身份。"""
        body = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
        with self._lock, self._db.transaction():
            if self._db.execute("SELECT 1 FROM center_transport_revocation WHERE invocation_uuid=?", (package.station_invocation_uuid,)).fetchone() is not None:
                raise CenterClientError("原搬运调用已撤销，禁止迟到任务包重新执行")
            old = self._db.execute("SELECT package_json FROM center_invocation_inbox WHERE invocation_uuid=?",
                                   (package.station_invocation_uuid,)).fetchone()
            if old is not None:
                try:
                    historical = json.loads(old[0])
                    if canonical_json(historical) != canonical_json(body):
                        raise CenterClientError("同一调用的冻结任务包已变化")
                except (DigestError, ValueError, TypeError) as error:
                    # 旧 JCS 包可能丢失数值类型；不猜测或改写已关联 Task 的请求身份。
                    raise CenterClientError("历史任务包不能验证，保留原包等待显式恢复") from error
                return TaskPackage.from_response(historical)
            self._db.execute(
                """INSERT INTO center_invocation_inbox(invocation_uuid,package_json) VALUES (?,?)
                   ON CONFLICT(invocation_uuid) DO NOTHING""",
                (package.station_invocation_uuid, json.dumps(body, allow_nan=False, ensure_ascii=False)),
            )
            return package

    def package(self, invocation_uuid: str) -> TaskPackage | None:
        """读取指定调用的原冻结包，包含已完成调用；不领取或创建任务。"""
        with self._lock:
            row = self._db.execute("SELECT package_json FROM center_invocation_inbox WHERE invocation_uuid=?",
                                   (invocation_uuid,)).fetchone()
            return TaskPackage.from_response(json.loads(row[0])) if row is not None else None

    def packages(self, *, after_invocation_uuid: str = "") -> list[TaskPackage]:
        with self._lock:
            return [TaskPackage.from_response(json.loads(row[0])) for row in self._db.execute(
                """SELECT package_json FROM center_invocation_inbox WHERE terminal_recorded=0
                   AND (claim_confirmed=1 OR reconciliation_uuid IS NOT NULL) AND invocation_uuid>?
                   AND NOT EXISTS (SELECT 1 FROM center_outcome_evidence e WHERE e.invocation_uuid=center_invocation_inbox.invocation_uuid AND json_extract(e.cancel_settlement_json,'$.release_receipt') IS NOT NULL)
                   ORDER BY invocation_uuid LIMIT ?""", (after_invocation_uuid, RECOVERY_BATCH_SIZE),
            ).fetchall()]

    def claim_confirmed(self, invocation_uuid: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT claim_confirmed FROM center_invocation_inbox WHERE invocation_uuid=?",
                                   (invocation_uuid,)).fetchone()
            return row is not None and bool(row[0])

    def reconciled_existing_task(self, invocation_uuid: str, task_uuid: str) -> bool:
        """只给现存精确 Task 复用恢复证明；Task 缺失时仍须走受保护 claim。"""
        from unilabos.workflow.center_boundary import validate_reconciliation_binding

        with self._lock, self._db.transaction():
            row = self._db.execute("SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
            if row is None or row["reconciliation_uuid"] is None or row["local_task_uuid"] != task_uuid:
                return False
            runtime = self._db.execute("SELECT * FROM center_runtime_state WHERE singleton='runtime'").fetchone()
            validate_reconciliation_binding(self._db, inbox=row, package=json.loads(row["package_json"]), runtime=runtime)
            return True

    def confirm_claim(self, invocation_uuid: str) -> None:
        """只证明旧 owner 证据归属，不能作为新进程启动本地任务的控制许可。"""
        with self._lock, self._db.transaction():
            if self._db.execute("SELECT 1 FROM center_material_input_issue WHERE invocation_uuid=?",
                                (invocation_uuid,)).fetchone() is not None:
                raise CenterClientError("物料输入异常仍待核实，禁止确认新执行")
            inbox = self._db.execute("SELECT package_json FROM center_invocation_inbox WHERE invocation_uuid=?",
                                     (invocation_uuid,)).fetchone()
            if inbox is None:
                raise CenterClientError("领取确认缺少原冻结任务包")
            package = json.loads(inbox[0])
            from unilabos.center_protocol.material_source import requires_material_input
            if requires_material_input(package["payload"]):
                proof = self._db.execute("SELECT * FROM center_material_input WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
                if proof is None or proof["state"] not in {"prepared", "claimed"}:
                    raise CenterClientError("物料调用缺少可领取的持久库存占用")
                self._db.execute("UPDATE center_material_input SET state='claimed' WHERE invocation_uuid=?", (invocation_uuid,))
            self._db.execute("UPDATE center_invocation_inbox SET claim_confirmed=1 WHERE invocation_uuid=?",
                             (invocation_uuid,))

    def remember_material_input_issue(self, package: TaskPackage, reason: str) -> None:
        """先持久化首次真实失败，再上报中心；后续重试不能改写原原因。"""
        from unilabos.center_protocol.material_input import validate_issue_request

        body = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
        request = validate_issue_request({"task_payload_digest": package.payload_digest,
            "reason": reason.encode("utf-8")[:500].decode("utf-8", errors="ignore")})
        identity = package.station_invocation_uuid
        with self._lock, self._db.transaction():
            recovery_mode.require_execution(self._db, invocation_uuid=identity)
            row = self._db.execute("SELECT package_json FROM center_invocation_inbox WHERE invocation_uuid=?",
                                   (identity,)).fetchone()
            if row is None or canonical_json(json.loads(row[0])) != canonical_json(body):
                raise CenterClientError("物料输入异常缺少原冻结任务包")
            self._db.execute("INSERT INTO center_material_input_issue VALUES (?,?,NULL) ON CONFLICT(invocation_uuid) DO NOTHING",
                             (identity, _json(request)))

    def material_input_issue(self, invocation_uuid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM center_material_input_issue WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
            return ({"request": json.loads(row["request_json"]),
                     "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None}
                    if row is not None else None)

    def pending_material_input_issues(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"invocation_uuid": row["invocation_uuid"], "request": json.loads(row["request_json"])}
                    for row in self._db.execute("SELECT * FROM center_material_input_issue WHERE receipt_json IS NULL ORDER BY invocation_uuid").fetchall()]

    def acknowledge_material_input_issue(self, invocation_uuid: str, receipt: dict[str, Any]) -> None:
        from unilabos.center_protocol.material_input import validate_issue_receipt

        with self._lock, self._db.transaction():
            row = self._db.execute("SELECT * FROM center_material_input_issue WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
            if row is None:
                raise CenterClientError("缺少原物料输入异常正文")
            runtime = self._db.execute("SELECT instance_uuid FROM center_runtime_state WHERE singleton='runtime'").fetchone()
            validate_issue_receipt(receipt, invocation_uuid=invocation_uuid, instance_uuid=runtime[0],
                                   request=json.loads(row["request_json"]))
            if row["receipt_json"] is not None and row["receipt_json"] != _json(receipt):
                raise CenterClientError("中心输入异常原回执不能改写")
            self._db.execute("UPDATE center_material_input_issue SET receipt_json=? WHERE invocation_uuid=?",
                             (_json(receipt), invocation_uuid))

    def remember_material_input(self, package: TaskPackage, receipt: dict[str, Any]) -> None:
        """跨库只复制已落盘回执，不在工作流库伪造库存占用。"""
        from unilabos.center_protocol.material_input import validate_input_receipt

        body = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
        receipt = validate_input_receipt(receipt, body)
        identity = package.station_invocation_uuid
        with self._lock, self._db.transaction():
            recovery_mode.require_execution(self._db, invocation_uuid=identity)
            inbox = self._db.execute("SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (identity,)).fetchone()
            if inbox is None or canonical_json(json.loads(inbox["package_json"])) != canonical_json(body):
                raise CenterClientError("库存占用没有对应的原冻结任务包")
            old = self._db.execute("SELECT * FROM center_material_input WHERE invocation_uuid=?", (identity,)).fetchone()
            if old is not None:
                if old["receipt_json"] != _json(receipt) or old["state"] not in {"prepared", "claimed"}:
                    raise CenterClientError("原库存输入占用身份变化或已结束")
                return
            if inbox["claim_confirmed"] or inbox["local_task_uuid"] is not None:
                raise CenterClientError("已有执行事实不能补造新的库存输入占用")
            self._db.execute("INSERT INTO center_material_input VALUES (?,?,?,'prepared')",
                             (identity, receipt["local_task_uuid"], _json(receipt)))

    def material_input(self, invocation_uuid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM center_material_input WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
            return {"receipt": json.loads(row["receipt_json"]), "state": row["state"]} if row is not None else None

    def reject_unclaimed_material_input(self, invocation_uuid: str) -> None:
        """只封存从未创建 Task 的领取前意图；与确认领取、绑定 Task 共用写事务。"""
        from unilabos.persistence.inspection import table_exists

        with self._lock, self._db.transaction():
            row = self._db.execute(
                "SELECT h.*,i.claim_confirmed,i.local_task_uuid bound_task FROM center_material_input h "
                "JOIN center_invocation_inbox i USING(invocation_uuid) WHERE h.invocation_uuid=?", (invocation_uuid,),
            ).fetchone()
            if row is None or row["state"] != "prepared" or row["claim_confirmed"] or row["bound_task"] is not None:
                return
            if table_exists(self._db, "workflow_task") and self._db.execute(
                "SELECT 1 FROM workflow_task WHERE uuid=?", (row["local_task_uuid"],),
            ).fetchone() is not None:
                return
            self._db.execute("UPDATE center_material_input SET state='rejected' WHERE invocation_uuid=?", (invocation_uuid,))

    def releasable_material_inputs(self) -> list[dict[str, Any]]:
        """真实 Task 终态且原 outcome 已持久化之后才可释放；拒绝意图不曾创建 Task。"""
        from unilabos.persistence.inspection import table_exists

        with self._lock, self._db.transaction():
            rows = self._db.execute(
                "SELECT h.*,i.package_json,i.claim_confirmed,i.local_task_uuid bound_task,i.terminal_recorded "
                "FROM center_material_input h JOIN center_invocation_inbox i USING(invocation_uuid) "
                "WHERE h.state IN ('claimed','rejected') ORDER BY h.invocation_uuid",
            ).fetchall()
            result = []
            for row in rows:
                task = (self._db.execute("SELECT status FROM workflow_task WHERE uuid=?", (row["local_task_uuid"],)).fetchone()
                        if table_exists(self._db, "workflow_task") else None)
                ready = (row["state"] == "rejected" and not row["claim_confirmed"] and row["bound_task"] is None and task is None)
                # 未建 Task 的明确拒绝也要释放领取前已准备的原输入占用。
                terminal = self._db.execute("SELECT event_json FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=1", (row["invocation_uuid"],)).fetchone()
                rejected_without_task = (terminal is not None and json.loads(terminal[0]).get("event_type") == "admission_failed")
                ready = ready or (row["state"] == "claimed" and row["terminal_recorded"] and row["bound_task"] is None
                                  and task is None and rejected_without_task)
                ready = ready or (row["state"] == "claimed" and row["terminal_recorded"]
                                  and row["bound_task"] == row["local_task_uuid"] and task is not None
                                  and task["status"] in {"succeeded", "failed", "canceled", "timeout"})
                if ready:
                    result.append({"package": json.loads(row["package_json"]), "receipt": json.loads(row["receipt_json"])})
            return result

    def finish_material_input_release(self, receipt: dict[str, Any]) -> None:
        with self._lock, self._db.transaction():
            self._db.execute("UPDATE center_material_input SET state='released' WHERE invocation_uuid=? AND receipt_json=?",
                             (receipt["station_invocation_uuid"], _json(receipt)))

    def record_event(self, invocation_uuid: str, *, terminal: bool, body: dict[str, Any]) -> dict[str, Any]:
        """同一写事务占用阶段、递增序号并持久信封；阶段重放不消耗序号。"""
        with self._lock, self._db.transaction():
            try:
                return center_events.append_event_in_transaction(self._db, invocation_uuid, terminal=terminal, body=body)
            except center_events.CenterEventConflict as error:
                raise CenterClientError(str(error)) from error

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(row[0], json.loads(row[1])) for row in self._db.execute(
                """SELECT invocation_uuid,event_json FROM center_boundary_outbox
                   WHERE acknowledged=0 AND application IS NULL
                   AND NOT EXISTS (SELECT 1 FROM center_recovery_event_holds h WHERE h.event_uuid=center_boundary_outbox.event_uuid)
                   AND NOT EXISTS (SELECT 1 FROM center_request_holds h WHERE h.active=1 AND h.event_uuid=center_boundary_outbox.event_uuid)
                   ORDER BY event_sequence LIMIT ?""", (RECOVERY_BATCH_SIZE,),
            ).fetchall()]

    def capture_transport_pickups(self) -> list[dict[str, Any]]:
        """只把原 G 的明确成功装载结果写入共享事件账本，不触发设备动作。"""
        from unilabos.workflow.transport_pickup import capture_transport_pickups

        with self._lock, self._db.transaction():
            events, cursor = capture_transport_pickups(self._db, limit=RECOVERY_BATCH_SIZE,
                                                       after_invocation_uuid=self._transport_pickup_cursor)
        self._transport_pickup_cursor = cursor
        return events

    def remember_handoff_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        """将已持久库存事实幂等写入共享序号账本，事务不访问库存库或网络。"""
        with self._lock, self._db.transaction():
            try:
                return center_events.append_handoff_in_transaction(self._db, intent)
            except center_events.CenterEventConflict as error:
                raise CenterClientError(str(error)) from error

    def event_held(self, event_uuid: str) -> bool:
        """独立保管证明不等于 ACK；保留正文但不进入普通热重传。"""
        with self._lock:
            return self._db.execute("SELECT 1 FROM center_recovery_event_holds WHERE event_uuid=?", (event_uuid,)).fetchone() is not None

    def handoff_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(row[0]) for row in self._db.execute(
                """SELECT event_json FROM center_handoff_outbox WHERE ack_json IS NULL
                   AND NOT EXISTS (SELECT 1 FROM center_recovery_event_holds h WHERE h.event_uuid=center_handoff_outbox.event_uuid)
                   AND NOT EXISTS (SELECT 1 FROM center_request_holds h WHERE h.active=1 AND h.event_uuid=center_handoff_outbox.event_uuid)
                   ORDER BY event_sequence LIMIT ?""", (RECOVERY_BATCH_SIZE,),
            ).fetchall()]

    def handoff_ack(self, event_uuid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT ack_json FROM center_handoff_outbox WHERE event_uuid=?", (event_uuid,)).fetchone()
            return json.loads(row[0]) if row is not None and row[0] is not None else None

    def acknowledge_handoff_event(self, event_uuid: str, response: dict[str, Any]) -> None:
        """保存完整精确确认，库存桥可在崩溃后恢复消费同一回执。"""
        from unilabos.center_protocol.handoff import HandoffProtocolError, validate_handoff_ack

        with self._lock, self._db.transaction():
            row = self._db.execute("SELECT * FROM center_handoff_outbox WHERE event_uuid=?", (event_uuid,)).fetchone()
            if row is None:
                raise CenterClientError("交接确认没有对应持久事件")
            try:
                response = validate_handoff_ack(response, json.loads(row["event_json"]))
            except HandoffProtocolError as error:
                raise CenterClientError(str(error)) from error
            if row["ack_json"] is not None and canonical_json(json.loads(row["ack_json"])) != canonical_json(response):
                raise CenterClientError("同一交接事件的精确回执发生变化")
            self._db.execute("UPDATE center_handoff_outbox SET ack_json=? WHERE event_uuid=?",
                             (json.dumps(response, allow_nan=False, ensure_ascii=False), event_uuid))

    def acknowledge_event(self, event_uuid: str, application: str) -> None:
        """精确 ACK 后保存处理结论；冲突保留未成功状态但不再参与热重传。"""
        if application not in ("applied", "duplicate", "conflict"):
            raise CenterClientError("不能确认未知事件入账结果")
        with self._lock, self._db.transaction():
            self._db.execute(
                "UPDATE center_boundary_outbox SET acknowledged=?,application=? WHERE event_uuid=? AND application IS NULL",
                (int(application != "conflict"), application, event_uuid),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def preparation_event(self, *, event_uuid: str, request: dict[str, Any], fact: str,
                          binding: dict[str, Any] | None = None, decision_uuid: str | None = None, waiting_reason: str | None = None) -> dict[str, Any]:
        """为已提交车辆事实分配共享序号；重放绝不重新分配身份。"""
        from unilabos.center_protocol.digest import digest
        from unilabos.center_protocol.transport_preparation import validate_event
        with self._lock, self._db.transaction():
            prior = self._db.execute("SELECT event_json FROM center_preparation_outbox WHERE event_uuid=?", (event_uuid,)).fetchone()
            if prior is not None:
                event = validate_event(json.loads(prior[0]))
                if (event['request_uuid'] != request['request_uuid'] or event['generation'] != request['generation']
                        or event['fact'] != fact or event.get('binding') != binding
                        or event['condition_digest'] != request['condition_digest']
                        or event.get('decision_uuid') != decision_uuid or event.get('waiting_reason') != waiting_reason):
                    raise CenterClientError("车辆准备事件身份被不同事实重复使用")
                return event
            runtime = center_events._runtime_for_write(self._db)
            event = {'protocol_version': '1.0', 'event_uuid': event_uuid,
                     'station_uuid': self.station_uuid, 'instance_uuid': self.instance_uuid,
                     'event_epoch_uuid': self.event_epoch_uuid, 'event_sequence': runtime['last_event_sequence']+1,
                     'request_uuid': request['request_uuid'], 'generation': request['generation'],
                     'condition_digest': request['condition_digest'], 'fact': fact}
            if binding is not None:
                event['binding'] = binding
            if decision_uuid is not None:
                event['decision_uuid'] = decision_uuid
            if waiting_reason is not None:
                event['waiting_reason'] = waiting_reason
            event['event_digest'] = digest(event)
            validate_event(event)
            for table in ('center_boundary_outbox','center_handoff_outbox'):
                if self._db.execute(f'SELECT 1 FROM {table} WHERE event_uuid=?',(event_uuid,)).fetchone():
                    raise CenterClientError('车辆准备事件与其他事件共享身份冲突')
            self._db.execute('INSERT INTO center_preparation_outbox VALUES(?,?,?,?,?,NULL)',
                             (event_uuid,request['request_uuid'],event['event_sequence'],self.event_epoch_uuid,_json(event)))
            self._db.execute("UPDATE center_runtime_state SET last_event_sequence=? WHERE singleton='runtime'", (event['event_sequence'],))
            return event

    def latest_preparation_event(self, request_uuid: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT event_json FROM center_preparation_outbox WHERE request_uuid=? ORDER BY event_sequence DESC LIMIT 1", (request_uuid,)).fetchone()
            return json.loads(row[0]) if row else None

    def preparation_events(self) -> list[dict[str, Any]]:
        """按共享序号读取独立准备事件；关闭事实必须先于同车的新预占。"""
        with self._lock:
            return [json.loads(r[0]) for r in self._db.execute(
                """SELECT event_json FROM center_preparation_outbox WHERE ack_json IS NULL
                   AND NOT EXISTS (SELECT 1 FROM center_recovery_event_holds h WHERE h.event_uuid=center_preparation_outbox.event_uuid)
                   AND NOT EXISTS (SELECT 1 FROM center_request_holds h WHERE h.active=1 AND h.event_uuid=center_preparation_outbox.event_uuid)
                   ORDER BY event_sequence LIMIT 100""").fetchall()]

    def acknowledge_preparation(self, event: dict[str, Any], ack: dict[str, Any]) -> None:
        """精确确认原事件，不能用连续前缀跳过其他待发事实。"""
        from unilabos.center_protocol.events import validate_ack
        validate_ack(ack,event)
        with self._lock, self._db.transaction():
            row = self._db.execute('SELECT event_json,ack_json FROM center_preparation_outbox WHERE event_uuid=?',
                                   (event['event_uuid'],)).fetchone()
            if row is None or canonical_json(json.loads(row['event_json'])) != canonical_json(event):
                raise CenterClientError('车辆准备确认没有对应原事件')
            if row['ack_json'] is not None:
                original = json.loads(row['ack_json'])
                if (original['application'] == 'conflict') != (ack['application'] == 'conflict'):
                    raise CenterClientError('车辆准备原确认结果发生变化')
            self._db.execute('UPDATE center_preparation_outbox SET ack_json=? WHERE event_uuid=? AND ack_json IS NULL',
                             (_json(ack),event['event_uuid']))
