"""materials.db 的迁移链：每一步把库从 ``version - 1`` 升到 ``version``。

迁移里的建表 / 索引语句是该版本冻结的字面量。
"""

from __future__ import annotations

from unilabos.server.database.schema import Migration, sql_migration

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

MATERIALS_MIGRATIONS: tuple[Migration, ...] = (
    sql_migration(2, "新增化学品定义表 chemical", _CHEMICAL_V2, *_CHEMICAL_V2_INDEXES),
)
MATERIALS_SCHEMA_VERSION = MATERIALS_MIGRATIONS[-1].version
