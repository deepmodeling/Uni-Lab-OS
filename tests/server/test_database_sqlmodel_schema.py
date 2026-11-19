"""验证 SQLModel 行映射与声明式 SQLite schema 的结构一致性。"""

from __future__ import annotations

import pytest
from sqlmodel import Session, create_engine, select

from unilabos.server.database import DATABASE_SPECS, initialize_database
from unilabos.server.database.tables import DATABASE_TABLE_MODELS
from unilabos.server.database.tables.telemetry import TelemetryEventRecord


@pytest.mark.parametrize("database_key", tuple(DATABASE_SPECS))
def test_sqlmodel_tables_match_declared_sqlite_schema(
    database_key: str, tmp_path
) -> None:
    """每张表的名称、字段顺序和复合主键与数据库声明一致。"""

    spec = DATABASE_SPECS[database_key]
    models = DATABASE_TABLE_MODELS[database_key]
    connection = initialize_database(tmp_path / spec.filename, spec)
    try:
        assert tuple(model.__table__.name for model in models) == spec.table_names
        for model in models:
            orm_table = model.__table__
            rows = connection.execute(
                f"PRAGMA table_info({orm_table.name})"
            ).fetchall()
            assert tuple(column.name for column in orm_table.columns) == tuple(
                str(row["name"]) for row in rows
            )
            assert tuple(column.name for column in orm_table.primary_key.columns) == tuple(
                str(row["name"])
                for row in sorted(rows, key=lambda row: int(row["pk"]))
                if int(row["pk"]) > 0
            )
    finally:
        connection.close()


def test_every_declared_database_has_a_sqlmodel_table_set() -> None:
    assert DATABASE_TABLE_MODELS.keys() == DATABASE_SPECS.keys()


def test_sqlmodel_can_round_trip_json_against_initialized_schema(tmp_path) -> None:
    """ORM 可读写初始化后的表，JSON TEXT 对调用方表现为 Python 对象。"""

    spec = DATABASE_SPECS["telemetry"]
    path = tmp_path / spec.filename
    initialize_database(path, spec).close()
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with Session(engine) as session:
            session.add(
                TelemetryEventRecord(
                    event_uuid="event",
                    endpoint_uuid="endpoint",
                    source_epoch="epoch",
                    source_generation=0,
                    source_sequence=1,
                    event_type="property_sample",
                    payload={"temperature": 25},
                    payload_hash="hash",
                    observed_at_ms=1,
                    received_at_ms=1,
                )
            )
            session.commit()
        with Session(engine) as session:
            row = session.exec(select(TelemetryEventRecord)).one()
            assert row.sequence == 1
            assert row.payload == {"temperature": 25}
    finally:
        engine.dispose()
