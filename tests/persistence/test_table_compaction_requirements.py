"""表合并的需求验收。

覆盖旧库升级时必须保住的事实：版本、有效位姿、软删除内容、指纹、
以及失败时不能把源表删掉一半。新库上的冒烟测试不替代这些用例。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from unilabos.app.edge_control.local_authority import LocalEdgeAuthorityStore
from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.inventory.store import SCHEMA_VERSION, InventoryStore
from unilabos.workflow.station_event_outbox import (
    StationEventOutboxStore,
    append_station_event,
)
from unilabos.workflow.store import StoreNotFound, WorkflowStore

_DROPPED_INVENTORY_TABLES = (
    "material_inventory",
    "relative_position",
    "material_content_version",
    "resource_template_inventory",
    "station_ingress_reservation_site",
    "lab_zone",
    "lab_placement",
    "sample",
    "current_substance",
    "reagent",
)
def _names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type=?",
            (kind,),
        )
    }


def _seed_legacy_inventory(
    path: Path,
    *,
    duplicate_active_content: bool = False,
    colliding_content_uuid: bool = False,
) -> None:
    """造一份 user_version=17 的旧库存库，子表仍是物理表。"""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE resource_template (
            uuid TEXT PRIMARY KEY NOT NULL,
            create_time DATETIME NOT NULL,
            update_time DATETIME NOT NULL,
            deleted_at DATETIME,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '{}',
            tags TEXT NOT NULL DEFAULT '[]',
            data_schema TEXT NOT NULL DEFAULT '{}',
            config_schema TEXT NOT NULL DEFAULT '{}',
            pose TEXT NOT NULL DEFAULT '{}',
            config_info TEXT NOT NULL DEFAULT '[]',
            scene TEXT NOT NULL DEFAULT '[]',
            device_params TEXT NOT NULL DEFAULT '{}',
            ui_overlay TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE resource_template_inventory (
            resource_template_uuid TEXT PRIMARY KEY,
            aggregate_version INTEGER NOT NULL
        );
        CREATE TABLE material (
            uuid TEXT PRIMARY KEY NOT NULL,
            create_time DATETIME NOT NULL,
            update_time DATETIME NOT NULL,
            deleted_at DATETIME,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            resource_template_uuid TEXT NOT NULL,
            parent_uuid TEXT,
            class TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT '',
            barcode TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            config TEXT NOT NULL DEFAULT '{}',
            data TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE material_inventory (
            material_uuid TEXT PRIMARY KEY,
            legacy_cloud_id TEXT NOT NULL,
            legacy_template_id TEXT NOT NULL,
            lot_id TEXT NOT NULL,
            inventory_status TEXT NOT NULL,
            disposition TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL
        );
        CREATE TABLE material_content_version (
            material_uuid TEXT PRIMARY KEY,
            version INTEGER NOT NULL
        );
        CREATE TABLE relative_position (
            uuid TEXT PRIMARY KEY,
            material_uuid TEXT NOT NULL,
            description TEXT,
            meta_data TEXT NOT NULL,
            position_x REAL,
            position_y REAL,
            position_z REAL,
            depth REAL,
            length REAL,
            width REAL,
            scale_x REAL,
            scale_y REAL,
            scale_z REAL,
            rotation_x REAL,
            rotation_y REAL,
            rotation_z REAL,
            deleted_at TEXT
        );
        CREATE TABLE reagent_info (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            cas TEXT,
            name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '[]',
            inchi_key TEXT,
            physical_state TEXT NOT NULL
        );
        CREATE TABLE sample (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            material_uuid TEXT NOT NULL,
            code TEXT,
            name TEXT,
            sample_type TEXT,
            source TEXT,
            quantity REAL,
            quantity_unit TEXT,
            collected_at TEXT,
            expires_at TEXT
        );
        CREATE TABLE current_substance (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            material_uuid TEXT NOT NULL,
            name TEXT,
            composition TEXT NOT NULL DEFAULT '[]',
            quantity REAL,
            quantity_unit TEXT,
            physical_state TEXT,
            revision INTEGER,
            observed_at TEXT
        );
        CREATE TABLE reagent (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            description TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            material_uuid TEXT NOT NULL,
            reagent_info_uuid TEXT NOT NULL,
            concentration_value REAL,
            concentration_unit TEXT,
            quantity REAL,
            quantity_unit TEXT,
            revision INTEGER,
            physical_state TEXT,
            density_g_per_ml REAL,
            density_source TEXT
        );
        CREATE TABLE site (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            deleted_at TEXT,
            meta_data TEXT NOT NULL DEFAULT '{}',
            material_uuid TEXT NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            allowed_resource_template_uuids TEXT NOT NULL DEFAULT '[]',
            occupied_material_uuid TEXT,
            position_x REAL NOT NULL DEFAULT 0,
            position_y REAL NOT NULL DEFAULT 0,
            position_z REAL NOT NULL DEFAULT 0,
            depth REAL NOT NULL DEFAULT 0,
            length REAL NOT NULL DEFAULT 0,
            width REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE station_ingress_reservation (
            uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            backend_task_uuid TEXT,
            invocation_key TEXT,
            carrier_material_uuid TEXT NOT NULL,
            state TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            transport_started_at TEXT,
            received_at TEXT,
            canceled_at TEXT,
            cancel_reason TEXT,
            revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE station_ingress_reservation_site (
            reservation_uuid TEXT PRIMARY KEY,
            site_uuid TEXT NOT NULL,
            active INTEGER NOT NULL
        );
        CREATE TABLE lab_meta (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL
        );
        CREATE TABLE lab_zone (uuid TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE lab_placement (uuid TEXT PRIMARY KEY, zone_uuid TEXT NOT NULL);
        CREATE VIEW material_instance AS
        SELECT material.uuid AS edge_uuid,
               inventory.aggregate_version AS version
        FROM material
        JOIN material_inventory AS inventory
          ON inventory.material_uuid = material.uuid;
        CREATE TRIGGER material_instance_insert
        INSTEAD OF INSERT ON material_instance
        BEGIN
            INSERT INTO material_inventory(
                material_uuid, legacy_cloud_id, legacy_template_id, lot_id,
                inventory_status, disposition, aggregate_version
            ) VALUES (NEW.edge_uuid, '', '', '', 'warehouse', 'active', 1);
        END;
        """
    )
    connection.executemany(
        """
        INSERT INTO resource_template(
            uuid, create_time, update_time, name, display_name, resource_type
        ) VALUES (?, '2026-01-01', '2026-01-01', ?, ?, ?)
        """,
        (
            ("tpl-versioned", "tpl-versioned", "带版本", "bottle"),
            ("tpl-bare", "tpl-bare", "无版本", "deck"),
        ),
    )
    connection.execute(
        "INSERT INTO resource_template_inventory VALUES ('tpl-versioned', 7)"
    )
    connection.executemany(
        """
        INSERT INTO material(
            uuid, create_time, update_time, resource_template_uuid,
            class, type, name
        ) VALUES (?, '2026-01-01', '2026-01-01', ?, ?, ?, ?)
        """,
        (
            ("mat-full", "tpl-versioned", "container", "bottle", "满"),
            ("mat-bare", "tpl-bare", "container", "bottle", "空"),
            ("mat-retired-pose", "tpl-versioned", "container", "bottle", "旧位姿"),
            ("mat-deck", "tpl-bare", "deck", "deck", "台面"),
            ("mat-carrier", "tpl-versioned", "carrier", "carrier", "载具"),
            ("mat-origin", "tpl-versioned", "container", "bottle", "原点"),
        ),
    )
    connection.execute(
        """
        INSERT INTO material_inventory VALUES (
            'mat-full', 'cloud-1', 'legacy-tpl', 'lot-1', 'in_use', 'hold', 4
        )
        """
    )
    connection.execute(
        "INSERT INTO material_content_version VALUES ('mat-full', 3)"
    )
    connection.execute(
        """
        INSERT INTO relative_position(
            uuid, material_uuid, description, meta_data,
            position_x, position_y, position_z, depth, length, width,
            scale_x, scale_y, scale_z, rotation_x, rotation_y, rotation_z,
            deleted_at
        ) VALUES (
            'pose-live', 'mat-full', '台面', '{}',
            1.5, 2.5, 3.5, 1, 2, 3,
            1, 1, 1, 0, 0, 90,
            NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO relative_position(
            uuid, material_uuid, description, meta_data,
            position_x, position_y, position_z, depth, length, width,
            scale_x, scale_y, scale_z, rotation_x, rotation_y, rotation_z,
            deleted_at
        ) VALUES (
            'pose-dead', 'mat-retired-pose', '已卸', '{}',
            9, 9, 9, 1, 1, 1,
            1, 1, 1, 0, 0, 0,
            '2026-02-01'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO relative_position(
            uuid, material_uuid, description, meta_data,
            position_x, position_y, position_z, depth, length, width,
            scale_x, scale_y, scale_z, rotation_x, rotation_y, rotation_z,
            deleted_at
        ) VALUES (
            'pose-origin', 'mat-origin', '原点', '{}',
            0, 0, 0, 0, 0, 0,
            1, 1, 1, 0, 0, 0,
            NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO reagent_info(
            uuid, create_time, update_time, name, physical_state
        ) VALUES ('info-1', '2026-01-01', '2026-01-01', '水', 'liquid')
        """
    )
    sample_uuid = "content-same" if colliding_content_uuid else "sample-dead"
    connection.execute(
        """
        INSERT INTO sample(
            uuid, create_time, update_time, deleted_at, meta_data, material_uuid,
            code, name, sample_type, source, quantity, quantity_unit
        ) VALUES (?, '2026-01-01', '2026-01-01', '2026-02-01', '{}', 'mat-full',
                  'S-1', '旧样', 'unknown', 'operator', 1, 'mL')
        """,
        (sample_uuid,),
    )
    substance_material = "mat-full" if duplicate_active_content else "mat-bare"
    substance_deleted = None if duplicate_active_content else None
    substance_uuid = "content-same" if colliding_content_uuid else "substance-1"
    if colliding_content_uuid:
        substance_material = "mat-bare"
    connection.execute(
        """
        INSERT INTO current_substance(
            uuid, create_time, update_time, deleted_at, meta_data, material_uuid,
            name, composition, quantity, quantity_unit, physical_state, revision, observed_at
        ) VALUES (?, '2026-01-01', '2026-01-02', ?, '{}', ?,
                  '溶液', '[]', 5, 'mL', 'liquid', 2, '2026-01-02')
        """,
        (substance_uuid, substance_deleted, substance_material),
    )
    connection.execute(
        """
        INSERT INTO reagent(
            uuid, create_time, update_time, deleted_at, meta_data, material_uuid,
            reagent_info_uuid, quantity, quantity_unit, revision, physical_state
        ) VALUES (
            'reagent-1', '2026-01-01', '2026-01-03', NULL, '{}', 'mat-full',
            'info-1', 10, 'mL', 6, 'liquid'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO site(
            uuid, create_time, update_time, meta_data, material_uuid, name
        ) VALUES ('site-1', '2026-01-01', '2026-01-01', '{}', 'mat-deck', 'A1')
        """
    )
    connection.execute(
        """
        INSERT INTO site(
            uuid, create_time, update_time, meta_data, material_uuid, name
        ) VALUES ('site-2', '2026-01-01', '2026-01-01', '{}', 'mat-deck', 'A2')
        """
    )
    connection.execute(
        """
        INSERT INTO station_ingress_reservation(
            uuid, create_time, update_time, idempotency_key, request_hash,
            carrier_material_uuid, state, expires_at
        ) VALUES (
            'rsv-1', '2026-01-01', '2026-01-01', 'idem-1', 'hash-1',
            'mat-carrier', 'reserved', '2026-12-31'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO station_ingress_reservation_site
        VALUES ('rsv-1', 'site-1', 1)
        """
    )
    connection.execute(
        """
        INSERT INTO station_ingress_reservation(
            uuid, create_time, update_time, idempotency_key, request_hash,
            carrier_material_uuid, state, expires_at
        ) VALUES (
            'rsv-done', '2026-01-01', '2026-01-02', 'idem-done', 'hash-done',
            'mat-carrier', 'received', '2026-12-31'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO station_ingress_reservation_site
        VALUES ('rsv-done', 'site-2', 0)
        """
    )
    connection.executemany(
        "INSERT INTO lab_meta(meta_key, meta_value) VALUES (?, ?)",
        (
            ("lab_name", "验收实验室"),
            ("lab_domain", "chem"),
            ("resource_graph_bootstrap_source", "graph-a"),
            ("resource_graph_bootstrap_fingerprint", "fp-1"),
            ("resource_graph_bootstrap_fingerprint_version", "2"),
            ("unrelated_note", "不得升成列"),
        ),
    )
    connection.execute("INSERT INTO lab_zone VALUES ('zone-1', '冷藏')")
    connection.execute("INSERT INTO lab_placement VALUES ('place-1', 'zone-1')")
    connection.execute("PRAGMA user_version = 17")
    connection.commit()
    connection.close()


def test_legacy_inventory_keeps_versions_pose_content_and_fingerprint(tmp_path: Path) -> None:
    """旧子表的有效事实进父表；软删除位姿不进；缺子行版本保持 0。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path)
    store = InventoryStore(str(path))
    try:
        connection = store._conn
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = _names(connection, "table")
        views = _names(connection, "view")
        for name in _DROPPED_INVENTORY_TABLES:
            assert name not in tables, name
        for name in (
            "material_instance",
            "resource_relation",
            "substance_content",
            "inventory_resource_template",
            "reagent",
            "sample",
            "current_substance",
        ):
            assert name in views, name
        assert "container_content" in tables
        assert "lab_meta" in tables

        full = connection.execute(
            """
            SELECT legacy_cloud_id, legacy_template_id, lot_id, inventory_status,
                   disposition, aggregate_version, content_version, pose_uuid,
                   position_x, rotation_z
            FROM material WHERE uuid='mat-full'
            """
        ).fetchone()
        assert tuple(full) == (
            "cloud-1",
            "legacy-tpl",
            "lot-1",
            "in_use",
            "hold",
            4,
            3,
            "pose-live",
            1.5,
            90,
        )
        bare = connection.execute(
            """
            SELECT aggregate_version, content_version, position_x
            FROM material WHERE uuid='mat-bare'
            """
        ).fetchone()
        assert tuple(bare) == (0, 0, None)
        retired = connection.execute(
            "SELECT pose_uuid, position_x FROM material WHERE uuid='mat-retired-pose'"
        ).fetchone()
        assert tuple(retired) == (None, None)
        origin = connection.execute(
            """
            SELECT pose_uuid, position_x, position_y, position_z
            FROM material WHERE uuid='mat-origin'
            """
        ).fetchone()
        assert tuple(origin) == ("pose-origin", 0, 0, 0)
        released = connection.execute(
            "SELECT site_uuid, site_active FROM station_ingress_reservation WHERE uuid='rsv-done'"
        ).fetchone()
        assert tuple(released) == ("site-2", 0)
        from unilabos.app.scheduler.inventory.station_resource import (
            SqliteStationResourceInventory,
        )
        from unilabos.app.scheduler.inventory.workflow_quantity import (
            WorkflowQuantityInventoryAuthority,
        )

        scheduler = SqliteStationResourceInventory(
            store, settle_material_transfer=lambda _command: {}
        )
        assert scheduler._active_ingress_site_uuids(["site-1", "site-2"]) == {"site-1"}
        reagent = connection.execute(
            WorkflowQuantityInventoryAuthority._content_sql("reagent"),
            ("reagent-1",),
        ).fetchone()
        assert reagent["quantity"] == 10
        assert reagent["material_uuid"] == "mat-full"
        instance = connection.execute(
            "SELECT version FROM material_instance WHERE edge_uuid='mat-full'"
        ).fetchone()
        assert instance["version"] == 4
        versions = dict(
            connection.execute(
                "SELECT uuid, aggregate_version FROM resource_template"
            ).fetchall()
        )
        assert versions == {"tpl-versioned": 7, "tpl-bare": 0}

        contents = connection.execute(
            """
            SELECT uuid, kind, deleted_at, material_uuid, revision
            FROM container_content ORDER BY uuid
            """
        ).fetchall()
        assert [tuple(row) for row in contents] == [
            ("reagent-1", "reagent", None, "mat-full", 6),
            ("sample-dead", "sample", "2026-02-01", "mat-full", None),
            ("substance-1", "substance", None, "mat-bare", 2),
        ]
        visible = connection.execute(
            "SELECT instance_uuid FROM substance_content ORDER BY instance_uuid"
        ).fetchall()
        assert [row[0] for row in visible] == ["mat-full"]

        reservation = connection.execute(
            "SELECT site_uuid, site_active FROM station_ingress_reservation WHERE uuid='rsv-1'"
        ).fetchone()
        assert tuple(reservation) == ("site-1", 1)
        meta = connection.execute("SELECT * FROM lab_meta").fetchone()
        assert meta["lab_name"] == "验收实验室"
        assert meta["lab_domain"] == "chem"
        assert meta["resource_graph_bootstrap_source"] == "graph-a"
        assert meta["resource_graph_bootstrap_fingerprint"] == "fp-1"
        assert meta["resource_graph_bootstrap_fingerprint_version"] == "2"
        assert "unrelated_note" not in meta.keys()
        assert store.get_meta("unknown-key", "缺省") == "缺省"
    finally:
        store.close()


def test_legacy_inventory_compaction_is_idempotent(tmp_path: Path) -> None:
    """第二次打开不得把版本重置，也不得把内容视图变回空表。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path)
    first = InventoryStore(str(path))
    first.close()
    second = InventoryStore(str(path))
    try:
        connection = second._conn
        assert connection.execute(
            "SELECT aggregate_version FROM material WHERE uuid='mat-full'"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM container_content"
        ).fetchone()[0] == 3
        assert "sample" in _names(connection, "view")
        assert "sample" not in _names(connection, "table")
        assert "lab_zone" not in _names(connection, "table")
    finally:
        second.close()


def test_two_active_contents_fail_closed(tmp_path: Path) -> None:
    """同一物料两条未删除内容必须拒绝启动，源表不能先被删掉。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path, duplicate_active_content=True)
    with pytest.raises(sqlite3.IntegrityError):
        InventoryStore(str(path))
    connection = sqlite3.connect(path)
    try:
        tables = _names(connection, "table")
        for name in (
            "sample",
            "current_substance",
            "reagent",
            "station_ingress_reservation_site",
            "material_inventory",
            "lab_zone",
        ):
            assert name in tables, name
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 17
    finally:
        connection.close()


def test_colliding_content_uuid_is_not_silently_dropped(tmp_path: Path) -> None:
    """样品和内容物碰巧同号时，不能靠 INSERT OR IGNORE 丢掉其中一行还报成功。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path, colliding_content_uuid=True)
    with pytest.raises(sqlite3.IntegrityError):
        InventoryStore(str(path))
    connection = sqlite3.connect(path)
    try:
        tables = _names(connection, "table")
        assert "sample" in tables
        assert "current_substance" in tables
        assert "station_ingress_reservation_site" in tables
    finally:
        connection.close()


def test_device_latest_fold_skips_identical_tail_and_unchanged_write(tmp_path: Path) -> None:
    """当前值与历史最后一行相同则不追加；之后同值写入也不追加。"""

    path = tmp_path / "device.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE device_property_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            property TEXT NOT NULL,
            value TEXT NOT NULL,
            value_type TEXT NOT NULL,
            recorded_at INTEGER NOT NULL
        );
        CREATE TABLE device_property_latest (
            device_id TEXT NOT NULL,
            property TEXT NOT NULL,
            value TEXT NOT NULL,
            value_type TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (device_id, property)
        );
        INSERT INTO device_property_history
            (device_id, property, value, value_type, recorded_at)
        VALUES
            ('pump', 'temp', '20', 'int', 100),
            ('pump', 'rpm', '1', 'int', 100);
        INSERT INTO device_property_latest VALUES
            ('pump', 'temp', '20', 'int', 250),
            ('pump', 'rpm', '2', 'int', 300),
            ('pump', 'gate', 'open', 'str', 400);
        """
    )
    connection.commit()
    connection.close()

    store = DeviceStateStore(str(path))
    try:
        assert "device_property_latest" not in _names(store._conn, "table")
        temp = store._conn.execute(
            """
            SELECT value, recorded_at FROM device_property_history
            WHERE device_id='pump' AND property='temp' ORDER BY id
            """
        ).fetchall()
        assert [tuple(row) for row in temp] == [("20", 100)]
        rpm = store._conn.execute(
            """
            SELECT value, recorded_at FROM device_property_history
            WHERE device_id='pump' AND property='rpm' ORDER BY id
            """
        ).fetchall()
        assert [tuple(row) for row in rpm] == [("1", 100), ("2", 300)]
        latest = store.latest_for("pump")
        assert latest["gate"] == {
            "value": "open",
            "value_type": "str",
            "updated_at": 400,
        }
        before = store._conn.execute(
            "SELECT COUNT(*) FROM device_property_history WHERE property='rpm'"
        ).fetchone()[0]
        assert store.set("pump", "rpm", 2, now_ms=999) is False
        after = store._conn.execute(
            "SELECT COUNT(*) FROM device_property_history WHERE property='rpm'"
        ).fetchone()[0]
        assert after == before
        assert store.latest_for("pump")["rpm"]["updated_at"] == 300
    finally:
        store.close()


def test_edge_pending_outcome_folds_onto_runtime_and_clear_keeps_row(tmp_path: Path) -> None:
    """待上报结果进运行行；完成后清空结果列，运行行还在，可以再次保存。"""

    path = tmp_path / "edge.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE edge_job_runtime (
            job_uuid TEXT PRIMARY KEY,
            task_uuid TEXT NOT NULL,
            node_uuid TEXT NOT NULL,
            command_uuid TEXT NOT NULL,
            claim_uuid TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            fences_json TEXT NOT NULL,
            job_access_token TEXT NOT NULL,
            status TEXT NOT NULL,
            feedback_sequence INTEGER NOT NULL DEFAULT 0,
            trace_id TEXT NOT NULL DEFAULT '',
            traceparent TEXT NOT NULL DEFAULT '',
            tracestate TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE TABLE edge_job_outcome_pending (
            job_uuid TEXT PRIMARY KEY,
            outcome TEXT NOT NULL,
            return_info_json TEXT NOT NULL,
            error_info_json TEXT NOT NULL,
            unknown_command_ids_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO edge_job_runtime VALUES (
            'job-1', 'task-1', 'node-1', 'cmd-1', 'claim-1', 1, '[]',
            'token', 'running', 0, '', '', '', 10
        );
        INSERT INTO edge_job_outcome_pending VALUES (
            'job-1', 'succeeded', '{"ok":true}', '[]', '[]', 11
        );
        """
    )
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    store = EdgeControlStore(str(path))
    try:
        assert "edge_job_outcome_pending" not in _names(store._connection, "table")
        pending = store.get_pending_outcome("job-1")
        assert pending is not None
        assert pending.outcome == "succeeded"
        assert pending.return_info == {"ok": True}
        assert store.save_pending_outcome(
            "job-1", "failed", {}, [{"code": "E"}], []
        ) is False
        assert store.get_pending_outcome("job-1").outcome == "succeeded"
        store.abort_restarted_job("job-1")
        assert store.get_pending_outcome("job-1") is None
        runtime = store._connection.execute(
            "SELECT status, outcome FROM edge_job_runtime WHERE job_uuid='job-1'"
        ).fetchone()
        assert runtime["status"] == "outcome_retired"
        assert runtime["outcome"] is None
        store.set_job_status("job-1", "running")
        assert store.save_pending_outcome(
            "job-1", "failed", {}, [{"code": "E"}], []
        ) is True
        assert store.save_pending_outcome(
            "job-1", "succeeded", {}, [], []
        ) is False
        assert store.get_pending_outcome("job-1").outcome == "failed"
    finally:
        store.close()


def test_local_edge_meta_missing_restart_flag_stays_null(tmp_path: Path) -> None:
    """旧键值表没有重启标记时，收成一行后该列必须是 NULL，不能填假值。"""

    path = tmp_path / "authority.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE local_edge_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO local_edge_meta VALUES ('execution_process_uuid', 'proc-1');
        INSERT INTO local_edge_meta VALUES ('sequence', '4');
        """
    )
    connection.commit()
    connection.close()

    store = LocalEdgeAuthorityStore(path)
    try:
        row = store._connection.execute("SELECT * FROM local_edge_meta").fetchone()
        assert row["id"] == 1
        assert row["execution_process_uuid"] == "proc-1"
        assert row["execution_restart_pending"] is None
        assert row["sequence"] == 4
        assert store._connection.execute(
            "SELECT COUNT(*) FROM local_edge_meta"
        ).fetchone()[0] == 1
    finally:
        store.close()


def _outbox_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE workflow_station_event_outbox (
            event_uuid TEXT PRIMARY KEY,
            station_sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_uuid TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_uuid TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            acked_at TEXT,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL
        )
        """
    )


def test_outbox_is_absent_until_an_unacked_row_must_be_kept(tmp_path: Path) -> None:
    """新库不建发件箱；全部已确认才删；还有未确认行时必须留表并能读出。"""

    fresh = WorkflowStore(tmp_path / "fresh.db", persist_workflow_definitions=False)
    try:
        with fresh.transaction() as connection:
            append_station_event(
                connection,
                event_type="job.completed",
                aggregate_type="workflow_task",
                aggregate_uuid="task-1",
                source_kind="dispatch",
                source_uuid="src-1",
                idempotency_key="idem-1",
                payload={"n": 1},
            )
            assert "workflow_station_event_outbox" not in _names(connection, "table")
        assert StationEventOutboxStore(fresh).list_pending() == []
    finally:
        fresh.close()

    acked_path = tmp_path / "acked.db"
    acked = WorkflowStore(acked_path, persist_workflow_definitions=False)
    acked.close()
    connection = sqlite3.connect(acked_path)
    _outbox_table(connection)
    connection.execute(
        """
        INSERT INTO workflow_station_event_outbox VALUES (
            'evt-acked', 1, 'job.completed', 'workflow_task', 'task-1',
            'dispatch', 'src-1', 'idem', '{}', 0, NULL, '2026-01-02',
            '2026-01-01', '2026-01-01'
        )
        """
    )
    connection.commit()
    connection.close()
    reopened = WorkflowStore(acked_path, persist_workflow_definitions=False)
    try:
        assert "workflow_station_event_outbox" not in _names(reopened._conn, "table")
    finally:
        reopened.close()

    pending_path = tmp_path / "pending.db"
    pending = WorkflowStore(pending_path, persist_workflow_definitions=False)
    pending.close()
    connection = sqlite3.connect(pending_path)
    _outbox_table(connection)
    connection.execute(
        """
        INSERT INTO workflow_station_event_outbox VALUES (
            'evt-open', 7, 'job.completed', 'workflow_task', 'task-1',
            'dispatch', 'src-1', 'idem', '{"n":1}', 0, NULL, NULL,
            '2026-01-01', '2026-01-01'
        )
        """
    )
    connection.commit()
    connection.close()
    kept = WorkflowStore(pending_path, persist_workflow_definitions=False)
    try:
        rows = StationEventOutboxStore(kept).list_pending()
        assert [row["event_uuid"] for row in rows] == ["evt-open"]
        assert rows[0]["payload"] == {"n": 1}
    finally:
        kept.close()


def test_outbox_ack_without_table_is_not_found(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "none.db", persist_workflow_definitions=False)
    try:
        outbox = StationEventOutboxStore(store)
        with pytest.raises(StoreNotFound):
            outbox.acknowledge(event_uuid="missing", station_sequence=1)
    finally:
        store.close()


def test_catalog_matches_dropped_tables_and_keeps_definition_shells() -> None:
    """PostgreSQL 新库目录跟 SQLite 新结构一致：合并掉的表不在，定义空壳还在。"""

    catalog = json.loads(
        Path("unilabos/persistence/schema.json").read_text(encoding="utf-8")
    )

    def names(scope: str, kind: str) -> set[str]:
        return {item["name"] for item in catalog[scope] if item["type"] == kind}

    inventory_tables = names("inventory", "table")
    for name in _DROPPED_INVENTORY_TABLES:
        assert name not in inventory_tables
    assert "container_content" in inventory_tables
    assert "lab_meta" in inventory_tables
    inventory_views = names("inventory", "view")
    for name in ("reagent", "sample", "current_substance", "substance_content"):
        assert name in inventory_views
    assert "device_property_latest" not in names("device", "table")
    assert "device_property_history" in names("device", "table")
    assert "edge_job_outcome_pending" not in names("edge", "table")
    assert "workflow_station_event_outbox" not in names("workflow", "table")
    workflow_tables = names("workflow", "table")
    for name in ("workflow", "workflow_node", "workflow_edge"):
        assert name in workflow_tables
    assert "workflow_task_debug_configuration" not in workflow_tables
    workflow_task = next(
        item
        for item in catalog["workflow"]
        if item["type"] == "table" and item["name"] == "workflow_task"
    )
    assert {
        "debug_create_time",
        "debug_update_time",
        "debug_start_node_uuids",
        "debug_breakpoint_node_uuids",
        "debug_execution_policy",
        "debug_status",
    } <= {column["name"] for column in workflow_task["columns"]}
    upgrade = Path("unilabos/persistence/migrations.py").read_text(encoding="utf-8")
    for name in (
        "material_inventory",
        "relative_position",
        "material_content_version",
        "resource_template_inventory",
        "station_ingress_reservation_site",
        "lab_zone",
        "lab_placement",
        "edge_job_outcome_pending",
        "device_property_latest",
    ):
        assert name in upgrade


def test_postgresql_upgrade_drops_dependent_views_and_ignores_replacement_views(
    postgres_test_url,
) -> None:
    """旧视图挡住删表，合并后的同名视图不能在第二次升级时被当成源表。"""

    psycopg = pytest.importorskip("psycopg")
    from unilabos.persistence.migrations import _compact_inventory_tables

    with psycopg.connect(postgres_test_url) as connection:
        # 同一会话库会被前面的 PostgreSQL 用例留下已提交的 material。
        # 先清空 public，结束时回滚，避免和那些用例互相污染。
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        connection.execute(
            """
            CREATE TABLE material (
                uuid TEXT PRIMARY KEY,
                aggregate_version INTEGER NOT NULL DEFAULT 0,
                legacy_cloud_id TEXT NOT NULL DEFAULT '',
                legacy_template_id TEXT NOT NULL DEFAULT '',
                lot_id TEXT NOT NULL DEFAULT '',
                inventory_status TEXT NOT NULL DEFAULT 'warehouse',
                disposition TEXT NOT NULL DEFAULT 'active',
                content_version INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE material_inventory (
                material_uuid TEXT PRIMARY KEY,
                legacy_cloud_id TEXT NOT NULL,
                legacy_template_id TEXT NOT NULL,
                lot_id TEXT NOT NULL,
                inventory_status TEXT NOT NULL,
                disposition TEXT NOT NULL,
                aggregate_version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO material(uuid) VALUES ('mat-1')
            """
        )
        connection.execute(
            """
            INSERT INTO material_inventory VALUES (
                'mat-1', 'cloud', 'tpl', 'lot', 'in_use', 'hold', 4
            )
            """
        )
        connection.execute(
            """
            CREATE VIEW material_instance AS
            SELECT material.uuid, inventory.aggregate_version
            FROM material
            JOIN material_inventory AS inventory
              ON inventory.material_uuid = material.uuid
            """
        )
        connection.execute(
            """
            CREATE TABLE container_content (
                uuid TEXT PRIMARY KEY,
                create_time TEXT,
                update_time TEXT,
                deleted_at TEXT,
                description TEXT,
                meta_data TEXT,
                material_uuid TEXT,
                code TEXT,
                name TEXT,
                sample_type TEXT,
                source TEXT,
                quantity DOUBLE PRECISION,
                quantity_unit TEXT,
                collected_at TEXT,
                expires_at TEXT,
                kind TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE sample (
                uuid TEXT PRIMARY KEY,
                create_time TEXT,
                update_time TEXT,
                deleted_at TEXT,
                description TEXT,
                meta_data TEXT,
                material_uuid TEXT,
                code TEXT,
                name TEXT,
                sample_type TEXT,
                source TEXT,
                quantity DOUBLE PRECISION,
                quantity_unit TEXT,
                collected_at TEXT,
                expires_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sample VALUES (
                'sample-1', 't', 't', NULL, NULL, '{}', 'mat-1',
                'S', '样', 'unknown', 'operator', 1, 'mL', NULL, NULL
            )
            """
        )
        _compact_inventory_tables(connection)
        version = connection.execute(
            "SELECT aggregate_version FROM material WHERE uuid='mat-1'"
        ).fetchone()[0]
        assert version == 4
        assert connection.execute(
            "SELECT to_regclass('public.material_inventory')"
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT to_regclass('public.material_instance')"
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT to_regclass('public.sample')"
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT COUNT(*) FROM container_content"
        ).fetchone()[0] == 1
        connection.execute(
            """
            CREATE VIEW sample AS
            SELECT * FROM container_content WHERE kind = 'sample'
            """
        )
        _compact_inventory_tables(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM container_content"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='sample'"
        ).fetchone()[0] == "VIEW"
        connection.rollback()


def test_postgresql_legacy_debug_configuration_is_migrated(
    postgres_test_url,
) -> None:
    """旧 PostgreSQL 调试配置表迁移后保留数据并删除源表。"""

    psycopg = pytest.importorskip("psycopg")
    from unilabos.persistence.migrations import _compact_workflow_debug_configuration

    with psycopg.connect(postgres_test_url) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        connection.execute(
            "CREATE TABLE workflow_task (uuid TEXT PRIMARY KEY)"
        )
        connection.execute(
            """
            CREATE TABLE workflow_task_debug_configuration (
                workflow_task_uuid TEXT PRIMARY KEY,
                create_time TEXT NOT NULL,
                update_time TEXT NOT NULL,
                start_node_uuids TEXT NOT NULL,
                breakpoint_node_uuids TEXT NOT NULL,
                execution_policy TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO workflow_task VALUES ('task-pg-debug')")
        connection.execute(
            """
            INSERT INTO workflow_task_debug_configuration VALUES (
                'task-pg-debug', '2026-01-01', '2026-01-02',
                '["node-start"]', '["node-break"]', 'step', 'paused'
            )
            """
        )
        _compact_workflow_debug_configuration(connection)
        row = connection.execute(
            """
            SELECT debug_create_time, debug_update_time,
                   debug_start_node_uuids, debug_breakpoint_node_uuids,
                   debug_execution_policy, debug_status
            FROM workflow_task
            WHERE uuid = 'task-pg-debug'
            """
        ).fetchone()
        assert row == (
            "2026-01-01",
            "2026-01-02",
            '["node-start"]',
            '["node-break"]',
            "step",
            "paused",
        )
        assert connection.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='public' AND table_name='workflow_task_debug_configuration'
            """
        ).fetchone() is None
        connection.rollback()


def test_workflow_debug_configuration_is_merged_into_task(
    tmp_path: Path,
) -> None:
    """调试配置并入任务表，其他工作流运行事实表保持独立。"""

    store = WorkflowStore(tmp_path / "workflow.db", persist_workflow_definitions=False)
    try:
        tables = _names(store._conn, "table")
        assert "workflow_task_debug_configuration" not in tables
        assert {
            "debug_create_time",
            "debug_update_time",
            "debug_start_node_uuids",
            "debug_breakpoint_node_uuids",
            "debug_execution_policy",
            "debug_status",
        } <= {
            row[1]
            for row in store._conn.execute("PRAGMA table_info(workflow_task)")
        }
        assert {
            "workflow_inventory_saga",
            "workflow_task_material_admission",
            "job_device_tenancy_transition",
        } <= tables
    finally:
        store.close()


def test_workflow_debug_configuration_is_merged_for_selected_database(
    database_mode, tmp_path: Path
) -> None:
    """SQLite 与 PostgreSQL 新库都不再创建调试配置子表。"""

    store = WorkflowStore(tmp_path / "workflow-selected.db", persist_workflow_definitions=False)
    try:
        assert "workflow_task_debug_configuration" not in store.db.table_names()
        assert {
            "debug_create_time",
            "debug_update_time",
            "debug_start_node_uuids",
            "debug_breakpoint_node_uuids",
            "debug_execution_policy",
            "debug_status",
        } <= {
            row[1]
            for row in store._conn.execute("PRAGMA table_info(workflow_task)")
        }
    finally:
        store.close()


def test_legacy_debug_configuration_is_migrated_into_task(
    tmp_path: Path,
) -> None:
    """旧库重启时保留调试配置并删除旧子表。"""

    path = tmp_path / "legacy-debug.db"
    store = WorkflowStore(path, persist_workflow_definitions=False)
    try:
        with store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workflow_task(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, workflow_uuid, status, workflow_snapshot,
                    execution_plan, run_mode, target_node_uuid, control_status,
                    cleanup_status, trace_context, input, output, error_info
                ) VALUES (?, ?, ?, NULL, NULL, '{}', NULL, 'pending',
                          '{}', '{}', 'step', NULL, 'paused', 'none',
                          '{}', '{}', '{}', '[]')
                """,
                ("task-legacy-debug", "2026-01-01", "2026-01-01"),
            )
    finally:
        store.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE workflow_task_debug_configuration (
            workflow_task_uuid TEXT PRIMARY KEY,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL,
            start_node_uuids TEXT NOT NULL,
            breakpoint_node_uuids TEXT NOT NULL,
            execution_policy TEXT NOT NULL CHECK (
                execution_policy IN ('step', 'continue')
            ),
            status TEXT NOT NULL CHECK (
                status IN ('paused', 'running', 'completed', 'stopped')
            ),
            FOREIGN KEY(workflow_task_uuid) REFERENCES workflow_task(uuid)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_task_debug_configuration(
            workflow_task_uuid, create_time, update_time,
            start_node_uuids, breakpoint_node_uuids, execution_policy, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-legacy-debug",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:01:00Z",
            '["node-start"]',
            '["node-break"]',
            "step",
            "paused",
        ),
    )
    connection.commit()
    connection.close()

    reopened = WorkflowStore(path, persist_workflow_definitions=False)
    try:
        assert "workflow_task_debug_configuration" not in reopened.db.table_names()
        row = reopened._conn.execute(
            """
            SELECT debug_create_time, debug_update_time,
                   debug_start_node_uuids, debug_breakpoint_node_uuids,
                   debug_execution_policy, debug_status
            FROM workflow_task
            WHERE uuid = ?
            """,
            ("task-legacy-debug",),
        ).fetchone()
        assert tuple(row) == (
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:01:00Z",
            '["node-start"]',
            '["node-break"]',
            "step",
            "paused",
        )
        assert reopened.get_debug_projection("task-legacy-debug") == {
            "configuration": {
                "start_node_uuids": ["node-start"],
                "breakpoint_node_uuids": ["node-break"],
            },
            "execution_policy": "step",
            "status": "paused",
            "holds": [],
        }
    finally:
        reopened.close()
