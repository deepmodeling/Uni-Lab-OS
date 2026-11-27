"""依据《Sqllite 数据库表分析》新增的表合并迁移 QA。

这些用例刻意从已有回归用例中抽离，验证迁移后的真实数据行为、兼容接口、
失败原子性和不同数据库边界。文档中列出的工作流候选合并也作为硬断言，
如果当前分支只实现了其中一部分，测试应明确失败而不是被标成 xfail。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from tests.persistence.test_table_compaction_requirements import (
    _names,
    _seed_legacy_inventory,
)
from unilabos.app.edge_control.local_authority import LocalEdgeAuthorityStore
from unilabos.app.edge_control.store import EdgeControlStore
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.persistence.schema import objects_for_scope
from unilabos.workflow.models import (
    WorkflowInventoryRequirementWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.store import WorkflowStore

_DOCUMENT_DROPPED_INVENTORY_TABLES = {
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
}

_WORKFLOW_DEFINITION_TABLES = {
    "workflow",
    "workflow_node",
    "workflow_edge",
    "workflow_node_template",
    "workflow_handle_template",
    "workflow_source_registration",
    "workflow_authoring",
    "workflow_inventory_requirement",
}


def _catalog_names(scope: str, object_type: str) -> set[str]:
    catalog = json.loads(
        Path("unilabos/persistence/schema.json").read_text(encoding="utf-8")
    )
    return {
        item["name"] for item in catalog[scope] if item["type"] == object_type
    }


def test_qa_document_inventory_source_tables_are_compacted(tmp_path: Path) -> None:
    """库存旧表全部收口，四个接口兼容视图和独立资源表仍可用。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path)
    store = InventoryStore(str(path))
    try:
        tables = _names(store._conn, "table")
        views = _names(store._conn, "view")
        assert tables.isdisjoint(_DOCUMENT_DROPPED_INVENTORY_TABLES)
        assert {"container_content", "material", "resource_template", "lab_meta"} <= tables
        assert {"lab_zone", "lab_placement"}.isdisjoint(tables)
        assert {
            "material_instance",
            "resource_relation",
            "substance_content",
            "inventory_resource_template",
        } <= views
        assert "station_ingress_reservation_resource" in tables
    finally:
        store.close()


def test_qa_document_inventory_compatibility_views_are_write_through(tmp_path: Path) -> None:
    """旧接口视图的新增、修改、删除必须实际更新合并后的主表。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path)
    store = InventoryStore(str(path))
    try:
        conn = store._conn
        conn.execute(
            """
            INSERT INTO material_instance(
                edge_uuid, legacy_cloud_id, lot_id, template_id, barcode,
                status, version, parent_uuid
            ) VALUES ('compat-material', 'cloud-x', 'lot-x', 'tpl-bare',
                      'BC-X', 'warehouse', 9, '')
            """
        )
        row = conn.execute(
            """
            SELECT legacy_cloud_id, lot_id, aggregate_version
            FROM material WHERE uuid='compat-material'
            """
        ).fetchone()
        assert tuple(row) == ("cloud-x", "lot-x", 9)

        conn.execute(
            """
            UPDATE material_instance
            SET status='consumed', version=10
            WHERE edge_uuid='compat-material'
            """
        )
        assert tuple(conn.execute(
            "SELECT disposition, aggregate_version FROM material WHERE uuid='compat-material'"
        ).fetchone()) == ("consumed", 10)
        conn.execute("DELETE FROM material_instance WHERE edge_uuid='compat-material'")
        assert conn.execute(
            "SELECT deleted_at IS NOT NULL FROM material WHERE uuid='compat-material'"
        ).fetchone()[0] == 1
        conn.commit()
    finally:
        store.close()


def test_qa_document_content_keeps_soft_deleted_history_and_active_uniqueness(
    tmp_path: Path,
) -> None:
    """三类内容合并后保留软删除历史，同时禁止同一物料两个活动内容。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path)
    store = InventoryStore(str(path))
    try:
        conn = store._conn
        assert tuple(conn.execute(
            "SELECT kind, deleted_at FROM container_content WHERE uuid='sample-dead'"
        ).fetchone()) == ("sample", "2026-02-01")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO container_content(
                    uuid, create_time, update_time, material_uuid, kind
                ) VALUES ('second-active', 't', 't', 'mat-bare', 'sample')
                """
            )
    finally:
        store.close()


def test_qa_document_inventory_migration_is_atomic_on_content_conflict(
    tmp_path: Path,
) -> None:
    """内容 UUID 冲突时迁移必须停在旧结构，不能删掉半套源表。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path, colliding_content_uuid=True)
    with pytest.raises(sqlite3.IntegrityError):
        InventoryStore(str(path))
    conn = sqlite3.connect(path)
    try:
        names = _names(conn, "table")
        assert {"sample", "current_substance", "reagent", "material_inventory"} <= names
        assert "container_content" not in names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
    finally:
        conn.close()


def test_qa_document_inventory_compaction_is_idempotent(tmp_path: Path) -> None:
    """重复重启不重复写入内容、不回退版本、不恢复已删除的旧表。"""

    path = tmp_path / "inventory.db"
    _seed_legacy_inventory(path)
    first = InventoryStore(str(path))
    first.close()
    second = InventoryStore(str(path))
    try:
        conn = second._conn
        assert conn.execute(
            "SELECT COUNT(*) FROM container_content"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT aggregate_version FROM material WHERE uuid='mat-full'"
        ).fetchone()[0] == 4
        assert _names(conn, "table").isdisjoint(_DOCUMENT_DROPPED_INVENTORY_TABLES)
        assert {"lab_zone", "lab_placement"}.isdisjoint(_names(conn, "table"))
    finally:
        second.close()


def test_qa_document_debug_command_remains_independent_from_task_projection(
    tmp_path: Path,
) -> None:
    """调试配置并入任务后，调试命令表仍是独立的可审计事实。"""

    store = WorkflowStore(tmp_path / "workflow.db", persist_workflow_definitions=False)
    try:
        tables = store.db.table_names()
        columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(workflow_task)")
        }
        assert "workflow_task_debug_configuration" not in tables
        assert "workflow_task_debug_command" in tables
        assert {
            "debug_create_time",
            "debug_update_time",
            "debug_start_node_uuids",
            "debug_breakpoint_node_uuids",
            "debug_execution_policy",
            "debug_status",
        } <= columns
    finally:
        store.close()


def test_qa_document_workflow_candidate_tables_remain_runtime_tables(
    database_mode, tmp_path: Path
) -> None:
    """本期不改动三张工作流候选表，运行时数据表继续保留。"""

    store = WorkflowStore(tmp_path / "workflow.db", persist_workflow_definitions=False)
    try:
        tables = store.db.table_names()
        assert {
            "job_device_tenancy_transition",
            "workflow_task_material_admission",
            "workflow_inventory_saga",
        } <= tables
    finally:
        store.close()


def test_qa_document_station_outbox_never_accepts_new_events(tmp_path: Path) -> None:
    """旧发件箱停写后，新增事件调用不得隐式重建物理表。"""

    store = WorkflowStore(tmp_path / "workflow.db", persist_workflow_definitions=False)
    try:
        with store.transaction() as conn:
            from unilabos.workflow.station_event_outbox import append_station_event

            append_station_event(
                conn,
                event_type="job.completed",
                aggregate_type="workflow_task",
                aggregate_uuid="task-qa",
                source_kind="dispatch",
                source_uuid="source-qa",
                idempotency_key="idem-qa",
                payload={"ok": True},
            )
            assert "workflow_station_event_outbox" not in _names(conn, "table")
    finally:
        store.close()


def test_qa_document_edge_outcome_fold_keeps_runtime_row(tmp_path: Path) -> None:
    """边缘待上报结果折叠到运行行，清理结果时不能删除运行镜像。"""

    store = EdgeControlStore(str(tmp_path / "edge.db"))
    try:
        job_uuid = "10000000-0000-4000-8000-000000000001"
        store.save_job_start(
            {
                "job_uuid": job_uuid,
                "task_uuid": "10000000-0000-4000-8000-000000000002",
                "node_uuid": "10000000-0000-4000-8000-000000000003",
                "job_access_token": "token",
                "claim_uuid": "10000000-0000-4000-8000-000000000004",
                "attempt": 1,
                "fences": [],
            },
            "10000000-0000-4000-8000-000000000005",
        )
        assert store.save_pending_outcome(
            job_uuid, "failed", {"return": 1}, [{"code": "E_QA"}], ["unknown-1"]
        )
        row = store.db.query_one(
            "SELECT outcome, return_info_json, error_info_json, unknown_command_ids_json "
            "FROM edge_job_runtime WHERE job_uuid=?",
            (job_uuid,),
        )
        assert row["outcome"] == "failed"
        assert json.loads(row["unknown_command_ids_json"]) == ["unknown-1"]
        store.abort_restarted_job(job_uuid)
        assert store.db.query_one(
            "SELECT outcome FROM edge_job_runtime WHERE job_uuid=?", (job_uuid,)
        )[0] is None
        assert "edge_job_outcome_pending" not in store.db.table_names()
    finally:
        store.close()


def test_qa_document_authority_meta_is_single_fixed_row(tmp_path: Path) -> None:
    """权限元数据只有固定 id=1 一行，缺失重启标记保留 NULL。"""

    path = tmp_path / "authority.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE local_edge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO local_edge_meta VALUES ('execution_process_uuid', 'proc-qa');
        INSERT INTO local_edge_meta VALUES ('sequence', '12');
        """
    )
    conn.commit()
    conn.close()
    store = LocalEdgeAuthorityStore(path)
    try:
        row = store._connection.execute("SELECT * FROM local_edge_meta").fetchone()
        assert tuple(row) == (1, "proc-qa", None, 12)
        assert store._connection.execute(
            "SELECT COUNT(*) FROM local_edge_meta"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_qa_document_device_history_deduplicates_and_enforces_limit(tmp_path: Path) -> None:
    """设备状态合并后值不变不追加，并按每个属性的历史上限裁剪。"""

    store = DeviceStateStore(str(tmp_path / "device.db"), max_history_per_key=2)
    try:
        assert store.set("device-qa", "pressure", 1, now_ms=1)
        assert not store.set("device-qa", "pressure", 1, now_ms=2)
        assert store.set("device-qa", "pressure", 2, now_ms=3)
        assert store.set("device-qa", "pressure", 3, now_ms=4)
        rows = store.history("device-qa", "pressure", limit=10)
        assert [item["value"] for item in rows] == [3, 2]
        assert store.latest_for("device-qa")["pressure"]["updated_at"] == 4
        assert "device_property_latest" not in store.db.table_names()
    finally:
        store.close()


def test_qa_document_database_files_remain_separate(tmp_path: Path) -> None:
    """库存、工作流、Edge Control、权限和设备状态仍是五个数据库边界。"""

    stores = [
        InventoryStore(str(tmp_path / "inventory.db")),
        WorkflowStore(tmp_path / "workflow.db", persist_workflow_definitions=False),
        EdgeControlStore(str(tmp_path / "edge.db")),
        LocalEdgeAuthorityStore(tmp_path / "authority.db"),
        DeviceStateStore(str(tmp_path / "device.db")),
    ]
    try:
        paths = {str(store.path) for store in stores if hasattr(store, "path")}
        assert len(paths) >= 4
        assert "material" in stores[0].db.table_names()
        assert "workflow_task" in stores[1].db.table_names()
        assert "edge_job_runtime" in stores[2].db.table_names()
        assert "local_edge_job" in stores[3].db.table_names()
        assert "device_property_history" in stores[4].db.table_names()
    finally:
        for store in reversed(stores):
            store.close()


def test_qa_document_catalog_keeps_history_and_compatibility_views() -> None:
    """工作流历史表、定义表和文档指定四个兼容视图不能被误删。"""

    assert {"workflow_runs", "job_runs"} <= _catalog_names("history", "table")
    assert {
        "material_instance",
        "resource_relation",
        "substance_content",
        "inventory_resource_template",
    } <= _catalog_names("inventory", "view")
    assert "workflow_task_debug_configuration" not in _catalog_names(
        "workflow", "table"
    )


def test_qa_document_selected_database_schema_contract(database_mode, tmp_path: Path) -> None:
    """同一份文档契约在 SQLite 和真实 PostgreSQL 初始化结果中都成立。"""

    inventory = InventoryStore(str(tmp_path / "selected-inventory.db"))
    workflow = WorkflowStore(
        tmp_path / "selected-workflow.db", persist_workflow_definitions=False
    )
    device = DeviceStateStore(str(tmp_path / "selected-device.db"))
    edge = EdgeControlStore(str(tmp_path / "selected-edge.db"))
    authority = LocalEdgeAuthorityStore(tmp_path / "selected-authority.db")
    try:
        assert set(_DOCUMENT_DROPPED_INVENTORY_TABLES).isdisjoint(
            inventory.db.table_names()
        )
        assert {"material", "container_content", "lab_meta"} <= inventory.db.table_names()
        assert {"lab_zone", "lab_placement"}.isdisjoint(inventory.db.table_names())
        assert _WORKFLOW_DEFINITION_TABLES.isdisjoint(workflow.db.table_names())
        assert "workflow_task_debug_configuration" not in workflow.db.table_names()
        assert "workflow_task_debug_command" in workflow.db.table_names()
        assert "device_property_latest" not in device.db.table_names()
        assert "device_property_history" in device.db.table_names()
        assert "edge_job_outcome_pending" not in edge.db.table_names()
        assert "edge_job_runtime" in edge.db.table_names()
        assert "local_edge_meta" in authority.db.table_names()
    finally:
        for store in (authority, edge, device, workflow, inventory):
            store.close()


def test_qa_document_postgresql_runtime_catalog_filters_definition_tables() -> None:
    """PostgreSQL 运行 catalog 不创建定义表，同时保留三张运行事实表。"""

    tables = {
        item["name"]
        for item in objects_for_scope(
            "workflow", persist_workflow_definitions=False
        )
        if item["type"] == "table"
    }
    assert _WORKFLOW_DEFINITION_TABLES.isdisjoint(tables)
    assert {
        "job_device_tenancy_transition",
        "workflow_task_material_admission",
        "workflow_inventory_saga",
    } <= tables


def test_qa_document_definition_store_keeps_definition_tables_in_memory() -> None:
    """定义 Store 仍保留完整定义表，运行 Store 才清理这些物理表。"""

    store = WorkflowStore(":memory:")
    try:
        assert _WORKFLOW_DEFINITION_TABLES <= store.db.table_names()
    finally:
        store.close()


def test_qa_document_legacy_runtime_empty_definition_tables_are_removed(
    tmp_path: Path,
) -> None:
    """旧运行库中的空定义表可清理；运行事实和任务外键保持可恢复。"""

    path = tmp_path / "legacy-runtime.db"
    legacy = WorkflowStore(path)
    legacy.close()

    runtime = WorkflowStore(path, persist_workflow_definitions=False)
    try:
        assert _WORKFLOW_DEFINITION_TABLES.isdisjoint(runtime.db.table_names())
        assert "workflow_task" in runtime.db.table_names()
        assert not any(
            row[2] == "workflow"
            for row in runtime._conn.execute("PRAGMA foreign_key_list(workflow_task)")
        )
    finally:
        runtime.close()

def test_qa_document_legacy_runtime_nonempty_definition_tables_are_preserved(
    tmp_path: Path,
) -> None:
    """旧运行库定义表存在历史数据时不强制删除，避免启动迁移造成数据丢失。"""

    path = tmp_path / "legacy-runtime-with-definition-data.db"
    legacy = WorkflowStore(path)
    workflow_uuid = str(uuid4())
    node_uuid = str(uuid4())
    requirement_uuid = str(uuid4())
    try:
        legacy.create_workflow_with_graph(
            workflow_uuid=workflow_uuid,
            name="legacy definition",
            tags=[],
            description=None,
            meta_data={},
            nodes=[
                WorkflowNodeWrite(
                    uuid=node_uuid,
                    name="consume",
                    type="compute",
                )
            ],
            edges=[],
            inventory_requirements=[
                WorkflowInventoryRequirementWrite(
                    uuid=requirement_uuid,
                    consume_node_uuid=node_uuid,
                    requirement_key="legacy-reagent",
                    target_type="current_substance",
                    required_quantity=1,
                    quantity_unit="mL",
                )
            ],
        )
    finally:
        legacy.close()

    runtime = WorkflowStore(path, persist_workflow_definitions=False)
    try:
        assert _WORKFLOW_DEFINITION_TABLES <= runtime.db.table_names()
        assert runtime._conn.execute(
            "SELECT COUNT(*) FROM workflow_inventory_requirement WHERE uuid = ?",
            (requirement_uuid,),
        ).fetchone()[0] == 1
    finally:
        runtime.close()
