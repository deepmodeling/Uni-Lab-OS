"""materials.db 的对象聚合、占用和账本约束。"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from unilabos.server.database.tables.materials import MATERIALS_DATABASE
from unilabos.server.database.schema import initialize_database
from unilabos.server.database.tables.materials import (
    InventoryReservationRecord,
    MaterialDataRecord,
    MaterialSubstanceRecord,
    ResourceTemplateRecord,
    SiteRecord,
)


def _open(tmp_path) -> sqlite3.Connection:
    return initialize_database(tmp_path / "materials.db", MATERIALS_DATABASE)


def _insert_template(connection: sqlite3.Connection, uuid: str, name: str) -> None:
    connection.execute(
        """
        INSERT INTO resource_template(
            template_uuid,name,display_name,resource_type,template_version,
            category_json,available_sites_json,handles_json,definition_json,
            definition_hash,status,created_at_ms,updated_at_ms
        ) VALUES (
            ?,?,?,'resource','1.0.0','["plate"]',
            '[{"index":0,"label":"A1"}]',
            '[{"key":"top","io_type":"target"}]','{}',?,
            'active',1,1
        )
        """,
        (uuid, name, name, f"hash-{uuid}"),
    )


def _insert_material(
    connection: sqlite3.Connection,
    uuid: str,
    template_uuid: str,
    *,
    parent_uuid: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO material(
            material_uuid,resource_id,template_uuid,parent_material_uuid,name,
            display_name,resource_type,class_name,template_name,lifecycle_status,
            created_at_ms,updated_at_ms
        ) VALUES (?,?,?,?,?,?,'resource','Resource',?,'active',1,1)
        """,
        (uuid, uuid, template_uuid, parent_uuid, uuid, uuid, template_uuid),
    )


def _insert_site(
    connection: sqlite3.Connection,
    uuid: str,
    owner_uuid: str,
    *,
    index: int = 0,
    label: str = "A1",
) -> None:
    connection.execute(
        """
        INSERT INTO site(
            site_uuid,owner_material_uuid,template_name,site_index,label,
            changed_at_ms,created_at_ms,updated_at_ms
        ) VALUES (?,?,'deck',?,?,1,1,1)
        """,
        (uuid, owner_uuid, index, label),
    )


def test_resource_template_fields_are_one_model_and_one_row(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            _insert_template(connection, "tpl", "plate")

        row = connection.execute(
            """
            SELECT
                json_extract(category_json, '$[0]'),
                json_extract(available_sites_json, '$[0].label'),
                json_extract(handles_json, '$[0].key')
            FROM resource_template WHERE template_uuid='tpl'
            """
        ).fetchone()
        assert tuple(row) == ("plate", "A1", "top")

        model = ResourceTemplateRecord(
            template_uuid="tpl",
            name="plate",
            display_name="Plate",
            resource_type="resource",
            template_version="1.0.0",
            category=[" plate ", "PLATE", "container"],
            definition_hash="hash",
            status="active",
            created_at_ms=1,
            updated_at_ms=1,
        )
        assert model.category == ["plate", "container"]

        with pytest.raises(ValidationError, match="duplicated in definition"):
            ResourceTemplateRecord.model_validate(
                {**model.model_dump(), "definition_json": {"category": ["plate"]}}
            )
    finally:
        connection.close()


def test_material_position_data_and_substances_are_separate_records(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            _insert_template(connection, "tpl", "template")
            _insert_material(connection, "material", "tpl")
            connection.execute(
                """
                UPDATE material_position
                SET position_x=1,position_y=2,position_z=3,rotation_z=90,
                    updated_at_ms=2,version=2
                WHERE material_uuid='material'
                """
            )
            connection.execute(
                """
                UPDATE material_data
                SET data_json='{"temperature":25}',sites_initialized=1,
                    state_status='ready',content_version=2,state_hash='hash',
                    source_event_uuid='event-1',observed_at_ms=2,
                    updated_at_ms=2,version=2
                WHERE material_uuid='material'
                """
            )
            connection.executemany(
                """
                INSERT INTO material_substance(
                    substance_uuid,material_uuid,ordinal,name,quantity,
                    quantity_unit,physical_state,composition_json,
                    content_version,observed_at_ms,updated_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    (
                        "water",
                        "material",
                        0,
                        "water",
                        100.0,
                        "ul",
                        "liquid",
                        "[]",
                        2,
                        2,
                        2,
                    ),
                    (
                        "salt",
                        "material",
                        1,
                        "NaCl",
                        5.0,
                        "ug",
                        "solid",
                        '[{"name":"NaCl","ratio":1}]',
                        2,
                        2,
                        2,
                    ),
                ),
            )

        position = connection.execute(
            """
            SELECT position_x,position_y,position_z,rotation_z
            FROM material_position WHERE material_uuid='material'
            """
        ).fetchone()
        data = connection.execute(
            """
            SELECT json_extract(data_json,'$.temperature'),sites_initialized,
                   content_version,source_event_uuid
            FROM material_data WHERE material_uuid='material'
            """
        ).fetchone()
        substances = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT name,quantity,quantity_unit,physical_state
                FROM material_substance
                WHERE material_uuid='material' ORDER BY ordinal
                """
            )
        ]

        assert tuple(position) == (1.0, 2.0, 3.0, 90.0)
        assert tuple(data) == (25, 1, 2, "event-1")
        assert substances == [
            ("water", 100.0, "ul", "liquid"),
            ("NaCl", 5.0, "ug", "solid"),
        ]

        current = MaterialDataRecord(
            material_uuid="material",
            data_json={"temperature": 25},
            substances=[
                MaterialSubstanceRecord(
                    substance_uuid="water",
                    material_uuid="material",
                    ordinal=0,
                    name="water",
                    quantity=100,
                    quantity_unit="ul",
                    content_version=2,
                    observed_at_ms=2,
                    updated_at_ms=2,
                )
            ],
            content_version=2,
            updated_at_ms=2,
        )
        assert [
            (item.name, item.quantity, item.quantity_unit)
            for item in current.substances
        ] == [("water", 100.0, "ul")]
    finally:
        connection.close()


def test_site_category_is_frontend_hint_not_database_admission(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            _insert_template(connection, "owner-tpl", "deck")
            _insert_template(connection, "child-tpl", "tube")
            _insert_material(connection, "owner", "owner-tpl")
            _insert_material(connection, "child", "child-tpl", parent_uuid="owner")
            _insert_site(connection, "site", "owner")
            connection.execute(
                """
                UPDATE site
                SET allowed_resource_categories_json='["plate"]',
                    occupied_material_uuid='child',changed_at_ms=2,
                    updated_at_ms=2,version=2
                WHERE site_uuid='site'
                """
            )

        assert (
            connection.execute(
                "SELECT occupied_material_uuid FROM site WHERE site_uuid='site'"
            ).fetchone()[0]
            == "child"
        )
    finally:
        connection.close()


def test_append_and_move_are_atomic_aggregate_updates(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            for uuid in ("deck-1-tpl", "deck-2-tpl", "child-tpl"):
                _insert_template(connection, uuid, uuid)
            _insert_material(connection, "deck-1", "deck-1-tpl")
            _insert_material(connection, "deck-2", "deck-2-tpl")
            _insert_material(connection, "child", "child-tpl", parent_uuid="deck-1")
            _insert_site(connection, "site-1", "deck-1")
            _insert_site(connection, "site-2", "deck-2")
            connection.execute(
                "UPDATE site SET occupied_material_uuid='child',changed_at_ms=2 "
                "WHERE site_uuid='site-1'"
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "UPDATE site SET occupied_material_uuid='child',changed_at_ms=2 "
                    "WHERE site_uuid='site-2'"
                )

        with connection:
            connection.execute(
                "UPDATE site SET occupied_material_uuid=NULL,changed_at_ms=3 "
                "WHERE site_uuid='site-1'"
            )
            connection.execute(
                "UPDATE material SET parent_material_uuid='deck-2',version=2 "
                "WHERE material_uuid='child'"
            )
            connection.execute(
                "UPDATE site SET occupied_material_uuid='child',changed_at_ms=3 "
                "WHERE site_uuid='site-2'"
            )

        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT site_uuid,occupied_material_uuid FROM site ORDER BY site_uuid"
            )
        ] == [("site-1", None), ("site-2", "child")]
    finally:
        connection.close()


def test_reservation_is_one_backend_job_row_with_items(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO inventory_reservation(
                    reservation_uuid,task_uuid,node_uuid,job_uuid,
                    scheduler_revision,request_hash,items_json,status,
                    created_at_ms,updated_at_ms
                ) VALUES (
                    'reservation','task','node','job',1,'hash',
                    '[{"material_uuid":"material"}]','active',1,1
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO inventory_reservation(
                        reservation_uuid,task_uuid,node_uuid,job_uuid,
                        scheduler_revision,request_hash,items_json,status,
                        created_at_ms,updated_at_ms
                    ) VALUES (
                        'empty','task','node','empty-job',1,'hash','[]','active',1,1
                    )
                    """
                )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO inventory_reservation(
                        reservation_uuid,task_uuid,node_uuid,job_uuid,
                        scheduler_revision,request_hash,items_json,status,
                        created_at_ms,updated_at_ms
                    ) VALUES (
                        'duplicate','task','node','job',1,'hash',
                        '[{"material_uuid":"other"}]','active',1,1
                    )
                    """
                )
    finally:
        connection.close()


def test_ledger_is_also_the_backend_delivery_outbox(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO inventory_command_effect(
                    command_uuid,effect_key,operation,request_json,request_hash,
                    status,started_at_ms,updated_at_ms
                ) VALUES ('command','move','move','{}','hash','applying',1,1)
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
            sequence = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE inventory_command_effect
                SET status='applied',ledger_sequence_start=?,ledger_sequence_end=?,
                    completed_at_ms=2,updated_at_ms=2
                WHERE command_uuid='command' AND effect_key='move'
                """,
                (sequence, sequence),
            )
            connection.execute(
                """
                UPDATE inventory_ledger
                SET delivery_status='acknowledged',acked_at_ms=3
                WHERE event_uuid='event'
                """
            )

        assert tuple(
            connection.execute(
                "SELECT delivery_status,acked_at_ms FROM inventory_ledger"
            ).fetchone()
        ) == ("acknowledged", 3)
    finally:
        connection.close()


def test_material_models_reject_ambiguous_shapes() -> None:
    with pytest.raises(ValidationError, match="boolean"):
        SiteRecord(
            site_uuid="site",
            owner_material_uuid="owner",
            template_name="template",
            site_index=True,
            label="A1",
            changed_at_ms=1,
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
            items=[],
            status="active",
            created_at_ms=1,
            updated_at_ms=1,
        )

    substance = MaterialSubstanceRecord(
        substance_uuid="water",
        material_uuid="other-material",
        ordinal=0,
        name="water",
        quantity=1,
        quantity_unit="ul",
        content_version=1,
        observed_at_ms=1,
        updated_at_ms=1,
    )
    with pytest.raises(ValidationError, match="must match MaterialData owner"):
        MaterialDataRecord(
            material_uuid="material",
            substances=[substance],
            updated_at_ms=1,
        )
