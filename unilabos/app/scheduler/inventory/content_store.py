"""容器内容的共享结构，以及已有合表库的原子约束修复。"""

from __future__ import annotations

import sqlite3

CONTAINER_CONTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS container_content (
    uuid TEXT PRIMARY KEY NOT NULL,
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    deleted_at DATETIME,
    description TEXT,
    meta_data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_data)),
    material_uuid TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('sample', 'substance', 'reagent')),
    code TEXT,
    name TEXT,
    sample_type TEXT,
    source TEXT,
    quantity REAL NOT NULL CHECK (quantity >= 0),
    quantity_unit TEXT NOT NULL CHECK (quantity_unit <> ''),
    collected_at DATETIME,
    expires_at DATETIME,
    composition TEXT NOT NULL DEFAULT '[]',
    physical_state TEXT,
    revision INTEGER,
    observed_at DATETIME,
    reagent_info_uuid TEXT,
    concentration_value REAL,
    concentration_unit TEXT,
    density_g_per_ml REAL,
    density_source TEXT,
    CONSTRAINT ck_container_content_sample CHECK (
        kind <> 'sample' OR (
            code IS NOT NULL AND name IS NOT NULL
            AND (expires_at IS NULL OR collected_at IS NULL OR expires_at >= collected_at)
        )
    ),
    CONSTRAINT ck_container_content_substance CHECK (
        kind <> 'substance' OR (
            json_valid(composition) AND json_type(composition) = 'array'
            AND physical_state IS NOT NULL AND physical_state <> ''
            AND revision IS NOT NULL AND revision > 0
            AND observed_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_container_content_reagent CHECK (
        kind <> 'reagent' OR (
            reagent_info_uuid IS NOT NULL
            AND revision IS NOT NULL AND revision > 0
            AND (density_g_per_ml IS NULL OR density_g_per_ml > 0)
            AND (
                (concentration_value IS NULL AND concentration_unit IS NULL)
                OR (concentration_value IS NOT NULL AND concentration_unit IS NOT NULL)
            )
        )
    ),
    FOREIGN KEY (material_uuid) REFERENCES material (uuid) ON DELETE RESTRICT,
    FOREIGN KEY (reagent_info_uuid) REFERENCES reagent_info (uuid) ON DELETE RESTRICT
)
"""

_CONTENT_CHECKS = (
    "ck_container_content_sample",
    "ck_container_content_substance",
    "ck_container_content_reagent",
)


def migrate_container_content_schema(connection: sqlite3.Connection) -> None:
    """在库存迁移事务中修复旧合表，非法数据导致整轮迁移回滚。

    外层在事务前设置 foreign_keys / legacy_alter_table；重建后保留原索引及
    表触发器，三类兼容视图由后续表压缩步骤统一重建。
    """

    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='container_content'"
    ).fetchone()
    if table is None or all(f"CONSTRAINT {name}" in table[0] for name in _CONTENT_CHECKS):
        return
    objects = connection.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='container_content' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL"
    ).fetchall()
    columns = ", ".join(
        '"' + row[1].replace('"', '""') + '"'
        for row in connection.execute("PRAGMA table_info(container_content)")
    )
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='view' "
        "AND name IN ('sample','current_substance','reagent')"
    ).fetchall():
        connection.execute(f'DROP VIEW "{row[0]}"')
    connection.execute("ALTER TABLE container_content RENAME TO container_content_before_constraints")
    connection.execute(CONTAINER_CONTENT_SCHEMA)
    # 保留全部旧列；若有无法承接的额外列，同样拒绝迁移，不能静默丢弃。
    connection.execute(
        f"INSERT INTO container_content ({columns}) "
        f"SELECT {columns} FROM container_content_before_constraints"
    )
    connection.execute("DROP TABLE container_content_before_constraints")
    for row in objects:
        connection.execute(row[0])


__all__ = ["CONTAINER_CONTENT_SCHEMA", "migrate_container_content_schema"]
