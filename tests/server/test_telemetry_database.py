"""telemetry.db 的设备快照和统一事件流测试。"""

from __future__ import annotations

import sqlite3

import pytest

from unilabos.server.database.schema import initialize_database
from unilabos.server.database.telemetry import TELEMETRY_DATABASE
from unilabos.server.models.telemetry import DeviceStateLatestRecord


def _open(tmp_path) -> sqlite3.Connection:
    return initialize_database(tmp_path / "telemetry.db", TELEMETRY_DATABASE)


def test_device_status_is_one_latest_aggregate(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO device_state_latest(
                    endpoint_uuid,device_uuid,source_event_uuid,source_epoch,
                    source_generation,source_sequence,state_json,properties_json,
                    connection_state,alarms_json,state_hash,
                    observed_at_ms,received_at_ms
                ) VALUES (
                    'endpoint','device','event','epoch',1,1,
                    '{"status":"idle"}','{"temperature":25}','online',
                    '[{"code":"warning"}]','hash',1,2
                )
                """
            )

        row = connection.execute(
            """
            SELECT json_extract(state_json,'$.status'),
                   json_extract(properties_json,'$.temperature'),
                   connection_state,json_extract(alarms_json,'$[0].code')
            FROM device_state_latest
            """
        ).fetchone()
        assert tuple(row) == ("idle", 25, "online", "warning")

        model = DeviceStateLatestRecord(
            endpoint_uuid="endpoint",
            device_uuid="device",
            source_event_uuid="event",
            source_epoch="epoch",
            source_generation=1,
            source_sequence=1,
            state={"status": "idle"},
            properties={"temperature": 25},
            connection_state="online",
            alarms=[{"code": "warning"}],
            state_hash="hash",
            observed_at_ms=1,
            received_at_ms=2,
        )
        assert model.properties["temperature"] == 25
    finally:
        connection.close()


def test_latest_rejects_stale_sequence_but_new_epoch_can_restart(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO device_state_latest(
                    endpoint_uuid,device_uuid,source_event_uuid,source_epoch,
                    source_generation,source_sequence,state_hash,
                    observed_at_ms,received_at_ms
                ) VALUES ('endpoint','device','event-2','epoch-1',1,2,'hash-2',2,2)
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="stale device state"):
            with connection:
                connection.execute(
                    """
                    UPDATE device_state_latest
                    SET source_event_uuid='event-1',source_sequence=1,state_hash='hash-1'
                    WHERE endpoint_uuid='endpoint' AND device_uuid='device'
                    """
                )

        with connection:
            connection.execute(
                """
                UPDATE device_state_latest
                SET source_event_uuid='event-new',source_epoch='epoch-2',
                    source_sequence=0,state_hash='hash-new'
                WHERE endpoint_uuid='endpoint' AND device_uuid='device'
                """
            )
        assert tuple(
            connection.execute(
                "SELECT source_epoch,source_sequence FROM device_state_latest"
            ).fetchone()
        ) == ("epoch-2", 0)
    finally:
        connection.close()


def test_all_high_frequency_history_uses_one_event_stream(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            for sequence, event_type in enumerate(
                ("state", "property_sample", "connection", "alarm"), start=1
            ):
                connection.execute(
                    """
                    INSERT INTO telemetry_event(
                        event_uuid,endpoint_uuid,device_uuid,source_epoch,
                        source_generation,source_sequence,event_type,payload_json,
                        payload_hash,observed_at_ms,received_at_ms
                    ) VALUES (?, 'endpoint','device','epoch',1,?,?, '{}',?,1,1)
                    """,
                    (f"event-{sequence}", sequence, event_type, f"hash-{sequence}"),
                )

        assert [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM telemetry_event ORDER BY sequence"
            )
        ] == ["state", "property_sample", "connection", "alarm"]

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO telemetry_event(
                        event_uuid,endpoint_uuid,source_epoch,source_generation,
                        source_sequence,event_type,payload_json,payload_hash,
                        observed_at_ms,received_at_ms
                    ) VALUES (
                        'event-1','endpoint','epoch',1,9,'state','{}','hash',1,1
                    )
                    """
                )
    finally:
        connection.close()
