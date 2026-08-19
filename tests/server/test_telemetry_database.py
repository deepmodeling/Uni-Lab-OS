"""``telemetry.db`` 的幂等、原子快照与投影单调性测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

import pytest
from pydantic import ValidationError

from unilabos.server.database.schema import initialize_database
from unilabos.server.database.telemetry import TELEMETRY_DATABASE
from unilabos.server.models.telemetry import (
    DeviceAlarmEventRecord,
    DeviceAlarmRecord,
    TelemetryIngestBatchRecord,
    TelemetryMaintenanceRecord,
)


@pytest.fixture
def telemetry_connection(tmp_path: Path) -> Iterable[sqlite3.Connection]:
    connection = initialize_database(
        tmp_path / TELEMETRY_DATABASE.filename,
        TELEMETRY_DATABASE,
    )
    try:
        yield connection
    finally:
        connection.close()


def _insert_batch(
    connection: sqlite3.Connection,
    *,
    batch_uuid: str,
    endpoint_uuid: str = "endpoint",
    transport: str = "hostlink",
    epoch: str = "epoch-1",
    generation: int = 1,
    sequence: int = 1,
    item_count: int = 1,
    received_at_ms: int = 100,
) -> None:
    connection.execute(
        """
        INSERT INTO telemetry_ingest_batch(
            batch_uuid,source_event_uuid,endpoint_uuid,transport,adapter_epoch,
            epoch_generation,adapter_sequence,status,item_count,payload_hash,
            received_at_ms
        ) VALUES (?,?,?,?,?,?,?,'committed',?,?,?)
        """,
        (
            batch_uuid,
            f"event-{batch_uuid}",
            endpoint_uuid,
            transport,
            epoch,
            generation,
            sequence,
            item_count,
            f"hash-{batch_uuid}",
            received_at_ms,
        ),
    )


def _advance_cursor(
    connection: sqlite3.Connection,
    *,
    batch_uuid: str,
    endpoint_uuid: str = "endpoint",
    transport: str = "hostlink",
    epoch: str = "epoch-1",
    generation: int = 1,
    sequence: int = 1,
    updated_at_ms: int = 100,
    version: int = 1,
) -> None:
    connection.execute(
        """
        INSERT INTO telemetry_source_cursor(
            endpoint_uuid,transport,adapter_epoch,epoch_generation,
            last_adapter_sequence,last_batch_uuid,updated_at_ms,version
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(endpoint_uuid) DO UPDATE SET
            transport=excluded.transport,
            adapter_epoch=excluded.adapter_epoch,
            epoch_generation=excluded.epoch_generation,
            last_adapter_sequence=excluded.last_adapter_sequence,
            last_batch_uuid=excluded.last_batch_uuid,
            updated_at_ms=excluded.updated_at_ms,
            version=excluded.version
        """,
        (
            endpoint_uuid,
            transport,
            epoch,
            generation,
            sequence,
            batch_uuid,
            updated_at_ms,
            version,
        ),
    )


def _insert_report(
    connection: sqlite3.Connection,
    *,
    report_uuid: str,
    batch_uuid: str,
    observed_at_ms: int,
    received_at_ms: int,
    properties: dict[str, tuple[str, str, str]],
    device_uuid: str = "device",
    source_job_uuid: str | None = "job",
) -> None:
    connection.execute(
        """
        INSERT INTO device_state_report(
            report_uuid,batch_uuid,item_index,device_uuid,source_job_uuid,
            report_mode,property_count,state_hash,observed_at_ms,received_at_ms
        ) VALUES (?,?,?,?,?,'full',?,?,?,?)
        """,
        (
            report_uuid,
            batch_uuid,
            0,
            device_uuid,
            source_job_uuid,
            len(properties),
            f"hash-{report_uuid}",
            observed_at_ms,
            received_at_ms,
        ),
    )
    for property_key, (value_type, value_json, value_hash) in properties.items():
        connection.execute(
            """
            INSERT INTO device_property_sample(
                report_uuid,device_uuid,property_key,value_type,value_json,
                value_hash,quality,observed_at_ms,received_at_ms
            ) VALUES (?,?,?,?,?,?,'good',?,?)
            """,
            (
                report_uuid,
                device_uuid,
                property_key,
                value_type,
                value_json,
                value_hash,
                observed_at_ms,
                received_at_ms,
            ),
        )


def _upsert_latest_property(
    connection: sqlite3.Connection,
    *,
    report_uuid: str,
    property_key: str,
    value_type: str,
    value_json: str,
    value_hash: str,
    epoch: str,
    generation: int,
    sequence: int,
    observed_at_ms: int,
    received_at_ms: int,
    version: int,
) -> None:
    connection.execute(
        """
        INSERT INTO device_property_latest(
            device_uuid,property_key,value_type,value_json,value_hash,quality,
            report_uuid,source_endpoint_uuid,source_transport,source_job_uuid,
            adapter_epoch,epoch_generation,adapter_sequence,observed_at_ms,
            received_at_ms,version
        ) VALUES ('device',?,?,?,?, 'good',?,'endpoint','hostlink','job',?,?,?,?,?,?)
        ON CONFLICT(device_uuid,property_key) DO UPDATE SET
            value_type=excluded.value_type,
            value_json=excluded.value_json,
            value_hash=excluded.value_hash,
            quality=excluded.quality,
            report_uuid=excluded.report_uuid,
            source_endpoint_uuid=excluded.source_endpoint_uuid,
            source_transport=excluded.source_transport,
            source_job_uuid=excluded.source_job_uuid,
            adapter_epoch=excluded.adapter_epoch,
            epoch_generation=excluded.epoch_generation,
            adapter_sequence=excluded.adapter_sequence,
            observed_at_ms=excluded.observed_at_ms,
            received_at_ms=excluded.received_at_ms,
            version=excluded.version
        """,
        (
            property_key,
            value_type,
            value_json,
            value_hash,
            report_uuid,
            epoch,
            generation,
            sequence,
            observed_at_ms,
            received_at_ms,
            version,
        ),
    )


def test_complete_state_report_atomically_groups_property_samples(
    telemetry_connection: sqlite3.Connection,
) -> None:
    connection = telemetry_connection
    with connection:
        _insert_batch(connection, batch_uuid="batch-1", item_count=1)
        _insert_report(
            connection,
            report_uuid="report-1",
            batch_uuid="batch-1",
            observed_at_ms=90,
            received_at_ms=100,
            properties={
                "temperature": ("float", "25.0", "hash-temperature"),
                "door_open": ("bool", "false", "hash-door"),
            },
        )
        _upsert_latest_property(
            connection,
            report_uuid="report-1",
            property_key="temperature",
            value_type="float",
            value_json="25.0",
            value_hash="hash-temperature",
            epoch="epoch-1",
            generation=1,
            sequence=1,
            observed_at_ms=90,
            received_at_ms=100,
            version=1,
        )
        _upsert_latest_property(
            connection,
            report_uuid="report-1",
            property_key="door_open",
            value_type="bool",
            value_json="false",
            value_hash="hash-door",
            epoch="epoch-1",
            generation=1,
            sequence=1,
            observed_at_ms=90,
            received_at_ms=100,
            version=1,
        )
        _advance_cursor(connection, batch_uuid="batch-1")

    samples = connection.execute(
        """
        SELECT property_key FROM device_property_sample
        WHERE report_uuid='report-1' ORDER BY property_key
        """
    ).fetchall()
    latest_reports = connection.execute(
        "SELECT DISTINCT report_uuid FROM device_property_latest"
    ).fetchall()
    assert [row[0] for row in samples] == ["door_open", "temperature"]
    assert [row[0] for row in latest_reports] == ["report-1"]

    with pytest.raises(sqlite3.IntegrityError, match="must match report sample"):
        with connection:
            _upsert_latest_property(
                connection,
                report_uuid="report-1",
                property_key="temperature",
                value_type="float",
                value_json="99.0",
                value_hash="wrong-hash",
                epoch="epoch-1",
                generation=1,
                sequence=1,
                observed_at_ms=90,
                received_at_ms=100,
                version=2,
            )


def test_failed_report_transaction_leaves_no_partial_snapshot(
    telemetry_connection: sqlite3.Connection,
) -> None:
    connection = telemetry_connection
    with pytest.raises(sqlite3.IntegrityError):
        with connection:
            _insert_batch(connection, batch_uuid="batch-invalid")
            _insert_report(
                connection,
                report_uuid="report-invalid",
                batch_uuid="batch-invalid",
                observed_at_ms=90,
                received_at_ms=100,
                properties={"temperature": ("float", "25.0", "hash-1")},
            )
            connection.execute(
                """
                INSERT INTO device_property_sample(
                    report_uuid,device_uuid,property_key,value_type,value_json,
                    value_hash,quality,observed_at_ms,received_at_ms
                ) VALUES (
                    'report-invalid','device','temperature','float','25.0',
                    'hash-1','good',90,100
                )
                """
            )

    for table in (
        "telemetry_ingest_batch",
        "device_state_report",
        "device_property_sample",
    ):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_latest_rejects_stale_observation_and_allows_reconnect_sequence_reset(
    telemetry_connection: sqlite3.Connection,
) -> None:
    connection = telemetry_connection
    with connection:
        _insert_batch(
            connection,
            batch_uuid="batch-10",
            sequence=10,
            received_at_ms=200,
        )
        _insert_report(
            connection,
            report_uuid="report-10",
            batch_uuid="batch-10",
            observed_at_ms=190,
            received_at_ms=200,
            properties={"temperature": ("float", "25.0", "hash-25")},
        )
        _upsert_latest_property(
            connection,
            report_uuid="report-10",
            property_key="temperature",
            value_type="float",
            value_json="25.0",
            value_hash="hash-25",
            epoch="epoch-1",
            generation=1,
            sequence=10,
            observed_at_ms=190,
            received_at_ms=200,
            version=1,
        )
        _advance_cursor(
            connection,
            batch_uuid="batch-10",
            sequence=10,
            updated_at_ms=200,
        )

    with pytest.raises(sqlite3.IntegrityError, match="stale telemetry ingest batch"):
        with connection:
            _insert_batch(connection, batch_uuid="batch-9", sequence=9)

    with pytest.raises(sqlite3.IntegrityError, match="cannot move backwards"):
        with connection:
            _insert_batch(
                connection,
                batch_uuid="batch-11-old-time",
                sequence=11,
                received_at_ms=210,
            )
            _insert_report(
                connection,
                report_uuid="report-11-old-time",
                batch_uuid="batch-11-old-time",
                observed_at_ms=180,
                received_at_ms=210,
                properties={"temperature": ("float", "24.0", "hash-24")},
            )
            _upsert_latest_property(
                connection,
                report_uuid="report-11-old-time",
                property_key="temperature",
                value_type="float",
                value_json="24.0",
                value_hash="hash-24",
                epoch="epoch-1",
                generation=1,
                sequence=11,
                observed_at_ms=180,
                received_at_ms=210,
                version=2,
            )

    with connection:
        _insert_batch(
            connection,
            batch_uuid="batch-reconnect",
            epoch="epoch-2",
            generation=2,
            sequence=0,
            received_at_ms=300,
        )
        _insert_report(
            connection,
            report_uuid="report-reconnect",
            batch_uuid="batch-reconnect",
            observed_at_ms=290,
            received_at_ms=300,
            properties={"temperature": ("float", "26.0", "hash-26")},
        )
        _upsert_latest_property(
            connection,
            report_uuid="report-reconnect",
            property_key="temperature",
            value_type="float",
            value_json="26.0",
            value_hash="hash-26",
            epoch="epoch-2",
            generation=2,
            sequence=0,
            observed_at_ms=290,
            received_at_ms=300,
            version=2,
        )
        _advance_cursor(
            connection,
            batch_uuid="batch-reconnect",
            epoch="epoch-2",
            generation=2,
            sequence=0,
            updated_at_ms=300,
            version=2,
        )

    assert tuple(
        connection.execute(
            """
            SELECT value_json,adapter_epoch,epoch_generation,adapter_sequence
            FROM device_property_latest
            WHERE device_uuid='device' AND property_key='temperature'
            """
        ).fetchone()
    ) == ("26.0", "epoch-2", 2, 0)

    # 即使旧 report/sample 仍在保留期内，也不能把 latest 回写到旧 epoch。
    with pytest.raises(sqlite3.IntegrityError, match="cannot move backwards"):
        with connection:
            _upsert_latest_property(
                connection,
                report_uuid="report-10",
                property_key="temperature",
                value_type="float",
                value_json="25.0",
                value_hash="hash-25",
                epoch="epoch-1",
                generation=1,
                sequence=10,
                observed_at_ms=190,
                received_at_ms=200,
                version=3,
            )


def test_connection_latest_is_route_scoped(
    telemetry_connection: sqlite3.Connection,
) -> None:
    connection = telemetry_connection
    with connection:
        for ordinal, (endpoint, transport) in enumerate(
            (("host-endpoint", "hostlink"), ("ros-endpoint", "ros2")),
            start=1,
        ):
            batch_uuid = f"batch-connection-{ordinal}"
            event_uuid = f"connection-{ordinal}"
            _insert_batch(
                connection,
                batch_uuid=batch_uuid,
                endpoint_uuid=endpoint,
                transport=transport,
                sequence=ordinal,
            )
            connection.execute(
                """
                INSERT INTO device_connection_event(
                    event_uuid,batch_uuid,item_index,device_uuid,new_state,
                    observed_at_ms,received_at_ms
                ) VALUES (?,?,0,'device','online',90,100)
                """,
                (event_uuid, batch_uuid),
            )
            connection.execute(
                """
                INSERT INTO device_connection_latest(
                    device_uuid,endpoint_uuid,transport,connection_state,
                    source_event_uuid,adapter_epoch,epoch_generation,
                    adapter_sequence,observed_at_ms,last_seen_at_ms,updated_at_ms
                ) VALUES ('device',?,?,'online',?,'epoch-1',1,?,90,90,100)
                """,
                (endpoint, transport, event_uuid, ordinal),
            )
            _advance_cursor(
                connection,
                batch_uuid=batch_uuid,
                endpoint_uuid=endpoint,
                transport=transport,
                sequence=ordinal,
            )

    routes = connection.execute(
        """
        SELECT endpoint_uuid,transport FROM device_connection_latest
        WHERE device_uuid='device' ORDER BY endpoint_uuid
        """
    ).fetchall()
    assert [tuple(row) for row in routes] == [
        ("host-endpoint", "hostlink"),
        ("ros-endpoint", "ros2"),
    ]


def test_alarm_projection_keeps_append_only_lifecycle(
    telemetry_connection: sqlite3.Connection,
) -> None:
    connection = telemetry_connection
    with connection:
        _insert_batch(connection, batch_uuid="batch-alarm")
        connection.execute(
            """
            INSERT INTO device_alarm_event(
                event_uuid,alarm_uuid,device_uuid,batch_uuid,item_index,
                source_kind,source_job_uuid,event_type,new_state,severity,
                summary,occurred_at_ms,received_at_ms
            ) VALUES (
                'alarm-open','alarm','device','batch-alarm',0,'adapter','job',
                'opened','active','error','overheat',90,100
            )
            """
        )
        connection.execute(
            """
            INSERT INTO device_alarm(
                alarm_uuid,device_uuid,source_endpoint_uuid,source_transport,
                source_job_uuid,alarm_code,severity,state,summary,last_event_uuid,
                opened_at_ms,updated_at_ms
            ) VALUES (
                'alarm','device','endpoint','hostlink','job','OVERHEAT','error',
                'active','overheat','alarm-open',90,100
            )
            """
        )
        connection.execute(
            """
            INSERT INTO device_alarm_event(
                event_uuid,alarm_uuid,device_uuid,source_kind,source_actor_uuid,
                event_type,previous_state,new_state,severity,summary,
                occurred_at_ms,received_at_ms
            ) VALUES (
                'alarm-ack','alarm','device','user','operator','acknowledged',
                'active','acknowledged','error','overheat',110,110
            )
            """
        )
        connection.execute(
            """
            UPDATE device_alarm SET
                state='acknowledged',last_event_uuid='alarm-ack',
                acknowledged_at_ms=110,updated_at_ms=110,version=2
            WHERE alarm_uuid='alarm'
            """
        )

    assert (
        connection.execute(
            "SELECT COUNT(*) FROM device_alarm_event WHERE alarm_uuid='alarm'"
        ).fetchone()[0]
        == 2
    )
    assert (
        connection.execute(
            "SELECT state FROM device_alarm WHERE alarm_uuid='alarm'"
        ).fetchone()[0]
        == "acknowledged"
    )

    with pytest.raises(sqlite3.IntegrityError):
        with connection:
            connection.execute(
                """
                UPDATE device_alarm SET state='cleared',cleared_at_ms=NULL,
                    updated_at_ms=120,version=3
                WHERE alarm_uuid='alarm'
                """
            )


def test_telemetry_models_enforce_batch_alarm_and_retention_invariants() -> None:
    with pytest.raises(ValidationError, match="rejection_reason"):
        TelemetryIngestBatchRecord(
            batch_uuid="batch",
            source_event_uuid="event",
            endpoint_uuid="endpoint",
            transport="hostlink",
            adapter_epoch="epoch",
            epoch_generation=1,
            adapter_sequence=1,
            status="rejected",
            payload_hash="hash",
            received_at_ms=1,
        )

    with pytest.raises(ValidationError, match="requires telemetry batch"):
        DeviceAlarmEventRecord(
            event_uuid="event",
            alarm_uuid="alarm",
            device_uuid="device",
            source_kind="adapter",
            event_type="opened",
            new_state="active",
            severity="warning",
            summary="warning",
            occurred_at_ms=1,
            received_at_ms=1,
        )

    with pytest.raises(ValidationError, match="cleared_at_ms"):
        DeviceAlarmRecord(
            alarm_uuid="alarm",
            device_uuid="device",
            alarm_code="CODE",
            severity="error",
            state="cleared",
            summary="alarm",
            last_event_uuid="event",
            opened_at_ms=1,
            updated_at_ms=2,
        )

    with pytest.raises(ValidationError, match="keep_days or max_rows"):
        TelemetryMaintenanceRecord(
            maintenance_key="device_property_sample",
            updated_at_ms=1,
        )
