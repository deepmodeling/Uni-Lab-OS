"""已发布的 materials.db 结构夹具：由当前声明还原 v5（去掉 v6 / v7 新增的表、列与索引）。

旧版本迁移测试都从这里取 v5 基线，再按各自版本继续回退。
"""

from __future__ import annotations

from dataclasses import replace

from unilabos.server.database import DATABASE_SPECS
from unilabos.server.database.schema import TableSpec
from unilabos.server.database.tables.materials_migrations import _MATERIAL_BARCODE_V6_INDEX

#: v5 发布时的结构指纹（materials schema v5，#360 + p5）。
MATERIALS_V5_CHECKSUM = "4dc3673d7e70579826e6b5d532c4383525e41d8eae6dd68ab22a18a849e43df5"
#: v6 新增的表。
MATERIALS_V6_TABLES = frozenset({
    "material_content",
    "material_content_history",
    "material_state_history",
    "material_content_reservation",
    "material_source_binding",
    "station_ingress_reservation",
    "inventory_processed_command",
    "lab_meta",
    "lab_zone",
    "lab_placement",
})
_V7_LOT_COLUMNS = (
    "            location_site_uuid TEXT,\n",
    "            warehouse_zone_id TEXT NOT NULL DEFAULT '',\n",
)


def _v5_table(table: TableSpec) -> TableSpec:
    if table.name == "inventory_lot":
        create_sql = table.create_sql
        for line in _V7_LOT_COLUMNS:
            assert line in create_sql
            create_sql = create_sql.replace(line, "")
        return replace(table, create_sql=create_sql)
    if table.name == "material":
        return replace(table, indexes=tuple(item for item in table.indexes if item != _MATERIAL_BARCODE_V6_INDEX))
    return table


def materials_spec_v5():
    """按当前声明还原已发布的 materials v5。"""

    spec = DATABASE_SPECS["materials"]
    return replace(
        spec,
        tables=tuple(_v5_table(table) for table in spec.tables if table.name not in MATERIALS_V6_TABLES),
        schema_version=5,
        migrations=spec.migrations[:4],
    )


__all__ = ["MATERIALS_V5_CHECKSUM", "MATERIALS_V6_TABLES", "materials_spec_v5"]
