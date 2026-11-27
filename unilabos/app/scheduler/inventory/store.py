"""SQLite WAL 持久化层.

单仓储分区单写者：所有写事务经进程内锁 + BEGIN IMMEDIATE 串行化，
业务变更、ledger、outbox 必须在同一事务提交（由 service 层保证，
store 只提供 transaction() 原语与行级 helper）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from unilabos.app.scheduler.inventory.content_store import (
    migrate_container_content_schema,
)
from unilabos.app.scheduler.inventory.dispatch_admission import (
    migrate_dispatch_admission_schema,
)
from unilabos.app.scheduler.inventory.domain import new_event_id
from unilabos.app.scheduler.inventory.ingress_schema import migrate_ingress_schema
from unilabos.app.scheduler.inventory.reagent_store import migrate_reagent_schema
from unilabos.persistence import open_database
from unilabos.registry.runtime_device_catalog import RuntimeDeviceTemplateCatalog

# v8 已由生产分支用于物料来源绑定；试剂和容器内容依次占用后续版本。
# v11 把物料模板引用改为由 SQLite 物料模板与内存设备目录共同校验；v14
# 原地放宽派发 Lease 的 scope CHECK；v15/v16 依次关闭旧 relation 写入口的
# 占用覆盖与活动资源绕过；v17 起 Material I/O 对账写入 processed_command。
# v18 合并库存子表；v19 恢复内容约束并修复已有合表数据库。
# v20 允许普通物料同名，设备根名称与非空条码仍保持唯一。
SCHEMA_VERSION = 20
_LAB_META_COLUMNS = frozenset(
    {
        "lab_name",
        "lab_domain",
        "resource_graph_bootstrap_source",
        "resource_graph_bootstrap_fingerprint",
        "resource_graph_bootstrap_fingerprint_version",
    }
)


class InvalidCursorAdvance(ValueError):
    """ACK cursor 试图回退、跳批或基于过期发送窗口推进."""


class SiteOccupancyConflict(ValueError):
    """原子 SiteOccupancy 修改违反目标占用或单一位置不变量。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        site_uuid: str = "",
        material_uuid: str = "",
        occupied_material_uuid: str = "",
    ) -> None:
        """保存稳定错误码与冲突身份，供各公共适配层映射自身错误。"""

        super().__init__(message)
        self.code = code
        self.site_uuid = site_uuid
        self.material_uuid = material_uuid
        self.occupied_material_uuid = occupied_material_uuid


def set_site_occupancy(
    connection: sqlite3.Connection,
    *,
    site_uuid: str,
    material_uuid: str,
    update_time: str | None = None,
) -> str:
    """在调用方事务内原子移动物料并占用目标 Site。

    目标为空时占用；目标已由同一物料占用时幂等返回；目标属于其他物料时拒绝。
    成功前会解除该物料的旧 Site，并由唯一索引兜底“一个 Material 至多一个
    Site”。返回原 Site UUID；原本不在任何 Site 时返回空字符串。
    """

    normalized_site = str(site_uuid or "").strip()
    normalized_material = str(material_uuid or "").strip()
    if not normalized_site or not normalized_material:
        raise SiteOccupancyConflict(
            "site_occupancy_identity_missing",
            "SiteOccupancy 需要非空 Site 和 Material 身份",
            site_uuid=normalized_site,
            material_uuid=normalized_material,
        )
    target = connection.execute(
        "SELECT material_uuid,occupied_material_uuid FROM site "
        "WHERE uuid=? AND deleted_at IS NULL",
        (normalized_site,),
    ).fetchone()
    if target is None:
        raise SiteOccupancyConflict(
            "site_not_found",
            f"目标 Site 不存在：{normalized_site}",
            site_uuid=normalized_site,
            material_uuid=normalized_material,
        )
    if str(target["material_uuid"]) == normalized_material:
        raise SiteOccupancyConflict(
            "site_occupancy_cycle",
            "Material 不能占用自身 Site",
            site_uuid=normalized_site,
            material_uuid=normalized_material,
        )
    occupied = str(target["occupied_material_uuid"] or "")
    if occupied and occupied != normalized_material:
        raise SiteOccupancyConflict(
            "site_occupied",
            f"目标 Site 已被其他 Material 占用：{occupied}",
            site_uuid=normalized_site,
            material_uuid=normalized_material,
            occupied_material_uuid=occupied,
        )

    locations = connection.execute(
        "SELECT uuid FROM site WHERE occupied_material_uuid=? "
        "AND deleted_at IS NULL ORDER BY uuid",
        (normalized_material,),
    ).fetchall()
    if len(locations) > 1:
        raise SiteOccupancyConflict(
            "material_multiple_sites",
            f"Material 同时占用多个 Site：{normalized_material}",
            site_uuid=normalized_site,
            material_uuid=normalized_material,
        )
    previous_site = str(locations[0]["uuid"]) if locations else ""
    if previous_site == normalized_site:
        return previous_site

    timestamp = update_time or str(
        connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()[0]
    )
    if previous_site:
        connection.execute(
            "UPDATE site SET occupied_material_uuid=NULL,update_time=? "
            "WHERE uuid=? AND occupied_material_uuid=? AND deleted_at IS NULL",
            (timestamp, previous_site, normalized_material),
        )
    try:
        changed = connection.execute(
            "UPDATE site SET occupied_material_uuid=?,update_time=? "
            "WHERE uuid=? AND deleted_at IS NULL "
            "AND (occupied_material_uuid IS NULL OR occupied_material_uuid=?)",
            (
                normalized_material,
                timestamp,
                normalized_site,
                normalized_material,
            ),
        ).rowcount
    except sqlite3.IntegrityError as error:
        raise SiteOccupancyConflict(
            "material_already_occupies_site",
            f"Material 已占用其他 Site：{normalized_material}",
            site_uuid=normalized_site,
            material_uuid=normalized_material,
        ) from error
    if changed != 1:
        raise SiteOccupancyConflict(
            "site_occupied",
            f"目标 Site 已被其他 Material 占用：{normalized_site}",
            site_uuid=normalized_site,
            material_uuid=normalized_material,
        )
    return previous_site


def clear_site_occupancy(
    connection: sqlite3.Connection,
    *,
    material_uuid: str,
    update_time: str | None = None,
) -> str:
    """在调用方事务内幂等解除一个 Material 的唯一 SiteOccupancy。"""

    normalized_material = str(material_uuid or "").strip()
    if not normalized_material:
        raise SiteOccupancyConflict(
            "site_occupancy_identity_missing",
            "SiteOccupancy 需要非空 Material 身份",
        )
    locations = connection.execute(
        "SELECT uuid FROM site WHERE occupied_material_uuid=? "
        "AND deleted_at IS NULL ORDER BY uuid",
        (normalized_material,),
    ).fetchall()
    if len(locations) > 1:
        raise SiteOccupancyConflict(
            "material_multiple_sites",
            f"Material 同时占用多个 Site：{normalized_material}",
            material_uuid=normalized_material,
        )
    if not locations:
        return ""
    previous_site = str(locations[0]["uuid"])
    timestamp = update_time or str(
        connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ).fetchone()[0]
    )
    connection.execute(
        "UPDATE site SET occupied_material_uuid=NULL,update_time=? "
        "WHERE uuid=? AND occupied_material_uuid=? AND deleted_at IS NULL",
        (timestamp, previous_site, normalized_material),
    )
    return previous_site


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

# v3：父物料列（云端 material.parent_material_uuid ≡ 资源树 parent_uuid，单一父）。
# 与 resource_relation 的关系：parent_uuid 列是唯一父层级事实；relation 行仅在
# 「父 + 具名位」时存在（slot_id = PLR site 名 ↔ 云端 sites.label，uuid 仅后端索引），
# 且 relation.parent_uuid 恒等于本列（_tx_upsert_relation 同步维护）。
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

# v5：前端共享资源合同以 Backend c35d821 的 SQLite 结构为准。
#
# 旧 Inventory 表不是直接删除：迁移先把事实搬入规范
# resource_template/material/site/material_state_history，再用只承载旧字段拼写的
# 可写视图维持 Edge 内部 Inventory 调用。这样公共接口和新写入只有一份 Material / Site
# 事实，同时既有 lot/reservation/ledger 能继续运行。
_SCHEMA_V5_BACKEND_CONTRACT = r"""
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

"""

# v6：补齐 Go Backend 物料图的实例类型字段。修订号继续以
# material_inventory.aggregate_version 为唯一权威，不复制第二份版本列。
_SCHEMA_V6_MATERIAL_TYPE = (
    "ALTER TABLE material ADD COLUMN type TEXT NOT NULL DEFAULT ''"
)

# v7：资源模板（ResourceTemplate）直接保存 Edge Registry 上报的库位（Site）
# 固定定义；运行时库位实例与库位占用仍由 site 表承载，不写入本列。
_SCHEMA_V7_RESOURCE_TEMPLATE_AVAILABLE_SITES = (
    "ALTER TABLE resource_template ADD COLUMN available_sites TEXT NOT NULL "
    "DEFAULT '[]' CHECK (json_valid(available_sites) "
    "AND json_type(available_sites) = 'array')"
)

# v11：``material.resource_template_uuid`` 是跨两个模板权威的稳定引用。
# 物料模板仍在 SQLite，由写服务验证；设备模板来自启动代际内存目录，因此
# 不能再用一个指向 ``resource_template`` 的物理外键表达。
_SCHEMA_V11_HYBRID_TEMPLATE_REFERENCE = r"""
ALTER TABLE material RENAME TO material_v10;
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
    type TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL,
    name TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config)),
    data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data)),
    CHECK (parent_uuid IS NULL OR parent_uuid <> uuid),
    FOREIGN KEY (parent_uuid) REFERENCES material (uuid) ON DELETE RESTRICT
);
INSERT INTO material(
    uuid,create_time,update_time,deleted_at,description,meta_data,
    resource_template_uuid,parent_uuid,class,type,barcode,name,config,data
)
SELECT
    uuid,create_time,update_time,deleted_at,description,meta_data,
    resource_template_uuid,parent_uuid,class,type,barcode,name,config,data
FROM material_v10;
DROP TABLE material_v10;
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
"""

# v16：旧 ``resource_relation`` 视图仍是受支持的兼容写入口，但它必须与公共
# 库存写 API 服从同一占用与活动资源门禁。守卫直接并入主 INSERT/DELETE
# trigger，避免多个同事件 INSTEAD OF trigger 的执行顺序影响正确性。
_SCHEMA_V16_RELATION_WRITE_GUARD = """
DROP TRIGGER IF EXISTS resource_relation_occupancy_guard;
DROP TRIGGER IF EXISTS resource_relation_insert;
DROP TRIGGER IF EXISTS resource_relation_delete;

CREATE TRIGGER resource_relation_insert
INSTEAD OF INSERT ON resource_relation
BEGIN
    SELECT RAISE(ABORT, 'target site is occupied by another material')
    WHERE NEW.slot_id <> '' AND EXISTS (
        SELECT 1 FROM site
        WHERE material_uuid = NEW.parent_uuid
          AND LOWER(name) = LOWER(NEW.slot_id)
          AND deleted_at IS NULL
          AND occupied_material_uuid IS NOT NULL
          AND occupied_material_uuid <> NEW.child_uuid
    );
    SELECT RAISE(ABORT, 'resource relation conflicts with an active claim')
    WHERE EXISTS (
        WITH requested(lock_key, material_uuid, whole_material) AS (
            VALUES
                ('material/' || NEW.child_uuid || '/exclusive', NEW.child_uuid, 1),
                ('/devices/' || NEW.child_uuid, NULL, 0),
                ('material/' || NEW.parent_uuid || '/exclusive', NEW.parent_uuid, 1),
                ('/devices/' || NEW.parent_uuid, NULL, 0)
            UNION
            SELECT
                'material/' || NEW.parent_uuid || '/site/' || site.uuid || '/exclusive',
                NEW.parent_uuid,
                0
            FROM site
            WHERE site.material_uuid = NEW.parent_uuid
              AND LOWER(site.name) = LOWER(NEW.slot_id)
              AND site.deleted_at IS NULL
            UNION
            SELECT
                'material/' || source.material_uuid || '/exclusive',
                source.material_uuid,
                1
            FROM site AS source
            WHERE source.occupied_material_uuid = NEW.child_uuid
              AND source.deleted_at IS NULL
            UNION
            SELECT '/devices/' || source.material_uuid, NULL, 0
            FROM site AS source
            WHERE source.occupied_material_uuid = NEW.child_uuid
              AND source.deleted_at IS NULL
            UNION
            SELECT
                'material/' || source.material_uuid || '/site/' || source.uuid || '/exclusive',
                source.material_uuid,
                0
            FROM site AS source
            WHERE source.occupied_material_uuid = NEW.child_uuid
              AND source.deleted_at IS NULL
        )
        SELECT 1
        FROM (
            SELECT lock_key
            FROM station_execution_lock_lease
            WHERE state IN ('prepared', 'reserved', 'running', 'uncertain')
            UNION ALL
            SELECT lock_key
            FROM station_ingress_reservation_resource
            WHERE active = 1
            UNION ALL
            SELECT lock_key FROM station_handoff_resource WHERE active = 1
            UNION ALL
            SELECT r.lock_key FROM station_material_input_resource r
            JOIN station_material_input h USING(invocation_uuid) WHERE h.state='active'
        ) AS active
        JOIN requested
          ON active.lock_key = requested.lock_key
          OR (
              requested.whole_material = 1
              AND active.lock_key GLOB (
                  'material/' || requested.material_uuid || '/site/*/exclusive'
              )
          )
        LIMIT 1
    );
    INSERT OR IGNORE INTO resource_template (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        name, display_name, resource_type, model, tags, data_schema,
        config_schema, pose, config_info, scene, device_params, ui_overlay,
        aggregate_version
    ) VALUES (
        '__edge_unknown_resource_template__',
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        'Edge legacy placeholder; hidden from the shared Backend Interface',
        '{"unilab_edge_placeholder":true}',
        '__edge_unknown_resource_template__', 'Unknown Edge resource',
        'resource', '{}', '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}',
        1
    );
    INSERT OR IGNORE INTO material (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        resource_template_uuid, parent_uuid, class, barcode, name, config, data,
        inventory_status, aggregate_version
    ) VALUES (
        NEW.parent_uuid,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        NULL,
        'Edge legacy parent placeholder',
        '{"unilab_edge_placeholder":true}',
        '__edge_unknown_resource_template__', NULL, 'resource', '',
        '__edge_placeholder__:' || NEW.parent_uuid, '{}', '{}',
        'warehouse', 1
    );
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

CREATE TRIGGER resource_relation_delete
INSTEAD OF DELETE ON resource_relation
BEGIN
    SELECT RAISE(ABORT, 'resource relation conflicts with an active claim')
    WHERE EXISTS (
        WITH requested(lock_key, material_uuid, whole_material) AS (
            VALUES
                ('material/' || OLD.child_uuid || '/exclusive', OLD.child_uuid, 1),
                ('/devices/' || OLD.child_uuid, NULL, 0),
                ('material/' || OLD.parent_uuid || '/exclusive', OLD.parent_uuid, 1),
                ('/devices/' || OLD.parent_uuid, NULL, 0)
            UNION
            SELECT
                'material/' || OLD.parent_uuid || '/site/' || site.uuid || '/exclusive',
                OLD.parent_uuid,
                0
            FROM site
            WHERE site.material_uuid = OLD.parent_uuid
              AND LOWER(site.name) = LOWER(OLD.slot_id)
              AND site.deleted_at IS NULL
        )
        SELECT 1
        FROM (
            SELECT lock_key
            FROM station_execution_lock_lease
            WHERE state IN ('prepared', 'reserved', 'running', 'uncertain')
            UNION ALL
            SELECT lock_key
            FROM station_ingress_reservation_resource
            WHERE active = 1
            UNION ALL
            SELECT lock_key FROM station_handoff_resource WHERE active = 1
            UNION ALL
            SELECT r.lock_key FROM station_material_input_resource r
            JOIN station_material_input h USING(invocation_uuid) WHERE h.state='active'
        ) AS active
        JOIN requested
          ON active.lock_key = requested.lock_key
          OR (
              requested.whole_material = 1
              AND active.lock_key GLOB (
                  'material/' || requested.material_uuid || '/site/*/exclusive'
              )
          )
        LIMIT 1
    );
    UPDATE site
    SET occupied_material_uuid = NULL,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE occupied_material_uuid = OLD.child_uuid AND deleted_at IS NULL;
END;
"""

# v8：物料来源（MaterialSource）在任务准入时冻结具体物料绑定。任务全程独占
# 仍由 inventory_reservation 持有；共享来源只写本表，动作执行期互斥由调度器的
# 物料执行锁/声明负责。selector_json 用于幂等重放时拒绝同一任务尝试偷换选择器。
_SCHEMA_V8_MATERIAL_SOURCE_BINDING = """
CREATE TABLE IF NOT EXISTS inventory_material_source_binding (
    binding_id             TEXT PRIMARY KEY,
    workflow_id            TEXT NOT NULL,
    node_id                TEXT NOT NULL,
    attempt                INTEGER NOT NULL DEFAULT 1,
    material_uuid          TEXT NOT NULL,
    resource_template_uuid TEXT NOT NULL,
    custody_policy         TEXT NOT NULL
        CHECK (custody_policy IN ('task_exclusive', 'shared_source')),
    selector_json          TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(selector_json)),
    status                 TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'released')),
    created_at             INTEGER NOT NULL DEFAULT 0,
    released_at            INTEGER,
    version                INTEGER NOT NULL DEFAULT 1,
    UNIQUE (workflow_id, node_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_material_source_binding_material
    ON inventory_material_source_binding(material_uuid, custody_policy, status);
CREATE INDEX IF NOT EXISTS idx_material_source_binding_workflow
    ON inventory_material_source_binding(workflow_id, attempt, status);
"""

# v2：实验室名称、领域和资源图指纹。俯视图分区不再建表。
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS lab_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    lab_name TEXT NOT NULL DEFAULT '',
    lab_domain TEXT NOT NULL DEFAULT '',
    resource_graph_bootstrap_source TEXT,
    resource_graph_bootstrap_fingerprint TEXT,
    resource_graph_bootstrap_fingerprint_version TEXT
);
"""


class InventoryStore:
    """SQLite WAL 存储：单连接 + 进程内写锁（单写者）."""

    def __init__(self, path: str = ":memory:"):
        """打开库存数据库并预留当前进程的设备模板目录槽位。"""

        self.path = path
        self._lock = threading.RLock()
        self._runtime_device_template_catalog: RuntimeDeviceTemplateCatalog | None = (
            None
        )
        self._runtime_device_template_catalog_required = False
        self._runtime_device_template_names: frozenset[str] = frozenset()
        self.db = open_database(path, "inventory")
        self._conn = self.db
        self._conn.row_factory = sqlite3.Row
        self.db.initialize(self._initialize_sqlite)
        from .execution_effects import migrate_execution_effects
        try:
            migrate_execution_effects(self)
        except BaseException:
            self.db.close()
            raise
        self.db.execute("INSERT OR IGNORE INTO runtime_scene_revision VALUES (1,0)")
        self.db.commit()
        from unilabos.persistence.resource_changes import initialize_resource_changes

        with self.transaction() as connection:
            initialize_resource_changes(connection, "inventory")

    def _initialize_sqlite(self) -> None:
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()
        from .runtime_view import initialize_observation_projection
        initialize_observation_projection(self._conn)
        from .execution_effects import SCHEMA as execution_effect_schema
        self._conn.execute(execution_effect_schema)
        self._conn.commit()

    def _migrate(self) -> None:
        """把库存（Inventory）数据库串行迁移到当前结构版本。

        参数：无。返回：无；迁移在当前连接上幂等补列并提交，SQLite 结构或数据
        约束失败时原样抛出异常，调用方不得在部分结构上继续启动。
        """

        with self._lock, self.db.schema_migration():
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            compatibility_object = self._conn.execute(
                "SELECT type FROM sqlite_master WHERE name='material_instance'"
            ).fetchone()
            # user_version 可能被备份/测试工具错误降写。规范 v5 以可写兼容视图
            # 为结构指纹；已是 v5 时绝不能再次运行旧表 ALTER 或重命名迁移，
            # 但仍要继续执行后续 additive migration。
            if (
                current < 5
                and compatibility_object is not None
                and compatibility_object[0] == "view"
            ):
                current = 5
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
                self._conn.executescript(_SCHEMA_V5_BACKEND_CONTRACT)
            if current < 6:
                material_columns = {
                    row[1]
                    for row in self._conn.execute(
                        "PRAGMA table_info(material)"
                    ).fetchall()
                }
                if "type" not in material_columns:
                    self._conn.execute(_SCHEMA_V6_MATERIAL_TYPE)
            if current < 7:
                template_columns = {
                    row[1]
                    for row in self._conn.execute(
                        "PRAGMA table_info(resource_template)"
                    ).fetchall()
                }
                if "available_sites" not in template_columns:
                    self._conn.execute(_SCHEMA_V7_RESOURCE_TEMPLATE_AVAILABLE_SITES)
            # 生产 v8 增加任务物料绑定。这里始终幂等执行，同时兼容开发分支中
            # 曾把 user_version 写到 9、但尚未创建本表的数据库。
            self._conn.executescript(_SCHEMA_V8_MATERIAL_SOURCE_BINDING)
            # v9 增加 Backend 同形的试剂身份与试剂实例，并把试剂变化收敛到
            # Edge 原有 ledger + outbox。这里同样幂等执行，以修复早期开发版
            # 已提升 user_version、但仍残留重复 material_ledger_entry 的数据库。
            migrate_reagent_schema(self._conn)
            # v10 增加样品与当前内容物。二者与试剂共用同一容器内容互斥规则；
            # 当前内容物数量事件继续复用统一 inventory_ledger + sync_outbox。
            migrate_container_content_schema(self._conn)
            if current < 11:
                self._migrate_hybrid_template_reference()
            # 在合并之前验证内容冲突；全部迁移由外层同一事务统一提交或回滚。
            from unilabos.app.scheduler.inventory.table_compaction import (
                assert_legacy_container_content_mergeable,
            )

            assert_legacy_container_content_mergeable(self._conn)
            # v12 增加 Backend AGV 调用的目标 Edge 入口预留状态机。结构迁移保持
            # 幂等，修复开发数据库被手工提高版本但缺少业务表的情况。
            migrate_ingress_schema(self._conn)
            # v13 把门禁 7 的条件复验、完整 Claim 和 Fence 收敛到库存写事务；
            # v14 在同一幂等入口原地放宽 Lease scope，加入通用命名资源。
            # 工作流库只保存同一 Permit 的审计投影，不再成为资源竞争权威。
            from unilabos.app.scheduler.inventory.table_compaction import (
                migrate_inventory_table_compaction,
            )

            migrate_inventory_table_compaction(self._conn)
            migrate_dispatch_admission_schema(self._conn)
            from unilabos.app.scheduler.inventory.handoff_schema import migrate_handoff_schema

            migrate_handoff_schema(self._conn)
            from unilabos.app.scheduler.inventory.material_inputs import SCHEMA as MATERIAL_INPUT_SCHEMA

            self._conn.executescript(MATERIAL_INPUT_SCHEMA)
            # v17 起 Material I/O 对账复用 processed_command；若开发库仍残留
            # material_io_effect，这里折进同一张命令表后删除。局部导入避免
            # 持久层 helper 与 Material I/O 服务形成模块初始化环。
            from unilabos.app.scheduler.inventory.material_io import (
                migrate_material_io_schema,
            )

            migrate_material_io_schema(self._conn)
            # v16 重建旧 relation 写触发器，把占用与活动资源守卫并入同一主
            # trigger；始终幂等执行以修复曾提前写高版本的开发数据库。仅含单表
            # 的旧版迁移夹具没有兼容视图，也没有可被绕过的 relation 写入口。
            relation_object = self._conn.execute(
                "SELECT type FROM sqlite_master WHERE name='resource_relation'"
            ).fetchone()
            if relation_object is not None:
                self._conn.executescript(_SCHEMA_V16_RELATION_WRITE_GUARD)
            if current >= 5:
                from unilabos.persistence.migrations import (
                    _backfill_blank_material_type,
                )

                _backfill_blank_material_type(self._conn)
            if current < 20:
                # 普通物料名称不是身份；离站清除父级后同名历史仍可并存。
                # 设备根节点保留原有命名约束，条码唯一索引不变。
                self._conn.execute("DROP INDEX IF EXISTS ux_material_root_name_active")
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_device_root_name_active "
                    "ON material (LOWER(name)) WHERE deleted_at IS NULL "
                    "AND parent_uuid IS NULL AND type='device'"
                )
            if current < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_hybrid_template_reference(self) -> None:
        """移除仅能表达 SQLite 模板的物料模板外键。

        参数：无。返回：无。迁移保持 ``material`` 的所有业务列、索引和父物料
        自关联；子表继续引用重建后的同名表。没有旧模板外键的兼容数据库直接
        返回；事务及外键检查设置由外层结构迁移统一管理。
        """

        foreign_keys = self._conn.execute(
            "PRAGMA foreign_key_list(material)"
        ).fetchall()
        if not any(row["table"] == "resource_template" for row in foreign_keys):
            return
        self._conn.executescript(_SCHEMA_V11_HYBRID_TEMPLATE_REFERENCE)
        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                "v11 material template reference migration left invalid foreign keys"
            )

    def require_runtime_device_template_catalog(
        self,
        device_names: Iterable[str] = (),
    ) -> None:
        """声明本进程设备模板只能来自待发布或已发布的运行时目录。

        参数：``device_names`` 是本次待发布代际中的设备业务名。返回：无；标记
        和已知名称一旦设置即不撤销。即使后续工作流 fixed-point 启动失败，设备
        模板写接口也必须关闭式拒绝，不能退回 SQLite 第二权威或写入同名资源。
        """

        normalized_names = frozenset(
            str(name).strip() for name in device_names if str(name).strip()
        )
        with self._lock:
            self._runtime_device_template_catalog_required = True
            self._runtime_device_template_names |= normalized_names

    @property
    def runtime_device_template_catalog_required(self) -> bool:
        """返回当前进程是否已经进入运行时设备目录单权威模式。"""

        with self._lock:
            return self._runtime_device_template_catalog_required

    @property
    def runtime_device_template_names(self) -> frozenset[str]:
        """返回当前进程已声明由运行时目录拥有的设备业务名。"""

        with self._lock:
            return self._runtime_device_template_names

    def install_runtime_device_template_catalog(
        self,
        catalog: RuntimeDeviceTemplateCatalog,
    ) -> None:
        """安装当前进程唯一的设备模板内存目录。

        参数：``catalog`` 必须提供稳定 ``fingerprint``、``get``、``list`` 与
        ``resolve_uuid`` 接口。返回：无；相同代际可幂等重装，不允许在库存服务
        存活期间静默切换到另一代设备定义。
        """

        if not isinstance(catalog, RuntimeDeviceTemplateCatalog):
            raise TypeError("设备模板内存目录类型无效")
        fingerprint = catalog.fingerprint
        if not isinstance(fingerprint, str) or not fingerprint:
            raise TypeError("设备模板内存目录缺少代际指纹")
        with self._lock:
            current = self._runtime_device_template_catalog
            if current is not None and current.fingerprint != fingerprint:
                raise RuntimeError("库存服务启动后不得切换设备模板目录代际")
            self._runtime_device_template_catalog_required = True
            self._runtime_device_template_names |= frozenset(
                str(item["name"]) for item in catalog.list()
            )
            self._runtime_device_template_catalog = catalog

    @property
    def runtime_device_template_catalog(self) -> RuntimeDeviceTemplateCatalog | None:
        """返回当前启动代际的设备模板内存目录；未安装时返回 ``None``。"""

        with self._lock:
            return self._runtime_device_template_catalog

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

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """在锁内借出共享连接做只读查询，不开启事务。

        供需要 ``sqlite3.Connection`` 的只读汇总（如活动预留量）使用；调用方在
        事务内再次进入时因可重入锁不会阻塞，也不会触发嵌套 ``BEGIN``。调用方
        绝不能在此连接上写入或提交。
        """
        with self._lock:
            yield self._conn

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
        """列出公开物料实例；已软删除物料按退出库存后的状态返回。

        软删除现在会把非终态库存收敛为 discarded（见
        ``material_io.retire_deleted_material_inventory``），但修复前软删除的
        旧行仍停留在 warehouse 等在库状态。这里对已软删除物料统一投影：
        consumed/discarded/used 保留原值，其余一律按 discarded 返回与过滤，
        既不再把它们当作在库物料列出，也仍可按 discarded/consumed 查询历史。
        """

        # 与 retire_deleted_material_inventory 保留集合一致
        effective = (
            "SELECT instance.edge_uuid,instance.legacy_cloud_id,instance.lot_id,"
            "instance.template_id,instance.barcode,"
            "CASE WHEN material.deleted_at IS NOT NULL "
            "AND instance.status NOT IN ('consumed','discarded','used') "
            "THEN 'discarded' ELSE instance.status END AS status,"
            "instance.version,instance.parent_uuid "
            "FROM material_instance AS instance "
            "JOIN material ON material.uuid = instance.edge_uuid "
            "WHERE json_extract(material.meta_data, '$.unilab_edge_placeholder') "
            "IS NOT 1"
        )
        if status:
            return self.query_all(
                f"SELECT * FROM ({effective}) WHERE status = ? "
                "ORDER BY edge_uuid DESC LIMIT ?",
                (status, limit),
            )
        return self.query_all(
            f"SELECT * FROM ({effective}) ORDER BY edge_uuid DESC LIMIT ?",
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

    # -- 实验室身份与资源图指纹（单行 lab_meta） ------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        if key not in _LAB_META_COLUMNS:
            return default
        row = self.query_one(f"SELECT {key} AS meta_value FROM lab_meta WHERE id = 1")
        if row is None or row["meta_value"] is None:
            return default
        return str(row["meta_value"])

    def set_meta(self, key: str, value: str) -> None:
        if key not in _LAB_META_COLUMNS:
            raise ValueError(f"未知实验室元数据字段: {key}")
        with self.transaction() as conn:
            conn.execute(
                f"INSERT INTO lab_meta(id, {key}) VALUES (1, ?) "
                f"ON CONFLICT(id) DO UPDATE SET {key} = excluded.{key}",
                (value,),
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
        *,
        entry_uuid: str = "",
        material_uuid: str = "",
        subject_type: str = "",
        quantity_delta: Optional[float] = None,
        quantity_unit: Optional[str] = None,
        revision: Optional[int] = None,
        workflow_task_uuid: Optional[str] = None,
        workflow_node_job_uuid: Optional[str] = None,
    ) -> None:
        """在当前业务事务中追加一条统一库存台账。

        参数：``conn`` 是调用方已开启的事务；前十个字段是既有台账事实；
        ``entry_uuid``、``material_uuid`` 与 ``subject_type`` 为公共历史接口提供
        稳定标识和查询维度。返回：无；SQLite 异常原样抛出并由外层事务回滚。
        """

        conn.execute(
            "INSERT INTO inventory_ledger(occurred_at, op_type, aggregate_type, aggregate_id, "
            "delta_json, actor, reason, causation_id, trace_id, span_id, entry_uuid, "
            "material_uuid, subject_type, quantity_delta, quantity_unit, revision, "
            "workflow_task_uuid, workflow_node_job_uuid) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                entry_uuid,
                material_uuid,
                subject_type,
                quantity_delta,
                quantity_unit,
                revision,
                workflow_task_uuid,
                workflow_node_job_uuid,
            ),
        )

    @staticmethod
    def append_material_ledger(
        conn: sqlite3.Connection,
        *,
        material_uuid: str,
        name: str,
        event_type: str,
        quantity_delta: Optional[float] = None,
        changes: Optional[Dict[str, Any]] = None,
        causation_id: str = "",
        workflow_task_uuid: Optional[str] = None,
        workflow_node_job_uuid: Optional[str] = None,
    ) -> None:
        """在当前事务追加一条物料台账，不写同步发件箱。

        参数：``name`` 是写入当时的 ``material.name``。返回：无。插入失败由外层事务回滚。
        """

        occurred_at = int(time.time() * 1000)
        payload = {"changes": {"name": name, **(changes or {})}}
        InventoryStore.tx_insert_ledger(
            conn,
            occurred_at,
            event_type,
            "material",
            material_uuid,
            payload,
            causation_id=causation_id,
            entry_uuid=new_event_id(occurred_at),
            material_uuid=material_uuid,
            subject_type="material",
            quantity_delta=quantity_delta,
            quantity_unit="piece" if quantity_delta is not None else None,
            workflow_task_uuid=workflow_task_uuid,
            workflow_node_job_uuid=workflow_node_job_uuid,
        )

    @staticmethod
    def tx_append_inventory_event(
        conn: sqlite3.Connection,
        *,
        entry_uuid: str,
        edge_id: str,
        lab_id: str,
        occurred_at: int,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        payload: Dict[str, Any],
        actor: str = "",
        reason: str = "",
        causation_id: str = "",
        traceparent: str = "",
        tracestate: str = "",
        trace_id: str = "",
        span_id: str = "",
        material_uuid: str = "",
        subject_type: str = "",
        quantity_delta: Optional[float] = None,
        quantity_unit: Optional[str] = None,
        revision: Optional[int] = None,
        workflow_task_uuid: Optional[str] = None,
        workflow_node_job_uuid: Optional[str] = None,
    ) -> None:
        """把同一领域事件同时追加到台账与事务发件箱。

        参数：``conn`` 是调用方业务事务；``entry_uuid`` 同时作为历史记录 UUID
        和发件箱事件 UUID；其余参数描述聚合版本、事件载荷、Edge 身份与追踪
        上下文。返回：无；任一写入失败都由外层事务整体回滚。
        """

        InventoryStore.tx_insert_ledger(
            conn,
            occurred_at,
            event_type,
            aggregate_type,
            aggregate_id,
            payload,
            actor,
            reason,
            causation_id,
            trace_id,
            span_id,
            entry_uuid=entry_uuid,
            material_uuid=material_uuid,
            subject_type=subject_type,
            quantity_delta=quantity_delta,
            quantity_unit=quantity_unit,
            revision=revision,
            workflow_task_uuid=workflow_task_uuid,
            workflow_node_job_uuid=workflow_node_job_uuid,
        )
        InventoryStore.tx_insert_outbox(
            conn,
            entry_uuid,
            edge_id,
            lab_id,
            aggregate_type,
            aggregate_id,
            aggregate_version,
            event_type,
            occurred_at,
            causation_id,
            payload,
            traceparent,
            tracestate,
            trace_id,
            span_id,
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
