"""调用方事务内的中心事件分号与持久化；不持有连接或事务生命周期。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from unilabos.center_protocol.digest import DigestError, canonical_json
from unilabos.center_protocol.events import MAX_EVENT_SEQUENCE, EventEnvelopeError, make_event, validate_event
from unilabos.persistence.database import Database


class CenterEventConflict(RuntimeError):
    """事件正文、调用绑定或持久序号账本不一致。"""


def _runtime_for_write(connection: Database | sqlite3.Connection) -> Any:
    """共同锁定序号行；不同事件流只能在持有该写锁后取号。"""
    if not connection.in_transaction:
        raise CenterEventConflict("中心事件必须在调用方的写事务内持久化")
    connection.execute("UPDATE center_runtime_state SET last_event_sequence=last_event_sequence WHERE singleton='runtime'")
    rows = connection.execute("SELECT * FROM center_runtime_state").fetchall()
    if len(rows) != 1 or rows[0]["singleton"] != "runtime":
        raise CenterEventConflict("事件纪元账本缺失或不唯一")
    runtime = rows[0]
    high_water = runtime["last_event_sequence"]
    if type(high_water) is not int or not 0 <= high_water <= MAX_EVENT_SEQUENCE:
        raise CenterEventConflict("持久事件序号账本无效")
    # 启动升级已验证全账本连续性；热路径只读取各事件流唯一索引的最大值。
    maximum = connection.execute(
        """SELECT COALESCE(MAX(position),0) FROM (
           SELECT MAX(event_sequence) AS position FROM center_boundary_outbox WHERE event_epoch_uuid=?
           UNION ALL SELECT MAX(event_sequence) AS position FROM center_handoff_outbox WHERE event_epoch_uuid=?
           UNION ALL SELECT MAX(event_sequence) AS position FROM center_preparation_outbox WHERE event_epoch_uuid=?) AS event_positions""",
        (runtime["event_epoch_uuid"], runtime["event_epoch_uuid"], runtime["event_epoch_uuid"]),
    ).fetchone()[0]
    if maximum != high_water:
        label = "事件序号耗尽且高水位不一致" if high_water == MAX_EVENT_SEQUENCE else "持久事件序号高水位与共享账本不一致"
        raise CenterEventConflict(label)
    return runtime


def append_event_in_transaction(
    connection: Database | sqlite3.Connection,
    invocation_uuid: str,
    *,
    terminal: bool,
    body: dict[str, Any],
) -> dict[str, Any]:
    """同一阶段保留原身份；不同结果拒绝，分号、正文和调用映射一起提交。

    调用方必须持有写事务。不存在本地调用行时仍允许独立账本写入（兼容旧恢复
    入口），但已有映射不能漂移。这里不创建表、不提交事务，也不发送网络请求。
    """
    if not connection.in_transaction:
        raise CenterEventConflict("中心事件必须在调用方的写事务内持久化")
    if type(terminal) is not bool or not isinstance(body, dict) or set(body) != {
        "event_uuid", "local_task_uuid", "event_type", "payload", "occurred_at",
    }:
        raise CenterEventConflict("待持久化事件正文结构无效")
    if body["event_type"] not in ({"outcome", "admission_failed"} if terminal else {"accepted"}):
        raise CenterEventConflict("本运行时阶段必须对应 accepted 或 outcome")
    runtime = _runtime_for_write(connection)
    high_water = runtime["last_event_sequence"]
    existing = connection.execute(
        "SELECT * FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=?",
        (invocation_uuid, int(terminal)),
    ).fetchone()
    event_epoch = runtime["event_epoch_uuid"]
    if existing is not None and existing["event_epoch_uuid"] != event_epoch:
        historical = connection.execute("SELECT last_event_sequence FROM center_epoch_history WHERE event_epoch_uuid=?", (existing["event_epoch_uuid"],)).fetchone()
        if historical is None:
            raise CenterEventConflict("原事件纪元未经过核实，禁止重写")
        event_epoch, high_water = existing["event_epoch_uuid"], historical[0]
    sequence = existing["event_sequence"] if existing is not None else high_water + 1
    if type(sequence) is not int or sequence < 1:
        raise CenterEventConflict("持久事件序号无效")
    if sequence > MAX_EVENT_SEQUENCE:
        raise CenterEventConflict("事件序号已耗尽，禁止回绕或自动更换纪元")
    if connection.execute("SELECT 1 FROM center_handoff_outbox WHERE event_uuid=? UNION ALL SELECT 1 FROM center_preparation_outbox WHERE event_uuid=?", (body["event_uuid"], body["event_uuid"])).fetchone():
        raise CenterEventConflict("事件 UUID 已属于交接事件")
    try:
        candidate = make_event(
            event_epoch_uuid=event_epoch, event_sequence=sequence,
            station_uuid=runtime["station_uuid"], instance_uuid=runtime["instance_uuid"],
            station_invocation_uuid=invocation_uuid, **body,
        )
        if existing is not None:
            event = validate_event(json.loads(existing["event_json"]))
            if any(event[key] != candidate[key] for key in (
                "station_uuid", "instance_uuid", "event_epoch_uuid", "event_sequence",
                "station_invocation_uuid", "local_task_uuid", "event_type",
            )) or event["event_uuid"] != existing["event_uuid"] or sequence > high_water:
                raise CenterEventConflict("持久事件身份或调用绑定已变化")
            if terminal:
                matching = canonical_json(event["payload"]) == canonical_json(candidate["payload"])
            else:
                # 旧 worker 的 accepted 时间可能来自首次观测，不能按 Task 时间重写。
                matching = event["payload"]["task_payload_digest"] == candidate["payload"]["task_payload_digest"]
            if not matching:
                raise CenterEventConflict("同一调用阶段的事件结果已变化")
        else:
            event = candidate
    except (EventEnvelopeError, DigestError, ValueError, TypeError) as error:
        raise CenterEventConflict("持久事件正文不能验证") from error
    mapped = connection.execute(
        "SELECT local_task_uuid FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation_uuid,),
    ).fetchone()
    if event["event_type"] == "admission_failed":
        if mapped is None or mapped[0] is not None:
            raise CenterEventConflict("受理失败只允许终结尚未绑定 Task 的原调用")
        accepted = connection.execute("SELECT 1 FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=0", (invocation_uuid,)).fetchone()
        if accepted is not None:
            raise CenterEventConflict("原调用已有受理事件，不能报告未创建 Task")
    if mapped is not None and mapped[0] is not None and mapped[0] != event["local_task_uuid"]:
        raise CenterEventConflict("同一中心调用不能更换本地任务")
    if existing is None:
        # JCS 仅用于摘要；普通 JSON 保留 binary64，重启不能读成不安全大整数。
        connection.execute(
            """INSERT INTO center_boundary_outbox(event_uuid,invocation_uuid,phase,event_json,event_sequence,event_epoch_uuid)
               VALUES (?,?,?,?,?,?)""",
            (event["event_uuid"], invocation_uuid, int(terminal),
             json.dumps(event, allow_nan=False, ensure_ascii=False), sequence, event["event_epoch_uuid"]),
        )
        connection.execute("UPDATE center_runtime_state SET last_event_sequence=? WHERE singleton='runtime'", (sequence,))
    connection.execute(
        """UPDATE center_invocation_inbox SET local_task_uuid=?,
           terminal_recorded=CASE WHEN terminal_recorded=1 THEN 1 ELSE ? END WHERE invocation_uuid=?""",
        (event["local_task_uuid"], int(terminal), invocation_uuid),
    )
    return event


def append_transport_progress_in_transaction(connection: Database | sqlite3.Connection,
                                             invocation_uuid: str, body: dict[str, Any]) -> dict[str, Any]:
    """取货使用独立 phase=2，保留原 accepted=0/outcome=1 和共享序号语义。"""
    if set(body) != {"event_uuid", "local_task_uuid", "event_type", "payload", "occurred_at"} or body["event_type"] != "transport_progress":
        raise CenterEventConflict("取货事件正文结构无效")
    runtime = _runtime_for_write(connection)
    mapping = connection.execute(
        "SELECT local_task_uuid,claim_confirmed FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation_uuid,),
    ).fetchone()
    if mapping is None or not mapping["claim_confirmed"] or mapping["local_task_uuid"] != body["local_task_uuid"]:
        raise CenterEventConflict("取货必须属于原已领取调用和本地任务")
    previous = connection.execute(
        "SELECT * FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=2", (invocation_uuid,),
    ).fetchone()
    if previous is not None:
        event = validate_event(json.loads(previous["event_json"]))
        if canonical_json({key:event[key] for key in body}) != canonical_json(body):
            raise CenterEventConflict("原调用的取货事实不可改写")
        return event
    sequence = runtime["last_event_sequence"] + 1
    event = make_event(event_epoch_uuid=runtime["event_epoch_uuid"], event_sequence=sequence,
                       station_uuid=runtime["station_uuid"], instance_uuid=runtime["instance_uuid"],
                       station_invocation_uuid=invocation_uuid, **body)
    if connection.execute("SELECT 1 FROM center_handoff_outbox WHERE event_uuid=? UNION ALL SELECT 1 FROM center_preparation_outbox WHERE event_uuid=?", (event["event_uuid"], event["event_uuid"])).fetchone():
        raise CenterEventConflict("取货事件身份已属于其他交接事件")
    connection.execute(
        "INSERT INTO center_boundary_outbox(event_uuid,invocation_uuid,phase,event_json,event_sequence,event_epoch_uuid) VALUES (?,?,2,?,?,?)",
        (event["event_uuid"], invocation_uuid, json.dumps(event, allow_nan=False, ensure_ascii=False), sequence, event["event_epoch_uuid"]),
    )
    connection.execute("UPDATE center_runtime_state SET last_event_sequence=? WHERE singleton='runtime'", (sequence,))
    return event


def append_handoff_in_transaction(connection: Database | sqlite3.Connection,
                                  intent: dict[str, Any]) -> dict[str, Any]:
    """只复制已提交的库存事实，固定事件 UUID 与原纪元；重试不重新分号。"""
    from unilabos.center_protocol.handoff import (
        INTENT_FIELDS,
        HandoffProtocolError,
        make_handoff_event,
        validate_handoff_event,
        validate_handoff_intent,
    )

    try:
        intent = validate_handoff_intent(intent)
        runtime = _runtime_for_write(connection)
        if connection.execute("SELECT 1 FROM center_boundary_outbox WHERE event_uuid=? UNION ALL SELECT 1 FROM center_preparation_outbox WHERE event_uuid=?", (intent["event_uuid"], intent["event_uuid"])).fetchone():
            raise CenterEventConflict("交接事件 UUID 已属于执行事件")
        previous = connection.execute("SELECT * FROM center_handoff_outbox WHERE event_uuid=?", (intent["event_uuid"],)).fetchone()
        if previous is not None:
            event = validate_handoff_event(json.loads(previous["event_json"]))
            if (canonical_json({key: event[key] for key in INTENT_FIELDS}) != canonical_json(intent)
                    or event["event_sequence"] != previous["event_sequence"]
                    or event["handoff_uuid"] != previous["handoff_uuid"]):
                raise CenterEventConflict("同一库存交接事实的正文发生变化")
            return event
        if any(intent[key] != runtime[key] for key in ("station_uuid", "instance_uuid", "event_epoch_uuid")):
            raise CenterEventConflict("库存交接事实与当前持久工站或纪元不匹配")
        sequence = runtime["last_event_sequence"] + 1
        event = make_handoff_event(intent, event_sequence=sequence)
        connection.execute(
            "INSERT INTO center_handoff_outbox(event_uuid,handoff_uuid,event_sequence,event_json,event_epoch_uuid) VALUES (?,?,?,?,?)",
            (event["event_uuid"], event["handoff_uuid"], sequence, json.dumps(event, allow_nan=False, ensure_ascii=False), event["event_epoch_uuid"]),
        )
        connection.execute("UPDATE center_runtime_state SET last_event_sequence=? WHERE singleton='runtime'", (sequence,))
        return event
    except (HandoffProtocolError, DigestError, ValueError, TypeError) as error:
        raise CenterEventConflict("库存交接事件无法持久化") from error
