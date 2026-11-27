"""把一对一的库存表收进父表，并删掉未发布的俯视图分区表。

lab_meta 收成固定列后仍保留资源图导入指纹，否则已有库存无法判断能否跳过导入。
"""

from __future__ import annotations

import sqlite3

from unilabos.app.scheduler.inventory.content_store import CONTAINER_CONTENT_SCHEMA


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None:
        return set()
    return {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def assert_legacy_container_content_mergeable(connection: sqlite3.Connection) -> None:
    """合并前拒绝同号或同一物料两条有效内容。

    参数：``connection`` 是尚未提交本轮库存迁移的连接。返回无。异常：冲突时抛
    ``sqlite3.IntegrityError``，调用方必须停在旧表上，不能先删源表再靠
    ``INSERT OR IGNORE`` 丢掉其中一行。
    """

    seen_uuid: set[str] = set()
    active_material: set[str] = set()
    for table in ("sample", "current_substance", "reagent"):
        if not _table_exists(connection, table):
            continue
        rows = connection.execute(
            f"SELECT uuid, material_uuid, deleted_at FROM {table}"
        ).fetchall()
        for content_uuid, material_uuid, deleted_at in rows:
            if content_uuid in seen_uuid:
                raise sqlite3.IntegrityError(
                    f"UNIQUE constraint failed: container_content.uuid"
                )
            seen_uuid.add(content_uuid)
            if deleted_at is None:
                if material_uuid in active_material:
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint failed: container_content.material_uuid"
                    )
                active_material.add(material_uuid)


def migrate_inventory_table_compaction(connection: sqlite3.Connection) -> None:
    """幂等合并库存表。已是新结构时只补齐视图。"""

    assert_legacy_container_content_mergeable(connection)
    _drop_compatibility_objects(connection)
    _merge_material_children(connection)
    _merge_template_version(connection)
    _merge_container_content(connection)
    _merge_ingress_site(connection)
    _collapse_lab_meta(connection)
    _drop_lab_layout(connection)
    _reinstall_compatibility_views(connection)
    _install_content_views(connection)


def _add_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if column not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _merge_material_children(connection: sqlite3.Connection) -> None:
    for column, definition in (
        ("legacy_cloud_id", "TEXT NOT NULL DEFAULT ''"),
        ("legacy_template_id", "TEXT NOT NULL DEFAULT ''"),
        ("lot_id", "TEXT NOT NULL DEFAULT ''"),
        ("inventory_status", "TEXT NOT NULL DEFAULT 'warehouse'"),
        ("disposition", "TEXT NOT NULL DEFAULT 'active'"),
        ("aggregate_version", "INTEGER NOT NULL DEFAULT 0"),
        ("content_version", "INTEGER NOT NULL DEFAULT 0"),
        ("pose_uuid", "TEXT"),
        ("pose_description", "TEXT"),
        ("pose_meta_data", "TEXT NOT NULL DEFAULT '{}'"),
        ("position_x", "REAL"),
        ("position_y", "REAL"),
        ("position_z", "REAL"),
        ("depth", "REAL"),
        ("length", "REAL"),
        ("width", "REAL"),
        ("scale_x", "REAL"),
        ("scale_y", "REAL"),
        ("scale_z", "REAL"),
        ("rotation_x", "REAL"),
        ("rotation_y", "REAL"),
        ("rotation_z", "REAL"),
    ):
        _add_column(connection, "material", column, definition)
    if _table_exists(connection, "material_inventory"):
        connection.execute(
            """
            UPDATE material SET
                legacy_cloud_id = inventory.legacy_cloud_id,
                legacy_template_id = inventory.legacy_template_id,
                lot_id = inventory.lot_id,
                inventory_status = inventory.inventory_status,
                disposition = inventory.disposition,
                aggregate_version = inventory.aggregate_version
            FROM material_inventory AS inventory
            WHERE inventory.material_uuid = material.uuid
            """
        )
        connection.execute("DROP TABLE material_inventory")
    if _table_exists(connection, "material_content_version"):
        connection.execute(
            """
            UPDATE material SET content_version = content.version
            FROM material_content_version AS content
            WHERE content.material_uuid = material.uuid
            """
        )
        connection.execute("DROP TABLE material_content_version")
    if _table_exists(connection, "relative_position"):
        connection.execute(
            """
            UPDATE material SET
                pose_uuid = pose.uuid,
                pose_description = pose.description,
                pose_meta_data = pose.meta_data,
                position_x = pose.position_x,
                position_y = pose.position_y,
                position_z = pose.position_z,
                depth = pose.depth,
                length = pose.length,
                width = pose.width,
                scale_x = pose.scale_x,
                scale_y = pose.scale_y,
                scale_z = pose.scale_z,
                rotation_x = pose.rotation_x,
                rotation_y = pose.rotation_y,
                rotation_z = pose.rotation_z
            FROM relative_position AS pose
            WHERE pose.material_uuid = material.uuid AND pose.deleted_at IS NULL
            """
        )
        connection.execute("DROP TABLE relative_position")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_material_legacy_cloud "
        "ON material (legacy_cloud_id)"
    )


def _merge_template_version(connection: sqlite3.Connection) -> None:
    _add_column(
        connection,
        "resource_template",
        "aggregate_version",
        "INTEGER NOT NULL DEFAULT 0",
    )
    if _table_exists(connection, "resource_template_inventory"):
        connection.execute(
            """
            UPDATE resource_template SET aggregate_version = inventory.aggregate_version
            FROM resource_template_inventory AS inventory
            WHERE inventory.resource_template_uuid = resource_template.uuid
            """
        )
        connection.execute("DROP TABLE resource_template_inventory")


def _merge_container_content(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "material") or not _table_exists(
        connection, "reagent_info"
    ):
        return
    connection.execute(CONTAINER_CONTENT_SCHEMA)
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_container_content_material_active
        ON container_content (material_uuid) WHERE deleted_at IS NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_container_content_sample_code_active
        ON container_content (LOWER(code))
        WHERE deleted_at IS NULL AND kind = 'sample'
        """
    )
    if _table_exists(connection, "sample"):
        connection.execute(
            """
            INSERT INTO container_content(
                uuid, create_time, update_time, deleted_at, description, meta_data,
                material_uuid, kind, code, name, sample_type, source, quantity,
                quantity_unit, collected_at, expires_at
            )
            SELECT uuid, create_time, update_time, deleted_at, description, meta_data,
                material_uuid, 'sample', code, name, sample_type, source, quantity,
                quantity_unit, collected_at, expires_at
            FROM sample
            """
        )
        connection.execute("DROP TABLE sample")
    if _table_exists(connection, "current_substance"):
        connection.execute(
            """
            INSERT INTO container_content(
                uuid, create_time, update_time, deleted_at, description, meta_data,
                material_uuid, kind, name, composition, quantity, quantity_unit,
                physical_state, revision, observed_at
            )
            SELECT uuid, create_time, update_time, deleted_at, description, meta_data,
                material_uuid, 'substance', name, composition, quantity, quantity_unit,
                physical_state, revision, observed_at
            FROM current_substance
            """
        )
        connection.execute("DROP TABLE current_substance")
    if _table_exists(connection, "reagent"):
        connection.execute(
            """
            INSERT INTO container_content(
                uuid, create_time, update_time, deleted_at, description, meta_data,
                material_uuid, kind, reagent_info_uuid, concentration_value,
                concentration_unit, quantity, quantity_unit, revision, physical_state,
                density_g_per_ml, density_source
            )
            SELECT uuid, create_time, update_time, deleted_at, description, meta_data,
                material_uuid, 'reagent', reagent_info_uuid, concentration_value,
                concentration_unit, quantity, quantity_unit, revision, physical_state,
                density_g_per_ml, density_source
            FROM reagent
            """
        )
        connection.execute("DROP TABLE reagent")


def _merge_ingress_site(connection: sqlite3.Connection) -> None:
    if "site_uuid" not in _columns(connection, "station_ingress_reservation"):
        if not _table_exists(connection, "station_ingress_reservation"):
            return
        _add_column(connection, "station_ingress_reservation", "site_uuid", "TEXT")
        _add_column(
            connection,
            "station_ingress_reservation",
            "site_active",
            "INTEGER NOT NULL DEFAULT 0",
        )
    if _table_exists(connection, "station_ingress_reservation_site"):
        connection.execute(
            """
            UPDATE station_ingress_reservation SET
                site_uuid = selected.site_uuid,
                site_active = selected.active
            FROM station_ingress_reservation_site AS selected
            WHERE selected.reservation_uuid = station_ingress_reservation.uuid
            """
        )
        connection.execute("DROP TABLE station_ingress_reservation_site")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_station_ingress_active_site
        ON station_ingress_reservation (site_uuid) WHERE site_active = 1
        """
    )


def _collapse_lab_meta(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "lab_meta")
    if not columns:
        connection.execute(_LAB_META_DDL)
        return
    if "id" in columns and "meta_key" not in columns:
        return
    connection.execute(_LAB_META_DDL.replace("lab_meta", "lab_meta_row"))
    connection.execute(
        """
        INSERT INTO lab_meta_row(
            id, lab_name, lab_domain, resource_graph_bootstrap_source,
            resource_graph_bootstrap_fingerprint,
            resource_graph_bootstrap_fingerprint_version
        )
        SELECT 1,
            COALESCE((SELECT meta_value FROM lab_meta WHERE meta_key='lab_name'), ''),
            COALESCE((SELECT meta_value FROM lab_meta WHERE meta_key='lab_domain'), ''),
            (SELECT meta_value FROM lab_meta WHERE meta_key='resource_graph_bootstrap_source'),
            (SELECT meta_value FROM lab_meta WHERE meta_key='resource_graph_bootstrap_fingerprint'),
            (SELECT meta_value FROM lab_meta
             WHERE meta_key='resource_graph_bootstrap_fingerprint_version')
        """
    )
    connection.execute("DROP TABLE lab_meta")
    connection.execute("ALTER TABLE lab_meta_row RENAME TO lab_meta")


_LAB_META_DDL = """
CREATE TABLE lab_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    lab_name TEXT NOT NULL DEFAULT '',
    lab_domain TEXT NOT NULL DEFAULT '',
    resource_graph_bootstrap_source TEXT,
    resource_graph_bootstrap_fingerprint TEXT,
    resource_graph_bootstrap_fingerprint_version TEXT
)
"""


def _drop_lab_layout(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS lab_placement")
    connection.execute("DROP TABLE IF EXISTS lab_zone")


def _drop_compatibility_objects(connection: sqlite3.Connection) -> None:
    for name in (
        "inventory_resource_template_insert",
        "inventory_resource_template_update",
        "inventory_resource_template_delete",
        "material_instance_insert",
        "material_instance_update",
        "material_instance_delete",
        "substance_content_insert",
        "substance_content_update",
        "resource_relation_insert",
        "resource_relation_update",
        "resource_relation_delete",
        "resource_relation_occupancy_guard",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in (
        "inventory_resource_template",
        "material_instance",
        "substance_content",
        "resource_relation",
    ):
        connection.execute(f"DROP VIEW IF EXISTS {name}")


def _reinstall_compatibility_views(connection: sqlite3.Connection) -> None:
    for name in (
        "inventory_resource_template_insert",
        "inventory_resource_template_update",
        "inventory_resource_template_delete",
        "material_instance_insert",
        "material_instance_update",
        "material_instance_delete",
        "substance_content_insert",
        "substance_content_update",
        "resource_relation_update",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in (
        "inventory_resource_template",
        "material_instance",
        "substance_content",
        "resource_relation",
    ):
        connection.execute(f"DROP VIEW IF EXISTS {name}")
    connection.executescript(_COMPATIBILITY_VIEWS)


_COMPATIBILITY_VIEWS = r"""
CREATE VIEW inventory_resource_template AS
SELECT
    template.uuid AS template_id,
    template.display_name AS name,
    template.resource_type AS category,
    template.model AS spec_json,
    template.aggregate_version AS version
FROM resource_template AS template
WHERE template.deleted_at IS NULL;

CREATE TRIGGER inventory_resource_template_insert
INSTEAD OF INSERT ON inventory_resource_template
BEGIN
    INSERT INTO resource_template (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        name, display_name, resource_type, model, tags, data_schema,
        config_schema, pose, config_info, scene, device_params, ui_overlay,
        aggregate_version
    ) VALUES (
        NEW.template_id,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        NULL, NULL, '{}',
        NEW.template_id,
        CASE WHEN NEW.name = '' THEN NEW.template_id ELSE NEW.name END,
        CASE WHEN NEW.category = '' THEN 'resource' ELSE NEW.category END,
        CASE WHEN json_valid(NEW.spec_json) THEN NEW.spec_json ELSE '{}' END,
        '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}',
        CASE WHEN NEW.version > 0 THEN NEW.version ELSE 1 END
    )
    ON CONFLICT(uuid) DO UPDATE SET
        update_time = excluded.update_time,
        deleted_at = NULL,
        display_name = excluded.display_name,
        resource_type = excluded.resource_type,
        model = excluded.model,
        aggregate_version = excluded.aggregate_version;
END;

CREATE TRIGGER inventory_resource_template_update
INSTEAD OF UPDATE ON inventory_resource_template
BEGIN
    UPDATE resource_template
    SET display_name = CASE WHEN NEW.name = '' THEN NEW.template_id ELSE NEW.name END,
        resource_type = CASE WHEN NEW.category = '' THEN 'resource' ELSE NEW.category END,
        model = CASE WHEN json_valid(NEW.spec_json) THEN NEW.spec_json ELSE '{}' END,
        aggregate_version = NEW.version,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = OLD.template_id AND deleted_at IS NULL;
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
    material.legacy_cloud_id AS legacy_cloud_id,
    material.lot_id AS lot_id,
    material.legacy_template_id AS template_id,
    material.barcode AS barcode,
    material.inventory_status AS status,
    material.aggregate_version AS version,
    COALESCE(material.parent_uuid, '') AS parent_uuid
FROM material AS material;

CREATE TRIGGER material_instance_insert
INSTEAD OF INSERT ON material_instance
BEGIN
    INSERT OR IGNORE INTO resource_template (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        name, display_name, resource_type, model, tags, data_schema,
        config_schema, pose, config_info, scene, device_params, ui_overlay,
        aggregate_version
    ) VALUES (
        COALESCE(NULLIF(NEW.template_id, ''), '__edge_unknown_resource_template__'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        'Edge legacy placeholder; hidden from the shared Backend Interface',
        '{"unilab_edge_placeholder":true}',
        COALESCE(NULLIF(NEW.template_id, ''), '__edge_unknown_resource_template__'),
        COALESCE(NULLIF(NEW.template_id, ''), 'Unknown Edge resource'),
        'resource', '{}', '[]', '{}', '{}', '{}', '[]', '[]', '{}', '{}',
        1
    );
    INSERT INTO material (
        uuid, create_time, update_time, deleted_at, description, meta_data,
        resource_template_uuid, parent_uuid, class, barcode, name, config, data,
        legacy_cloud_id, legacy_template_id, lot_id, inventory_status, disposition,
        aggregate_version
    ) VALUES (
        NEW.edge_uuid,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        NULL, NULL, '{}',
        COALESCE(NULLIF(NEW.template_id, ''), '__edge_unknown_resource_template__'),
        NULL,
        'resource', NEW.barcode, NEW.edge_uuid, '{}', '{}',
        NEW.legacy_cloud_id, NEW.template_id, NEW.lot_id, NEW.status,
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
        legacy_cloud_id = NEW.legacy_cloud_id,
        legacy_template_id = NEW.template_id,
        lot_id = NEW.lot_id,
        inventory_status = NEW.status,
        disposition = CASE NEW.status
            WHEN 'consumed' THEN 'consumed'
            WHEN 'discarded' THEN 'discarded'
            WHEN 'quarantined' THEN 'quarantined'
            ELSE 'active'
        END,
        aggregate_version = NEW.version,
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
END;

CREATE TRIGGER material_instance_delete
INSTEAD OF DELETE ON material_instance
BEGIN
    UPDATE material
    SET deleted_at = COALESCE(
            material.deleted_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ),
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = OLD.edge_uuid;
END;

CREATE VIEW resource_relation AS
SELECT
    site.material_uuid AS parent_uuid,
    site.name AS slot_id,
    material.uuid AS child_uuid,
    material.aggregate_version AS version
FROM material AS material
LEFT JOIN site AS site
    ON site.occupied_material_uuid = material.uuid AND site.deleted_at IS NULL
WHERE material.deleted_at IS NULL
  AND site.uuid IS NOT NULL;

CREATE TRIGGER resource_relation_update
INSTEAD OF UPDATE ON resource_relation
BEGIN
    DELETE FROM resource_relation WHERE child_uuid = OLD.child_uuid;
    INSERT INTO resource_relation(parent_uuid, slot_id, child_uuid, version)
    VALUES (NEW.parent_uuid, NEW.slot_id, NEW.child_uuid, NEW.version);
END;

CREATE VIEW substance_content AS
SELECT
    material.uuid AS instance_uuid,
    material.data AS state_json,
    material.content_version AS version
FROM material
WHERE material.deleted_at IS NULL AND material.content_version > 0;

CREATE TRIGGER substance_content_insert
INSTEAD OF INSERT ON substance_content
BEGIN
    UPDATE material
    SET data = CASE WHEN json_valid(NEW.state_json) THEN NEW.state_json ELSE '{}' END,
        content_version = CASE WHEN NEW.version > 0 THEN NEW.version ELSE 1 END,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = NEW.instance_uuid AND deleted_at IS NULL;
END;

CREATE TRIGGER substance_content_update
INSTEAD OF UPDATE ON substance_content
BEGIN
    UPDATE material
    SET data = CASE WHEN json_valid(NEW.state_json) THEN NEW.state_json ELSE '{}' END,
        content_version = NEW.version,
        update_time = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE uuid = OLD.instance_uuid AND deleted_at IS NULL;
END;
"""

_CONTENT_VIEWS = r"""
DROP TRIGGER IF EXISTS reagent_insert;
DROP TRIGGER IF EXISTS reagent_update;
DROP TRIGGER IF EXISTS reagent_delete;
DROP TRIGGER IF EXISTS sample_insert;
DROP TRIGGER IF EXISTS sample_update;
DROP TRIGGER IF EXISTS sample_delete;
DROP TRIGGER IF EXISTS current_substance_insert;
DROP TRIGGER IF EXISTS current_substance_update;
DROP TRIGGER IF EXISTS current_substance_delete;
DROP VIEW IF EXISTS reagent;
DROP VIEW IF EXISTS sample;
DROP VIEW IF EXISTS current_substance;

CREATE VIEW reagent AS
SELECT uuid, create_time, update_time, deleted_at, description, meta_data,
       material_uuid, reagent_info_uuid, concentration_value, concentration_unit,
       quantity, quantity_unit, revision, physical_state, density_g_per_ml,
       density_source
FROM container_content WHERE kind = 'reagent';

CREATE TRIGGER reagent_insert INSTEAD OF INSERT ON reagent
BEGIN
    INSERT INTO container_content(
        uuid, create_time, update_time, deleted_at, description, meta_data,
        material_uuid, kind, reagent_info_uuid, concentration_value,
        concentration_unit, quantity, quantity_unit, revision, physical_state,
        density_g_per_ml, density_source
    ) VALUES (
        NEW.uuid, NEW.create_time, NEW.update_time, NEW.deleted_at, NEW.description,
        NEW.meta_data, NEW.material_uuid, 'reagent', NEW.reagent_info_uuid,
        NEW.concentration_value, NEW.concentration_unit, NEW.quantity,
        NEW.quantity_unit, NEW.revision, NEW.physical_state, NEW.density_g_per_ml,
        NEW.density_source
    );
END;

CREATE TRIGGER reagent_update INSTEAD OF UPDATE ON reagent
BEGIN
    UPDATE container_content SET
        uuid=NEW.uuid,
        create_time=NEW.create_time, update_time=NEW.update_time,
        deleted_at=NEW.deleted_at, description=NEW.description, meta_data=NEW.meta_data,
        material_uuid=NEW.material_uuid, reagent_info_uuid=NEW.reagent_info_uuid,
        concentration_value=NEW.concentration_value,
        concentration_unit=NEW.concentration_unit, quantity=NEW.quantity,
        quantity_unit=NEW.quantity_unit, revision=NEW.revision,
        physical_state=NEW.physical_state, density_g_per_ml=NEW.density_g_per_ml,
        density_source=NEW.density_source
    WHERE uuid=OLD.uuid AND kind='reagent';
END;

CREATE TRIGGER reagent_delete INSTEAD OF DELETE ON reagent
BEGIN
    DELETE FROM container_content WHERE uuid=OLD.uuid AND kind='reagent';
END;

CREATE VIEW sample AS
SELECT uuid, create_time, update_time, deleted_at, description, meta_data,
       material_uuid, code, name, sample_type, source, quantity, quantity_unit,
       collected_at, expires_at
FROM container_content WHERE kind = 'sample';

CREATE TRIGGER sample_insert INSTEAD OF INSERT ON sample
BEGIN
    INSERT INTO container_content(
        uuid, create_time, update_time, deleted_at, description, meta_data,
        material_uuid, kind, code, name, sample_type, source, quantity,
        quantity_unit, collected_at, expires_at
    ) VALUES (
        NEW.uuid, NEW.create_time, NEW.update_time, NEW.deleted_at, NEW.description,
        NEW.meta_data, NEW.material_uuid, 'sample', NEW.code, NEW.name,
        NEW.sample_type, NEW.source, NEW.quantity, NEW.quantity_unit,
        NEW.collected_at, NEW.expires_at
    );
END;

CREATE TRIGGER sample_update INSTEAD OF UPDATE ON sample
BEGIN
    UPDATE container_content SET
        uuid=NEW.uuid,
        create_time=NEW.create_time, update_time=NEW.update_time,
        deleted_at=NEW.deleted_at, description=NEW.description, meta_data=NEW.meta_data,
        material_uuid=NEW.material_uuid, code=NEW.code, name=NEW.name,
        sample_type=NEW.sample_type, source=NEW.source, quantity=NEW.quantity,
        quantity_unit=NEW.quantity_unit, collected_at=NEW.collected_at,
        expires_at=NEW.expires_at
    WHERE uuid=OLD.uuid AND kind='sample';
END;

CREATE TRIGGER sample_delete INSTEAD OF DELETE ON sample
BEGIN
    DELETE FROM container_content WHERE uuid=OLD.uuid AND kind='sample';
END;

CREATE VIEW current_substance AS
SELECT uuid, create_time, update_time, deleted_at, description, meta_data,
       material_uuid, name, composition, quantity, quantity_unit, physical_state,
       revision, observed_at
FROM container_content WHERE kind = 'substance';

CREATE TRIGGER current_substance_insert INSTEAD OF INSERT ON current_substance
BEGIN
    INSERT INTO container_content(
        uuid, create_time, update_time, deleted_at, description, meta_data,
        material_uuid, kind, name, composition, quantity, quantity_unit,
        physical_state, revision, observed_at
    ) VALUES (
        NEW.uuid, NEW.create_time, NEW.update_time, NEW.deleted_at, NEW.description,
        NEW.meta_data, NEW.material_uuid, 'substance', NEW.name, NEW.composition,
        NEW.quantity, NEW.quantity_unit, NEW.physical_state, NEW.revision,
        NEW.observed_at
    );
END;

CREATE TRIGGER current_substance_update INSTEAD OF UPDATE ON current_substance
BEGIN
    UPDATE container_content SET
        uuid=NEW.uuid,
        create_time=NEW.create_time, update_time=NEW.update_time,
        deleted_at=NEW.deleted_at, description=NEW.description, meta_data=NEW.meta_data,
        material_uuid=NEW.material_uuid, name=NEW.name, composition=NEW.composition,
        quantity=NEW.quantity, quantity_unit=NEW.quantity_unit,
        physical_state=NEW.physical_state, revision=NEW.revision,
        observed_at=NEW.observed_at
    WHERE uuid=OLD.uuid AND kind='substance';
END;

CREATE TRIGGER current_substance_delete INSTEAD OF DELETE ON current_substance
BEGIN
    DELETE FROM container_content WHERE uuid=OLD.uuid AND kind='substance';
END;
"""


def _install_content_views(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "container_content"):
        return
    connection.executescript(_CONTENT_VIEWS)
