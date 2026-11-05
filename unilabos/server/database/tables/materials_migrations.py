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
)
MATERIALS_SCHEMA_VERSION = MATERIALS_MIGRATIONS[-1].version
