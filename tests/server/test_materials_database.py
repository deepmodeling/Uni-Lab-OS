"""materials.db 的独立表级约束与恢复语义测试。"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from unilabos.server.database.materials import MATERIALS_DATABASE
from unilabos.server.database.schema import initialize_database
from unilabos.server.models.materials import (
    InventoryCommandEffectRecord,
    InventoryReservationRecord,
    MaterialPoseRecord,
    SiteRecord,
)


def _open_materials(tmp_path) -> sqlite3.Connection:
    return initialize_database(tmp_path / "materials.db", MATERIALS_DATABASE)


def _insert_template(
    connection: sqlite3.Connection,
    template_uuid: str,
    name: str,
) -> None:
    connection.execute(
        """
        INSERT INTO resource_template(
            template_uuid,name,display_name,resource_type,template_version,
            definition_json,definition_hash,status,created_at_ms,updated_at_ms
        ) VALUES (?,?,?,'resource','1.0.0','{}',?,'active',1,1)
        """,
        (template_uuid, name, name, f"hash-{template_uuid}"),
    )


def _insert_material(
    connection: sqlite3.Connection,
    material_uuid: str,
    template_uuid: str,
    *,
    name: str | None = None,
    parent_material_uuid: str | None = None,
) -> None:
    material_name = name or material_uuid
    connection.execute(
        """
        INSERT INTO material(
            material_uuid,resource_id,template_uuid,parent_material_uuid,name,
            resource_type,class_name,template_name,lifecycle_status,
            created_at_ms,updated_at_ms
        ) VALUES (?,?,?,?,?,'resource','Resource',?,'active',1,1)
        """,
        (
            material_uuid,
            material_uuid,
            template_uuid,
            parent_material_uuid,
            material_name,
            template_uuid,
        ),
    )


def _insert_site(
    connection: sqlite3.Connection,
    site_uuid: str,
    owner_material_uuid: str,
    template_name: str,
    *,
    label: str = "A1",
) -> None:
    connection.execute(
        """
        INSERT INTO site(
            site_uuid,owner_material_uuid,template_name,site_index,label,
            occupancy_changed_at_ms,created_at_ms,updated_at_ms
        ) VALUES (?,?,?,0,?,1,1,1)
        """,
        (site_uuid, owner_material_uuid, template_name, label),
    )


def test_materials_schema_covers_canonical_resource_boundaries(tmp_path) -> None:
    connection = _open_materials(tmp_path)
    try:
        assert set(MATERIALS_DATABASE.table_names) == {
            "schema_migration",
            "resource_template",
            "resource_template_category",
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
        }
        site_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(site)").fetchall()
        }
        assert "content_type" not in site_columns
        assert "allowed_resource_template_uuids" not in site_columns
        assert "site_template_uuid" not in site_columns
        assert {
            "pose_json",
            "allowed_resource_categories_json",
            "occupied_material_uuid",
        } <= site_columns

        state_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(material_state)")
        }
        assert "sites_initialized" in state_columns
        assert "source_event_uuid" in state_columns
        assert "joint_state_json" not in state_columns
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_site_category_is_only_a_frontend_hint(tmp_path) -> None:
    connection = _open_materials(tmp_path)
    try:
        with connection:
            _insert_template(connection, "tpl-owner", "owner")
            _insert_template(connection, "tpl-occupant", "occupant")
            _insert_material(connection, "owner", "tpl-owner")
            _insert_material(connection, "occupant", "tpl-occupant")
            connection.execute(
                "UPDATE material SET parent_material_uuid='owner',version=2 "
                "WHERE material_uuid='occupant'"
            )
            _insert_site(
                connection,
                "site-1",
                "owner",
                "tpl-owner",
            )
            connection.execute(
                "UPDATE site SET allowed_resource_categories_json='[\"plate\"]' "
                "WHERE site_uuid='site-1'"
            )
            # occupant 没有 plate category；数据库仍允许放置。
            connection.execute(
                """
                UPDATE site
                SET occupied_material_uuid='occupant', version=2,
                    occupancy_changed_at_ms=2, updated_at_ms=2
                WHERE site_uuid='site-1'
                """
            )

        assert tuple(
            connection.execute(
                "SELECT occupied_material_uuid,allowed_resource_categories_json "
                "FROM site WHERE site_uuid='site-1'"
            ).fetchone()
        ) == ("occupant", '["plate"]')
    finally:
        connection.close()


def test_append_and_move_keep_a_versioned_occupancy_projection(tmp_path) -> None:
    connection = _open_materials(tmp_path)
    try:
        with connection:
            _insert_template(connection, "tpl-deck-1", "deck-1")
            _insert_template(connection, "tpl-deck-2", "deck-2")
            _insert_template(connection, "tpl-child", "child")
            _insert_material(connection, "deck-1", "tpl-deck-1")
            _insert_material(connection, "deck-2", "tpl-deck-2")
            _insert_material(connection, "child", "tpl-child")
            _insert_site(
                connection,
                "site-1",
                "deck-1",
                "tpl-deck-1",
            )
            _insert_site(
                connection,
                "site-2",
                "deck-2",
                "tpl-deck-2",
            )
            connection.execute(
                "UPDATE material SET parent_material_uuid='deck-1', version=2 "
                "WHERE material_uuid='child'"
            )
            connection.execute(
                """
                INSERT INTO material_pose(
                    material_uuid,frame_kind,frame_site_uuid,updated_at_ms
                ) VALUES ('child','site','site-1',2)
                """
            )
            connection.execute(
                """
                UPDATE site
                SET occupied_material_uuid='child',version=2,
                    occupancy_changed_by_command_uuid='append-command',
                    occupancy_changed_at_ms=2,updated_at_ms=2
                WHERE site_uuid='site-1'
                """
            )

        # 不先 clear 源 Site 时，同一物料不能出现在第二个 Site。
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    UPDATE site
                    SET occupied_material_uuid='child',version=2,
                        occupancy_changed_at_ms=3,updated_at_ms=3
                    WHERE site_uuid='site-2'
                    """
                )

        with connection:
            connection.execute(
                """
                UPDATE site
                SET occupied_material_uuid=NULL,version=3,
                    occupancy_changed_by_command_uuid='move-command',
                    occupancy_changed_at_ms=3,updated_at_ms=3
                WHERE site_uuid='site-1'
                """
            )
            connection.execute(
                "UPDATE material SET parent_material_uuid='deck-2',version=3 "
                "WHERE material_uuid='child'"
            )
            connection.execute(
                """
                UPDATE site
                SET occupied_material_uuid='child',version=2,
                    occupancy_changed_by_command_uuid='move-command',
                    occupancy_changed_at_ms=3,updated_at_ms=3
                WHERE site_uuid='site-2'
                """
            )
            connection.execute(
                """
                UPDATE material_pose
                SET frame_site_uuid='site-2',version=2,updated_at_ms=3
                WHERE material_uuid='child'
                """
            )

        rows = connection.execute(
            "SELECT site_uuid,occupied_material_uuid,version FROM site "
            "ORDER BY site_uuid"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("site-1", None, 3),
            ("site-2", "child", 2),
        ]

        with pytest.raises(sqlite3.IntegrityError, match="material tree cycle"):
            with connection:
                connection.execute(
                    "UPDATE material SET parent_material_uuid='child' "
                    "WHERE material_uuid='deck-2'"
                )
    finally:
        connection.close()


def test_material_state_source_event_is_a_durable_idempotency_key(tmp_path) -> None:
    connection = _open_materials(tmp_path)
    try:
        with connection:
            _insert_template(connection, "tpl", "template")
            _insert_material(connection, "material", "tpl")
            connection.execute(
                """
                INSERT INTO material_state_source_event(
                    source_event_uuid,material_uuid,source_kind,state_hash,
                    applied_content_version,observed_at_ms,received_at_ms
                ) VALUES ('event-1','material','adapter_report','hash-1',1,1,2)
                """
            )
            connection.execute(
                """
                INSERT INTO material_state(
                    material_uuid,status,sites_initialized,state_hash,
                    source_event_uuid,observed_at_ms,updated_at_ms
                ) VALUES ('material','ready',1,'hash-1','event-1',1,2)
                """
            )

        row = connection.execute(
            "SELECT sites_initialized,source_event_uuid FROM material_state"
        ).fetchone()
        assert tuple(row) == (1, "event-1")

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO material_state_source_event(
                        source_event_uuid,material_uuid,source_kind,state_hash,
                        applied_content_version,observed_at_ms,received_at_ms
                    ) VALUES ('event-1','material','adapter_report','hash-1',1,1,3)
                    """
                )
    finally:
        connection.close()


def test_root_names_and_active_handles_have_stable_business_keys(tmp_path) -> None:
    connection = _open_materials(tmp_path)
    try:
        with connection:
            _insert_template(connection, "tpl", "template")
            _insert_material(connection, "root-1", "tpl", name="Deck")
            connection.execute(
                """
                INSERT INTO resource_handle_template(
                    handle_uuid,template_uuid,handle_key,label,io_type,data_type,
                    definition_hash,created_at_ms,updated_at_ms
                ) VALUES ('handle-1','tpl','top','Top','target','fluid','hash-1',1,1)
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                _insert_material(connection, "root-2", "tpl", name="deck")

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO resource_handle_template(
                        handle_uuid,template_uuid,handle_key,label,io_type,data_type,
                        definition_hash,created_at_ms,updated_at_ms
                    ) VALUES (
                        'handle-2','tpl','top','Top v2','target','fluid','hash-2',2,2
                    )
                    """
                )

        with connection:
            connection.execute(
                "UPDATE resource_handle_template SET deleted_at_ms=3,updated_at_ms=3 "
                "WHERE handle_uuid='handle-1'"
            )
            connection.execute(
                """
                INSERT INTO resource_handle_template(
                    handle_uuid,template_uuid,handle_key,label,io_type,data_type,
                    definition_hash,created_at_ms,updated_at_ms
                ) VALUES ('handle-2','tpl','top','Top v2','target','fluid','hash-2',3,3)
                """
            )
    finally:
        connection.close()


def test_reservation_is_one_backend_job_aggregate(tmp_path) -> None:
    connection = _open_materials(tmp_path)
    try:
        with connection:
            for suffix in ("1", "2"):
                connection.execute(
                    """
                    INSERT INTO inventory_reservation(
                        reservation_uuid,task_uuid,node_uuid,job_uuid,
                        scheduler_revision,request_hash,items_json,status,
                        created_at_ms,updated_at_ms
                    ) VALUES (?, 'task','node',?,1,?,?,'active',1,1)
                    """,
                    (
                        f"reservation-{suffix}",
                        f"job-{suffix}",
                        f"hash-{suffix}",
                        f'[{{"kind":"material","material_uuid":"material-{suffix}"}}]',
                    ),
                )

        # retry 使用新 job，因此可以拥有自己的原子 reservation。
        assert (
            connection.execute("SELECT COUNT(*) FROM inventory_reservation").fetchone()[
                0
            ]
            == 2
        )

        # 同一 job 不能产生第二份 reservation。
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO inventory_reservation(
                        reservation_uuid,task_uuid,node_uuid,job_uuid,
                        scheduler_revision,request_hash,items_json,status,
                        created_at_ms,updated_at_ms
                    ) VALUES (
                        'reservation-duplicate','task','node','job-1',1,
                        'hash-duplicate','[{"kind":"material"}]','active',1,1
                    )
                    """
                )

        # reservation 必须包含至少一个 item，明细由 JSON 快照整体更新。
        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO inventory_reservation(
                        reservation_uuid,task_uuid,node_uuid,job_uuid,
                        scheduler_revision,request_hash,items_json,status,
                        created_at_ms,updated_at_ms
                    ) VALUES (
                        'reservation-empty','task','node','job-empty',1,
                        'hash-empty','[]','active',1,1
                    )
                    """
                )
    finally:
        connection.close()


def test_command_ledger_and_outbox_form_one_replay_chain(tmp_path) -> None:
    connection = _open_materials(tmp_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO inventory_command_effect(
                    command_uuid,effect_key,operation,request_json,request_hash,
                    status,started_at_ms,updated_at_ms
                ) VALUES (
                    'command','move','move_resource','{}','request-hash',
                    'applying',1,1
                )
                """
            )
            cursor = connection.execute(
                """
                INSERT INTO inventory_ledger(
                    event_uuid,aggregate_type,aggregate_uuid,operation,
                    previous_version,aggregate_version,state_hash,delta_json,
                    command_uuid,effect_key,actor_type,occurred_at_ms
                ) VALUES (
                    'event','site','site','move',0,1,'state-hash','{}',
                    'command','move','backend',2
                )
                """
            )
            ledger_sequence = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO inventory_event_outbox(event_uuid,created_at_ms) "
                "VALUES ('event',2)"
            )
            connection.execute(
                """
                UPDATE inventory_command_effect
                SET status='applied',ledger_sequence_start=?,ledger_sequence_end=?,
                    updated_at_ms=2,completed_at_ms=2
                WHERE command_uuid='command' AND effect_key='move'
                """,
                (ledger_sequence, ledger_sequence),
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO inventory_ledger(
                        event_uuid,aggregate_type,aggregate_uuid,operation,
                        previous_version,aggregate_version,state_hash,delta_json,
                        actor_type,occurred_at_ms
                    ) VALUES (
                        'fork','site','site','move',0,1,'other','{}','backend',3
                    )
                    """
                )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "INSERT INTO inventory_event_outbox(event_uuid,created_at_ms) "
                    "VALUES ('missing-ledger-event',3)"
                )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO inventory_command_effect(
                        command_uuid,effect_key,operation,request_json,request_hash,
                        status,started_at_ms,updated_at_ms
                    ) VALUES (
                        'command','move','move_resource','{}','different-hash',
                        'applying',3,3
                    )
                    """
                )
    finally:
        connection.close()


def test_models_reject_ambiguous_site_and_effect_shapes() -> None:
    with pytest.raises(ValidationError, match="boolean"):
        SiteRecord(
            site_uuid="site",
            owner_material_uuid="owner",
            template_name="template",
            site_index=True,
            label="A1",
            occupancy_changed_at_ms=1,
            created_at_ms=1,
            updated_at_ms=1,
        )

    with pytest.raises(ValidationError, match="at least one item"):
        InventoryReservationRecord(
            reservation_uuid="reservation",
            task_uuid="task",
            node_uuid="node",
            job_uuid="job",
            scheduler_revision=1,
            request_hash="hash",
            items_json=[],
            status="active",
            created_at_ms=1,
            updated_at_ms=1,
        )

    with pytest.raises(ValidationError, match="frame reference"):
        MaterialPoseRecord(
            material_uuid="material",
            frame_kind="site",
            updated_at_ms=1,
        )

    with pytest.raises(ValidationError, match="ledger range"):
        InventoryCommandEffectRecord(
            command_uuid="command",
            effect_key="move",
            operation="move_resource",
            request_json={},
            request_hash="hash",
            status="applied",
            started_at_ms=1,
            updated_at_ms=2,
            completed_at_ms=2,
        )
