"""history.db 的 payload 与统一追加历史事件测试。"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from unilabos.server.database.tables.history import (
    HISTORY_DATABASE,
    INLINE_PAYLOAD_LIMIT_BYTES,
)
from unilabos.server.database.schema import initialize_database
from unilabos.server.database.tables.history import (
    HistoryEventRecord,
    PayloadObjectRecord,
)


def _open(tmp_path) -> sqlite3.Connection:
    return initialize_database(tmp_path / "history.db", HISTORY_DATABASE)


def test_payload_storage_is_bounded(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO payload_object(
                    payload_uuid,media_type,encoding,byte_length,sha256,
                    storage_kind,inline_payload,created_at_ms
                ) VALUES ('inline','application/json','utf-8',2,'hash','inline',X'7B7D',1)
                """
            )
            connection.execute(
                """
                INSERT INTO payload_object(
                    payload_uuid,media_type,encoding,byte_length,sha256,
                    storage_kind,external_uri,created_at_ms
                ) VALUES (
                    'external','application/octet-stream','binary',300000,'hash-2',
                    'external','s3://bucket/object',1
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO payload_object(
                        payload_uuid,media_type,encoding,byte_length,sha256,
                        storage_kind,inline_payload,created_at_ms
                    ) VALUES (
                        'too-big','application/json','utf-8',?,'hash-3',
                        'inline',X'7B7D',1
                    )
                    """,
                    (INLINE_PAYLOAD_LIMIT_BYTES + 1,),
                )
    finally:
        connection.close()


def test_all_job_history_uses_one_append_stream(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO payload_object(
                    payload_uuid,media_type,encoding,byte_length,sha256,
                    storage_kind,inline_payload,created_at_ms
                ) VALUES ('payload','application/json','utf-8',2,'hash','inline',X'7B7D',1)
                """
            )
            for ordinal, event_type in enumerate(
                (
                    "job_transition",
                    "job_feedback",
                    "job_result",
                    "job_log",
                    "error_snapshot",
                    "decision_audit",
                ),
                start=1,
            ):
                connection.execute(
                    """
                    INSERT INTO history_event(
                        event_uuid,event_type,job_uuid,event_key,payload_uuid,
                        summary_json,occurred_at_ms,recorded_at_ms
                    ) VALUES (?,?,'job',?,'payload','{}',?,?)
                    """,
                    (f"event-{ordinal}", event_type, str(ordinal), ordinal, ordinal),
                )

        assert (
            connection.execute(
                "SELECT COUNT(*) FROM history_event WHERE job_uuid='job'"
            ).fetchone()[0]
            == 6
        )
    finally:
        connection.close()


def test_result_replacement_is_an_append_only_supersedes_link(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO history_event(
                    event_uuid,event_type,job_uuid,state_version,summary_json,
                    occurred_at_ms,recorded_at_ms
                ) VALUES (
                    'result-1','job_result','job',1,'{"result":"original"}',1,1
                )
                """
            )
            connection.execute(
                """
                INSERT INTO history_event(
                    event_uuid,event_type,job_uuid,state_version,summary_json,
                    supersedes_event_uuid,occurred_at_ms,recorded_at_ms
                ) VALUES (
                    'result-2','job_result','job',2,'{"result":"replacement"}',
                    'result-1',2,2
                )
                """
            )

        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT event_uuid,supersedes_event_uuid FROM history_event "
                "ORDER BY state_version"
            )
        ] == [("result-1", None), ("result-2", "result-1")]

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO history_event(
                        event_uuid,event_type,job_uuid,state_version,
                        occurred_at_ms,recorded_at_ms
                    ) VALUES ('fork','job_result','job',2,3,3)
                    """
                )
    finally:
        connection.close()


def test_history_models_preserve_storage_and_time_invariants() -> None:
    with pytest.raises(ValidationError, match="storage shape"):
        PayloadObjectRecord(
            payload_uuid="payload",
            media_type="application/json",
            encoding="utf-8",
            byte_length=2,
            sha256="hash",
            storage_kind="external",
            inline_payload=b"{}",
            external_uri="s3://bucket/object",
            created_at_ms=1,
        )

    with pytest.raises(ValidationError, match="cannot precede"):
        HistoryEventRecord(
            event_uuid="event",
            event_type="job_log",
            occurred_at_ms=2,
            recorded_at_ms=1,
        )
