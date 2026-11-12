"""materials.db 的迁移链：每一步把库从 ``version - 1`` 升到 ``version``。

迁移里的建表 / 索引语句是该版本冻结的字面量。
"""

from __future__ import annotations

from unilabos.server.database.schema import Migration, rebuild_table_migration, sql_migration

# v2：化学品定义表（CAS / InChIKey 可空唯一键，软删除后让出）。
_CHEMICAL_V2 = """
        CREATE TABLE IF NOT EXISTS chemical (
            chemical_uuid TEXT PRIMARY KEY CHECK (TRIM(chemical_uuid) <> ''),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            name_en TEXT,
            aliases_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(aliases_json) AND json_type(aliases_json) = 'array'
            ),
            cas TEXT CHECK (cas IS NULL OR TRIM(cas) <> ''),
            inchi_key TEXT CHECK (inchi_key IS NULL OR TRIM(inchi_key) <> ''),
            molecular_formula TEXT,
            smiles TEXT,
            molecular_weight REAL CHECK (molecular_weight IS NULL OR molecular_weight > 0),
            density_g_per_ml REAL CHECK (density_g_per_ml IS NULL OR density_g_per_ml > 0),
            physical_state TEXT NOT NULL DEFAULT 'unknown' CHECK (
                physical_state IN ('liquid','solid','gas','unknown')
            ),
            description TEXT NOT NULL DEFAULT '',
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            deleted_at_ms INTEGER CHECK (deleted_at_ms IS NULL OR deleted_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
        )
        """
_CHEMICAL_V2_INDEXES = (
    """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_chemical_cas_active
            ON chemical(cas) WHERE deleted_at_ms IS NULL AND cas IS NOT NULL
            """,
    """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_chemical_inchi_key_active
            ON chemical(inchi_key) WHERE deleted_at_ms IS NULL AND inchi_key IS NOT NULL
            """,
    """
            CREATE INDEX IF NOT EXISTS idx_chemical_name_active
            ON chemical(LOWER(name)) WHERE deleted_at_ms IS NULL
            """,
)

# v3：内容物引用化学品定义（可空 chemical_uuid）。不加外键：化学品是软删除，
# 引用在写入时由服务校验，读取时已删除的化学品摘要为 null、uuid 保留。
_MATERIAL_SUBSTANCE_V3 = """
        CREATE TABLE IF NOT EXISTS material_substance (
            substance_uuid TEXT PRIMARY KEY CHECK (TRIM(substance_uuid) <> ''),
            material_uuid TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            chemical_uuid TEXT CHECK (chemical_uuid IS NULL OR TRIM(chemical_uuid) <> ''),
            quantity REAL NOT NULL CHECK (quantity >= 0),
            quantity_unit TEXT NOT NULL CHECK (TRIM(quantity_unit) <> ''),
            physical_state TEXT NOT NULL DEFAULT 'liquid' CHECK (
                physical_state IN ('liquid','solid','gas','unknown')
            ),
            composition_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(composition_json)
                AND json_type(composition_json) = 'array'
            ),
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            content_version INTEGER NOT NULL CHECK (content_version > 0),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= observed_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            UNIQUE(material_uuid, ordinal),
            FOREIGN KEY(material_uuid) REFERENCES material_data(material_uuid)
                ON DELETE CASCADE
        )
        """
_MATERIAL_SUBSTANCE_V3_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_material_substance_name
            ON material_substance(LOWER(name), quantity_unit, material_uuid)
            """,
    """
            CREATE INDEX IF NOT EXISTS idx_material_substance_chemical
            ON material_substance(chemical_uuid) WHERE chemical_uuid IS NOT NULL
            """,
)

# v4：显式解除关联后记为已解除（chemical_unlinked），省略引用的写入不再按名称自动匹配。
# 旧行一律为未解除（默认 0）：v3 存不下"解除过"，与升级前的行为一致。
_MATERIAL_SUBSTANCE_V4 = """
        CREATE TABLE IF NOT EXISTS material_substance (
            substance_uuid TEXT PRIMARY KEY CHECK (TRIM(substance_uuid) <> ''),
            material_uuid TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            name TEXT NOT NULL CHECK (TRIM(name) <> ''),
            chemical_uuid TEXT CHECK (chemical_uuid IS NULL OR TRIM(chemical_uuid) <> ''),
            chemical_unlinked INTEGER NOT NULL DEFAULT 0 CHECK (
                chemical_unlinked IN (0, 1) AND (chemical_unlinked = 0 OR chemical_uuid IS NULL)
            ),
            quantity REAL NOT NULL CHECK (quantity >= 0),
            quantity_unit TEXT NOT NULL CHECK (TRIM(quantity_unit) <> ''),
            physical_state TEXT NOT NULL DEFAULT 'liquid' CHECK (
                physical_state IN ('liquid','solid','gas','unknown')
            ),
            composition_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(composition_json)
                AND json_type(composition_json) = 'array'
            ),
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            content_version INTEGER NOT NULL CHECK (content_version > 0),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= observed_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            UNIQUE(material_uuid, ordinal),
            FOREIGN KEY(material_uuid) REFERENCES material_data(material_uuid)
                ON DELETE CASCADE
        )
        """
_MATERIAL_SUBSTANCE_V4_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_material_substance_name
            ON material_substance(LOWER(name), quantity_unit, material_uuid)
            """,
    """
            CREATE INDEX IF NOT EXISTS idx_material_substance_chemical
            ON material_substance(chemical_uuid) WHERE chemical_uuid IS NOT NULL
            """,
)

# v5：物理动作在材料库内冻结全体占用，并持久保存实际结算回执。
_MATERIAL_EXECUTION_V5 = (
    """CREATE TABLE IF NOT EXISTS material_execution (
        job_uuid TEXT PRIMARY KEY, task_uuid TEXT NOT NULL, claim_uuid TEXT NOT NULL,
        request_json TEXT NOT NULL CHECK(json_valid(request_json)), request_hash TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        state TEXT NOT NULL CHECK(state IN ('prepared','settled','released')),
        receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
        result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
        confirmation_json TEXT CHECK(confirmation_json IS NULL OR json_valid(confirmation_json)),
        version INTEGER NOT NULL CHECK(version>0), created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS material_execution_hold (
        aggregate_type TEXT NOT NULL CHECK(aggregate_type IN ('material','site')),
        aggregate_uuid TEXT NOT NULL, job_uuid TEXT NOT NULL REFERENCES material_execution(job_uuid),
        PRIMARY KEY(aggregate_type,aggregate_uuid)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_material_execution_task ON material_execution(task_uuid,state)",
)

# v6：V1.0 库存概念并入 materials.db（单一库存权威，不另建 inventory.db）：
# - material_content：V1 试剂 / 样品 / 当前内容物是 material_substance 行上的角色扩展，
#   数量仍以 material_substance 为唯一权威；
# - material_content_history：试剂 / 内容物的变更历史（reagent-history / substance-history）；
# - material_state_history：物料状态追加历史（/materials/{uuid}/states）；
# - material_content_reservation：容器内容物数量预留，防止并发任务超量抽取同一瓶；
# - material_source_binding：任务准入时冻结的物料来源绑定（task_exclusive / shared_source）；
# - station_ingress_reservation：工站入站库位预留；
# - inventory_processed_command：V1 类型化库存命令的幂等结果；
# - lab_meta / lab_zone / lab_placement：实验室档案、分区与在台摆放；
# - inventory_lot 增加库位（location_site_uuid）与仓储分区（warehouse_zone_id）；
# - material.barcode 非空且未删除时唯一（迁移前把重复条码让给最早创建的物料）。
_MATERIAL_CONTENT_V6 = """
        CREATE TABLE IF NOT EXISTS material_content (
            substance_uuid TEXT PRIMARY KEY CHECK (TRIM(substance_uuid) <> ''),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            content_kind TEXT NOT NULL CHECK (content_kind IN ('reagent','sample','current')),
            concentration_value REAL,
            concentration_unit TEXT,
            density_g_per_ml REAL CHECK (density_g_per_ml IS NULL OR density_g_per_ml > 0),
            density_source TEXT,
            code TEXT,
            sample_type TEXT,
            source TEXT,
            collected_at_ms INTEGER CHECK (collected_at_ms IS NULL OR collected_at_ms >= 0),
            expires_at_ms INTEGER CHECK (expires_at_ms IS NULL OR expires_at_ms >= 0),
            description TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            CHECK (
                (concentration_value IS NULL AND concentration_unit IS NULL)
                OR (concentration_value IS NOT NULL AND concentration_unit IS NOT NULL)
            ),
            CHECK (content_kind = 'sample' OR code IS NULL),
            CHECK (expires_at_ms IS NULL OR collected_at_ms IS NULL OR expires_at_ms >= collected_at_ms)
        )
        """
_MATERIAL_CONTENT_V6_INDEXES = (
    """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_content_kind
            ON material_content(material_uuid, content_kind)
            """,
    """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_content_sample_code
            ON material_content(LOWER(code)) WHERE code IS NOT NULL
            """,
)
_MATERIAL_CONTENT_HISTORY_V6 = """
        CREATE TABLE IF NOT EXISTS material_content_history (
            history_uuid TEXT PRIMARY KEY CHECK (TRIM(history_uuid) <> ''),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            subject_type TEXT NOT NULL CHECK (subject_type IN ('reagent','sample','current_substance')),
            subject_uuid TEXT NOT NULL CHECK (TRIM(subject_uuid) <> ''),
            event_type TEXT NOT NULL CHECK (TRIM(event_type) <> ''),
            operator_type TEXT NOT NULL DEFAULT 'system',
            quantity_delta REAL,
            quantity_unit TEXT,
            revision INTEGER,
            changes_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(changes_json) AND json_type(changes_json) = 'object'
            ),
            extension_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(extension_json) AND json_type(extension_json) = 'object'
            ),
            workflow_task_uuid TEXT,
            workflow_node_job_uuid TEXT,
            trace_id TEXT,
            causation_id TEXT,
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0)
        )
        """
_MATERIAL_CONTENT_HISTORY_V6_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_material_content_history_material
            ON material_content_history(material_uuid, subject_type, recorded_at_ms, history_uuid)
            """,
)
_MATERIAL_STATE_V6 = """
        CREATE TABLE IF NOT EXISTS material_state_history (
            state_uuid TEXT PRIMARY KEY CHECK (TRIM(state_uuid) <> ''),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            status TEXT,
            state_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(state_data_json) AND json_type(state_data_json) = 'object'
            ),
            source TEXT,
            description TEXT,
            meta_data_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_data_json) AND json_type(meta_data_json) = 'object'
            ),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0)
        )
        """
_MATERIAL_STATE_V6_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_material_state_history_timeline
            ON material_state_history(material_uuid, observed_at_ms DESC, state_uuid DESC)
            """,
)
_MATERIAL_CONTENT_RESERVATION_V6 = """
        CREATE TABLE IF NOT EXISTS material_content_reservation (
            reservation_uuid TEXT PRIMARY KEY CHECK (TRIM(reservation_uuid) <> ''),
            substance_uuid TEXT NOT NULL CHECK (TRIM(substance_uuid) <> ''),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            task_uuid TEXT NOT NULL CHECK (TRIM(task_uuid) <> ''),
            node_uuid TEXT NOT NULL DEFAULT '',
            job_uuid TEXT,
            quantity REAL NOT NULL CHECK (quantity > 0),
            quantity_unit TEXT NOT NULL CHECK (TRIM(quantity_unit) <> ''),
            status TEXT NOT NULL CHECK (status IN ('active','consumed','released')),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
        )
        """
_MATERIAL_CONTENT_RESERVATION_V6_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_material_content_reservation_active
            ON material_content_reservation(substance_uuid, status)
            """,
    """
            CREATE INDEX IF NOT EXISTS idx_material_content_reservation_task
            ON material_content_reservation(task_uuid, status)
            """,
)
_MATERIAL_SOURCE_BINDING_V6 = """
        CREATE TABLE IF NOT EXISTS material_source_binding (
            binding_uuid TEXT PRIMARY KEY CHECK (TRIM(binding_uuid) <> ''),
            task_uuid TEXT NOT NULL CHECK (TRIM(task_uuid) <> ''),
            node_uuid TEXT NOT NULL CHECK (TRIM(node_uuid) <> ''),
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            material_uuid TEXT NOT NULL CHECK (TRIM(material_uuid) <> ''),
            template_uuid TEXT NOT NULL CHECK (TRIM(template_uuid) <> ''),
            custody_policy TEXT NOT NULL CHECK (custody_policy IN ('task_exclusive','shared_source')),
            selector_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(selector_json) AND json_type(selector_json) = 'object'
            ),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','released')),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            released_at_ms INTEGER CHECK (released_at_ms IS NULL OR released_at_ms >= created_at_ms),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            UNIQUE(task_uuid, node_uuid, attempt)
        )
        """
_MATERIAL_SOURCE_BINDING_V6_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_material_source_binding_material
            ON material_source_binding(material_uuid, custody_policy, status)
            """,
)
_STATION_INGRESS_RESERVATION_V6 = """
        CREATE TABLE IF NOT EXISTS station_ingress_reservation (
            reservation_uuid TEXT PRIMARY KEY CHECK (TRIM(reservation_uuid) <> ''),
            idempotency_key TEXT NOT NULL UNIQUE CHECK (TRIM(idempotency_key) <> ''),
            request_hash TEXT NOT NULL CHECK (TRIM(request_hash) <> ''),
            backend_task_uuid TEXT,
            invocation_key TEXT,
            carrier_material_uuid TEXT NOT NULL CHECK (TRIM(carrier_material_uuid) <> ''),
            site_uuid TEXT,
            state TEXT NOT NULL CHECK (
                state IN ('reserved','in_transit','received','expired','canceled')
            ),
            expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms >= 0),
            transport_started_at_ms INTEGER,
            received_at_ms INTEGER,
            canceled_at_ms INTEGER,
            cancel_reason TEXT,
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
        )
        """
_STATION_INGRESS_RESERVATION_V6_INDEXES = (
    """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_station_ingress_active_site
            ON station_ingress_reservation(site_uuid)
            WHERE site_uuid IS NOT NULL AND state IN ('reserved','in_transit')
            """,
    """
            CREATE INDEX IF NOT EXISTS idx_station_ingress_state_expiry
            ON station_ingress_reservation(state, expires_at_ms, reservation_uuid)
            """,
)
# V1 ``POST /inventory/commands`` 的类型化命令（试剂分装、内容物预留、来源绑定 …）按 command_id
# 幂等：首次结果（完成或拒绝）原样留存，重放直接返回；也是 ``/inventory/commands/processed`` 的来源。
_INVENTORY_PROCESSED_COMMAND_V6 = """
        CREATE TABLE IF NOT EXISTS inventory_processed_command (
            command_id TEXT PRIMARY KEY CHECK (TRIM(command_id) <> ''),
            command_type TEXT NOT NULL CHECK (TRIM(command_type) <> ''),
            request_hash TEXT NOT NULL CHECK (TRIM(request_hash) <> ''),
            status TEXT NOT NULL CHECK (status IN ('completed','rejected')),
            result_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_json)),
            error_code TEXT,
            error_message TEXT,
            business_code INTEGER,
            actor TEXT NOT NULL DEFAULT '',
            processed_at_ms INTEGER NOT NULL CHECK (processed_at_ms >= 0)
        )
        """
_INVENTORY_PROCESSED_COMMAND_V6_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_inventory_processed_command_time
            ON inventory_processed_command(processed_at_ms DESC, command_id)
            """,
)
_LAB_META_V6 = """
        CREATE TABLE IF NOT EXISTS lab_meta (
            meta_key TEXT PRIMARY KEY CHECK (TRIM(meta_key) <> ''),
            meta_value_json TEXT NOT NULL DEFAULT 'null' CHECK (json_valid(meta_value_json)),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
_LAB_ZONE_V6 = """
        CREATE TABLE IF NOT EXISTS lab_zone (
            zone_id TEXT PRIMARY KEY CHECK (TRIM(zone_id) <> ''),
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'bench',
            x REAL NOT NULL DEFAULT 0,
            y REAL NOT NULL DEFAULT 0,
            w REAL NOT NULL DEFAULT 100,
            h REAL NOT NULL DEFAULT 100,
            meta_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_json) AND json_type(meta_json) = 'object'
            ),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
_LAB_PLACEMENT_V6 = """
        CREATE TABLE IF NOT EXISTS lab_placement (
            subject_id TEXT PRIMARY KEY CHECK (TRIM(subject_id) <> ''),
            subject_kind TEXT NOT NULL DEFAULT 'container',
            zone_id TEXT NOT NULL DEFAULT '',
            x REAL NOT NULL DEFAULT 0,
            y REAL NOT NULL DEFAULT 0,
            w REAL NOT NULL DEFAULT 40,
            h REAL NOT NULL DEFAULT 40,
            rotation REAL NOT NULL DEFAULT 0,
            label TEXT NOT NULL DEFAULT '',
            meta_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(meta_json) AND json_type(meta_json) = 'object'
            ),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
        )
        """
_LAB_PLACEMENT_V6_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_lab_placement_zone ON lab_placement(zone_id)",
)
_MATERIAL_BARCODE_V6_INDEX = """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_material_barcode_active
            ON material(barcode) WHERE deleted_at_ms IS NULL AND barcode <> ''
            """
# 重复条码：保留最早创建（同时刻按 uuid）的物料，其余清空条码，原值记到 extra_json.duplicate_barcode，
# 便于人工复核后重新贴码。
_MATERIAL_BARCODE_V6_DEDUP = """
        UPDATE material
        SET extra_json = json_set(extra_json, '$.duplicate_barcode', barcode), barcode = ''
        WHERE deleted_at_ms IS NULL AND barcode <> '' AND EXISTS (
            SELECT 1 FROM material AS earlier
            WHERE earlier.deleted_at_ms IS NULL
              AND earlier.barcode = material.barcode
              AND (earlier.created_at_ms < material.created_at_ms
                   OR (earlier.created_at_ms = material.created_at_ms
                       AND earlier.material_uuid < material.material_uuid))
        )
        """
_INVENTORY_LOT_V6 = """
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
            location_site_uuid TEXT,
            warehouse_zone_id TEXT NOT NULL DEFAULT '',
            CHECK (quantity_available + quantity_reserved <= quantity_total),
            FOREIGN KEY(template_uuid) REFERENCES resource_template(template_uuid)
                ON DELETE RESTRICT
        )
        """
_INVENTORY_LOT_V6_INDEXES = (
    """
            CREATE INDEX IF NOT EXISTS idx_inventory_lot_available
            ON inventory_lot(template_uuid, quarantined, expiry_at_ms)
            WHERE quantity_available > 0
            """,
)

# v8：作业 / 试剂消费读模型（V1 inventory-consumptions）按作业与内容物身份查账本、内容物预留与历史。
_INVENTORY_LEDGER_JOB_V8_INDEX = """
            CREATE INDEX IF NOT EXISTS idx_inventory_ledger_job
            ON inventory_ledger(job_uuid, sequence) WHERE job_uuid IS NOT NULL
            """
_MATERIAL_CONTENT_RESERVATION_JOB_V8_INDEX = """
            CREATE INDEX IF NOT EXISTS idx_material_content_reservation_job
            ON material_content_reservation(job_uuid, status) WHERE job_uuid IS NOT NULL
            """
_MATERIAL_CONTENT_HISTORY_SUBJECT_V8_INDEX = """
            CREATE INDEX IF NOT EXISTS idx_material_content_history_subject
            ON material_content_history(subject_uuid, recorded_at_ms, history_uuid)
            """

MATERIALS_MIGRATIONS: tuple[Migration, ...] = (
    sql_migration(2, "新增化学品定义表 chemical", _CHEMICAL_V2, *_CHEMICAL_V2_INDEXES),
    rebuild_table_migration(
        3,
        "内容物引用化学品定义：material_substance 新增可空 chemical_uuid",
        table="material_substance",
        create_sql=_MATERIAL_SUBSTANCE_V3,
        indexes=_MATERIAL_SUBSTANCE_V3_INDEXES,
    ),
    rebuild_table_migration(
        4,
        "显式解除化学品关联后不再自动匹配：material_substance 新增 chemical_unlinked",
        table="material_substance",
        create_sql=_MATERIAL_SUBSTANCE_V4,
        indexes=_MATERIAL_SUBSTANCE_V4_INDEXES,
    ),
    sql_migration(5, "物理动作占用及实际回执结算", *_MATERIAL_EXECUTION_V5),
    sql_migration(
        6,
        "V1.0 库存概念并入 materials.db：试剂 / 样品 / 当前内容物、状态历史、内容物预留、来源绑定、"
        "工站入站预留、实验室布局、批次库位与条码唯一",
        _MATERIAL_CONTENT_V6, *_MATERIAL_CONTENT_V6_INDEXES,
        _MATERIAL_CONTENT_HISTORY_V6, *_MATERIAL_CONTENT_HISTORY_V6_INDEXES,
        _MATERIAL_STATE_V6, *_MATERIAL_STATE_V6_INDEXES,
        _MATERIAL_CONTENT_RESERVATION_V6, *_MATERIAL_CONTENT_RESERVATION_V6_INDEXES,
        _MATERIAL_SOURCE_BINDING_V6, *_MATERIAL_SOURCE_BINDING_V6_INDEXES,
        _STATION_INGRESS_RESERVATION_V6, *_STATION_INGRESS_RESERVATION_V6_INDEXES,
        _INVENTORY_PROCESSED_COMMAND_V6, *_INVENTORY_PROCESSED_COMMAND_V6_INDEXES,
        _LAB_META_V6, _LAB_ZONE_V6, _LAB_PLACEMENT_V6, *_LAB_PLACEMENT_V6_INDEXES,
        _MATERIAL_BARCODE_V6_DEDUP, _MATERIAL_BARCODE_V6_INDEX,
    ),
    rebuild_table_migration(
        7,
        "批次库位与仓储分区：inventory_lot 新增 location_site_uuid / warehouse_zone_id",
        table="inventory_lot",
        create_sql=_INVENTORY_LOT_V6,
        indexes=_INVENTORY_LOT_V6_INDEXES,
    ),
    sql_migration(
        8,
        "作业 / 试剂消费读模型索引：账本按作业、内容物预留按作业、内容物历史按内容物",
        _INVENTORY_LEDGER_JOB_V8_INDEX,
        _MATERIAL_CONTENT_RESERVATION_JOB_V8_INDEX,
        _MATERIAL_CONTENT_HISTORY_SUBJECT_V8_INDEX,
    ),
)
MATERIALS_SCHEMA_VERSION = MATERIALS_MIGRATIONS[-1].version
