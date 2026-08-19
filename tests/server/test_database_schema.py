"""微后端四库 schema 的建库和关键约束测试。"""

from __future__ import annotations

import sqlite3

import pytest

from unilabos.server.database import DATABASE_SPECS, initialize_database


EXPECTED_TABLES = {
    "runtime": {
        "schema_migration",
        "backend_session",
        "executor_endpoint",
        "device_route",
        "device_action_capability",
        "command_inbox",
        "execution_job",
        "device_action_availability",
        "job_material_binding",
        "terminal_gate",
        "terminal_decision",
        "adapter_command_outbox",
        "adapter_event_inbox",
        "backend_event_outbox",
    },
    "materials": {
        "schema_migration",
        "resource_template",
        "resource_handle_template",
        "inventory_lot",
        "material",
        "material_pose",
        "material_state",
        "material_state_source_event",
        "site",
        "inventory_reservation",
        "inventory_command_effect",
        "inventory_ledger",
        "inventory_event_outbox",
        "inventory_sync_state",
    },
    "telemetry": {
        "schema_migration",
        "telemetry_source_cursor",
        "telemetry_ingest_batch",
        "device_state_report",
        "device_property_latest",
        "device_property_sample",
        "device_connection_latest",
        "device_connection_event",
        "device_alarm",
        "device_alarm_event",
        "telemetry_maintenance",
    },
    "history": {
        "schema_migration",
        "payload_object",
        "job_transition",
        "action_availability_event",
        "job_feedback",
        "job_result",
        "job_log",
        "error_snapshot",
        "decision_audit",
        "history_maintenance",
    },
}


@pytest.mark.parametrize("key", tuple(EXPECTED_TABLES))
def test_database_v1_schema_is_complete_and_replay_safe(tmp_path, key: str) -> None:
    spec = DATABASE_SPECS[key]
    database_path = tmp_path / spec.filename

    connection = initialize_database(database_path, spec)
    connection.close()
    connection = initialize_database(database_path, spec)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not str(row[0]).startswith("sqlite_")
        }
        assert tables == EXPECTED_TABLES[key]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migration WHERE version=1"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def _insert_template(connection: sqlite3.Connection, uuid: str, name: str) -> None:
    connection.execute(
        """
        INSERT INTO resource_template(
            template_uuid,name,display_name,resource_type,template_version,
            definition_json,definition_hash,status,created_at_ms,updated_at_ms
        ) VALUES (?,?,?,?,1,'{}',?,'active',1,1)
        """,
        (uuid, name, name, "resource", f"hash-{uuid}"),
    )


def _insert_material(
    connection: sqlite3.Connection,
    uuid: str,
    template_uuid: str,
) -> None:
    connection.execute(
        """
        INSERT INTO material(
            material_uuid,resource_id,template_uuid,name,resource_type,class_name,
            template_name,lifecycle_status,created_at_ms,updated_at_ms
        ) VALUES (?,?,?,?,?,?,?,'active',1,1)
        """,
        (uuid, uuid, template_uuid, uuid, "resource", "Resource", template_uuid),
    )


def test_site_category_is_hint_and_not_database_admission_rule(tmp_path) -> None:
    spec = DATABASE_SPECS["materials"]
    connection = initialize_database(tmp_path / spec.filename, spec)
    try:
        with connection:
            _insert_template(connection, "tpl-owner", "owner")
            _insert_template(connection, "tpl-occupant", "occupant")
            _insert_material(connection, "mat-owner", "tpl-owner")
            _insert_material(connection, "mat-occupant", "tpl-occupant")
            connection.execute(
                "UPDATE material SET parent_material_uuid='mat-owner',version=2 "
                "WHERE material_uuid='mat-occupant'"
            )
            connection.execute(
                """
                INSERT INTO site(
                    site_uuid,owner_material_uuid,template_name,site_index,label,
                    occupied_material_uuid,allowed_resource_categories_json,
                    occupancy_changed_at_ms,created_at_ms,updated_at_ms
                ) VALUES (
                    'site-1','mat-owner','owner',0,'A1','mat-occupant',
                    '["different-category"]',1,1,1
                )
                """
            )

        assert (
            connection.execute(
                "SELECT occupied_material_uuid FROM site WHERE site_uuid='site-1'"
            ).fetchone()[0]
            == "mat-occupant"
        )
    finally:
        connection.close()


def test_runtime_allows_two_protocol_routes_but_only_one_selected(tmp_path) -> None:
    spec = DATABASE_SPECS["runtime"]
    connection = initialize_database(tmp_path / spec.filename, spec)
    try:
        with connection:
            for endpoint_uuid, transport in (
                ("host-endpoint", "hostlink"),
                ("ros-endpoint", "ros2"),
            ):
                connection.execute(
                    """
                    INSERT INTO executor_endpoint(
                        endpoint_uuid,transport,host_uuid,instance_name,
                        authority_epoch,state,registered_at_ms,last_seen_at_ms
                    ) VALUES (?,?,?,?,?,'online',1,1)
                    """,
                    (endpoint_uuid, transport, "host-1", endpoint_uuid, "epoch-1"),
                )
            connection.execute(
                """
                INSERT INTO device_route(
                    route_uuid,device_uuid,endpoint_uuid,transport,driver_key,
                    selected,config_hash,created_at_ms,updated_at_ms
                ) VALUES ('route-host','device-1','host-endpoint','hostlink',
                    'driver',1,'hash-host',1,1)
                """
            )
            connection.execute(
                """
                INSERT INTO device_route(
                    route_uuid,device_uuid,endpoint_uuid,transport,driver_key,
                    selected,config_hash,created_at_ms,updated_at_ms
                ) VALUES ('route-ros','device-1','ros-endpoint','ros2',
                    'driver',0,'hash-ros',1,1)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "UPDATE device_route SET selected=1 WHERE route_uuid='route-ros'"
                )
    finally:
        connection.close()


def test_action_capability_and_availability_are_durable_projections(tmp_path) -> None:
    spec = DATABASE_SPECS["runtime"]
    connection = initialize_database(tmp_path / spec.filename, spec)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO executor_endpoint(
                    endpoint_uuid,transport,host_uuid,instance_name,
                    authority_epoch,state,registered_at_ms,last_seen_at_ms
                ) VALUES ('endpoint','hostlink','host','edge','epoch','online',1,1)
                """
            )
            connection.execute(
                """
                INSERT INTO device_action_capability(
                    capability_uuid,endpoint_uuid,device_uuid,action_name,
                    concurrency_mode,state,descriptor_hash,discovery_epoch,
                    discovery_generation,
                    discovered_at_ms,last_seen_at_ms
                ) VALUES (
                    'capability','endpoint','device','transfer','exclusive',
                    'active','hash','epoch-1',1,1,1
                )
                """
            )
            # 旧 report_action_lock 不带 job_id；busy 投影必须仍能被持久化。
            connection.execute(
                """
                INSERT INTO device_action_availability(
                    endpoint_uuid,device_uuid,action_name,state,source,
                    source_event_uuid,discovery_epoch,discovery_generation,
                    observed_at_ms,
                    received_at_ms
                ) VALUES (
                    'endpoint','device','transfer','busy','adapter_report',
                    'event-1','epoch-1',1,2,3
                )
                """
            )

        assert tuple(
            connection.execute(
                "SELECT state,active_job_uuid FROM device_action_availability"
            ).fetchone()
        ) == ("busy", None)
    finally:
        connection.close()


def test_endpoint_reconciliation_events_are_not_forced_into_a_job(tmp_path) -> None:
    spec = DATABASE_SPECS["runtime"]
    connection = initialize_database(tmp_path / spec.filename, spec)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO executor_endpoint(
                    endpoint_uuid,transport,host_uuid,instance_name,
                    authority_epoch,state,registered_at_ms,last_seen_at_ms
                ) VALUES ('endpoint','ros2','host','edge','epoch','online',1,1)
                """
            )
            connection.execute(
                """
                INSERT INTO adapter_event_inbox(
                    adapter_event_uuid,endpoint_uuid,adapter_epoch,
                    adapter_sequence,event_type,status,received_at_ms
                ) VALUES (
                    'event-1','endpoint','epoch-1',1,
                    'action_availability_snapshot',
                    'received',1
                )
                """
            )
            connection.execute(
                """
                INSERT INTO adapter_command_outbox(
                    adapter_command_uuid,endpoint_uuid,trigger_event_uuid,
                    command_type,status,created_at_ms
                ) VALUES (
                    'reconcile-1','endpoint','event-1','reconcile_state','pending',1
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO adapter_event_inbox(
                        adapter_event_uuid,endpoint_uuid,adapter_epoch,
                        adapter_sequence,event_type,status,received_at_ms
                    ) VALUES (
                        'event-duplicate','endpoint','epoch-1',1,
                        'endpoint_ready','received',1
                    )
                    """
                )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO adapter_event_inbox(
                        adapter_event_uuid,endpoint_uuid,adapter_epoch,
                        adapter_sequence,event_type,status,received_at_ms
                    ) VALUES (
                        'event-2','endpoint','epoch-1',2,'running','received',1
                    )
                    """
                )
    finally:
        connection.close()


def test_legacy_scheduler_and_site_admission_tables_do_not_exist(tmp_path) -> None:
    forbidden = {
        "workflow",
        "workflow_node",
        "workflow_edge",
        "workflow_runs",
        "job_runs",
        "execution_lock_lease",
        "edge_job_runtime",
        "edge_job_outcome_pending",
        "device_action_queue",
        "action_lock_lease",
        "material_instance",
        "resource_relation",
        "substance_content",
        "resource_template_category",
        "resource_site_template",
        "resource_site_template_category_hint",
        "site_pose",
        "site_category_hint",
        "site_occupancy",
        "inventory_reservation_item",
    }
    existing: set[str] = set()
    for spec in DATABASE_SPECS.values():
        connection = initialize_database(tmp_path / spec.filename, spec)
        try:
            existing.update(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            )
        finally:
            connection.close()
    assert existing.isdisjoint(forbidden)

    materials = initialize_database(
        tmp_path / DATABASE_SPECS["materials"].filename,
        DATABASE_SPECS["materials"],
    )
    try:
        site_columns = {
            row[1] for row in materials.execute("PRAGMA table_info(site)").fetchall()
        }
        assert "content_type" not in site_columns
        assert "allowed_resource_template_uuids" not in site_columns
        assert "site_template_uuid" not in site_columns
        assert {
            "pose_json",
            "allowed_resource_categories_json",
            "occupied_material_uuid",
        } <= site_columns
    finally:
        materials.close()
