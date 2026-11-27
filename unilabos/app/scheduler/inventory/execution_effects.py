"""将模拟效果关联到原生库存结算；本地过程封存从来不是库存回执。"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict

from unilabos.app.scheduler.inventory.station_resource import (
    MaterialAliquotCommand,
    MaterialTransferCommand,
)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


SCHEMA = """CREATE TABLE IF NOT EXISTS execution_effect_intent (
    effect_uuid TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL)"""


def migrate_execution_effects(store):
    """初始化阶段原子接管历史效果意图；运行请求不执行 DDL。"""
    if store.db.table_exists("twin_effect_intent"):
        with store.transaction() as connection:
            for row in connection.execute(
                "SELECT * FROM twin_effect_intent"
            ).fetchall():
                previous = connection.execute(
                    "SELECT digest FROM execution_effect_intent WHERE effect_uuid=?",
                    (row["effect_uuid"],),
                ).fetchone()
                if previous and previous["digest"] != row["digest"]:
                    raise ValueError("effect_migration_conflict")
                connection.execute(
                    "INSERT OR IGNORE INTO execution_effect_intent VALUES (?,?,?)",
                    (row["effect_uuid"], row["payload"], row["digest"]),
                )
            connection.execute("DROP TABLE twin_effect_intent")


class CanonicalSettlement:
    """先冻结完整意图，再复用库存 Permit/Fence 事务；丢失 ACK 可重取原回执。"""

    def __init__(self, inventory_service):
        self.service = inventory_service
        self.store = inventory_service.store

    def commit(self, command, evidence):
        if isinstance(command, MaterialTransferCommand):
            settle = self.service.station_resources.settle_material_transfer
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"unilabos-transfer-settlement:{command.effect_uuid}",
                )
            )
            operation = "physical_settlement.material_transfer"
        elif isinstance(command, MaterialAliquotCommand):
            settle = self.service.station_resources.settle_material_aliquot
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"unilabos-aliquot-source:{command.effect_uuid}"
                )
            )
            operation = "current_substance.aliquot_source"
        else:
            raise TypeError("unsupported_canonical_settlement")
        identity = evidence.get("identity", {})
        if identity.get("node_job_uuid") != command.job_uuid or not identity.get(
            "task_uuid"
        ):
            raise ValueError("effect_job_identity_mismatch")
        payload = _encode({"command": asdict(command), "evidence": evidence})
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self.store.transaction() as connection:
            claim = connection.execute(
                "SELECT task_uuid FROM station_execution_claim WHERE claim_uuid=?",
                (command.claim_uuid,),
            ).fetchone()
            if claim is None or claim["task_uuid"] != identity["task_uuid"]:
                raise ValueError("effect_task_identity_mismatch")
            old = connection.execute(
                "SELECT payload,digest FROM execution_effect_intent WHERE effect_uuid=?",
                (command.effect_uuid,),
            ).fetchone()
            if old is not None and (
                old["payload"] != payload or old["digest"] != fingerprint
            ):
                raise ValueError("effect_replay_conflict")
            connection.execute(
                "INSERT OR IGNORE INTO execution_effect_intent VALUES (?,?,?)",
                (command.effect_uuid, payload, fingerprint),
            )
        # 原生结算验证完整凭据。第一次提交失败保留意图；恢复只能重放同一意图。
        settle(command)
        with self.store.transaction() as connection:
            ledger = connection.execute(
                "SELECT ledger_id AS sequence,entry_uuid,op_type,causation_id,workflow_node_job_uuid,delta_json FROM inventory_ledger WHERE entry_uuid=?",
                (event_id,),
            ).fetchone()
            if (
                ledger is None
                or ledger["op_type"] != operation
                or ledger["causation_id"] != command.effect_uuid
                or ledger["workflow_node_job_uuid"] != command.job_uuid
            ):
                raise RuntimeError("canonical_inventory_receipt_missing")
            return {
                "receipt_id": ledger["entry_uuid"],
                "effect_id": command.effect_uuid,
                "sequence": ledger["sequence"],
                "job_uuid": command.job_uuid,
                "task_uuid": identity["task_uuid"],
                "intent_sha256": fingerprint,
                "scope": "canonical_inventory",
                "operation": operation,
                "result": json.loads(ledger["delta_json"])["changes"]["result"],
            }
