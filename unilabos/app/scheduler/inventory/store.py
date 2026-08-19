"""SQLite WAL 持久化层.

单仓储分区单写者：所有写事务经进程内锁 + BEGIN IMMEDIATE 串行化，
业务变更、ledger、outbox 必须在同一事务提交（由 service 层保证，
store 只提供 transaction() 原语与行级 helper）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

SCHEMA_VERSION = 8


class InvalidCursorAdvance(ValueError):
    """ACK cursor 试图回退、跳批或基于过期发送窗口推进."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_template (
    template_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',
    spec_json     TEXT NOT NULL DEFAULT '{}',
    version       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory_lot (
    lot_id             TEXT PRIMARY KEY,
    template_id        TEXT NOT NULL DEFAULT '',
    batch_no           TEXT NOT NULL DEFAULT '',
    unit               TEXT NOT NULL DEFAULT '',
    quantity_total     REAL NOT NULL DEFAULT 0,
    quantity_available REAL NOT NULL DEFAULT 0,
    quantity_reserved  REAL NOT NULL DEFAULT 0,
    expiry             TEXT NOT NULL DEFAULT '',
    quarantined        INTEGER NOT NULL DEFAULT 0,
    warehouse_zone_id  TEXT NOT NULL DEFAULT '',
    created_at         INTEGER NOT NULL DEFAULT 0,
    version            INTEGER NOT NULL DEFAULT 1,
    CHECK (quantity_total >= 0),
    CHECK (quantity_available >= 0),
    CHECK (quantity_reserved >= 0),
    CHECK (quantity_available + quantity_reserved <= quantity_total + 1e-9)
);
CREATE INDEX IF NOT EXISTS idx_lot_template ON inventory_lot(template_id, created_at);

CREATE TABLE IF NOT EXISTS material_instance (
    edge_uuid       TEXT PRIMARY KEY,
    legacy_cloud_id TEXT NOT NULL DEFAULT '',
    lot_id          TEXT NOT NULL DEFAULT '',
    template_id     TEXT NOT NULL DEFAULT '',
    barcode         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'warehouse',
    version         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_instance_barcode ON material_instance(barcode);
CREATE INDEX IF NOT EXISTS idx_instance_legacy ON material_instance(legacy_cloud_id);

CREATE TABLE IF NOT EXISTS resource_relation (
    parent_uuid TEXT NOT NULL,
    slot_id     TEXT NOT NULL DEFAULT '',
    child_uuid  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (child_uuid)
);
CREATE INDEX IF NOT EXISTS idx_relation_parent ON resource_relation(parent_uuid);

CREATE TABLE IF NOT EXISTS substance_content (
    instance_uuid TEXT PRIMARY KEY,
    state_json    TEXT NOT NULL DEFAULT '{}',
    version       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory_reservation (
    reservation_id TEXT PRIMARY KEY,
    workflow_id    TEXT NOT NULL,
    node_id        TEXT NOT NULL DEFAULT '',
    attempt        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'active',
    amounts_json   TEXT NOT NULL DEFAULT '{}',
    created_at     INTEGER NOT NULL DEFAULT 0,
    version        INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservation_idem
    ON inventory_reservation(workflow_id, node_id, attempt);

CREATE TABLE IF NOT EXISTS inventory_ledger (
    ledger_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   INTEGER NOT NULL,
    op_type       TEXT NOT NULL,
    aggregate_type TEXT NOT NULL DEFAULT '',
    aggregate_id  TEXT NOT NULL DEFAULT '',
    delta_json    TEXT NOT NULL DEFAULT '{}',
    actor         TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    causation_id  TEXT NOT NULL DEFAULT '',
    trace_id      TEXT NOT NULL DEFAULT '',
    span_id       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_outbox (
    sequence          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL UNIQUE,
    edge_id           TEXT NOT NULL,
    lab_id            TEXT NOT NULL,
    aggregate_type    TEXT NOT NULL,
    aggregate_id      TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL,
    event_type        TEXT NOT NULL,
    occurred_at       INTEGER NOT NULL,
    causation_id      TEXT NOT NULL DEFAULT '',
    payload_json      TEXT NOT NULL DEFAULT '{}',
    traceparent       TEXT NOT NULL DEFAULT '',
    tracestate        TEXT NOT NULL DEFAULT '',
    trace_id          TEXT NOT NULL DEFAULT '',
    span_id           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS processed_command (
    command_id   TEXT PRIMARY KEY,
    result_json  TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'completed',
    processed_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_cursor (
    cursor_name    TEXT PRIMARY KEY,
    acked_sequence INTEGER NOT NULL DEFAULT 0,
    updated_at     INTEGER NOT NULL DEFAULT 0
);
"""

# v3：旧 material_instance.parent_uuid 在 v5 迁入 canonical material.parent_uuid（单一父）。
# 与 resource_relation 的关系：parent_uuid 列是唯一组成父层级事实；旧 relation 行仅在
# 「父 + 具名位」时存在（slot_id = PLR Site 名 ↔ canonical site.name）。canonical
# site.uuid 是库位稳定身份；旧 View 不暴露它，不能把 label/index 当作 UUID。
# relation.parent_uuid 恒等于本列（_tx_upsert_relation 同步维护）。
# 空串表示顶层物料；单父由列语义天然保证（树形父）。
_SCHEMA_V3_ADD_PARENT = (
    "ALTER TABLE material_instance ADD COLUMN parent_uuid TEXT NOT NULL DEFAULT ''"
)
_SCHEMA_V3_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_instance_parent ON material_instance(parent_uuid)"
)

# v4：W3C Trace Context 与只读关联 ID。全部为 additive、空串兼容旧数据；
# 不把 action/material payload 写入追踪列。
_SCHEMA_V4_COLUMNS = {
    "inventory_ledger": {
        "trace_id": "TEXT NOT NULL DEFAULT ''",
        "span_id": "TEXT NOT NULL DEFAULT ''",
    },
    "sync_outbox": {
        "traceparent": "TEXT NOT NULL DEFAULT ''",
        "tracestate": "TEXT NOT NULL DEFAULT ''",
        "trace_id": "TEXT NOT NULL DEFAULT ''",
        "span_id": "TEXT NOT NULL DEFAULT ''",
    },
}

# v5：共享资源表以 Backend c35d821/000046 为迁移基线；复核至 d552078 时这六表结构未变。
#
# 旧 Inventory 表不是直接删除：迁移先把事实搬入规范
# resource_template/material/site/material_state_history，再用只承载旧字段拼写的
# 可写视图维持 Edge 内部 Inventory 调用。这样公共接口和新写入只有一份 Material / Site
# 事实，同时既有 lot/reservation/ledger 能继续运行。
_SCHEMA_V5_BACKEND_CONTRACT = r"""
BEGIN IMMEDIATE;

ALTER TABLE resource_template RENAME TO resource_template_before_backend_contract;
ALTER TABLE material_instance RENAME TO material_instance_before_backend_contract;
ALTER TABLE resource_relation RENAME TO resource_relation_before_backend_contract;
ALTER TABLE substance_content RENAME TO substance_content_before_backend_contract;

DROP INDEX IF EXISTS idx_instance_barcode;
DROP INDEX IF EXISTS idx_instance_legacy;
DROP INDEX IF EXISTS idx_instance_parent;
DROP INDEX IF EXISTS idx_relation_parent;

CREATE TABLE resource_template (
    uuid TEXT PRIMARY KEY NOT NULL,
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    deleted_at DATETIME,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_data)),
    name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    header TEXT,
    footer TEXT,
    icon TEXT,
    model TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(model)),
    module TEXT,
    language TEXT,
    tags TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags)),
    data_schema TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data_schema)),
    config_schema TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config_schema)),
    pose TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(pose)),
    config_info TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(config_info)),
    cover TEXT,
    scene TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(scene)),
    device_params TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(device_params)),
    manufacturer_uuid TEXT,
    ui_overlay TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(ui_overlay))
);
CREATE UNIQUE INDEX ux_resource_template_name_active
    ON resource_template (name) WHERE deleted_at IS NULL;
CREATE INDEX idx_resource_template_type_active
    ON resource_template (resource_type) WHERE deleted_at IS NULL;

CREATE TABLE resource_handle_template (
    uuid TEXT PRIMARY KEY NOT NULL,
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    deleted_at DATETIME,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_data)),
    resource_template_uuid TEXT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    type TEXT NOT NULL,
    io_type TEXT NOT NULL CHECK (
        io_type IN ('source', 'target', 'bidirectional')
    ),
    source TEXT,
    key TEXT,
    side TEXT,
    FOREIGN KEY (resource_template_uuid) REFERENCES resource_template (uuid)
        ON DELETE RESTRICT
);
CREATE UNIQUE INDEX ux_resource_handle_business_key_active
    ON resource_handle_template (resource_template_uuid, io_type, name)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_resource_handle_template_active
    ON resource_handle_template (resource_template_uuid)
    WHERE deleted_at IS NULL;

CREATE TABLE resource_template_inventory (
    resource_template_uuid TEXT PRIMARY KEY,
    aggregate_version INTEGER NOT NULL DEFAULT 1 CHECK (aggregate_version > 0),
    FOREIGN KEY (resource_template_uuid) REFERENCES resource_template (uuid)
        ON DELETE RESTRICT
);

CREATE TABLE material (
    uuid TEXT PRIMARY KEY NOT NULL,
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    deleted_at DATETIME,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_data)),
    resource_template_uuid TEXT NOT NULL,
    parent_uuid TEXT,
    class TEXT NOT NULL,
    barcode TEXT NOT NULL,
    name TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config)),
    data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data)),
    CHECK (parent_uuid IS NULL OR parent_uuid <> uuid),
    FOREIGN KEY (resource_template_uuid) REFERENCES resource_template (uuid)
        ON DELETE RESTRICT,
    FOREIGN KEY (parent_uuid) REFERENCES material (uuid) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX ux_barcode_active
    ON material (LOWER(barcode))
    WHERE deleted_at IS NULL AND barcode <> '';
CREATE UNIQUE INDEX ux_material_root_name_active
    ON material (LOWER(name))
    WHERE deleted_at IS NULL AND parent_uuid IS NULL;
CREATE INDEX idx_material_template_active
    ON material (resource_template_uuid) WHERE deleted_at IS NULL;
CREATE INDEX idx_material_parent_active
    ON material (parent_uuid)
    WHERE deleted_at IS NULL AND parent_uuid IS NOT NULL;

CREATE TABLE relative_position (
    uuid TEXT PRIMARY KEY NOT NULL,
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    deleted_at DATETIME,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_data)),
    material_uuid TEXT NOT NULL,
    position_x REAL NOT NULL DEFAULT 0,
    position_y REAL NOT NULL DEFAULT 0,
    position_z REAL NOT NULL DEFAULT 0,
    depth REAL NOT NULL DEFAULT 0 CHECK (depth >= 0),
    length REAL NOT NULL DEFAULT 0 CHECK (length >= 0),
    width REAL NOT NULL DEFAULT 0 CHECK (width >= 0),
    scale_x REAL NOT NULL DEFAULT 1 CHECK (scale_x > 0),
    scale_y REAL NOT NULL DEFAULT 1 CHECK (scale_y > 0),
    scale_z REAL NOT NULL DEFAULT 1 CHECK (scale_z > 0),
    rotation_x REAL NOT NULL DEFAULT 0,
    rotation_y REAL NOT NULL DEFAULT 0,
    rotation_z REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (material_uuid) REFERENCES material (uuid) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX ux_relative_position_material_active
    ON relative_position (material_uuid) WHERE deleted_at IS NULL;

CREATE TABLE material_inventory (
    material_uuid TEXT PRIMARY KEY,
    legacy_cloud_id TEXT NOT NULL DEFAULT '',
    legacy_template_id TEXT NOT NULL DEFAULT '',
    lot_id TEXT NOT NULL DEFAULT '',
    inventory_status TEXT NOT NULL DEFAULT 'warehouse',
    disposition TEXT NOT NULL DEFAULT 'active',
    aggregate_version INTEGER NOT NULL DEFAULT 1 CHECK (aggregate_version > 0),
    FOREIGN KEY (material_uuid) REFERENCES material (uuid) ON DELETE RESTRICT
);
CREATE INDEX idx_material_inventory_legacy
    ON material_inventory (legacy_cloud_id);

CREATE TABLE material_content_version (
    material_uuid TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    FOREIGN KEY (material_uuid) REFERENCES material (uuid) ON DELETE RESTRICT
);

CREATE TABLE site (
    uuid TEXT PRIMARY KEY NOT NULL DEFAULT (
        lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' ||
        substr(hex(randomblob(2)), 2) || '-' ||
        substr('89ab', abs(random()) % 4 + 1, 1) ||
        substr(hex(randomblob(2)), 2) || '-' || hex(randomblob(6)))
    ),
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    deleted_at DATETIME,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_data)),
    material_uuid TEXT NOT NULL,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    allowed_resource_template_uuids TEXT NOT NULL DEFAULT '[]'
        CHECK (
            json_valid(allowed_resource_template_uuids)
            AND json_type(allowed_resource_template_uuids) = 'array'
        ),
    occupied_material_uuid TEXT,
    position_x REAL NOT NULL DEFAULT 0,
    position_y REAL NOT NULL DEFAULT 0,
    position_z REAL NOT NULL DEFAULT 0,
    depth REAL NOT NULL DEFAULT 0 CHECK (depth >= 0),
    length REAL NOT NULL DEFAULT 0 CHECK (length >= 0),
    width REAL NOT NULL DEFAULT 0 CHECK (width >= 0),
    CHECK (occupied_material_uuid IS NULL OR occupied_material_uuid <> material_uuid),
    FOREIGN KEY (material_uuid) REFERENCES material (uuid) ON DELETE RESTRICT,
    FOREIGN KEY (occupied_material_uuid) REFERENCES material (uuid) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX ux_site_material_name_active
    ON site (material_uuid, LOWER(name)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX ux_site_occupied_material_active
    ON site (occupied_material_uuid)
    WHERE deleted_at IS NULL AND occupied_material_uuid IS NOT NULL;
CREATE INDEX idx_site_material_order_active
    ON site (material_uuid, sort_order, create_time, uuid)
    WHERE deleted_at IS NULL;

CREATE TABLE material_state_history (
    uuid TEXT PRIMARY KEY NOT NULL DEFAULT (
        lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' ||
        substr(hex(randomblob(2)), 2) || '-' ||
        substr('89ab', abs(random()) % 4 + 1, 1) ||
        substr(hex(randomblob(2)), 2) || '-' || hex(randomblob(6)))
    ),
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    deleted_at DATETIME,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_data)),
    material_uuid TEXT NOT NULL,
    status TEXT,
    state_data TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(state_data) AND json_type(state_data) = 'object'),
    source TEXT,
    observed_at DATETIME NOT NULL,
    FOREIGN KEY (material_uuid) REFERENCES material (uuid) ON DELETE RESTRICT
);
CREATE INDEX idx_material_state_history_timeline
    ON material_state_history (material_uuid, observed_at DESC, uuid DESC);

INSERT INTO resource_template (
    uuid, create_time, update_time, deleted_at, description, meta_data,
    name, display_name, resource_type, model, tags, data_schema,
    config_schema, pose, config_info, scene, device_params, ui_overlay
)
SELECT
    template_id,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    NULL,
    NULL,
    '{}',
    template_id,
    CASE WHEN name = '' THEN template_id ELSE name END,
    CASE WHEN category = '' THEN 'resource' ELSE category END,
    CASE WHEN json_valid(spec_json) THEN spec_json ELSE '{}' END,
    '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}'
FROM resource_template_before_backend_contract;

-- 旧 Edge 允许先登记实例、后同步模板。规范 Material 必须始终引用一个模板，
-- 因此为这类引用创建“已软删除”的占位模板；后续模板同步会原位复活它。
INSERT OR IGNORE INTO resource_template (
    uuid, create_time, update_time, deleted_at, description, meta_data,
    name, display_name, resource_type, model, tags, data_schema,
    config_schema, pose, config_info, scene, device_params, ui_overlay
)
SELECT DISTINCT
    CASE WHEN template_id = '' THEN '__edge_unknown_resource_template__'
         ELSE template_id END,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    'Edge legacy placeholder; hidden from the shared Backend Interface',
    '{"unilab_edge_placeholder":true}',
    CASE WHEN template_id = '' THEN '__edge_unknown_resource_template__'
         ELSE template_id END,
    CASE WHEN template_id = '' THEN 'Unknown Edge resource'
         ELSE template_id END,
    'resource', '{}', '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}'
FROM material_instance_before_backend_contract;

INSERT OR IGNORE INTO resource_template (
    uuid, create_time, update_time, deleted_at, description, meta_data,
    name, display_name, resource_type, model, tags, data_schema,
    config_schema, pose, config_info, scene, device_params, ui_overlay
) VALUES (
    '__edge_unknown_resource_template__',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    'Edge legacy placeholder; hidden from the shared Backend Interface',
    '{"unilab_edge_placeholder":true}',
    '__edge_unknown_resource_template__', 'Unknown Edge resource',
    'resource', '{}', '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}'
);

INSERT INTO resource_template_inventory(resource_template_uuid, aggregate_version)
SELECT template_id, CASE WHEN version > 0 THEN version ELSE 1 END
FROM resource_template_before_backend_contract;

INSERT INTO material (
    uuid, create_time, update_time, deleted_at, description, meta_data,
    resource_template_uuid, parent_uuid, class, barcode, name, config, data
)
SELECT
    legacy.edge_uuid,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    CASE legacy.status
        WHEN 'consumed' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHEN 'discarded' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ELSE NULL
    END,
    NULL,
    json_object(
        'unilab_legacy',
        json_object('barcode', legacy.barcode, 'cloud_id', legacy.legacy_cloud_id)
    ),
    COALESCE(NULLIF(legacy.template_id, ''), '__edge_unknown_resource_template__'),
    NULL,
    'resource',
    CASE
        WHEN legacy.barcode = '' THEN ''
        WHEN legacy.status IN ('consumed', 'discarded') THEN legacy.barcode
        WHEN legacy.edge_uuid = (
            SELECT MIN(other.edge_uuid)
            FROM material_instance_before_backend_contract AS other
            WHERE LOWER(other.barcode) = LOWER(legacy.barcode)
              AND other.status NOT IN ('consumed', 'discarded')
        ) THEN legacy.barcode
        ELSE ''
    END,
    legacy.edge_uuid,
    '{}',
    COALESCE(
        (SELECT CASE WHEN json_valid(content.state_json) THEN content.state_json ELSE '{}' END
         FROM substance_content_before_backend_contract AS content
         WHERE content.instance_uuid = legacy.edge_uuid),
        '{}'
    )
FROM material_instance_before_backend_contract AS legacy;

-- v1-v4 接受未先登记的父容器。把这些标识提升为明确标记的规范 Material，
-- 从而既保留旧关系，又不放宽 Backend 的外键不变量。
INSERT OR IGNORE INTO material (
    uuid, create_time, update_time, deleted_at, description, meta_data,
    resource_template_uuid, parent_uuid, class, barcode, name, config, data
)
SELECT DISTINCT
    parent_uuid,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    NULL,
    'Edge legacy parent placeholder',
    '{"unilab_edge_placeholder":true}',
    '__edge_unknown_resource_template__', NULL, 'resource', '',
    '__edge_placeholder__:' || parent_uuid, '{}', '{}'
FROM (
    SELECT parent_uuid
    FROM material_instance_before_backend_contract
    WHERE parent_uuid <> ''
    UNION
    SELECT parent_uuid
    FROM resource_relation_before_backend_contract
    WHERE parent_uuid <> ''
)
WHERE parent_uuid NOT IN (SELECT uuid FROM material);

UPDATE material
SET parent_uuid = (
    SELECT NULLIF(legacy.parent_uuid, '')
    FROM material_instance_before_backend_contract AS legacy
    WHERE legacy.edge_uuid = material.uuid
)
WHERE EXISTS (
    SELECT 1
    FROM material_instance_before_backend_contract AS legacy
    JOIN material AS parent ON parent.uuid = legacy.parent_uuid
    WHERE legacy.edge_uuid = material.uuid
      AND legacy.parent_uuid <> ''
      AND legacy.parent_uuid <> legacy.edge_uuid
);

INSERT INTO material_inventory(
    material_uuid, legacy_cloud_id, legacy_template_id, lot_id, inventory_status,
    disposition, aggregate_version
)
SELECT
    edge_uuid,
    legacy_cloud_id,
    template_id,
    lot_id,
    status,
    CASE status
        WHEN 'consumed' THEN 'consumed'
        WHEN 'discarded' THEN 'discarded'
        WHEN 'quarantined' THEN 'quarantined'
        ELSE 'active'
    END,
    CASE WHEN version > 0 THEN version ELSE 1 END
FROM material_instance_before_backend_contract;

INSERT OR IGNORE INTO material_inventory(
    material_uuid, legacy_template_id, inventory_status, aggregate_version
)
SELECT material.uuid, '', 'warehouse', 1
FROM material
WHERE json_extract(material.meta_data, '$.unilab_edge_placeholder') = 1;

INSERT INTO material_content_version(material_uuid, version)
SELECT content.instance_uuid, CASE WHEN content.version > 0 THEN content.version ELSE 1 END
FROM substance_content_before_backend_contract AS content
JOIN material ON material.uuid = content.instance_uuid;

INSERT INTO site (
    create_time, update_time, deleted_at, description, meta_data,
    material_uuid, name, sort_order, allowed_resource_template_uuids,
    occupied_material_uuid, position_x, position_y, position_z,
    depth, length, width
)
SELECT
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    NULL,
    NULL,
    '{}',
    relation.parent_uuid,
    relation.slot_id,
    0,
    '[]',
    MIN(relation.child_uuid),
    0, 0, 0, 0, 0, 0
FROM resource_relation_before_backend_contract AS relation
JOIN material AS owner ON owner.uuid = relation.parent_uuid
JOIN material AS occupant ON occupant.uuid = relation.child_uuid
WHERE relation.slot_id <> ''
GROUP BY relation.parent_uuid, LOWER(relation.slot_id);

DROP TABLE resource_relation_before_backend_contract;
DROP TABLE material_instance_before_backend_contract;
DROP TABLE resource_template_before_backend_contract;
DROP TABLE substance_content_before_backend_contract;

CREATE VIEW inventory_resource_template AS
SELECT
    template.uuid AS template_id,
    template.display_name AS name,
    template.resource_type AS category,
    template.model AS spec_json,
    inventory.aggregate_version AS version
FROM resource_template AS template
JOIN resource_template_inventory AS inventory
    ON inventory.resource_template_uuid = template.uuid
WHERE template.deleted_at IS NULL;

CREATE TRIGGER inventory_resource_template_insert
INSTEAD OF INSERT ON inventory_resource_template
BEGIN
    INSERT INTO resource_template (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        name, display_name, resource_type, model, tags, data_schema,
        config_schema, pose, config_info, scene, device_params, ui_overlay
    ) VALUES (
        NEW.template_id,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        NULL, NULL, '{}',
        NEW.template_id,
        CASE WHEN NEW.name = '' THEN NEW.template_id ELSE NEW.name END,
        CASE WHEN NEW.category = '' THEN 'resource' ELSE NEW.category END,
        CASE WHEN json_valid(NEW.spec_json) THEN NEW.spec_json ELSE '{}' END,
        '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}'
    )
    ON CONFLICT(uuid) DO UPDATE SET
        update_time = excluded.update_time,
        deleted_at = NULL,
        display_name = excluded.display_name,
        resource_type = excluded.resource_type,
        model = excluded.model;
    INSERT INTO resource_template_inventory(resource_template_uuid, aggregate_version)
    VALUES (NEW.template_id, CASE WHEN NEW.version > 0 THEN NEW.version ELSE 1 END)
    ON CONFLICT(resource_template_uuid) DO UPDATE SET
        aggregate_version = excluded.aggregate_version;
END;

CREATE TRIGGER inventory_resource_template_update
INSTEAD OF UPDATE ON inventory_resource_template
BEGIN
    UPDATE resource_template
    SET display_name = CASE WHEN NEW.name = '' THEN NEW.template_id ELSE NEW.name END,
        resource_type = CASE WHEN NEW.category = '' THEN 'resource' ELSE NEW.category END,
        model = CASE WHEN json_valid(NEW.spec_json) THEN NEW.spec_json ELSE '{}' END,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = OLD.template_id AND deleted_at IS NULL;
    UPDATE resource_template_inventory
    SET aggregate_version = NEW.version
    WHERE resource_template_uuid = OLD.template_id;
END;

CREATE TRIGGER inventory_resource_template_delete
INSTEAD OF DELETE ON inventory_resource_template
BEGIN
    UPDATE resource_template
    SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = OLD.template_id AND deleted_at IS NULL;
    UPDATE resource_handle_template
    SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE resource_template_uuid = OLD.template_id AND deleted_at IS NULL;
END;

CREATE VIEW material_instance AS
SELECT
    material.uuid AS edge_uuid,
    inventory.legacy_cloud_id AS legacy_cloud_id,
    inventory.lot_id AS lot_id,
    inventory.legacy_template_id AS template_id,
    material.barcode AS barcode,
    inventory.inventory_status AS status,
    inventory.aggregate_version AS version,
    COALESCE(material.parent_uuid, '') AS parent_uuid
FROM material AS material
JOIN material_inventory AS inventory ON inventory.material_uuid = material.uuid
;

CREATE TRIGGER material_instance_insert
INSTEAD OF INSERT ON material_instance
BEGIN
    INSERT OR IGNORE INTO resource_template (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        name, display_name, resource_type, model, tags, data_schema,
        config_schema, pose, config_info, scene, device_params, ui_overlay
    ) VALUES (
        COALESCE(NULLIF(NEW.template_id, ''), '__edge_unknown_resource_template__'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        'Edge legacy placeholder; hidden from the shared Backend Interface',
        '{"unilab_edge_placeholder":true}',
        COALESCE(NULLIF(NEW.template_id, ''), '__edge_unknown_resource_template__'),
        COALESCE(NULLIF(NEW.template_id, ''), 'Unknown Edge resource'),
        'resource', '{}', '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}'
    );
    INSERT OR IGNORE INTO resource_template_inventory(
        resource_template_uuid, aggregate_version
    ) VALUES (
        COALESCE(NULLIF(NEW.template_id, ''), '__edge_unknown_resource_template__'), 1
    );
    INSERT INTO material (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        resource_template_uuid, parent_uuid, class, barcode, name, config, data
    ) VALUES (
        NEW.edge_uuid,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        NULL, NULL, '{}',
        COALESCE(NULLIF(NEW.template_id, ''), '__edge_unknown_resource_template__'),
        NULL,
        'resource', NEW.barcode, NEW.edge_uuid, '{}', '{}'
    );
    INSERT INTO material_inventory(
        material_uuid, legacy_cloud_id, legacy_template_id, lot_id, inventory_status,
        disposition, aggregate_version
    ) VALUES (
        NEW.edge_uuid, NEW.legacy_cloud_id, NEW.template_id, NEW.lot_id, NEW.status,
        CASE NEW.status
            WHEN 'consumed' THEN 'consumed'
            WHEN 'discarded' THEN 'discarded'
            WHEN 'quarantined' THEN 'quarantined'
            ELSE 'active'
        END,
        CASE WHEN NEW.version > 0 THEN NEW.version ELSE 1 END
    );
END;

CREATE TRIGGER material_instance_update
INSTEAD OF UPDATE ON material_instance
BEGIN
    UPDATE material
    SET resource_template_uuid = COALESCE(
            NULLIF(NEW.template_id, ''), '__edge_unknown_resource_template__'
        ),
        parent_uuid = NULLIF(NEW.parent_uuid, ''),
        barcode = NEW.barcode,
        deleted_at = CASE NEW.status
            WHEN 'consumed' THEN COALESCE(
                material.deleted_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            WHEN 'discarded' THEN COALESCE(
                material.deleted_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
            ELSE NULL
        END,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = OLD.edge_uuid;
    UPDATE material_inventory
    SET legacy_cloud_id = NEW.legacy_cloud_id,
        legacy_template_id = NEW.template_id,
        lot_id = NEW.lot_id,
        inventory_status = NEW.status,
        disposition = CASE NEW.status
            WHEN 'consumed' THEN 'consumed'
            WHEN 'discarded' THEN 'discarded'
            WHEN 'quarantined' THEN 'quarantined'
            ELSE 'active'
        END,
        aggregate_version = NEW.version
    WHERE material_uuid = OLD.edge_uuid;
END;

CREATE VIEW resource_relation AS
SELECT
    site.material_uuid AS parent_uuid,
    site.name AS slot_id,
    material.uuid AS child_uuid,
    inventory.aggregate_version AS version
FROM material AS material
JOIN material_inventory AS inventory ON inventory.material_uuid = material.uuid
LEFT JOIN site AS site
    ON site.occupied_material_uuid = material.uuid AND site.deleted_at IS NULL
WHERE material.deleted_at IS NULL
  AND site.uuid IS NOT NULL;

CREATE TRIGGER resource_relation_insert
INSTEAD OF INSERT ON resource_relation
BEGIN
    INSERT OR IGNORE INTO resource_template (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        name, display_name, resource_type, model, tags, data_schema,
        config_schema, pose, config_info, scene, device_params, ui_overlay
    ) VALUES (
        '__edge_unknown_resource_template__',
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        'Edge legacy placeholder; hidden from the shared Backend Interface',
        '{"unilab_edge_placeholder":true}',
        '__edge_unknown_resource_template__', 'Unknown Edge resource',
        'resource', '{}', '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}'
    );
    INSERT OR IGNORE INTO resource_template_inventory(
        resource_template_uuid, aggregate_version
    ) VALUES ('__edge_unknown_resource_template__', 1);
    INSERT OR IGNORE INTO material (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        resource_template_uuid, parent_uuid, class, barcode, name, config, data
    ) VALUES (
        NEW.parent_uuid,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        NULL,
        'Edge legacy parent placeholder',
        '{"unilab_edge_placeholder":true}',
        '__edge_unknown_resource_template__', NULL, 'resource', '',
        '__edge_placeholder__:' || NEW.parent_uuid, '{}', '{}'
    );
    INSERT OR IGNORE INTO material_inventory(
        material_uuid, legacy_template_id, inventory_status, aggregate_version
    ) VALUES (NEW.parent_uuid, '', 'warehouse', 1);
    UPDATE material
    SET parent_uuid = NULLIF(NEW.parent_uuid, ''),
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = NEW.child_uuid AND deleted_at IS NULL;
    UPDATE site
    SET occupied_material_uuid = NULL,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE occupied_material_uuid = NEW.child_uuid AND deleted_at IS NULL;
    INSERT OR IGNORE INTO site (
        create_time, update_time, deleted_at, description, meta_data,
        material_uuid, name, sort_order, allowed_resource_template_uuids,
        occupied_material_uuid, position_x, position_y, position_z,
        depth, length, width
    )
    SELECT
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        NULL, NULL, '{}', NEW.parent_uuid, NEW.slot_id, 0, '[]',
        NEW.child_uuid, 0, 0, 0, 0, 0, 0
    WHERE NEW.slot_id <> '';
    UPDATE site
    SET occupied_material_uuid = NEW.child_uuid,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE material_uuid = NEW.parent_uuid
      AND LOWER(name) = LOWER(NEW.slot_id)
      AND deleted_at IS NULL
      AND NEW.slot_id <> '';
END;

CREATE TRIGGER resource_relation_update
INSTEAD OF UPDATE ON resource_relation
BEGIN
    DELETE FROM resource_relation WHERE child_uuid = OLD.child_uuid;
    INSERT INTO resource_relation(parent_uuid, slot_id, child_uuid, version)
    VALUES (NEW.parent_uuid, NEW.slot_id, NEW.child_uuid, NEW.version);
END;

CREATE TRIGGER resource_relation_delete
INSTEAD OF DELETE ON resource_relation
BEGIN
    UPDATE site
    SET occupied_material_uuid = NULL,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE occupied_material_uuid = OLD.child_uuid AND deleted_at IS NULL;
END;

CREATE VIEW substance_content AS
SELECT
    material.uuid AS instance_uuid,
    material.data AS state_json,
    content.version AS version
FROM material
JOIN material_content_version AS content ON content.material_uuid = material.uuid
WHERE material.deleted_at IS NULL;

CREATE TRIGGER substance_content_insert
INSTEAD OF INSERT ON substance_content
BEGIN
    UPDATE material
    SET data = CASE WHEN json_valid(NEW.state_json) THEN NEW.state_json ELSE '{}' END,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = NEW.instance_uuid AND deleted_at IS NULL;
    INSERT INTO material_content_version(material_uuid, version)
    VALUES (NEW.instance_uuid, CASE WHEN NEW.version > 0 THEN NEW.version ELSE 1 END);
END;

CREATE TRIGGER substance_content_update
INSTEAD OF UPDATE ON substance_content
BEGIN
    UPDATE material
    SET data = CASE WHEN json_valid(NEW.state_json) THEN NEW.state_json ELSE '{}' END,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = OLD.instance_uuid AND deleted_at IS NULL;
    UPDATE material_content_version
    SET version = NEW.version
    WHERE material_uuid = OLD.instance_uuid;
END;

PRAGMA user_version = 5;
COMMIT;
"""

# v6：对齐 Backend feat/workflow 的 000050/000051。type 是 Material 实例事实，
# 由冻结模板组件派生；兼容 material_instance View 只读投影该列，不授予旧请求写权。
_SCHEMA_V6_MATERIAL_TYPE_BACKFILL = r"""
UPDATE material
SET type = COALESCE(
    NULLIF(TRIM(
        CASE
            WHEN TRIM(class) = TRIM((
                SELECT name
                FROM resource_template
                WHERE resource_template.uuid = material.resource_template_uuid
            )) THEN (
                SELECT json_extract(config_info, '$[0].type')
                FROM resource_template
                WHERE resource_template.uuid = material.resource_template_uuid
            )
            ELSE (
                SELECT json_extract(component.value, '$.type')
                FROM resource_template
                JOIN json_each(resource_template.config_info) AS component
                WHERE resource_template.uuid = material.resource_template_uuid
                  AND TRIM(COALESCE(
                      NULLIF(json_extract(component.value, '$.name'), ''),
                      json_extract(component.value, '$.id'),
                      ''
                  )) = TRIM(material.name)
                LIMIT 1
            )
        END
    ), ''),
    NULLIF(TRIM((
        SELECT resource_type
        FROM resource_template
        WHERE resource_template.uuid = material.resource_template_uuid
    )), ''),
    'resource'
);
CREATE INDEX IF NOT EXISTS idx_material_type_active
    ON material (LOWER(TRIM(type)))
    WHERE deleted_at IS NULL;
"""

_SCHEMA_V6_MATERIAL_INSTANCE_VIEW = r"""
CREATE VIEW material_instance AS
SELECT
    material.uuid AS edge_uuid,
    inventory.legacy_cloud_id AS legacy_cloud_id,
    inventory.lot_id AS lot_id,
    inventory.legacy_template_id AS template_id,
    material.barcode AS barcode,
    material.type AS type,
    inventory.inventory_status AS status,
    inventory.aggregate_version AS version,
    COALESCE(material.parent_uuid, '') AS parent_uuid
FROM material AS material
JOIN material_inventory AS inventory ON inventory.material_uuid = material.uuid;
"""

# v7: Backend persists Site content types independently from the currently
# registered Resource Templates. Admission resolves these semantic tags only
# when a Material is placed, so later template registration remains effective.
_SCHEMA_V7_SITE_CONTENT_TYPE = (
    "ALTER TABLE site ADD COLUMN content_type "
    "TEXT NOT NULL DEFAULT '[]' "
    "CHECK (json_valid(content_type) AND json_type(content_type) = 'array')"
)

# v8：与 ResourceSite schema_version=1 对齐。``name``/``sort_order`` 继续作为
# Backend/旧 Edge 物理兼容字段；规范接口分别投影为 ``label`` 和 ``index``。
# index 允许 int 或 str，因此用 JSON 标量保存，避免把 "01" 错写成整数 1。
_SCHEMA_V8_SITE_MODEL_COLUMNS = {
    "schema_version": "INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1)",
    "site_index": (
        "TEXT NOT NULL DEFAULT '0' CHECK ("
        "json_valid(site_index) AND json_type(site_index) IN ('integer', 'text'))"
    ),
    "visible": "INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1))",
    "rotation_x": "REAL NOT NULL DEFAULT 0",
    "rotation_y": "REAL NOT NULL DEFAULT 0",
    "rotation_z": "REAL NOT NULL DEFAULT 0",
    "parent_link": "TEXT NOT NULL DEFAULT ''",
}

# v2：实验室操作系统布局层（元信息 / 分区 / 2D 摆放）。
# 只增表不改旧表，v1 库可原地升级。
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS lab_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lab_zone (
    zone_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT 'bench',
    x         REAL NOT NULL DEFAULT 0,
    y         REAL NOT NULL DEFAULT 0,
    w         REAL NOT NULL DEFAULT 100,
    h         REAL NOT NULL DEFAULT 100,
    meta_json TEXT NOT NULL DEFAULT '{}',
    version   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lab_placement (
    subject_id   TEXT PRIMARY KEY,
    subject_kind TEXT NOT NULL DEFAULT 'container',
    zone_id      TEXT NOT NULL DEFAULT '',
    x            REAL NOT NULL DEFAULT 0,
    y            REAL NOT NULL DEFAULT 0,
    w            REAL NOT NULL DEFAULT 40,
    h            REAL NOT NULL DEFAULT 40,
    rotation     REAL NOT NULL DEFAULT 0,
    label        TEXT NOT NULL DEFAULT '',
    meta_json    TEXT NOT NULL DEFAULT '{}',
    version      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_placement_zone ON lab_placement(zone_id);
"""


class InventoryStore:
    """SQLite WAL 存储：单连接 + 进程内写锁（单写者）."""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            compatibility_object = self._conn.execute(
                "SELECT type FROM sqlite_master WHERE name='material_instance'"
            ).fetchone()
            canonical_material_object = self._conn.execute(
                "SELECT type FROM sqlite_master WHERE name='material'"
            ).fetchone()

            # 本分支合入 canonical v5 前曾短暂发布过一版 Edge-local v5：它仍以
            # material_instance 物理表为事实，只额外增加了 type 列。其
            # user_version 与新的 canonical v5 撞号，不能按版本号直接跳过迁移。
            #
            # 先把实例 type 保存到一个可跨进程崩溃恢复的临时迁移表；canonical
            # v5 会删除旧物理表，v6 完成 Backend 规则回填后再以旧实例事实覆盖并
            # 删除备份表。这样即使进程在 v5 COMMIT 后退出，下次启动仍能恢复 type。
            if (
                current == 5
                and compatibility_object is not None
                and compatibility_object[0] == "table"
                and canonical_material_object is None
            ):
                legacy_columns = {
                    row[1]
                    for row in self._conn.execute(
                        "PRAGMA table_info(material_instance)"
                    ).fetchall()
                }
                if "type" not in legacy_columns:
                    raise RuntimeError(
                        "unsupported inventory schema v5: physical "
                        "material_instance table has no type column"
                    )
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    "_edge_v5_material_type_backup ("
                    "material_uuid TEXT PRIMARY KEY, type TEXT NOT NULL)"
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO _edge_v5_material_type_backup "
                    "SELECT edge_uuid, type FROM material_instance"
                )
                self._conn.commit()
                # 结构等价于 v4 + type；让下面的 canonical v5 迁移正常执行。
                current = 4
            # user_version 可能被备份/测试工具错误降写。规范 v5 以可写兼容视图
            # 为结构指纹；已是 v5 时绝不能再次运行旧表 ALTER 或重命名迁移。
            if (
                current < 5
                and compatibility_object is not None
                and compatibility_object[0] == "view"
            ):
                current = 5
                self._conn.execute("PRAGMA user_version = 5")
                self._conn.commit()
            if current < 1:
                self._conn.executescript(_SCHEMA)
            if current < 2:
                self._conn.executescript(_SCHEMA_V2)
            if current < 3:
                # ALTER 前先查列（半途中断的迁移可安全重放）
                cols = {
                    r[1]
                    for r in self._conn.execute(
                        "PRAGMA table_info(material_instance)"
                    ).fetchall()
                }
                if "parent_uuid" not in cols:
                    self._conn.execute(_SCHEMA_V3_ADD_PARENT)
                self._conn.execute(_SCHEMA_V3_INDEX)
                # v1/v2 只有 relation 父事实；新列为空时可确定性补齐。非空值永不
                # 覆盖，避免在伪降版/半迁移数据库里静默猜测冲突。
                self._conn.execute(
                    "UPDATE material_instance SET parent_uuid = ("
                    "SELECT r.parent_uuid FROM resource_relation r "
                    "WHERE r.child_uuid = material_instance.edge_uuid"
                    ") WHERE parent_uuid = '' AND EXISTS ("
                    "SELECT 1 FROM resource_relation r "
                    "WHERE r.child_uuid = material_instance.edge_uuid"
                    ")"
                )
            if current < 4:
                for table, columns in _SCHEMA_V4_COLUMNS.items():
                    existing = {
                        row[1]
                        for row in self._conn.execute(
                            f"PRAGMA table_info({table})"
                        ).fetchall()
                    }
                    for column, definition in columns.items():
                        if column not in existing:
                            self._conn.execute(
                                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                            )
            if current < 5:
                try:
                    self._conn.executescript(_SCHEMA_V5_BACKEND_CONTRACT)
                except BaseException:
                    self._conn.rollback()
                    raise
            if current < 6:
                material_columns = {
                    row[1]
                    for row in self._conn.execute(
                        "PRAGMA table_info(material)"
                    ).fetchall()
                }
                if "type" not in material_columns:
                    self._conn.execute(
                        "ALTER TABLE material ADD COLUMN type "
                        "TEXT NOT NULL DEFAULT 'resource'"
                    )
                self._conn.executescript(_SCHEMA_V6_MATERIAL_TYPE_BACKFILL)

                # SQLite cannot ALTER a View. Preserve the already-tested v5
                # INSTEAD OF trigger bodies while replacing only its projection.
                trigger_rows = self._conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                    "AND name IN ('material_instance_insert', "
                    "'material_instance_update') ORDER BY name"
                ).fetchall()
                trigger_sql = [row["sql"] for row in trigger_rows if row["sql"]]
                if len(trigger_sql) != 2:
                    raise RuntimeError(
                        "material_instance compatibility triggers are incomplete"
                    )
                self._conn.execute("DROP TRIGGER material_instance_insert")
                self._conn.execute("DROP TRIGGER material_instance_update")
                self._conn.execute("DROP VIEW material_instance")
                self._conn.executescript(_SCHEMA_V6_MATERIAL_INSTANCE_VIEW)
                for statement in trigger_sql:
                    self._conn.execute(statement)

                type_backup = self._conn.execute(
                    "SELECT type FROM sqlite_master "
                    "WHERE name='_edge_v5_material_type_backup'"
                ).fetchone()
                if type_backup is not None:
                    self._conn.execute(
                        "UPDATE material SET type = ("
                        "SELECT TRIM(backup.type) "
                        "FROM _edge_v5_material_type_backup AS backup "
                        "WHERE backup.material_uuid = material.uuid"
                        ") WHERE EXISTS ("
                        "SELECT 1 FROM _edge_v5_material_type_backup AS backup "
                        "WHERE backup.material_uuid = material.uuid "
                        "AND TRIM(backup.type) <> '')"
                    )
                    self._conn.execute("DROP TABLE _edge_v5_material_type_backup")
            if current < 7:
                site_columns = {
                    row[1]
                    for row in self._conn.execute("PRAGMA table_info(site)").fetchall()
                }
                if "content_type" not in site_columns:
                    self._conn.execute(_SCHEMA_V7_SITE_CONTENT_TYPE)
            if current < 8:
                site_columns = {
                    row[1]
                    for row in self._conn.execute("PRAGMA table_info(site)").fetchall()
                }
                for column, definition in _SCHEMA_V8_SITE_MODEL_COLUMNS.items():
                    if column not in site_columns:
                        self._conn.execute(
                            f"ALTER TABLE site ADD COLUMN {column} {definition}"
                        )
                # 老数据只有非负 sort_order；将其确定性提升为整数 index。条件更新
                # 使半途中断的迁移可安全重放，并保留已写入的字符串 index。
                self._conn.execute(
                    "UPDATE site SET site_index = CAST(sort_order AS TEXT) "
                    "WHERE site_index = '0' AND sort_order <> 0"
                )
            if current < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 事务原语 -----------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """串行化写事务：业务行 + ledger + outbox 在此上下文内一起提交."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # -- 只读 helper ---------------------------------------------------------

    def query_one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def query_all(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # -- 常用读 -------------------------------------------------------------

    def get_lot(self, lot_id: str) -> Optional[Dict[str, Any]]:
        return self.query_one("SELECT * FROM inventory_lot WHERE lot_id = ?", (lot_id,))

    def list_lots(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM inventory_lot ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )

    def get_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM inventory_resource_template WHERE template_id = ?",
            (template_id,),
        )

    def list_templates(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM inventory_resource_template ORDER BY template_id LIMIT ?",
            (limit,),
        )

    def get_instance(self, edge_uuid: str) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM material_instance WHERE edge_uuid = ?", (edge_uuid,)
        )

    def list_instances(
        self, status: str = "", limit: int = 500
    ) -> List[Dict[str, Any]]:
        if status:
            return self.query_all(
                "SELECT * FROM material_instance WHERE status = ? AND edge_uuid NOT IN ("
                "SELECT uuid FROM material WHERE "
                "json_extract(meta_data, '$.unilab_edge_placeholder') = 1) "
                "ORDER BY edge_uuid DESC LIMIT ?",
                (status, limit),
            )
        return self.query_all(
            "SELECT * FROM material_instance WHERE edge_uuid NOT IN ("
            "SELECT uuid FROM material WHERE "
            "json_extract(meta_data, '$.unilab_edge_placeholder') = 1) "
            "ORDER BY edge_uuid DESC LIMIT ?",
            (limit,),
        )

    def find_instance_by_barcode_active(
        self, barcode: str, active_states: tuple
    ) -> Optional[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in active_states)
        return self.query_one(
            f"SELECT * FROM material_instance WHERE barcode = ? AND status IN ({placeholders})",
            (barcode, *active_states),
        )

    def find_instance_by_legacy_cloud_id(
        self, cloud_id: str
    ) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM material_instance WHERE legacy_cloud_id = ?", (cloud_id,)
        )

    def lots_by_template_fifo(self, template_id: str) -> List[Dict[str, Any]]:
        """FIFO：按 created_at 升序（同毫秒按 rowid 插入序）返回可用批次."""
        return self.query_all(
            "SELECT * FROM inventory_lot WHERE template_id = ? AND quarantined = 0 "
            "AND quantity_available > 0 ORDER BY created_at ASC, rowid ASC",
            (template_id,),
        )

    def get_reservation(
        self, workflow_id: str, node_id: str, attempt: int
    ) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM inventory_reservation WHERE workflow_id = ? AND node_id = ? AND attempt = ?",
            (workflow_id, node_id, attempt),
        )

    def reservations_for_workflow(self, workflow_id: str) -> List[Dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM inventory_reservation WHERE workflow_id = ? ORDER BY created_at ASC, reservation_id ASC",
            (workflow_id,),
        )

    def get_reservation_by_id(self, reservation_id: str) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM inventory_reservation WHERE reservation_id = ?",
            (reservation_id,),
        )

    def list_reservations(
        self, status: str = "", limit: int = 500
    ) -> List[Dict[str, Any]]:
        if status:
            return self.query_all(
                "SELECT * FROM inventory_reservation WHERE status = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (status, limit),
            )
        return self.query_all(
            "SELECT * FROM inventory_reservation "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )

    def get_relation(self, child_uuid: str) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM resource_relation WHERE child_uuid = ?", (child_uuid,)
        )

    def list_relations(self) -> List[Dict[str, Any]]:
        return self.query_all("SELECT * FROM resource_relation ORDER BY child_uuid ASC")

    def list_material_sites(self, material_uuid: str) -> List[Dict[str, Any]]:
        return self.query_all(
            "SELECT site.*, resource_template.name AS template_name "
            "FROM site "
            "JOIN material ON material.uuid = site.material_uuid "
            "JOIN resource_template ON resource_template.uuid = "
            "material.resource_template_uuid "
            "WHERE site.material_uuid = ? AND site.deleted_at IS NULL "
            "ORDER BY site.sort_order, site.create_time, site.uuid",
            (material_uuid,),
        )

    def children_of(self, parent_uuid: str) -> List[Dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM resource_relation WHERE parent_uuid = ? ORDER BY slot_id ASC",
            (parent_uuid,),
        )

    def get_content(self, instance_uuid: str) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM substance_content WHERE instance_uuid = ?", (instance_uuid,)
        )

    def list_contents(self) -> List[Dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM substance_content ORDER BY instance_uuid ASC"
        )

    def component_children_of(self, parent_uuid: str) -> List[Dict[str, Any]]:
        """组成父子（material_instance.parent_uuid）下的直接子物料；与 site 放置无关."""
        return self.query_all(
            "SELECT * FROM material_instance WHERE parent_uuid = ? ORDER BY edge_uuid ASC",
            (parent_uuid,),
        )

    def get_processed_command(self, command_id: str) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM processed_command WHERE command_id = ?", (command_id,)
        )

    def list_processed_commands(self, limit: int = 200) -> List[Dict[str, Any]]:
        """最近的幂等命令结果（只读诊断面，不暴露任意表查询）。"""
        return self.query_all(
            "SELECT * FROM processed_command ORDER BY processed_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )

    def list_ledger(self, after_id: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM inventory_ledger WHERE ledger_id > ? "
            "ORDER BY ledger_id ASC LIMIT ?",
            (after_id, limit),
        )

    @staticmethod
    def tx_parent_consistency_issues(
        conn: sqlite3.Connection,
    ) -> List[Dict[str, Any]]:
        """Return only deterministic parent/relation inconsistencies."""

        rows = conn.execute(
            "SELECT r.child_uuid, r.parent_uuid AS relation_parent_uuid, "
            "i.parent_uuid AS instance_parent_uuid "
            "FROM resource_relation r "
            "LEFT JOIN material_instance i ON i.edge_uuid = r.child_uuid "
            "WHERE i.edge_uuid IS NULL OR i.parent_uuid <> r.parent_uuid "
            "ORDER BY r.child_uuid ASC"
        ).fetchall()
        issues: List[Dict[str, Any]] = []
        for row in rows:
            instance_parent = row["instance_parent_uuid"]
            issues.append(
                {
                    "kind": (
                        "orphan_relation"
                        if instance_parent is None
                        else "parent_mismatch"
                    ),
                    "child_uuid": row["child_uuid"],
                    "instance_parent_uuid": instance_parent or "",
                    "relation_parent_uuid": row["relation_parent_uuid"] or "",
                }
            )
        return issues

    def parent_consistency_issues(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self.tx_parent_consistency_issues(self._conn)

    def snapshot_rows(self) -> Dict[str, List[Dict[str, Any]]]:
        """Read the complete v1 snapshot collections in stable order."""

        return {
            "templates": self.query_all(
                "SELECT * FROM inventory_resource_template ORDER BY template_id ASC"
            ),
            "lots": self.query_all("SELECT * FROM inventory_lot ORDER BY lot_id ASC"),
            "instances": self.query_all(
                "SELECT * FROM material_instance WHERE edge_uuid NOT IN ("
                "SELECT uuid FROM material WHERE "
                "json_extract(meta_data, '$.unilab_edge_placeholder') = 1) "
                "ORDER BY edge_uuid ASC"
            ),
            "relations": self.list_relations(),
            "contents": self.list_contents(),
            "reservations": self.query_all(
                "SELECT * FROM inventory_reservation ORDER BY reservation_id ASC"
            ),
        }

    # -- 实验室布局（lab_meta / lab_zone / lab_placement） --------------------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.query_one(
            "SELECT meta_value FROM lab_meta WHERE meta_key = ?", (key,)
        )
        return str(row["meta_value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO lab_meta(meta_key, meta_value) VALUES (?, ?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value",
                (key, value),
            )

    def list_zones(self) -> List[Dict[str, Any]]:
        return self.query_all("SELECT * FROM lab_zone ORDER BY zone_id ASC")

    def list_placements(self, zone_id: str = "") -> List[Dict[str, Any]]:
        if zone_id:
            return self.query_all(
                "SELECT * FROM lab_placement WHERE zone_id = ? ORDER BY subject_id ASC",
                (zone_id,),
            )
        return self.query_all("SELECT * FROM lab_placement ORDER BY subject_id ASC")

    def get_placement(self, subject_id: str) -> Optional[Dict[str, Any]]:
        return self.query_one(
            "SELECT * FROM lab_placement WHERE subject_id = ?", (subject_id,)
        )

    # -- outbox / cursor -----------------------------------------------------

    def pending_outbox(
        self, after_sequence: int, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM sync_outbox WHERE sequence > ? ORDER BY sequence ASC LIMIT ?",
            (after_sequence, limit),
        )

    def list_cursors(self) -> List[Dict[str, Any]]:
        """列出同步游标；ACK 推进仍只能由同步协议写入。"""
        return self.query_all("SELECT * FROM sync_cursor ORDER BY cursor_name ASC")

    def get_cursor(self, name: str = "cloud") -> int:
        row = self.query_one(
            "SELECT acked_sequence FROM sync_cursor WHERE cursor_name = ?", (name,)
        )
        return int(row["acked_sequence"]) if row else 0

    def set_cursor(self, name: str, acked_sequence: int, now_ms: int) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT acked_sequence FROM sync_cursor WHERE cursor_name = ?",
                (name,),
            ).fetchone()
            current = int(row["acked_sequence"]) if row else 0
            if acked_sequence < current:
                raise InvalidCursorAdvance(
                    f"ACK regression for {name}: {acked_sequence} < {current}"
                )
            maximum = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM sync_outbox"
                ).fetchone()[0]
            )
            if acked_sequence > maximum:
                raise InvalidCursorAdvance(
                    f"ACK {acked_sequence} exceeds outbox sequence {maximum}"
                )
            if acked_sequence > current:
                count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM sync_outbox "
                        "WHERE sequence > ? AND sequence <= ?",
                        (current, acked_sequence),
                    ).fetchone()[0]
                )
                if count != acked_sequence - current:
                    raise InvalidCursorAdvance(
                        f"ACK range {current + 1}..{acked_sequence} is not contiguous"
                    )
            conn.execute(
                "INSERT INTO sync_cursor(cursor_name, acked_sequence, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cursor_name) DO UPDATE SET acked_sequence = excluded.acked_sequence, "
                "updated_at = excluded.updated_at",
                (name, acked_sequence, now_ms),
            )

    def advance_cursor(
        self,
        name: str,
        expected_current: int,
        acked_sequence: int,
        sent_through: int,
        now_ms: int,
    ) -> int:
        """Atomically validate an ACK against the exact batch that was sent."""

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT acked_sequence FROM sync_cursor WHERE cursor_name = ?",
                (name,),
            ).fetchone()
            current = int(row["acked_sequence"]) if row else 0
            if current != expected_current:
                raise InvalidCursorAdvance(
                    f"stale ACK window for {name}: expected cursor "
                    f"{expected_current}, current {current}"
                )
            if acked_sequence < current:
                raise InvalidCursorAdvance(
                    f"ACK regression for {name}: {acked_sequence} < {current}"
                )
            if acked_sequence > sent_through:
                raise InvalidCursorAdvance(
                    f"ACK {acked_sequence} exceeds sent sequence {sent_through}"
                )
            if acked_sequence == current:
                return current
            conn.execute(
                "INSERT INTO sync_cursor(cursor_name, acked_sequence, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(cursor_name) DO UPDATE SET "
                "acked_sequence = excluded.acked_sequence, "
                "updated_at = excluded.updated_at",
                (name, acked_sequence, now_ms),
            )
            return acked_sequence

    def max_outbox_sequence(self) -> int:
        row = self.query_one("SELECT COALESCE(MAX(sequence), 0) AS s FROM sync_outbox")
        return int(row["s"]) if row else 0

    # -- 事务内写 helper（必须在 transaction() 上下文中调用） -----------------

    @staticmethod
    def tx_insert_ledger(
        conn: sqlite3.Connection,
        occurred_at: int,
        op_type: str,
        aggregate_type: str,
        aggregate_id: str,
        delta: Dict[str, Any],
        actor: str = "",
        reason: str = "",
        causation_id: str = "",
        trace_id: str = "",
        span_id: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO inventory_ledger(occurred_at, op_type, aggregate_type, aggregate_id, "
            "delta_json, actor, reason, causation_id, trace_id, span_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                occurred_at,
                op_type,
                aggregate_type,
                aggregate_id,
                json.dumps(delta, ensure_ascii=False),
                actor,
                reason,
                causation_id,
                trace_id,
                span_id,
            ),
        )

    @staticmethod
    def tx_insert_outbox(
        conn: sqlite3.Connection,
        event_id: str,
        edge_id: str,
        lab_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        occurred_at: int,
        causation_id: str,
        payload: Dict[str, Any],
        traceparent: str = "",
        tracestate: str = "",
        trace_id: str = "",
        span_id: str = "",
    ) -> int:
        cur = conn.execute(
            "INSERT INTO sync_outbox(event_id, edge_id, lab_id, aggregate_type, aggregate_id, "
            "aggregate_version, event_type, occurred_at, causation_id, payload_json, "
            "traceparent, tracestate, trace_id, span_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                edge_id,
                lab_id,
                aggregate_type,
                aggregate_id,
                aggregate_version,
                event_type,
                occurred_at,
                causation_id,
                json.dumps(payload, ensure_ascii=False),
                traceparent,
                tracestate,
                trace_id,
                span_id,
            ),
        )
        return int(cur.lastrowid or 0)
