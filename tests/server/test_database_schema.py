"""四库的精简表边界与聚合字段测试。"""

from __future__ import annotations

import pytest

from unilabos.server.database import DATABASE_SPECS, initialize_database


EXPECTED_TABLES = {
    "runtime": {
        "schema_migration",
        "backend_session",
        "executor_endpoint",
        "command_inbox",
        "execution_job",
        "adapter_command_outbox",
        "adapter_event_inbox",
        "backend_event_outbox",
    },
    "materials": {
        "schema_migration",
        "resource_template",
        "inventory_lot",
        "material",
        "material_position",
        "material_data",
        "material_substance",
        "site",
        "inventory_reservation",
        "inventory_command_effect",
        "inventory_ledger",
    },
    "telemetry": {
        "schema_migration",
        "telemetry_source_cursor",
        "device_state_latest",
        "telemetry_event",
    },
    "history": {
        "schema_migration",
        "payload_object",
        "history_event",
    },
}


@pytest.mark.parametrize("key", tuple(EXPECTED_TABLES))
def test_database_v1_schema_is_complete_and_replay_safe(tmp_path, key: str) -> None:
    spec = DATABASE_SPECS[key]
    path = tmp_path / spec.filename
    initialize_database(path, spec).close()
    connection = initialize_database(path, spec)
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
    finally:
        connection.close()


def test_aggregate_fields_and_material_storage_exceptions(tmp_path) -> None:
    existing: set[str] = set()
    columns: dict[str, set[str]] = {}
    for spec in DATABASE_SPECS.values():
        connection = initialize_database(tmp_path / spec.filename, spec)
        try:
            existing.update(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            for table in spec.table_names:
                columns[table] = {
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
        finally:
            connection.close()

    removed = {
        "resource_template_category",
        "resource_handle_template",
        "resource_site_template",
        "resource_site_template_category_hint",
        "material_pose",
        "material_state",
        "material_state_source_event",
        "site_pose",
        "site_category_hint",
        "site_occupancy",
        "inventory_reservation_item",
        "inventory_event_outbox",
        "inventory_sync_state",
        "device_route",
        "device_action_capability",
        "device_action_availability",
        "job_material_binding",
        "terminal_gate",
        "terminal_decision",
        "telemetry_ingest_batch",
        "device_state_report",
        "device_property_latest",
        "device_property_sample",
        "device_connection_latest",
        "device_connection_event",
        "device_alarm",
        "device_alarm_event",
        "job_transition",
        "action_availability_event",
        "job_feedback",
        "job_result",
        "job_log",
        "error_snapshot",
        "decision_audit",
    }
    assert existing.isdisjoint(removed)

    assert {"category_json", "available_sites_json", "handles_json"} <= columns[
        "resource_template"
    ]
    assert {"position_x", "position_y", "position_z", "rotation_x"} <= columns[
        "material_position"
    ]
    assert {"data_json", "sites_initialized", "content_version"} <= columns[
        "material_data"
    ]
    assert {"name", "quantity", "quantity_unit", "physical_state"} <= columns[
        "material_substance"
    ]
    assert {
        "pose_json",
        "data_json",
        "liquids_json",
        "sites_initialized",
    }.isdisjoint(columns["material"])
    assert {"device_routes_json", "action_capabilities_json"} <= columns[
        "executor_endpoint"
    ]
    assert {"material_bindings_json", "terminal_gate_state"} <= columns["execution_job"]


def test_total_table_count_stays_small() -> None:
    assert {key: len(spec.table_names) for key, spec in DATABASE_SPECS.items()} == {
        "runtime": 8,
        "materials": 11,
        "telemetry": 4,
        "history": 3,
    }
    assert sum(len(spec.table_names) for spec in DATABASE_SPECS.values()) == 26
