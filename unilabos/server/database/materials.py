"""资源、物料、Site 与库存事实的 ``materials.db`` v1 schema。

本库只保存物料域事实及其事务 outbox。Job、command 等跨库身份仅保存规范
UUID，不建立跨 SQLite 文件的外键。所有投影、ledger 和 outbox 必须由本库唯一
writer 在同一事务内提交。
"""

from unilabos.server.database.schema import (
    SCHEMA_MIGRATION_TABLE,
    DatabaseSpec,
    TableSpec,
)


_POSE_COLUMNS = """
    size_depth REAL NOT NULL DEFAULT 0 CHECK (size_depth >= 0),
    size_width REAL NOT NULL DEFAULT 0 CHECK (size_width >= 0),
    size_height REAL NOT NULL DEFAULT 0 CHECK (size_height >= 0),
    scale_x REAL NOT NULL DEFAULT 0,
    scale_y REAL NOT NULL DEFAULT 0,
    scale_z REAL NOT NULL DEFAULT 0,
    layout TEXT NOT NULL DEFAULT 'x-y'
        CHECK (layout IN ('2d','x-y','z-y','x-z')),
    position_x REAL,
    position_y REAL,
    position_z REAL,
    position3d_x REAL NOT NULL DEFAULT 0,
    position3d_y REAL NOT NULL DEFAULT 0,
    position3d_z REAL NOT NULL DEFAULT 0,
    rotation_x REAL NOT NULL DEFAULT 0,
    rotation_y REAL NOT NULL DEFAULT 0,
    rotation_z REAL NOT NULL DEFAULT 0,
    cross_section_type TEXT NOT NULL DEFAULT 'rectangle'
        CHECK (cross_section_type IN ('rectangle','circle','rounded_rectangle')),
    pose_extra_json TEXT CHECK (
        pose_extra_json IS NULL OR (
            json_valid(pose_extra_json)
            AND json_type(pose_extra_json) = 'object'
        )
    ),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK (
        (position_x IS NULL AND position_y IS NULL AND position_z IS NULL)
        OR
        (position_x IS NOT NULL AND position_y IS NOT NULL AND position_z IS NOT NULL)
    )
"""


MATERIALS_TABLES = (
    SCHEMA_MIGRATION_TABLE,
    TableSpec(
        "resource_template",
        """
        CREATE TABLE IF NOT EXISTS resource_template (
            template_uuid TEXT PRIMARY KEY CHECK (TRIM(template_uuid) <> ''),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            display_name TEXT NOT NULL CHECK (TRIM(display_name) <> ''),
            resource_type TEXT NOT NULL CHECK (TRIM(resource_type) <> ''),
            class_name TEXT,
            module_name TEXT,
            template_version TEXT NOT NULL CHECK (TRIM(template_version) <> ''),
            definition_json TEXT NOT NULL CHECK (
                json_valid(definition_json)
                AND json_type(definition_json) = 'object'
            ),
            definition_hash TEXT NOT NULL CHECK (TRIM(definition_hash) <> ''),
            status TEXT NOT NULL CHECK (status IN ('active','deprecated','deleted')),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (
                (status = 'deleted' AND deleted_at_ms IS NOT NULL)
                OR (status <> 'deleted' AND deleted_at_ms IS NULL)
            )
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_resource_template_name_active
            ON resource_template(name) WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_resource_template_type_active
            ON resource_template(resource_type, name)
            WHERE deleted_at_ms IS NULL
            """,
        ),
    ),
    TableSpec(
        "resource_handle_template",
        """
        CREATE TABLE IF NOT EXISTS resource_handle_template (
            handle_uuid TEXT PRIMARY KEY CHECK (TRIM(handle_uuid) <> ''),
            template_uuid TEXT NOT NULL,
            handle_key TEXT NOT NULL CHECK (TRIM(handle_key) <> ''),
            label TEXT NOT NULL CHECK (TRIM(label) <> ''),
            io_type TEXT NOT NULL CHECK (
                io_type IN ('source','target','bidirectional')
            ),
            data_type TEXT NOT NULL CHECK (TRIM(data_type) <> ''),
            side TEXT CHECK (
                side IS NULL OR side IN ('NORTH','SOUTH','EAST','WEST')
            ),
            data_key TEXT,
            data_source TEXT,
            description TEXT NOT NULL DEFAULT '',
            handle_schema_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(handle_schema_json)
                AND json_type(handle_schema_json) = 'object'
            ),
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json)
                AND json_type(meta_data_json) = 'object'
            ),
            definition_hash TEXT NOT NULL CHECK (TRIM(definition_hash) <> ''),
            sort_order INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (data_key IS NULL OR TRIM(data_key) <> ''),
            CHECK (data_source IS NULL OR TRIM(data_source) <> ''),
            FOREIGN KEY(template_uuid) REFERENCES resource_template(template_uuid)
                ON DELETE CASCADE
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_resource_handle_business_key_active
            ON resource_handle_template(template_uuid, io_type, handle_key)
            WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_resource_handle_template_order
            ON resource_handle_template(template_uuid, sort_order, handle_uuid)
            WHERE deleted_at_ms IS NULL
            """,
        ),
    ),
    TableSpec(
        "inventory_lot",
        """
        CREATE TABLE IF NOT EXISTS inventory_lot (
            lot_uuid TEXT PRIMARY KEY CHECK (TRIM(lot_uuid) <> ''),
            template_uuid TEXT NOT NULL,
            batch_no TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL CHECK (TRIM(unit) <> ''),
            quantity_total REAL NOT NULL CHECK (quantity_total >= 0),
            quantity_available REAL NOT NULL CHECK (quantity_available >= 0),
            quantity_reserved REAL NOT NULL CHECK (quantity_reserved >= 0),
            expiry_at_ms INTEGER CHECK (expiry_at_ms >= 0),
            quarantined INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0,1)),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (quantity_available + quantity_reserved <= quantity_total),
            FOREIGN KEY(template_uuid) REFERENCES resource_template(template_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_lot_allocation
            ON inventory_lot(template_uuid, expiry_at_ms, created_at_ms, lot_uuid)
            WHERE quarantined = 0 AND quantity_available > 0
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_lot_batch
            ON inventory_lot(template_uuid, batch_no)
            WHERE batch_no <> ''
            """,
        ),
    ),
    TableSpec(
        "material",
        """
        CREATE TABLE IF NOT EXISTS material (
            material_uuid TEXT PRIMARY KEY CHECK (TRIM(material_uuid) <> ''),
            resource_id TEXT NOT NULL CHECK (TRIM(resource_id) <> ''),
            template_uuid TEXT NOT NULL,
            parent_material_uuid TEXT,
            lot_uuid TEXT,
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            description TEXT NOT NULL DEFAULT '',
            resource_type TEXT NOT NULL CHECK (TRIM(resource_type) <> ''),
            class_name TEXT NOT NULL CHECK (TRIM(class_name) <> ''),
            machine_name TEXT NOT NULL DEFAULT '',
            barcode TEXT NOT NULL DEFAULT '',
            barcode_symbology TEXT NOT NULL DEFAULT '',
            template_name TEXT NOT NULL CHECK (TRIM(template_name) <> ''),
            resource_schema_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(resource_schema_json)
                AND json_type(resource_schema_json) = 'object'
            ),
            model_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(model_json) AND json_type(model_json) = 'object'
            ),
            icon_uri TEXT NOT NULL DEFAULT '',
            config_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(config_json) AND json_type(config_json) = 'object'
            ),
            extra_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extra_json) AND json_type(extra_json) = 'object'
            ),
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN (
                'active','reserved','in_use','quarantined','consumed','retired'
            )),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (parent_material_uuid IS NULL OR parent_material_uuid <> material_uuid),
            FOREIGN KEY(template_uuid) REFERENCES resource_template(template_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(parent_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(lot_uuid) REFERENCES inventory_lot(lot_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_barcode_active
            ON material(LOWER(barcode))
            WHERE deleted_at_ms IS NULL AND barcode <> ''
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_sibling_resource_id_active
            ON material(COALESCE(parent_material_uuid, ''), resource_id)
            WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_sibling_name_active
            ON material(COALESCE(parent_material_uuid, ''), LOWER(name))
            WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_template_active
            ON material(template_uuid, created_at_ms, material_uuid)
            WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_parent_active
            ON material(parent_material_uuid, material_uuid)
            WHERE deleted_at_ms IS NULL AND parent_material_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_lot_active
            ON material(lot_uuid, material_uuid)
            WHERE deleted_at_ms IS NULL AND lot_uuid IS NOT NULL
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_material_parent_cycle
            BEFORE UPDATE OF parent_material_uuid ON material
            WHEN NEW.parent_material_uuid IS NOT NULL
                AND NEW.parent_material_uuid IS NOT OLD.parent_material_uuid
            BEGIN
                WITH RECURSIVE ancestors(material_uuid) AS (
                    SELECT NEW.parent_material_uuid
                    UNION
                    SELECT material.parent_material_uuid
                    FROM material
                    JOIN ancestors
                        ON material.material_uuid = ancestors.material_uuid
                    WHERE material.parent_material_uuid IS NOT NULL
                )
                SELECT RAISE(ABORT, 'material tree cycle')
                WHERE EXISTS (
                    SELECT 1 FROM ancestors
                    WHERE material_uuid = NEW.material_uuid
                );
            END
            """,
        ),
    ),
    TableSpec(
        "material_pose",
        f"""
        CREATE TABLE IF NOT EXISTS material_pose (
            material_uuid TEXT PRIMARY KEY,
            frame_kind TEXT NOT NULL CHECK (frame_kind IN ('lab','material','site')),
            frame_material_uuid TEXT,
            frame_site_uuid TEXT,
            {_POSE_COLUMNS},
            CHECK (
                (frame_kind = 'lab' AND frame_material_uuid IS NULL
                    AND frame_site_uuid IS NULL)
                OR
                (frame_kind = 'material' AND frame_material_uuid IS NOT NULL
                    AND frame_site_uuid IS NULL)
                OR
                (frame_kind = 'site' AND frame_material_uuid IS NULL
                    AND frame_site_uuid IS NOT NULL)
            ),
            FOREIGN KEY(material_uuid) REFERENCES material(material_uuid)
                ON DELETE CASCADE,
            FOREIGN KEY(frame_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(frame_site_uuid) REFERENCES site(site_uuid)
                ON DELETE RESTRICT
        )
        """,
    ),
    TableSpec(
        "material_state",
        """
        CREATE TABLE IF NOT EXISTS material_state (
            material_uuid TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (TRIM(status) <> ''),
            sites_initialized INTEGER NOT NULL DEFAULT 0
                CHECK (sites_initialized IN (0,1)),
            data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(data_json) AND json_type(data_json) = 'object'
            ),
            liquids_json TEXT CHECK (
                liquids_json IS NULL OR
                (json_valid(liquids_json) AND json_type(liquids_json) = 'array')
            ),
            unknown_counter INTEGER CHECK (unknown_counter >= 0),
            content_version INTEGER NOT NULL DEFAULT 1 CHECK (content_version > 0),
            state_hash TEXT NOT NULL CHECK (TRIM(state_hash) <> ''),
            source_event_uuid TEXT NOT NULL CHECK (TRIM(source_event_uuid) <> ''),
            source_job_uuid TEXT,
            source_command_uuid TEXT,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= observed_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            FOREIGN KEY(material_uuid) REFERENCES material(material_uuid)
                ON DELETE CASCADE,
            FOREIGN KEY(source_event_uuid, material_uuid)
                REFERENCES material_state_source_event(
                    source_event_uuid, material_uuid
                ) ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_material_state_source_job
            ON material_state(source_job_uuid)
            WHERE source_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_state_source_command
            ON material_state(source_command_uuid)
            WHERE source_command_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "material_state_source_event",
        """
        CREATE TABLE IF NOT EXISTS material_state_source_event (
            source_event_uuid TEXT NOT NULL CHECK (TRIM(source_event_uuid) <> ''),
            material_uuid TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK (source_kind IN (
                'adapter_report','backend_command','import','reconcile',
                'manual_override'
            )),
            state_hash TEXT NOT NULL CHECK (TRIM(state_hash) <> ''),
            applied_content_version INTEGER NOT NULL
                CHECK (applied_content_version > 0),
            source_job_uuid TEXT,
            source_command_uuid TEXT,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= observed_at_ms),
            PRIMARY KEY(source_event_uuid, material_uuid),
            FOREIGN KEY(material_uuid) REFERENCES material(material_uuid)
                ON DELETE CASCADE
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_material_state_event_job
            ON material_state_source_event(source_job_uuid, received_at_ms)
            WHERE source_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_state_event_command
            ON material_state_source_event(source_command_uuid, received_at_ms)
            WHERE source_command_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "site",
        """
        CREATE TABLE IF NOT EXISTS site (
            site_uuid TEXT PRIMARY KEY CHECK (TRIM(site_uuid) <> ''),
            schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
            owner_material_uuid TEXT NOT NULL,
            template_name TEXT NOT NULL CHECK (TRIM(template_name) <> ''),
            site_index NOT NULL CHECK (
                (typeof(site_index) = 'integer') OR
                (typeof(site_index) = 'text' AND TRIM(site_index) <> '')
            ),
            label TEXT NOT NULL CHECK (TRIM(label) <> ''),
            visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0,1)),
            occupied_material_uuid TEXT,
            pose_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(pose_json) AND json_type(pose_json) = 'object'
            ),
            allowed_resource_categories_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(allowed_resource_categories_json)
                AND json_type(allowed_resource_categories_json) = 'array'
            ),
            parent_link TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            extra_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extra_json) AND json_type(extra_json) = 'object'
            ),
            occupancy_changed_by_job_uuid TEXT,
            occupancy_changed_by_command_uuid TEXT,
            occupancy_changed_at_ms INTEGER NOT NULL
                CHECK (occupancy_changed_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (
                occupied_material_uuid IS NULL
                OR occupied_material_uuid <> owner_material_uuid
            ),
            CHECK (deleted_at_ms IS NULL OR occupied_material_uuid IS NULL),
            FOREIGN KEY(owner_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(occupied_material_uuid) REFERENCES material(material_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_site_occupied_material_active
            ON site(occupied_material_uuid)
            WHERE deleted_at_ms IS NULL AND occupied_material_uuid IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_site_owner_index_active
            ON site(owner_material_uuid, site_index)
            WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_site_owner_label_active
            ON site(owner_material_uuid, LOWER(label))
            WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_site_owner_order
            ON site(owner_material_uuid, site_index, site_uuid)
            WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_site_occupancy_job
            ON site(occupancy_changed_by_job_uuid, occupancy_changed_at_ms)
            WHERE occupancy_changed_by_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_site_occupancy_command
            ON site(occupancy_changed_by_command_uuid, occupancy_changed_at_ms)
            WHERE occupancy_changed_by_command_uuid IS NOT NULL
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_site_occupant_requires_descendant_insert
            BEFORE INSERT ON site
            WHEN NEW.occupied_material_uuid IS NOT NULL
            BEGIN
                WITH RECURSIVE ancestors(material_uuid) AS (
                    SELECT parent_material_uuid
                    FROM material
                    WHERE material_uuid = NEW.occupied_material_uuid
                    UNION
                    SELECT material.parent_material_uuid
                    FROM material
                    JOIN ancestors
                        ON material.material_uuid = ancestors.material_uuid
                    WHERE material.parent_material_uuid IS NOT NULL
                )
                SELECT RAISE(ABORT, 'site occupant must be an owner descendant')
                WHERE NOT EXISTS (
                    SELECT 1 FROM ancestors
                    WHERE material_uuid = NEW.owner_material_uuid
                );
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_site_occupant_requires_descendant_update
            BEFORE UPDATE OF occupied_material_uuid, owner_material_uuid ON site
            WHEN NEW.occupied_material_uuid IS NOT NULL
            BEGIN
                WITH RECURSIVE ancestors(material_uuid) AS (
                    SELECT parent_material_uuid
                    FROM material
                    WHERE material_uuid = NEW.occupied_material_uuid
                    UNION
                    SELECT material.parent_material_uuid
                    FROM material
                    JOIN ancestors
                        ON material.material_uuid = ancestors.material_uuid
                    WHERE material.parent_material_uuid IS NOT NULL
                )
                SELECT RAISE(ABORT, 'site occupant must be an owner descendant')
                WHERE NOT EXISTS (
                    SELECT 1 FROM ancestors
                    WHERE material_uuid = NEW.owner_material_uuid
                );
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_occupied_material_parent_change
            BEFORE UPDATE OF parent_material_uuid ON material
            WHEN NEW.parent_material_uuid IS NOT OLD.parent_material_uuid
                AND EXISTS (
                    SELECT 1 FROM site
                    WHERE deleted_at_ms IS NULL
                        AND occupied_material_uuid = OLD.material_uuid
                )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'clear site occupant before changing material parent'
                );
            END
            """,
        ),
    ),
    TableSpec(
        "inventory_reservation",
        """
        CREATE TABLE IF NOT EXISTS inventory_reservation (
            reservation_uuid TEXT PRIMARY KEY CHECK (TRIM(reservation_uuid) <> ''),
            task_uuid TEXT NOT NULL CHECK (TRIM(task_uuid) <> ''),
            node_uuid TEXT NOT NULL CHECK (TRIM(node_uuid) <> ''),
            job_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(job_uuid) <> ''),
            scheduler_revision INTEGER NOT NULL CHECK (scheduler_revision >= 0),
            request_hash TEXT NOT NULL CHECK (TRIM(request_hash) <> ''),
            items_json TEXT NOT NULL CHECK (
                json_valid(items_json)
                AND json_type(items_json) = 'array'
                AND json_array_length(items_json) > 0
            ),
            status TEXT NOT NULL CHECK (status IN (
                'active','consumed','released','canceled','expired','quarantined'
            )),
            expires_at_ms INTEGER CHECK (expires_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_reservation_task
            ON inventory_reservation(task_uuid, node_uuid, created_at_ms)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_reservation_expiry
            ON inventory_reservation(expires_at_ms, reservation_uuid)
            WHERE status = 'active' AND expires_at_ms IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "inventory_command_effect",
        """
        CREATE TABLE IF NOT EXISTS inventory_command_effect (
            command_uuid TEXT NOT NULL CHECK (TRIM(command_uuid) <> ''),
            effect_key TEXT NOT NULL CHECK (TRIM(effect_key) <> ''),
            job_uuid TEXT,
            operation TEXT NOT NULL CHECK (TRIM(operation) <> ''),
            request_json TEXT NOT NULL CHECK (
                json_valid(request_json) AND json_type(request_json) = 'object'
            ),
            request_hash TEXT NOT NULL CHECK (TRIM(request_hash) <> ''),
            status TEXT NOT NULL CHECK (status IN ('applying','applied','rejected')),
            result_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(result_json) AND json_type(result_json) = 'object'
            ),
            error_code TEXT,
            error_message TEXT,
            ledger_sequence_start INTEGER,
            ledger_sequence_end INTEGER,
            started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= started_at_ms),
            completed_at_ms INTEGER CHECK (completed_at_ms >= started_at_ms),
            PRIMARY KEY(command_uuid, effect_key),
            CHECK (
                (status = 'applying' AND completed_at_ms IS NULL)
                OR (status IN ('applied','rejected') AND completed_at_ms IS NOT NULL)
            ),
            CHECK (
                (status = 'applied' AND ledger_sequence_start IS NOT NULL
                    AND ledger_sequence_end IS NOT NULL
                    AND ledger_sequence_end >= ledger_sequence_start)
                OR (status <> 'applied' AND ledger_sequence_start IS NULL
                    AND ledger_sequence_end IS NULL)
            ),
            FOREIGN KEY(ledger_sequence_start) REFERENCES inventory_ledger(sequence)
                ON DELETE RESTRICT,
            FOREIGN KEY(ledger_sequence_end) REFERENCES inventory_ledger(sequence)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_command_effect_job
            ON inventory_command_effect(job_uuid, started_at_ms)
            WHERE job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_command_effect_recovery
            ON inventory_command_effect(updated_at_ms, command_uuid, effect_key)
            WHERE status = 'applying'
            """,
        ),
    ),
    TableSpec(
        "inventory_ledger",
        """
        CREATE TABLE IF NOT EXISTS inventory_ledger (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            aggregate_type TEXT NOT NULL CHECK (aggregate_type IN (
                'resource_template','handle_template','material',
                'material_state','site','lot','reservation'
            )),
            aggregate_uuid TEXT NOT NULL CHECK (TRIM(aggregate_uuid) <> ''),
            operation TEXT NOT NULL CHECK (TRIM(operation) <> ''),
            previous_version INTEGER NOT NULL CHECK (previous_version >= 0),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
            state_hash TEXT NOT NULL CHECK (TRIM(state_hash) <> ''),
            delta_json TEXT NOT NULL CHECK (
                json_valid(delta_json) AND json_type(delta_json) = 'object'
            ),
            job_uuid TEXT,
            command_uuid TEXT,
            effect_key TEXT,
            actor_type TEXT NOT NULL CHECK (TRIM(actor_type) <> ''),
            actor_uuid TEXT,
            occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
            CHECK (aggregate_version = previous_version + 1),
            CHECK (
                (command_uuid IS NULL AND effect_key IS NULL)
                OR (command_uuid IS NOT NULL AND effect_key IS NOT NULL)
            ),
            UNIQUE(aggregate_type, aggregate_uuid, aggregate_version),
            FOREIGN KEY(command_uuid, effect_key)
                REFERENCES inventory_command_effect(command_uuid, effect_key)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_aggregate
            ON inventory_ledger(aggregate_type, aggregate_uuid, sequence)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_job
            ON inventory_ledger(job_uuid, sequence) WHERE job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_command
            ON inventory_ledger(command_uuid, effect_key, sequence)
            WHERE command_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "inventory_event_outbox",
        """
        CREATE TABLE IF NOT EXISTS inventory_event_outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            FOREIGN KEY(event_uuid) REFERENCES inventory_ledger(event_uuid)
                ON DELETE RESTRICT
        )
        """,
    ),
    TableSpec(
        "inventory_sync_state",
        """
        CREATE TABLE IF NOT EXISTS inventory_sync_state (
            peer_key TEXT PRIMARY KEY CHECK (TRIM(peer_key) <> ''),
            acked_sequence INTEGER NOT NULL DEFAULT 0 CHECK (acked_sequence >= 0),
            sent_through_sequence INTEGER NOT NULL DEFAULT 0
                CHECK (sent_through_sequence >= 0),
            snapshot_version INTEGER NOT NULL DEFAULT 0 CHECK (snapshot_version >= 0),
            last_attempt_at_ms INTEGER CHECK (last_attempt_at_ms >= 0),
            last_ack_at_ms INTEGER CHECK (last_ack_at_ms >= 0),
            consecutive_failures INTEGER NOT NULL DEFAULT 0
                CHECK (consecutive_failures >= 0),
            last_error TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (sent_through_sequence >= acked_sequence)
        )
        """,
    ),
)


MATERIALS_DATABASE = DatabaseSpec(
    key="materials",
    filename="materials.db",
    role="resource, material, site and inventory authority",
    version=1,
    synchronous="FULL",
    tables=MATERIALS_TABLES,
)


__all__ = ["MATERIALS_DATABASE", "MATERIALS_TABLES"]
