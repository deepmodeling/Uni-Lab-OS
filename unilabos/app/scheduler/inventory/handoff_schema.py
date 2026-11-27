"""中心物料交接在本站库存事务内的身份、保护与边界发件箱。"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS station_handoff_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS container_station_binding (
 container_uuid TEXT PRIMARY KEY, station_uuid TEXT NOT NULL,
 carrier_material_uuid TEXT NOT NULL UNIQUE REFERENCES material(uuid),
 barcode_namespace TEXT NOT NULL, barcode TEXT NOT NULL,
 registration_request_json TEXT, registration_response_json TEXT, registration_last_error TEXT,
 UNIQUE(barcode_namespace,barcode)
);
CREATE TABLE IF NOT EXISTS station_handoff (
 handoff_uuid TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('source','target')),
 station_uuid TEXT NOT NULL, instance_uuid TEXT NOT NULL,
 revision INTEGER NOT NULL, package_json TEXT NOT NULL,
 proof_uuid TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'protected',
 plan_digest TEXT NOT NULL, created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS station_handoff_item (
 handoff_item_uuid TEXT PRIMARY KEY, handoff_uuid TEXT NOT NULL REFERENCES station_handoff(handoff_uuid),
 container_uuid TEXT NOT NULL REFERENCES container_station_binding(container_uuid),
 carrier_material_uuid TEXT NOT NULL REFERENCES material(uuid),
 reservation_uuid TEXT UNIQUE REFERENCES station_ingress_reservation(uuid),
 state TEXT NOT NULL CHECK(state IN ('protected','received','settled','canceled')),
 receipt_json TEXT, receive_request_digest TEXT,
 UNIQUE(handoff_uuid,container_uuid)
);
CREATE TABLE IF NOT EXISTS station_handoff_settlement (
 handoff_item_uuid TEXT PRIMARY KEY REFERENCES station_handoff_item(handoff_item_uuid),
 settlement_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS station_handoff_source_location (
 handoff_item_uuid TEXT PRIMARY KEY REFERENCES station_handoff_item(handoff_item_uuid),
 carrier_material_uuid TEXT NOT NULL REFERENCES material(uuid),
 source_site_uuid TEXT REFERENCES site(uuid), source_parent_uuid TEXT REFERENCES material(uuid),
 pickup_json TEXT
);
CREATE TABLE IF NOT EXISTS station_handoff_settlement_history (
 handoff_item_uuid TEXT NOT NULL, handoff_revision INTEGER NOT NULL,
 settlement_json TEXT NOT NULL, PRIMARY KEY(handoff_item_uuid,handoff_revision)
);
CREATE TABLE IF NOT EXISTS station_handoff_movement (
 handoff_uuid TEXT NOT NULL REFERENCES station_handoff(handoff_uuid),
 handoff_item_uuid TEXT NOT NULL, review_revision INTEGER NOT NULL,
 request_digest TEXT NOT NULL, proof_json TEXT NOT NULL,
 reservation_uuid TEXT REFERENCES station_ingress_reservation(uuid),
 settlement_json TEXT,
 PRIMARY KEY(handoff_item_uuid,review_revision)
);
CREATE TABLE IF NOT EXISTS material_station_binding (
 handoff_item_uuid TEXT NOT NULL REFERENCES station_handoff_item(handoff_item_uuid),
 material_lineage_uuid TEXT NOT NULL, local_material_uuid TEXT NOT NULL REFERENCES material(uuid),
 source_ref_json TEXT NOT NULL, initial_revision INTEGER NOT NULL,
 PRIMARY KEY(handoff_item_uuid,material_lineage_uuid)
);
CREATE TABLE IF NOT EXISTS station_handoff_resource (
 handoff_uuid TEXT NOT NULL REFERENCES station_handoff(handoff_uuid),
 handoff_item_uuid TEXT NOT NULL REFERENCES station_handoff_item(handoff_item_uuid),
 lock_key TEXT NOT NULL, material_uuid TEXT REFERENCES material(uuid),
 original_status TEXT, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 PRIMARY KEY(handoff_item_uuid,lock_key)
);
CREATE INDEX IF NOT EXISTS ix_station_handoff_resource_active ON station_handoff_resource(lock_key) WHERE active=1;
CREATE INDEX IF NOT EXISTS ix_station_handoff_resource_material_active ON station_handoff_resource(material_uuid,handoff_item_uuid) WHERE active=1 AND material_uuid IS NOT NULL;
CREATE TABLE IF NOT EXISTS station_handoff_recovery_pending (event_uuid TEXT PRIMARY KEY, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS station_handoff_outbox (
 event_uuid TEXT PRIMARY KEY, handoff_uuid TEXT NOT NULL REFERENCES station_handoff(handoff_uuid),
 purpose TEXT NOT NULL, body_json TEXT NOT NULL, application TEXT, envelope_json TEXT, response_json TEXT,
 UNIQUE(handoff_uuid,purpose)
);
"""


def migrate_handoff_schema(connection: sqlite3.Connection) -> None:
    """幂等建表；库存及保护仍使用原 material/site 权威。"""
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(container_station_binding)")}
    for name in ("registration_request_json", "registration_response_json", "registration_last_error"):
        if name not in columns:
            connection.execute(f"ALTER TABLE container_station_binding ADD COLUMN {name} TEXT")
