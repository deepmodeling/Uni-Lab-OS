"""Backend-shaped inventory schema migration and legacy adapter coverage."""

from __future__ import annotations

import sqlite3

import pytest

from unilabos.app.scheduler.inventory import store as store_module
from unilabos.app.scheduler.inventory.store import InventoryStore


def _create_v4_database(path: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(store_module._SCHEMA)
    connection.executescript(store_module._SCHEMA_V2)
    connection.execute(store_module._SCHEMA_V3_ADD_PARENT)
    connection.execute(store_module._SCHEMA_V3_INDEX)
    for table, columns in store_module._SCHEMA_V4_COLUMNS.items():
        existing = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for column, definition in columns.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
    connection.execute(
        "INSERT INTO resource_template VALUES (?,?,?,?,?)",
        ("template-a", "Template A", "device", "{}", 2),
    )
    connection.execute(
        """
        INSERT INTO material_instance(
            edge_uuid,legacy_cloud_id,lot_id,template_id,barcode,status,
            version,parent_uuid
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        ("owner", "", "", "template-a", "OWNER", "warehouse", 2, ""),
    )
    connection.execute(
        """
        INSERT INTO material_instance(
            edge_uuid,legacy_cloud_id,lot_id,template_id,barcode,status,
            version,parent_uuid
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            "occupant",
            "legacy-cloud-id",
            "",
            "template-a",
            "OCCUPANT",
            "reserved",
            4,
            "owner",
        ),
    )
    connection.execute(
        "INSERT INTO resource_relation VALUES (?,?,?,?)",
        ("owner", "A1", "occupant", 4),
    )
    connection.execute(
        "INSERT INTO substance_content VALUES (?,?,?)",
        ("occupant", '{"temperature":25}', 1),
    )
    connection.execute("PRAGMA user_version=4")
    connection.commit()
    connection.close()


def test_v4_migrates_to_backend_tables_without_losing_edge_inventory(tmp_path):
    database = tmp_path / "inventory.db"
    _create_v4_database(str(database))

    store = InventoryStore(str(database))
    assert store.query_one("PRAGMA user_version")["user_version"] == (
        store_module.SCHEMA_VERSION
    )

    table_names = {
        row["name"]
        for row in store.query_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "resource_template",
        "resource_handle_template",
        "material",
        "site",
        "material_state_history",
        "inventory_material_source_binding",
    } <= table_names
    view_names = {
        row["name"]
        for row in store.query_all(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )
    }
    assert {
        "inventory_resource_template",
        "material_instance",
        "resource_relation",
        "substance_content",
    } <= view_names
    material_columns = {
        row["name"] for row in store.query_all("PRAGMA table_info(material)")
    }
    assert {
        "uuid",
        "create_time",
        "update_time",
        "deleted_at",
        "description",
        "meta_data",
        "resource_template_uuid",
        "parent_uuid",
        "barcode",
    } <= material_columns
    # 设备模板已由启动内存目录承担；物料模板引用不再错误地强制只指向
    # SQLite resource_template，父物料自关联仍由数据库保护。
    material_foreign_keys = {
        row["table"]
        for row in store.query_all("PRAGMA foreign_key_list(material)")
    }
    assert "resource_template" not in material_foreign_keys
    assert "material" in material_foreign_keys

    occupant = store.query_one("SELECT * FROM material WHERE uuid='occupant'")
    assert occupant["parent_uuid"] == "owner"
    assert occupant["data"] == '{"temperature":25}'
    assert occupant["inventory_status"] == "reserved"
    assert store.get_instance("occupant")["status"] == "reserved"
    assert store.query_one("PRAGMA integrity_check")["integrity_check"] == "ok"
    assert store.query_all("PRAGMA foreign_key_check") == []

    site = store.query_one("SELECT * FROM site WHERE name='A1'")
    assert site["uuid"] not in {"owner", "occupant"}
    assert site["material_uuid"] == "owner"
    assert site["occupied_material_uuid"] == "occupant"
    store.close()


def test_v11_hybrid_template_migration_rolls_back_before_foreign_key_violation(
    tmp_path,
) -> None:
    """v11 重建发现旧父物料引用损坏时不得提交半迁移结构。

    参数：``tmp_path`` 隔离一份模拟 v10 的库存库。返回：无；断言外键检查在
    提交前执行，失败后仍保留 v10 的模板外键、版本号和原表名。
    """

    database = tmp_path / "inventory.db"
    InventoryStore(str(database)).close()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript(
        """
        DROP TABLE material;
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
            FOREIGN KEY (resource_template_uuid)
                REFERENCES resource_template (uuid) ON DELETE RESTRICT,
            FOREIGN KEY (parent_uuid) REFERENCES material (uuid) ON DELETE RESTRICT
        );
        INSERT INTO resource_template(
            uuid,create_time,update_time,meta_data,name,display_name,resource_type,
            model,tags,data_schema,config_schema,pose,config_info,available_sites,
            scene,device_params,ui_overlay
        ) VALUES (
            'template-v10','now','now','{}','template-v10','Template v10','material',
            '{}','[]','{}','{}','{}','[]','[]','[]','{}','{}'
        );
        INSERT INTO material(
            uuid,create_time,update_time,meta_data,resource_template_uuid,
            parent_uuid,class,type,barcode,name,config,data
        ) VALUES (
            'orphan','now','now','{}','template-v10','missing-parent',
            'material','material','ORPHAN','Orphan','{}','{}'
        );
        PRAGMA user_version = 10;
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(sqlite3.IntegrityError, match="invalid foreign keys"):
        InventoryStore(str(database))

    reopened = sqlite3.connect(database)
    try:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == 10
        assert {
            row[2] for row in reopened.execute("PRAGMA foreign_key_list(material)")
        } == {"material", "resource_template"}
        assert reopened.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='material_v10'"
        ).fetchone()[0] == 0
    finally:
        reopened.close()


def test_fresh_v5_legacy_views_write_the_canonical_material_once(tmp_path):
    store = InventoryStore(str(tmp_path / "inventory.db"))
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO inventory_resource_template VALUES (?,?,?,?,?)",
            ("template-a", "Template A", "device", "{}", 1),
        )
        connection.execute(
            """
            INSERT INTO material_instance(
                edge_uuid,legacy_cloud_id,lot_id,template_id,barcode,status,
                version,parent_uuid
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            ("material-a", "", "", "template-a", "A", "warehouse", 1, ""),
        )

    assert store.query_one("SELECT COUNT(*) AS n FROM material")["n"] == 1
    assert store.query_one(
        "SELECT aggregate_version FROM material WHERE uuid='material-a'"
    )["aggregate_version"] == 1
    store.close()
