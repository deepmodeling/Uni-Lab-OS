"""Backend-shaped inventory schema migration and legacy adapter coverage."""

from __future__ import annotations

import sqlite3

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
    assert store.query_one("PRAGMA user_version")["user_version"] == 5

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
        "relative_position",
        "site",
        "material_state_history",
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

    occupant = store.query_one("SELECT * FROM material WHERE uuid='occupant'")
    assert occupant["parent_uuid"] == "owner"
    assert occupant["data"] == '{"temperature":25}'
    assert store.query_one(
        "SELECT inventory_status FROM material_inventory "
        "WHERE material_uuid='occupant'"
    )["inventory_status"] == "reserved"
    assert store.get_instance("occupant")["status"] == "reserved"
    assert store.query_one("PRAGMA integrity_check")["integrity_check"] == "ok"
    assert store.query_all("PRAGMA foreign_key_check") == []

    site = store.query_one("SELECT * FROM site WHERE name='A1'")
    assert site["uuid"] not in {"owner", "occupant"}
    assert site["material_uuid"] == "owner"
    assert site["occupied_material_uuid"] == "occupant"
    store.close()


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
        "SELECT COUNT(*) AS n FROM material_inventory"
    )["n"] == 1
    store.close()
