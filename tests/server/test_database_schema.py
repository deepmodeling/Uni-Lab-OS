"""四库的精简表边界与聚合字段测试。"""

from __future__ import annotations

import pytest

from unilabos.server.database import DATABASE_SPECS, initialize_database


EXPECTED_TABLES = {
    "runtime": {
        "schema_identity",
        "backend_session",
        "executor_endpoint",
        "command_inbox",
        "execution_job",
        "adapter_command_outbox",
        "adapter_event_inbox",
        "backend_event_outbox",
        # workflow authority（调度产物与运行控制同库）
        "workflow",
        "workflow_node_template",
        "workflow_operation_category",
        "workflow_handle_template",
        "workflow_node",
        "workflow_edge",
        "workflow_task",
        "workflow_node_run",
        "workflow_node_job",
        "workflow_task_command",
        "execution_lock_lease",
        "workflow_execution_claim",
        "workflow_execution_fence",
        "workflow_execution_lease",
        "workflow_execution_authority",
        "workflow_execution_dispatch",
        "workflow_resource_interval",
        "workflow_resource_admission",
        "workflow_resource_use",
        "workflow_resource_loan",
        "workflow_device_tenancy",
        "workflow_tenancy_transition",
        "workflow_resource_waiter",
        "workflow_human_interaction",
        "workflow_station_control",
        "workflow_station_control_command",
        "workflow_error_session",
        "workflow_station_error",
        "workflow_node_job_result",
        "workflow_node_job_feedback_history",
        "workflow_intervention",
        "workflow_manual_confirmation",
        "workflow_source_registration",
        "workflow_authoring",
        "workflow_publication",
        "frontend_event",
        # registry authority（edge 注册表版本快照）
        "registry_entry",
        "registry_entry_state",
        "registry_report",
        "lab_layout",
        # schema v12：V1 Workspace Host 操作记录与快照修订号
        "workspace_host_operation",
        "workspace_host_state",
    },
    "materials": {
        "schema_identity",
        "resource_template",
        "inventory_lot",
        "material",
        "material_position",
        "material_data",
        "material_substance",
        "site",
        "material_link",
        "lab_graph",
        "inventory_reservation",
        "inventory_command_effect",
        "inventory_ledger",
        "chemical",
        # schema v5：物理动作的冻结计划 / 结算回执 + 未结算动作对物料或库位的唯一占用
        "material_execution",
        "material_execution_hold",
        # schema v6：V1.0 库存概念并入 materials.db
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
    },
    "telemetry": {
        "schema_identity",
        "telemetry_source_cursor",
        "device_state_latest",
        "telemetry_event",
    },
    "history": {
        "schema_identity",
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
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # 新库直接按当前声明建成：记为当前版本，不产生迁移记录、不留迁移前备份
        assert connection.execute("PRAGMA user_version").fetchone()[0] == spec.schema_version
    finally:
        connection.close()
    assert not list(tmp_path.glob(f"{spec.filename}*.bak*"))


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
    # 图域并入 materials：material_link（拓扑边）+ lab_graph（命名快照）；
    # workflow / registry 并入 runtime：调度产物与注册表条目版本同库单写者
    # （registry 为条目级版本三表：entry / entry_state / report）；
    # workflow 节点执行为两级：workflow_node_run（节点运行）+ workflow_node_job（attempt）；
    # lab_layout：实验室布局（区域 / 围墙）单行文档；chemical：化学品定义；
    # workflow_publication：工作流发布的不可变修订快照（工作流即节点）。
    # materials：material_execution（物理动作冻结计划与结算回执）+ material_execution_hold
    # （未结算动作对物料 / 库位的唯一占用）；V1.0 库存概念（试剂 / 样品 / 当前内容物角色、
    # 内容物历史、状态历史、内容物预留、来源绑定、工站入站预留、实验室档案 / 分区 / 摆放，
    # V1 命令幂等结果）。
    # runtime：V1 实验操作类别、结构化人工交互、工站错误处置（工站控制 / 控制命令 /
    # 处置会话 / 工站错误）与 Workspace Host（工作区操作记录 / 快照修订号）。
    assert {key: len(spec.table_names) for key, spec in DATABASE_SPECS.items()} == {
        "runtime": 50,
        "materials": 26,
        "telemetry": 4,
        "history": 3,
    }
    assert sum(len(spec.table_names) for spec in DATABASE_SPECS.values()) == 83
