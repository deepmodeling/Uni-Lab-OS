"""显式历史恢复：只操作既有纪元事件，不连接 WorkflowService 或创建 Task/Job。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from unilabos.app.center_client.client import CenterClient, CenterClientError, ControlSession
from unilabos.app.center_client.transport import CenterTransportError
from unilabos.center_protocol.digest import canonical_json
from unilabos.center_protocol.event_history import validate_response
from unilabos.center_protocol.events import MAX_EVENT_SEQUENCE, EventEnvelopeError, validate_event
from unilabos.center_protocol.handoff import HandoffProtocolError, validate_handoff_event
from unilabos.center_protocol.recovery_evidence import validate_envelope
from unilabos.persistence.center_schema import CenterSchemaError, ensure_center_schema
from unilabos.persistence.database import open_database


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _event(value: dict[str, Any]) -> dict[str, Any]:
    return validate_envelope(value)


class EventHistoryStore:
    """离线修复入口允许原账本有洞；正常启动仍要求完整连续，不分配任何新序号。"""

    def __init__(self, path: str | Path, *, station_uuid: str, instance_uuid: str, event_epoch_uuid: str | None = None) -> None:
        self.station_uuid, self.instance_uuid = station_uuid, instance_uuid
        self._lock = threading.RLock()
        self._db = open_database(str(path), "workflow", private=True)
        self._db.row_factory = sqlite3.Row
        try:
            with self._db.transaction():
                epoch = ensure_center_schema(self._db, station_uuid=station_uuid, instance_uuid=instance_uuid,
                                             allow_event_gaps=True)
                assert epoch is not None
                self.event_epoch_uuid = event_epoch_uuid or epoch
                self._runtime()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def session(self) -> ControlSession | None:
        with self._lock:
            with self._db.transaction():
                raw = self._runtime()["session_json"]
            return ControlSession(**json.loads(raw)) if raw else None

    def _runtime(self):
        # 同一连接事务内锁定原 runtime，与正常序号分配互斥。
        self._db.execute("UPDATE center_runtime_state SET last_event_sequence=last_event_sequence WHERE singleton='runtime'")
        rows = self._db.execute("SELECT * FROM center_runtime_state").fetchall()
        if len(rows) != 1:
            raise CenterSchemaError("历史恢复期间实例账本改变")
        runtime = rows[0]
        if runtime["event_epoch_uuid"] != self.event_epoch_uuid:
            runtime = self._db.execute("SELECT * FROM center_epoch_history WHERE event_epoch_uuid=?", (self.event_epoch_uuid,)).fetchone()
        if runtime is None or any(runtime[key] != getattr(self, key) for key in
                                  ("station_uuid", "instance_uuid", "event_epoch_uuid")):
            raise CenterSchemaError("历史恢复期间实例或纪元改变")
        return runtime

    def gaps(self, *, after_sequence: int = 0, limit: int = 100) -> list[int]:
        """按最多 100 个位置检查本地缺口；不按高水位分配巨大数组。"""
        if (type(after_sequence) is not int or not 0 <= after_sequence <= MAX_EVENT_SEQUENCE
                or type(limit) is not int or not 1 <= limit <= 100):
            raise CenterClientError("缺口查询窗口无效")
        with self._lock, self._db.transaction():
            runtime = self._runtime()
            through = min(runtime["last_event_sequence"], after_sequence + limit)
            positions = {row[0] for row in self._db.execute(
                """SELECT event_sequence FROM center_boundary_outbox WHERE event_epoch_uuid=? AND event_sequence>? AND event_sequence<=?
                   UNION SELECT event_sequence FROM center_handoff_outbox WHERE event_epoch_uuid=? AND event_sequence>? AND event_sequence<=?
                   UNION SELECT event_sequence FROM center_preparation_outbox WHERE event_epoch_uuid=? AND event_sequence>? AND event_sequence<=?""",
                (self.event_epoch_uuid, after_sequence, through) * 3,
            )}
            return [n for n in range(after_sequence + 1, through + 1) if n not in positions]

    def sequences(self, *, after_sequence: int = 0, limit: int = 100) -> range:
        """恢复模式逐个检查原高水位以内的位置，包括已 ACK 事件和本地缺口。"""
        if (type(after_sequence) is not int or not 0 <= after_sequence <= MAX_EVENT_SEQUENCE
                or type(limit) is not int or not 1 <= limit <= 100):
            raise CenterClientError("历史恢复窗口无效")
        with self._lock, self._db.transaction():
            high = self._runtime()["last_event_sequence"]
            return range(after_sequence + 1, min(high, after_sequence + limit) + 1)

    def _local(self, sequence: int) -> dict[str, Any] | None:
        rows = self._db.execute(
            """SELECT event_json FROM center_boundary_outbox WHERE event_epoch_uuid=? AND event_sequence=?
               UNION ALL SELECT event_json FROM center_handoff_outbox WHERE event_epoch_uuid=? AND event_sequence=?
               UNION ALL SELECT event_json FROM center_preparation_outbox WHERE event_epoch_uuid=? AND event_sequence=?""", (self.event_epoch_uuid, sequence) * 3,
        ).fetchall()
        if len(rows) > 1:
            raise CenterSchemaError("原事件位置存在多个身份，禁止覆盖")
        return _event(json.loads(rows[0][0])) if rows else None

    def _recovery_revision(self) -> int:
        from unilabos.persistence.recovery_mode import status

        return status(self._db)["revision"]

    def prepare(self, sequence: int) -> dict[str, Any] | None:
        """网络前记录待恢复位置及已有原信封；响应丢失后仍使用相同身份。"""
        if type(sequence) is not int or not 1 <= sequence <= MAX_EVENT_SEQUENCE:
            raise CenterClientError("历史恢复序号无效")
        with self._lock, self._db.transaction():
            runtime = self._runtime()
            if sequence > runtime["last_event_sequence"]:
                raise CenterClientError("历史恢复不能扩展原序号高水位")
            local = self._local(sequence)
            prior = self._db.execute(
                "SELECT event_json FROM center_event_history_recovery WHERE event_epoch_uuid=? AND event_sequence=?",
                (self.event_epoch_uuid, sequence),
            ).fetchone()
            original = _event(json.loads(prior[0])) if prior and prior[0] else None
            if local is not None and original is not None and canonical_json(local) != canonical_json(original):
                raise CenterSchemaError("原恢复信封发生漂移")
            event = local or original
            if event is not None and (event["event_sequence"] != sequence or any(event[key] != getattr(self, key) for key in
                                      ("station_uuid", "instance_uuid", "event_epoch_uuid"))):
                raise CenterSchemaError("原恢复信封身份不匹配")
            self._db.execute(
                """INSERT INTO center_event_history_recovery(event_epoch_uuid,event_sequence,event_json,state,recovery_revision)
                   VALUES (?,?,?,'pending',?) ON CONFLICT(event_epoch_uuid,event_sequence) DO UPDATE
                   SET event_json=excluded.event_json,state='pending',reason=NULL,recovery_revision=excluded.recovery_revision""",
                (self.event_epoch_uuid, sequence, _json(event) if event else None, self._recovery_revision()),
            )
            return event

    def attention(self, sequence: int, reason: str) -> dict[str, Any]:
        with self._lock, self._db.transaction():
            self._runtime()
            self._db.execute(
                "UPDATE center_event_history_recovery SET state='needs_review',reason=? WHERE event_epoch_uuid=? AND event_sequence=?",
                (reason, self.event_epoch_uuid, sequence),
            )
        return {"state": "needs_review", "event_sequence": sequence, "reason": reason}

    def accept_retained(self, event: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        """保留原 outbox/ACK 状态，单独保存中心接管的未核实责任。"""
        from unilabos.center_protocol.recovery_evidence import validate_receipt

        receipt = validate_receipt(receipt, event)
        with self._lock, self._db.transaction():
            runtime = self._runtime()
            sequence = event["event_sequence"]
            if not 1 <= sequence <= runtime["last_event_sequence"]:
                raise CenterSchemaError("证据保管不能改变原高水位")
            row = self._db.execute(
                "SELECT event_json,retention_receipt_json FROM center_event_history_recovery WHERE event_epoch_uuid=? AND event_sequence=?",
                (self.event_epoch_uuid, sequence),
            ).fetchone()
            if (row is None or row["event_json"] is None
                    or canonical_json(json.loads(row["event_json"])) != canonical_json(event)
                    or any(event[key] != getattr(self, key) for key in ("station_uuid", "instance_uuid", "event_epoch_uuid"))):
                raise CenterSchemaError("保管回执没有对应原恢复意图")
            if row["retention_receipt_json"] is not None and canonical_json(json.loads(row["retention_receipt_json"])) != canonical_json(receipt):
                raise CenterSchemaError("原保管回执发生漂移，须核实中心恢复历史")
            self._db.execute("INSERT INTO center_recovery_event_holds VALUES (?,?,?) ON CONFLICT DO NOTHING",
                             (event["event_uuid"], event["event_digest"], _json(receipt)))
            self._db.execute(
                "UPDATE center_event_history_recovery SET retention_receipt_json=?,state='retained_needs_review',reason=? WHERE event_epoch_uuid=? AND event_sequence=?",
                (_json(receipt), "中心已保管原证据，未应用业务且未恢复执行资格", self.event_epoch_uuid, sequence),
            )
        return {"state": "retained_needs_review", "event_sequence": sequence,
                "receipt_uuid": receipt["receipt_uuid"], "owner_verification": receipt["owner_verification"]}

    def accept(self, request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        if request["event_epoch_uuid"] != self.event_epoch_uuid:
            raise CenterSchemaError("历史恢复请求不能换用其他纪元")
        response = validate_response(response, request, station_uuid=self.station_uuid, instance_uuid=self.instance_uuid)
        sequence, record = request["event_sequence"], response["record"]
        if record is None:
            return self.attention(sequence, "原事件证据缺失或身份不唯一，保留缺口等待核实")
        event = record["envelope"]
        handoff = "handoff_uuid" in event
        preparation = "request_uuid" in event
        if handoff and record["handoff_response"] is None:
            return self.attention(sequence, "交接原精确响应缺失，不能重建承诺或确认")
        if not handoff and not preparation and event["event_type"] not in ("accepted", "outcome", "admission_failed", "transport_progress"):
            return self.attention(sequence, "历史事件不属于本站持久出口支持的阶段，须核实")
        with self._lock, self._db.transaction():
            runtime = self._runtime()
            if not 1 <= sequence <= runtime["last_event_sequence"]:
                raise CenterSchemaError("历史恢复不能改变原高水位")
            prior = self._db.execute(
                "SELECT event_json FROM center_event_history_recovery WHERE event_epoch_uuid=? AND event_sequence=?",
                (self.event_epoch_uuid, sequence),
            ).fetchone()
            if prior is None or (prior[0] and canonical_json(json.loads(prior[0])) != canonical_json(event)):
                raise CenterSchemaError("历史证明没有对应原恢复请求")
            existing = self._local(sequence)
            if existing is not None and canonical_json(existing) != canonical_json(event):
                raise CenterSchemaError("历史证明不能覆盖已有事件")
            if existing is None:
                duplicate = self._db.execute(
                    "SELECT event_uuid FROM center_boundary_outbox WHERE event_uuid=? UNION ALL SELECT event_uuid FROM center_handoff_outbox WHERE event_uuid=? UNION ALL SELECT event_uuid FROM center_preparation_outbox WHERE event_uuid=?",
                    (event["event_uuid"],) * 3,
                ).fetchone()
                if duplicate is not None:
                    raise CenterSchemaError("历史 UUID 已占据其他位置")
                if preparation:
                    self._db.execute(
                        "INSERT INTO center_preparation_outbox(event_uuid,request_uuid,event_sequence,event_epoch_uuid,event_json,ack_json) VALUES (?,?,?,?,?,?)",
                        (event["event_uuid"], event["request_uuid"], sequence, event["event_epoch_uuid"], _json(event), _json(record["acknowledgement"])),
                    )
                elif handoff:
                    self._db.execute(
                        "INSERT INTO center_handoff_outbox(event_uuid,handoff_uuid,event_sequence,event_json,ack_json,event_epoch_uuid) VALUES (?,?,?,?,?,?)",
                        (event["event_uuid"], event["handoff_uuid"], sequence, _json(event), _json(record["handoff_response"]), event["event_epoch_uuid"]),
                    )
                else:
                    application = record["acknowledgement"]["application"]
                    self._db.execute(
                        """INSERT INTO center_boundary_outbox(event_uuid,invocation_uuid,phase,event_sequence,event_json,acknowledged,application,event_epoch_uuid)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (event["event_uuid"], event["station_invocation_uuid"],
                         2 if event["event_type"] == "transport_progress" else int(event["event_type"] in ("outcome", "admission_failed")),
                         sequence, _json(event), int(application != "conflict"), application, event["event_epoch_uuid"]),
                    )
            elif preparation:
                old = self._db.execute("SELECT ack_json FROM center_preparation_outbox WHERE event_uuid=?", (event["event_uuid"],)).fetchone()[0]
                if old is not None and ((json.loads(old)["application"] == "conflict") != (record["acknowledgement"]["application"] == "conflict")):
                    raise CenterSchemaError("车辆准备原确认结果发生漂移，禁止覆盖")
                self._db.execute("UPDATE center_preparation_outbox SET ack_json=? WHERE event_uuid=? AND ack_json IS NULL",
                                 (_json(record["acknowledgement"]), event["event_uuid"]))
            elif handoff:
                old = self._db.execute("SELECT ack_json FROM center_handoff_outbox WHERE event_uuid=?", (event["event_uuid"],)).fetchone()[0]
                if old is not None and canonical_json(json.loads(old)) != canonical_json(record["handoff_response"]):
                    raise CenterSchemaError("交接原精确回执发生漂移，禁止覆盖")
                self._db.execute("UPDATE center_handoff_outbox SET ack_json=? WHERE event_uuid=? AND ack_json IS NULL",
                                 (_json(record["handoff_response"]), event["event_uuid"]))
            else:
                application = record["acknowledgement"]["application"]
                old = self._db.execute("SELECT application FROM center_boundary_outbox WHERE event_uuid=?", (event["event_uuid"],)).fetchone()[0]
                if old in ("applied", "duplicate", "conflict") and ((old == "conflict") != (application == "conflict")):
                    raise CenterSchemaError("原调用收件结果发生漂移，须核实")
                self._db.execute("UPDATE center_boundary_outbox SET application=?,acknowledged=? WHERE event_uuid=? AND application IS NULL",
                                 (application, int(application != "conflict"), event["event_uuid"]))
            if not handoff and not preparation and event["event_type"] == "admission_failed" and record["acknowledgement"]["application"] in ("applied", "duplicate"):
                inbox = self._db.execute("SELECT local_task_uuid FROM center_invocation_inbox WHERE invocation_uuid=?", (event["station_invocation_uuid"],)).fetchone()
                if inbox is not None and inbox[0] is not None:
                    raise CenterSchemaError("恢复拒绝证据不能覆盖已有真实 Task")
                self._db.execute("UPDATE center_invocation_inbox SET terminal_recorded=1 WHERE invocation_uuid=? AND local_task_uuid IS NULL", (event["station_invocation_uuid"],))
            # 既有 ACK/legacy_acknowledged 保持原样；此次精确确认单独持久化。
            self._db.execute(
                """UPDATE center_event_history_recovery SET event_json=?,receipt_json=?,state='recovered',reason=NULL
                   WHERE event_epoch_uuid=? AND event_sequence=?""",
                (_json(event), _json(response), self.event_epoch_uuid, sequence),
            )
        return {"state": "recovered", "event_sequence": sequence,
                "application": record["acknowledgement"]["application"],
                "last_contiguous_event_sequence": response["last_contiguous_event_sequence"]}

    def status(self, sequence: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT state,reason,event_sequence FROM center_event_history_recovery WHERE event_epoch_uuid=? AND event_sequence=?",
                (self.event_epoch_uuid, sequence),
            ).fetchone()
            return dict(row) if row else None


class EventHistoryRecovery:
    """操作者逐项选择原序号；网络重试不会重新生成事件、claim 或执行任务。"""

    def __init__(self, client: CenterClient, store: EventHistoryStore, *, retain_missing: bool = False) -> None:
        self.client, self.store = client, store
        self.retain_missing = retain_missing
        session = store.session()
        if session is None:
            raise CenterClientError("缺少原历史会话，请先恢复原凭据与会话证据")
        client.restore_evidence_session(session)

    def prefix(self) -> dict[str, Any]:
        return self.client.event_history({"event_epoch_uuid": self.store.event_epoch_uuid,
                                          "event_sequence": None, "identity": None})

    def recover(self, sequence: int) -> dict[str, Any]:
        try:
            return self._recover(sequence)
        except (CenterSchemaError, EventEnvelopeError, HandoffProtocolError, sqlite3.IntegrityError):
            return self.store.attention(sequence, "历史身份、摘要或原确认无法一致核验，保留原账本等待核实")
        except CenterTransportError as error:
            if not error.retryable and error.status_code in (400, 401, 403, 404, 409, 413, 415, 422, 431):
                return self.store.attention(sequence, "原会话、凭据或业务证据需核实，禁止重建执行事实")
            raise

    def _recover(self, sequence: int) -> dict[str, Any]:
        event = self.store.prepare(sequence)
        request = {"event_epoch_uuid": self.store.event_epoch_uuid, "event_sequence": sequence,
                   "identity": {key: event[key] for key in ("event_uuid", "event_digest")} if event else None}
        response = self.client.event_history(request)
        if response["status"] == "missing" and event is not None:
            if self.retain_missing:
                receipt = self.client.retain_recovery_evidence(event)
                return self.store.accept_retained(event, receipt)
            if "request_uuid" in event:
                if event["fact"] in ("waiting", "reserved"):
                    return self.store.attention(sequence, "中心没有原车辆准备收件证据，禁止借历史恢复重新启用执行")
                self.client.report_preparation(event)
            elif "handoff_uuid" in event:
                if event["payload"]["kind"] in ("prepared", "released", "reservation_renewed"):
                    return self.store.attention(sequence, "中心没有原交接承诺收件证据，禁止以历史恢复重新提交控制动作")
                self.client.report_handoff(event)
            else:
                self.client.report(event)
            response = self.client.event_history(request)
        return self.store.accept(request, response)
