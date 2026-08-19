"""history.db 的追加、幂等和归档边界测试。"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from unilabos.server.database.history import (
    HISTORY_DATABASE,
    MAX_INLINE_PAYLOAD_BYTES,
)
from unilabos.server.database.schema import initialize_database
from unilabos.server.models.history import (
    DecisionAuditRecord,
    HistoryMaintenanceRecord,
    JobLogRecord,
    JobResultRecord,
    PayloadObjectRecord,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def history(tmp_path):
    connection = initialize_database(tmp_path / "history.db", HISTORY_DATABASE)
    try:
        yield connection
    finally:
        connection.close()


def _insert_inline_payload(
    connection: sqlite3.Connection,
    payload_uuid: str,
    data: bytes = b"{}",
) -> None:
    connection.execute(
        """
        INSERT INTO payload_object(
            payload_uuid,payload_kind,media_type,codec,storage_kind,size_bytes,
            sha256,inline_data,retention_class,created_at_ms
        ) VALUES (?, 'test', 'application/json', 'identity', 'inline', ?, ?, ?,
            'job', 1)
        """,
        (payload_uuid, len(data), SHA_A, data),
    )


def test_payload_storage_is_bounded_and_content_addressable(history) -> None:
    with history:
        _insert_inline_payload(history, "inline")
        history.execute(
            """
            INSERT INTO payload_object(
                payload_uuid,payload_kind,media_type,codec,storage_kind,
                size_bytes,sha256,external_uri,retention_class,created_at_ms
            ) VALUES (
                'external','result','application/octet-stream','identity',
                'external',1048576,?,'s3://bucket/sha256/b','archive',1
            )
            """,
            (SHA_B,),
        )

    with pytest.raises(sqlite3.IntegrityError):
        with history:
            history.execute(
                """
                INSERT INTO payload_object(
                    payload_uuid,payload_kind,media_type,codec,storage_kind,
                    size_bytes,sha256,inline_data,retention_class,created_at_ms
                ) VALUES ('too-large','log','text/plain','identity','inline',
                    ?,?,zeroblob(?),'short',1)
                """,
                (
                    MAX_INLINE_PAYLOAD_BYTES + 1,
                    SHA_A,
                    MAX_INLINE_PAYLOAD_BYTES + 1,
                ),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with history:
            history.execute(
                """
                INSERT INTO payload_object(
                    payload_uuid,payload_kind,media_type,codec,storage_kind,
                    size_bytes,sha256,inline_data,retention_class,
                    expires_at_ms,created_at_ms
                ) VALUES ('bad-expiry','log','text/plain','identity','inline',
                    0,?,x'','short',1,2)
                """,
                (SHA_A,),
            )


def test_job_transition_replay_is_idempotent_by_job_version(history) -> None:
    insert_sql = """
        INSERT INTO job_transition(
            event_uuid,job_uuid,job_version,from_status,to_status,source,
            source_event_uuid,occurred_at_ms,recorded_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """
    with history:
        history.execute(
            insert_sql,
            (
                "transition-1",
                "job-1",
                2,
                "dispatched",
                "running",
                "adapter",
                "adapter-event-1",
                10,
                11,
            ),
        )

    # 重放即使生成了另一个 history event UUID，也不能制造第二个 job version。
    with pytest.raises(sqlite3.IntegrityError):
        with history:
            history.execute(
                insert_sql,
                (
                    "transition-replay",
                    "job-1",
                    2,
                    "dispatched",
                    "running",
                    "adapter",
                    "adapter-event-1",
                    10,
                    12,
                ),
            )


def test_action_snapshot_replay_has_source_and_projection_versions(history) -> None:
    insert_sql = """
        INSERT INTO action_availability_event(
            event_uuid,endpoint_uuid,device_uuid,action_name,
            availability_version,from_state,to_state,active_job_uuid,source,
            source_event_uuid,discovery_epoch,discovery_generation,
            observed_at_ms,recorded_at_ms
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    with history:
        history.execute(
            insert_sql,
            (
                "availability-1",
                "endpoint-1",
                "device-1",
                "transfer",
                3,
                "free",
                "busy",
                None,
                "adapter_report",
                "snapshot-9",
                "adapter-epoch-1",
                4,
                10,
                11,
            ),
        )

    # 旧 HostLink 报告没有 job UUID；busy 历史仍然有效。
    assert tuple(
        history.execute(
            "SELECT to_state,active_job_uuid FROM action_availability_event"
        ).fetchone()
    ) == ("busy", None)

    with pytest.raises(sqlite3.IntegrityError):
        with history:
            history.execute(
                insert_sql,
                (
                    "availability-replay",
                    "endpoint-1",
                    "device-1",
                    "transfer",
                    4,
                    "free",
                    "busy",
                    None,
                    "adapter_report",
                    "snapshot-9",
                    "adapter-epoch-1",
                    4,
                    10,
                    12,
                ),
            )


def test_feedback_and_log_stream_positions_deduplicate_replay(history) -> None:
    with history:
        _insert_inline_payload(history, "feedback-payload")
        history.execute(
            """
            INSERT INTO job_feedback(
                feedback_uuid,job_uuid,feedback_sequence,feedback_type,
                source_event_uuid,payload_uuid,observed_at_ms,received_at_ms,
                recorded_at_ms
            ) VALUES ('feedback-1','job-1',1,'progress','event-1',
                'feedback-payload',1,2,2)
            """
        )
        history.execute(
            """
            INSERT INTO job_log(
                log_uuid,job_uuid,stream_uuid,stream_sequence,level,message,
                occurred_at_ms,recorded_at_ms
            ) VALUES ('log-1','job-1','adapter-log-stream',8,'info','running',1,2)
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        with history:
            history.execute(
                """
                INSERT INTO job_feedback(
                    feedback_uuid,job_uuid,feedback_sequence,feedback_type,
                    source_event_uuid,payload_uuid,observed_at_ms,received_at_ms,
                    recorded_at_ms
                ) VALUES ('feedback-replay','job-1',1,'progress','event-replay',
                    'feedback-payload',1,2,3)
                """
            )

    with pytest.raises(sqlite3.IntegrityError):
        with history:
            history.execute(
                """
                INSERT INTO job_log(
                    log_uuid,job_uuid,stream_uuid,stream_sequence,level,message,
                    occurred_at_ms,recorded_at_ms
                ) VALUES ('log-replay','job-1','adapter-log-stream',8,
                    'info','running',1,3)
                """
            )


def test_result_versions_preserve_manual_replacement_lineage(history) -> None:
    with history:
        history.execute(
            """
            INSERT INTO job_result(
                result_uuid,job_uuid,result_version,result_origin,outcome,
                source_event_uuid,result_hash,committed_at_ms
            ) VALUES ('result-1','job-1',1,'adapter','failed','event-failed',
                'hash-1',10)
            """
        )
        history.execute(
            """
            INSERT INTO job_result(
                result_uuid,job_uuid,result_version,result_origin,outcome,
                supersedes_result_uuid,supersedes_result_version,decision_uuid,
                result_hash,committed_at_ms
            ) VALUES ('result-2','job-1',2,'manual_replacement','succeeded',
                'result-1',1,'decision-1','hash-2',20)
            """
        )
        history.execute(
            """
            INSERT INTO decision_audit(
                audit_uuid,decision_uuid,gate_uuid,job_uuid,actor_type,actor_uuid,
                action,scheduler_revision,request_fingerprint,
                replacement_result_uuid,replacement_result_version,
                occurred_at_ms,recorded_at_ms
            ) VALUES ('audit-1','decision-1','gate-1','job-1','human','user-1',
                'replace_result',7,'request-hash','result-2',2,20,21)
            """
        )

    assert [
        tuple(row)
        for row in history.execute(
            """
            SELECT result_uuid,result_version,supersedes_result_uuid
            FROM job_result WHERE job_uuid='job-1' ORDER BY result_version
            """
        ).fetchall()
    ] == [
        ("result-1", 1, None),
        ("result-2", 2, "result-1"),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        with history:
            history.execute(
                """
                INSERT INTO job_result(
                    result_uuid,job_uuid,result_version,result_origin,outcome,
                    source_event_uuid,result_hash,committed_at_ms
                ) VALUES ('duplicate-version','job-1',2,'adapter','succeeded',
                    'different-event','hash',30)
                """
            )

    # 替换审计不能把另一个 job 的结果挂到当前 gate。
    with history:
        history.execute(
            """
            INSERT INTO job_result(
                result_uuid,job_uuid,result_version,result_origin,outcome,
                source_event_uuid,result_hash,committed_at_ms
            ) VALUES ('other-result','job-2',1,'adapter','succeeded',
                'other-event','hash',40)
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        with history:
            history.execute(
                """
                INSERT INTO decision_audit(
                    audit_uuid,decision_uuid,gate_uuid,job_uuid,actor_type,
                    action,scheduler_revision,request_fingerprint,
                    replacement_result_uuid,replacement_result_version,
                    occurred_at_ms,recorded_at_ms
                ) VALUES ('bad-audit','bad-decision','gate-2','job-1','human',
                    'replace_result',8,'request-hash-2','other-result',1,40,41)
                """
            )


def test_history_has_no_cross_database_foreign_keys(history) -> None:
    allowed_targets = {"payload_object", "job_result"}
    for table_name in HISTORY_DATABASE.table_names:
        foreign_targets = {
            row[2]
            for row in history.execute(
                f'PRAGMA foreign_key_list("{table_name}")'
            ).fetchall()
        }
        assert foreign_targets <= allowed_targets

    transition_columns = {
        row[1] for row in history.execute("PRAGMA table_info(job_transition)")
    }
    feedback_columns = {
        row[1] for row in history.execute("PRAGMA table_info(job_feedback)")
    }
    assert "is_current" not in transition_columns
    assert "published_at_ms" not in feedback_columns


def test_history_models_match_storage_and_retention_invariants() -> None:
    payload = PayloadObjectRecord(
        payload_uuid="payload",
        payload_kind="result",
        media_type="application/json",
        codec="identity",
        storage_kind="inline",
        size_bytes=2,
        sha256=SHA_A,
        inline_data=b"{}",
        retention_class="job",
        created_at_ms=1,
    )
    assert payload.inline_data == b"{}"

    with pytest.raises(ValidationError, match="prior version"):
        JobResultRecord(
            result_uuid="result-2",
            job_uuid="job",
            result_version=2,
            result_origin="manual_replacement",
            outcome="succeeded",
            decision_uuid="decision",
            result_hash="hash",
            committed_at_ms=1,
        )

    with pytest.raises(ValidationError, match="set together"):
        JobLogRecord(
            log_uuid="log",
            stream_uuid="stream",
            level="info",
            message="message",
            occurred_at_ms=1,
            recorded_at_ms=1,
        )

    with pytest.raises(ValidationError, match="replacement result identity"):
        DecisionAuditRecord(
            audit_uuid="audit",
            decision_uuid="decision",
            gate_uuid="gate",
            job_uuid="job",
            actor_type="backend",
            action="replace_result",
            scheduler_revision=1,
            request_fingerprint="fingerprint",
            occurred_at_ms=1,
            recorded_at_ms=1,
        )

    with pytest.raises(ValidationError, match="archive_uri_prefix"):
        HistoryMaintenanceRecord(
            dataset_key="job_log",
            retention_action="archive_then_delete",
            keep_days=30,
            updated_at_ms=1,
        )

    maintenance = HistoryMaintenanceRecord(
        dataset_key="job_log",
        retention_action="archive_then_delete",
        keep_days=30,
        archive_uri_prefix="s3://archive/history/job-log",
        maintenance_state="deleting",
        inflight_run_uuid="archive-run-1",
        inflight_cutoff_at_ms=100,
        inflight_archive_uri="s3://archive/history/job-log/100.parquet",
        inflight_archive_sha256=SHA_B,
        updated_at_ms=101,
    )
    assert maintenance.inflight_archive_sha256 == SHA_B
