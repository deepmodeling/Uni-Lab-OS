"""对象聚合优先的 ``materials.db`` v1 schema。"""

from unilabos.server.database.schema import (
    SCHEMA_MIGRATION_TABLE,
    DatabaseSpec,
    TableSpec,
)


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
            category_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(category_json) AND json_type(category_json) = 'array'
            ),
            available_sites_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(available_sites_json)
                AND json_type(available_sites_json) = 'array'
            ),
            handles_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(handles_json) AND json_type(handles_json) = 'array'
            ),
            definition_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(definition_json) AND json_type(definition_json) = 'object'
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
            ),
            CHECK (json_type(definition_json, '$.category') IS NULL),
            CHECK (json_type(definition_json, '$.available_sites') IS NULL),
            CHECK (json_type(definition_json, '$.handles') IS NULL)
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
            CREATE INDEX IF NOT EXISTS idx_inventory_lot_available
            ON inventory_lot(template_uuid, quarantined, expiry_at_ms)
            WHERE quantity_available > 0
            """,
        ),
    ),
    TableSpec(
        "material",
        """
        CREATE TABLE IF NOT EXISTS material (
            material_uuid TEXT PRIMARY KEY CHECK (TRIM(material_uuid) <> ''),
            resource_id TEXT NOT NULL UNIQUE CHECK (TRIM(resource_id) <> ''),
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
            pose_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(pose_json) AND json_type(pose_json) = 'object'
            ),
            config_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(config_json) AND json_type(config_json) = 'object'
            ),
            data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(data_json) AND json_type(data_json) = 'object'
            ),
            liquids_json TEXT CHECK (
                liquids_json IS NULL OR (
                    json_valid(liquids_json) AND json_type(liquids_json) = 'array'
                )
            ),
            sites_initialized INTEGER NOT NULL DEFAULT 0
                CHECK (sites_initialized IN (0,1)),
            unknown_counter INTEGER CHECK (unknown_counter >= 0),
            extra_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extra_json) AND json_type(extra_json) = 'object'
            ),
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            state_status TEXT NOT NULL DEFAULT 'created'
                CHECK (TRIM(state_status) <> ''),
            state_hash TEXT NOT NULL DEFAULT '',
            source_event_uuid TEXT,
            source_job_uuid TEXT,
            source_command_uuid TEXT,
            observed_at_ms INTEGER NOT NULL DEFAULT 0 CHECK (observed_at_ms >= 0),
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
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_root_name_active
            ON material(LOWER(name))
            WHERE parent_material_uuid IS NULL AND deleted_at_ms IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_parent
            ON material(parent_material_uuid, material_uuid)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_material_template_status
            ON material(template_uuid, lifecycle_status, material_uuid)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_material_prevent_cycle
            BEFORE UPDATE OF parent_material_uuid ON material
            WHEN NEW.parent_material_uuid IS NOT NULL
            BEGIN
                WITH RECURSIVE descendants(material_uuid) AS (
                    SELECT OLD.material_uuid
                    UNION
                    SELECT material.material_uuid
                    FROM material JOIN descendants
                        ON material.parent_material_uuid = descendants.material_uuid
                )
                SELECT RAISE(ABORT, 'material tree cycle')
                WHERE NEW.parent_material_uuid IN descendants;
            END
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
                typeof(site_index) = 'integer'
                OR (typeof(site_index) = 'text' AND TRIM(site_index) <> '')
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
            changed_by_job_uuid TEXT,
            changed_by_command_uuid TEXT,
            changed_at_ms INTEGER NOT NULL CHECK (changed_at_ms >= 0),
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
            ON site(owner_material_uuid, site_index) WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_site_owner_label_active
            ON site(owner_material_uuid, LOWER(label)) WHERE deleted_at_ms IS NULL
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_site_occupant_requires_descendant_insert
            BEFORE INSERT ON site WHEN NEW.occupied_material_uuid IS NOT NULL
            BEGIN
                WITH RECURSIVE ancestors(material_uuid) AS (
                    SELECT parent_material_uuid FROM material
                    WHERE material_uuid = NEW.occupied_material_uuid
                    UNION
                    SELECT material.parent_material_uuid
                    FROM material JOIN ancestors
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
                    SELECT parent_material_uuid FROM material
                    WHERE material_uuid = NEW.occupied_material_uuid
                    UNION
                    SELECT material.parent_material_uuid
                    FROM material JOIN ancestors
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
                    ABORT, 'clear site occupant before changing material parent'
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
                json_valid(items_json) AND json_type(items_json) = 'array'
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
            ledger_sequence_start INTEGER,
            ledger_sequence_end INTEGER,
            error_code TEXT,
            error_message TEXT,
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
    ),
    TableSpec(
        "inventory_ledger",
        """
        CREATE TABLE IF NOT EXISTS inventory_ledger (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            aggregate_type TEXT NOT NULL CHECK (aggregate_type IN (
                'resource_template','material','site','lot','reservation'
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
            delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
                delivery_status IN ('pending','sent','acknowledged','dead_letter')
            ),
            delivery_attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_attempt_count >= 0),
            available_at_ms INTEGER NOT NULL DEFAULT 0 CHECK (available_at_ms >= 0),
            last_sent_at_ms INTEGER CHECK (last_sent_at_ms >= 0),
            acked_at_ms INTEGER CHECK (acked_at_ms >= 0),
            last_error TEXT,
            CHECK (aggregate_version = previous_version + 1),
            CHECK (
                (command_uuid IS NULL AND effect_key IS NULL)
                OR (command_uuid IS NOT NULL AND effect_key IS NOT NULL)
            ),
            CHECK (
                (delivery_status IN ('pending','sent','dead_letter')
                    AND acked_at_ms IS NULL)
                OR (delivery_status = 'acknowledged' AND acked_at_ms IS NOT NULL)
            ),
            UNIQUE(aggregate_type, aggregate_uuid, aggregate_version),
            FOREIGN KEY(command_uuid, effect_key)
                REFERENCES inventory_command_effect(command_uuid, effect_key)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_delivery
            ON inventory_ledger(delivery_status, available_at_ms, sequence)
            WHERE delivery_status IN ('pending','sent')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_aggregate
            ON inventory_ledger(aggregate_type, aggregate_uuid, sequence)
            """,
        ),
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
