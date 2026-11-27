"""试剂（Reagent）公共合同的 SQLite 结构迁移。

本模块只声明 v9 新增结构；``InventoryStore`` 仍是 ``inventory.db`` 唯一迁移
入口。试剂变化复用 Edge 既有 ``inventory_ledger`` 与 ``sync_outbox``，不创建
第二套物料台账。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from unilabos.persistence.inspection import table_exists

_SCHEMA_V9_REAGENT = r"""
CREATE TABLE IF NOT EXISTS inventory_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at INTEGER NOT NULL,
    op_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL DEFAULT '',
    aggregate_id TEXT NOT NULL DEFAULT '',
    delta_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    causation_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    span_id TEXT NOT NULL DEFAULT '',
    entry_uuid TEXT NOT NULL DEFAULT '',
    material_uuid TEXT NOT NULL DEFAULT '',
    subject_type TEXT NOT NULL DEFAULT '',
    quantity_delta REAL,
    quantity_unit TEXT,
    revision INTEGER,
    workflow_task_uuid TEXT,
    workflow_node_job_uuid TEXT
);

CREATE TABLE IF NOT EXISTS sync_outbox (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    edge_id TEXT NOT NULL,
    lab_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    causation_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    traceparent TEXT NOT NULL DEFAULT '',
    tracestate TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    span_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reagent_info (
    uuid TEXT PRIMARY KEY NOT NULL,
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    deleted_at DATETIME,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_data)),
    cas TEXT,
    name TEXT NOT NULL,
    name_en TEXT,
    aliases TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(aliases) AND json_type(aliases) = 'array'
    ),
    molecular_formula TEXT,
    smiles TEXT,
    inchi_key TEXT,
    molecular_weight REAL CHECK (
        molecular_weight IS NULL OR molecular_weight > 0
    ),
    density_g_per_ml REAL CHECK (
        density_g_per_ml IS NULL OR density_g_per_ml > 0
    ),
    physical_state TEXT NOT NULL CHECK (
        physical_state IN ('solid', 'liquid', 'gas', 'other', 'unknown')
    ),
    structure_3d_identity_key TEXT,
    structure_3d_format TEXT,
    structure_3d_source TEXT,
    structure_3d_source_id TEXT,
    structure_3d_content TEXT,
    structure_3d_checksum TEXT,
    structure_3d_status TEXT,
    structure_3d_generated_at DATETIME,
    structure_3d_error_message TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reagent_info_cas_active
    ON reagent_info (LOWER(cas))
    WHERE deleted_at IS NULL AND cas IS NOT NULL AND cas <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_reagent_info_inchi_key_active
    ON reagent_info (LOWER(inchi_key))
    WHERE deleted_at IS NULL AND inchi_key IS NOT NULL AND inchi_key <> '';
CREATE INDEX IF NOT EXISTS idx_reagent_info_name_active
    ON reagent_info (LOWER(name)) WHERE deleted_at IS NULL;

"""

_LEDGER_COLUMNS = {
    "entry_uuid": "TEXT NOT NULL DEFAULT ''",
    "material_uuid": "TEXT NOT NULL DEFAULT ''",
    "subject_type": "TEXT NOT NULL DEFAULT ''",
    "quantity_delta": "REAL",
    "quantity_unit": "TEXT",
    "revision": "INTEGER",
    "workflow_task_uuid": "TEXT",
    "workflow_node_job_uuid": "TEXT",
}

_LEDGER_INDEXES = r"""
CREATE UNIQUE INDEX IF NOT EXISTS ux_inventory_ledger_entry_uuid
    ON inventory_ledger(entry_uuid) WHERE entry_uuid <> '';
CREATE INDEX IF NOT EXISTS idx_inventory_ledger_material_subject_time
    ON inventory_ledger(material_uuid, subject_type, occurred_at DESC, ledger_id DESC)
    WHERE material_uuid <> '' AND subject_type <> '';
"""


def _json(value: Any, fallback: Any) -> Any:
    """解析旧台账 JSON；非法历史值返回调用方给出的安全默认值。"""

    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _milliseconds(value: Any) -> int:
    """把旧台账 ISO 时间或毫秒时间转换为 Edge 台账使用的 UTC 毫秒。"""

    if isinstance(value, (int, float)):
        return int(value)
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return int(timestamp.timestamp() * 1000)


def _migrate_legacy_material_ledger(connection: sqlite3.Connection) -> None:
    """把早期开发版重复台账并入 Edge 台账后删除旧表。

    参数：``connection`` 是迁移入口持有的 SQLite 连接。返回：无。迁移保留
    Backend 历史接口所需字段；旧记录没有可靠 Edge 身份，因此只迁入台账，
    不伪造已经发送过的 ``sync_outbox`` 事件。
    """

    if not table_exists(connection, "material_ledger_entry"):
        return
    rows = connection.execute(
        "SELECT * FROM material_ledger_entry ORDER BY recorded_at,uuid"
    ).fetchall()
    for row in rows:
        item = dict(row)
        subject_type = str(item.get("subject_type") or "material")
        subject_uuid = str(item.get("subject_uuid") or item["material_uuid"])
        payload = {
            "changes": _json(item.get("changes"), {}),
            "extension": _json(item.get("extension"), {}),
            "workflow_task_uuid": item.get("workflow_task_uuid"),
            "workflow_node_job_uuid": item.get("workflow_node_job_uuid"),
            "quantity_delta": item.get("quantity_delta"),
            "quantity_unit": item.get("quantity_unit"),
            "revision": item.get("revision"),
        }
        existing = connection.execute(
            "SELECT material_uuid,subject_type,aggregate_id,op_type "
            "FROM inventory_ledger WHERE entry_uuid=?",
            (item["uuid"],),
        ).fetchone()
        expected = (
            item["material_uuid"],
            subject_type,
            subject_uuid,
            f"{subject_type}.{item['event_type']}",
        )
        if existing is not None:
            actual = tuple(existing)
            if actual != expected:
                raise sqlite3.IntegrityError(
                    "legacy material ledger UUID conflicts with a different "
                    "inventory ledger event"
                )
            continue
        connection.execute(
            """INSERT INTO inventory_ledger(
            occurred_at,op_type,aggregate_type,aggregate_id,delta_json,actor,reason,
            causation_id,trace_id,span_id,entry_uuid,material_uuid,subject_type,
            quantity_delta,quantity_unit,revision,workflow_task_uuid,
            workflow_node_job_uuid)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _milliseconds(item["recorded_at"]),
                f"{subject_type}.{item['event_type']}",
                subject_type,
                subject_uuid,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                item.get("operator_type") or "",
                "",
                "",
                item.get("trace_id") or "",
                "",
                item["uuid"],
                item["material_uuid"],
                subject_type,
                item.get("quantity_delta"),
                item.get("quantity_unit"),
                item.get("revision"),
                item.get("workflow_task_uuid"),
                item.get("workflow_node_job_uuid"),
            ),
        )
    connection.execute("DROP TABLE material_ledger_entry")


def migrate_reagent_schema(connection: sqlite3.Connection) -> None:
    """幂等创建 v9 试剂身份、实例并扩展 Edge 统一台账。

    参数：``connection`` 是 ``InventoryStore`` 独占的 SQLite 连接。返回：无。
    异常：任何 SQLite 结构或约束错误原样抛出，由唯一迁移入口终止启动。
    """

    connection.executescript(_SCHEMA_V9_REAGENT)
    existing = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(inventory_ledger)"
        ).fetchall()
    }
    for column, definition in _LEDGER_COLUMNS.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE inventory_ledger ADD COLUMN {column} {definition}"
            )
    connection.executescript(_LEDGER_INDEXES)
    _migrate_legacy_material_ledger(connection)


__all__ = ["migrate_reagent_schema"]
