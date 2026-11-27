"""显式灾备退出的纪元迁移；原 outbox、精确 ACK 和历史高水位原样保留。"""
from __future__ import annotations

import json
import sqlite3
from time import time_ns
from uuid import uuid4

from unilabos.persistence.database import Database

Connection = Database | sqlite3.Connection


def ensure_epoch_storage(connection: Connection) -> None:
    """调用方写事务内迁移唯一键；不改变任何原事件信封或确认。"""
    connection.execute("""CREATE TABLE IF NOT EXISTS center_epoch_history (
        event_epoch_uuid TEXT PRIMARY KEY,station_uuid TEXT NOT NULL,instance_uuid TEXT NOT NULL,
        last_event_sequence BIGINT NOT NULL,session_json TEXT,recovery_revision BIGINT NOT NULL,
        retired_at_ms BIGINT NOT NULL,next_event_epoch_uuid TEXT NOT NULL UNIQUE)""")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(center_epoch_history)")}
    if "next_event_epoch_uuid" not in columns:
        connection.execute("ALTER TABLE center_epoch_history ADD COLUMN next_event_epoch_uuid TEXT")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(center_boundary_outbox)")}
    if "event_epoch_uuid" not in columns:
        connection.execute("ALTER TABLE center_boundary_outbox ADD COLUMN event_epoch_uuid TEXT")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(center_handoff_outbox)")}
    if "event_epoch_uuid" not in columns:
        # SQLite 内联 UNIQUE 无法单独删除；两库同事务复制全部原字段再换表。
        connection.execute("""CREATE TABLE center_handoff_outbox_epoch_upgrade (
            event_uuid TEXT PRIMARY KEY,handoff_uuid TEXT NOT NULL,event_sequence BIGINT NOT NULL,
            event_json TEXT NOT NULL,ack_json TEXT,event_epoch_uuid TEXT)""")
        connection.execute("""INSERT INTO center_handoff_outbox_epoch_upgrade
            (event_uuid,handoff_uuid,event_sequence,event_json,ack_json)
            SELECT event_uuid,handoff_uuid,event_sequence,event_json,ack_json FROM center_handoff_outbox""")
        connection.execute("DROP TABLE center_handoff_outbox")
        connection.execute("ALTER TABLE center_handoff_outbox_epoch_upgrade RENAME TO center_handoff_outbox")
    connection.execute("DROP INDEX IF EXISTS ux_center_event_sequence")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_center_boundary_epoch_sequence ON center_boundary_outbox(event_epoch_uuid,event_sequence)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_center_handoff_epoch_sequence ON center_handoff_outbox(event_epoch_uuid,event_sequence)")


def rotate_after_review(connection: Connection, revision: int) -> str:
    """仅由核实退出事务调用；新纪元仍须中心审批才能取得控制权。"""
    if not connection.in_transaction:
        raise RuntimeError("纪元切换必须在核实退出事务内执行")
    runtime = connection.execute("SELECT * FROM center_runtime_state WHERE singleton='runtime'").fetchone()
    epoch = str(uuid4())
    connection.execute("""INSERT INTO center_epoch_history
        (event_epoch_uuid,station_uuid,instance_uuid,last_event_sequence,session_json,recovery_revision,retired_at_ms,next_event_epoch_uuid)
        VALUES (?,?,?,?,?,?,?,?)""", (runtime["event_epoch_uuid"], runtime["station_uuid"], runtime["instance_uuid"],
            runtime["last_event_sequence"], runtime["session_json"], revision, time_ns() // 1_000_000, epoch))
    # session_json 仅供历史补报；CenterClient 从不会把它当作新进程控制会话。
    connection.execute("UPDATE center_runtime_state SET event_epoch_uuid=?,last_event_sequence=0 WHERE singleton='runtime'", (epoch,))
    return epoch


def synchronize_inventory_epoch(workflow: Connection, inventory: Connection) -> dict[str, object]:
    """离线显式迁移分库绑定；调用方依次持有工作流、库存写事务，崩溃可原样重试。"""
    from unilabos.persistence.inspection import table_exists
    from unilabos.persistence.recovery_mode import EvidenceOnlyError, require_execution

    if not workflow.in_transaction or not inventory.in_transaction:
        raise EvidenceOnlyError("库存纪元迁移必须同时持有核实来源和库存写事务")
    require_execution(workflow)
    if not table_exists(workflow, "center_epoch_history"):
        raise EvidenceOnlyError("缺少持久核实的纪元迁移记录")
    runtime = workflow.execute("SELECT * FROM center_runtime_state WHERE singleton='runtime'").fetchone()
    if runtime is None:
        raise EvidenceOnlyError("原实例账本缺失")
    current, transitions, seen = runtime["event_epoch_uuid"], {}, set()
    while True:
        row = workflow.execute("SELECT * FROM center_epoch_history WHERE next_event_epoch_uuid=?", (current,)).fetchone()
        if row is None:
            break
        if row["event_epoch_uuid"] in seen or any(row[key] != runtime[key] for key in ("station_uuid", "instance_uuid")):
            raise EvidenceOnlyError("纪元迁移链身份不一致")
        verification = workflow.execute("SELECT verification_json FROM center_recovery_mode_history WHERE revision=? AND mode='normal'",
                                        (row["recovery_revision"] + 1,)).fetchone()
        proof = json.loads(verification[0]) if verification and verification[0] else {}
        if proof.get("previous_work") != "keep_isolated" or proof.get("expected_revision") != row["recovery_revision"]:
            raise EvidenceOnlyError("纪元迁移缺少已提交的显式核实")
        current = row["event_epoch_uuid"]
        seen.add(current)
        transitions[current] = {"from_epoch": current, "to_epoch": row["next_event_epoch_uuid"],
                                "recovery_revision": row["recovery_revision"], "verification": proof}
    if not transitions:
        raise EvidenceOnlyError("没有通向当前纪元的核实迁移链")
    inventory.execute("""CREATE TABLE IF NOT EXISTS station_inventory_epoch_history (
        authority TEXT NOT NULL,old_event_epoch_uuid TEXT NOT NULL,new_event_epoch_uuid TEXT NOT NULL,
        verification_json TEXT NOT NULL,changed_at_ms BIGINT NOT NULL,
        PRIMARY KEY(authority,old_event_epoch_uuid,new_event_epoch_uuid))""")
    updated = []
    # 固定表白名单；只变更入口绑定，历史 intent/保护/映射/回执均原样保留。
    for table in ("station_handoff_identity", "station_material_export_identity"):
        if not table_exists(inventory, table):
            continue
        row = inventory.execute(f"SELECT identity_json FROM {table} WHERE singleton=1").fetchone()
        if row is None:
            continue
        identity = json.loads(row[0])
        size = 4 if table == "station_handoff_identity" else 3
        if not isinstance(identity, list) or len(identity) != size or identity[:2] != [runtime["station_uuid"], runtime["instance_uuid"]]:
            raise EvidenceOnlyError("库存与工作流不属于同一原工站实例")
        if identity[2] == runtime["event_epoch_uuid"]:
            continue
        if identity[2] not in transitions:
            raise EvidenceOnlyError("库存纪元不在本地已核实迁移链中，禁止配置覆盖")
        old = identity[2]
        identity[2] = runtime["event_epoch_uuid"]
        chain, cursor = [], old
        while cursor != runtime["event_epoch_uuid"]:
            step = transitions[cursor]
            chain.append(step)
            cursor = step["to_epoch"]
        inventory.execute("INSERT INTO station_inventory_epoch_history VALUES (?,?,?,?,?)",
                          (table, old, identity[2], json.dumps(chain, ensure_ascii=False), time_ns() // 1_000_000))
        inventory.execute(f"UPDATE {table} SET identity_json=? WHERE singleton=1",
                          (json.dumps(identity, ensure_ascii=False, separators=(",", ":")),))
        updated.append(table)
    return {"event_epoch_uuid": runtime["event_epoch_uuid"], "updated_authorities": updated}
