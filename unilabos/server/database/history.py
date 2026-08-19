"""执行历史与大 payload 的 ``history.db`` v1 schema。

本库只保存可追加、可归档的事实。job 当前状态、调度决策状态和消息投递状态
分别由 runtime/backend 持有，不能把 history 当作控制面恢复来源。
"""

from unilabos.server.database.schema import (
    SCHEMA_MIGRATION_TABLE,
    DatabaseSpec,
    TableSpec,
)


# 避免大量二进制内容进入 SQLite/WAL；更大的对象必须先写入外部对象存储。
MAX_INLINE_PAYLOAD_BYTES = 256 * 1024

JOB_STATUS_CHECK = """
    'accepted','dispatch_pending','dispatched','running',
    'failure_waiting','terminal_waiting','succeeded','failed',
    'canceled','execution_unknown','rejected'
"""


HISTORY_TABLES = (
    SCHEMA_MIGRATION_TABLE,
    TableSpec(
        "payload_object",
        f"""
        CREATE TABLE IF NOT EXISTS payload_object (
            payload_uuid TEXT PRIMARY KEY CHECK (TRIM(payload_uuid) <> ''),
            payload_kind TEXT NOT NULL CHECK (TRIM(payload_kind) <> ''),
            media_type TEXT NOT NULL CHECK (TRIM(media_type) <> ''),
            codec TEXT NOT NULL CHECK (TRIM(codec) <> ''),
            storage_kind TEXT NOT NULL CHECK (storage_kind IN ('inline','external')),
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            sha256 TEXT NOT NULL CHECK (
                length(sha256) = 64
                AND sha256 = lower(sha256)
                AND sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            inline_data BLOB,
            external_uri TEXT,
            retention_class TEXT NOT NULL CHECK (TRIM(retention_class) <> ''),
            expires_at_ms INTEGER CHECK (expires_at_ms >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            CHECK (
                (storage_kind = 'inline'
                    AND inline_data IS NOT NULL
                    AND external_uri IS NULL
                    AND size_bytes <= {MAX_INLINE_PAYLOAD_BYTES})
                OR
                (storage_kind = 'external'
                    AND inline_data IS NULL
                    AND external_uri IS NOT NULL
                    AND TRIM(external_uri) <> '')
            ),
            CHECK (inline_data IS NULL OR length(inline_data) = size_bytes),
            CHECK (expires_at_ms IS NULL OR expires_at_ms >= created_at_ms)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_payload_object_digest
            ON payload_object(sha256, size_bytes)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_payload_object_expiry
            ON payload_object(expires_at_ms, payload_uuid)
            WHERE expires_at_ms IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "job_transition",
        f"""
        CREATE TABLE IF NOT EXISTS job_transition (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            job_uuid TEXT NOT NULL CHECK (TRIM(job_uuid) <> ''),
            job_version INTEGER NOT NULL CHECK (job_version > 0),
            from_status TEXT CHECK (from_status IN ({JOB_STATUS_CHECK})),
            to_status TEXT NOT NULL CHECK (to_status IN ({JOB_STATUS_CHECK})),
            source TEXT NOT NULL CHECK (TRIM(source) <> ''),
            command_uuid TEXT,
            source_event_uuid TEXT,
            payload_uuid TEXT,
            occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            UNIQUE(job_uuid, job_version),
            CHECK (from_status IS NULL OR from_status <> to_status),
            FOREIGN KEY(payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_job_transition_timeline
            ON job_transition(job_uuid, occurred_at_ms, sequence)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_job_transition_retention
            ON job_transition(occurred_at_ms, sequence)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_job_transition_source_event
            ON job_transition(job_uuid, source_event_uuid, to_status)
            WHERE source_event_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "action_availability_event",
        """
        CREATE TABLE IF NOT EXISTS action_availability_event (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            endpoint_uuid TEXT NOT NULL CHECK (TRIM(endpoint_uuid) <> ''),
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            action_name TEXT NOT NULL CHECK (TRIM(action_name) <> ''),
            availability_version INTEGER NOT NULL CHECK (availability_version > 0),
            from_state TEXT CHECK (from_state IN ('free','busy','unknown')),
            to_state TEXT NOT NULL CHECK (to_state IN ('free','busy','unknown')),
            active_job_uuid TEXT,
            source TEXT NOT NULL CHECK (TRIM(source) <> ''),
            source_event_uuid TEXT NOT NULL CHECK (TRIM(source_event_uuid) <> ''),
            discovery_epoch TEXT NOT NULL CHECK (TRIM(discovery_epoch) <> ''),
            discovery_generation INTEGER NOT NULL CHECK (discovery_generation >= 0),
            payload_uuid TEXT,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            UNIQUE(endpoint_uuid, device_uuid, action_name, availability_version),
            UNIQUE(
                endpoint_uuid, source_event_uuid, device_uuid, action_name
            ),
            CHECK (to_state <> 'free' OR active_job_uuid IS NULL),
            FOREIGN KEY(payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_action_availability_timeline
            ON action_availability_event(
                endpoint_uuid, device_uuid, action_name, observed_at_ms, sequence
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_action_availability_retention
            ON action_availability_event(observed_at_ms, sequence)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_action_availability_job
            ON action_availability_event(active_job_uuid, observed_at_ms, sequence)
            WHERE active_job_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "job_feedback",
        """
        CREATE TABLE IF NOT EXISTS job_feedback (
            feedback_uuid TEXT PRIMARY KEY CHECK (TRIM(feedback_uuid) <> ''),
            job_uuid TEXT NOT NULL CHECK (TRIM(job_uuid) <> ''),
            feedback_sequence INTEGER NOT NULL CHECK (feedback_sequence > 0),
            feedback_type TEXT NOT NULL CHECK (TRIM(feedback_type) <> ''),
            source_event_uuid TEXT NOT NULL CHECK (TRIM(source_event_uuid) <> ''),
            payload_uuid TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            UNIQUE(job_uuid, feedback_sequence),
            UNIQUE(job_uuid, source_event_uuid),
            FOREIGN KEY(payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_job_feedback_timeline
            ON job_feedback(job_uuid, observed_at_ms, feedback_sequence)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_job_feedback_retention
            ON job_feedback(observed_at_ms, job_uuid, feedback_sequence)
            """,
        ),
    ),
    TableSpec(
        "job_result",
        """
        CREATE TABLE IF NOT EXISTS job_result (
            result_uuid TEXT PRIMARY KEY CHECK (TRIM(result_uuid) <> ''),
            job_uuid TEXT NOT NULL CHECK (TRIM(job_uuid) <> ''),
            result_version INTEGER NOT NULL CHECK (result_version > 0),
            result_origin TEXT NOT NULL CHECK (result_origin IN (
                'adapter','failure_release','manual_replacement'
            )),
            outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','canceled')),
            supersedes_result_uuid TEXT,
            supersedes_result_version INTEGER CHECK (supersedes_result_version > 0),
            source_event_uuid TEXT,
            decision_uuid TEXT,
            return_payload_uuid TEXT,
            error_payload_uuid TEXT,
            summary_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(summary_json)),
            result_hash TEXT NOT NULL CHECK (TRIM(result_hash) <> ''),
            committed_at_ms INTEGER NOT NULL CHECK (committed_at_ms >= 0),
            UNIQUE(job_uuid, result_version),
            UNIQUE(result_uuid, job_uuid, result_version),
            CHECK (supersedes_result_uuid IS NULL OR supersedes_result_uuid <> result_uuid),
            CHECK (
                (result_version = 1
                    AND supersedes_result_uuid IS NULL
                    AND supersedes_result_version IS NULL)
                OR
                (result_version > 1
                    AND supersedes_result_uuid IS NOT NULL
                    AND supersedes_result_version = result_version - 1)
            ),
            CHECK (
                (result_origin = 'adapter'
                    AND source_event_uuid IS NOT NULL
                    AND decision_uuid IS NULL)
                OR
                (result_origin IN ('failure_release','manual_replacement')
                    AND decision_uuid IS NOT NULL)
            ),
            FOREIGN KEY(
                supersedes_result_uuid, job_uuid, supersedes_result_version
            ) REFERENCES job_result(result_uuid, job_uuid, result_version)
                ON DELETE RESTRICT,
            FOREIGN KEY(return_payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(error_payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_job_result_timeline
            ON job_result(job_uuid, committed_at_ms, result_version)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_job_result_committed
            ON job_result(committed_at_ms, result_uuid)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_job_result_source_event
            ON job_result(job_uuid, source_event_uuid)
            WHERE source_event_uuid IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_job_result_decision
            ON job_result(decision_uuid)
            WHERE decision_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "job_log",
        """
        CREATE TABLE IF NOT EXISTS job_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(log_uuid) <> ''),
            job_uuid TEXT,
            endpoint_uuid TEXT,
            device_uuid TEXT,
            stream_uuid TEXT,
            stream_sequence INTEGER CHECK (stream_sequence >= 0),
            level TEXT NOT NULL CHECK (
                level IN ('debug','info','warning','error','critical')
            ),
            logger_name TEXT,
            message TEXT NOT NULL CHECK (TRIM(message) <> ''),
            context_payload_uuid TEXT,
            occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            CHECK ((stream_uuid IS NULL) = (stream_sequence IS NULL)),
            FOREIGN KEY(context_payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_job_log_stream_position
            ON job_log(stream_uuid, stream_sequence)
            WHERE stream_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_job_log_job_timeline
            ON job_log(job_uuid, occurred_at_ms, log_id) WHERE job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_job_log_device_timeline
            ON job_log(device_uuid, occurred_at_ms, log_id)
            WHERE device_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_job_log_retention
            ON job_log(occurred_at_ms, log_id)
            """,
        ),
    ),
    TableSpec(
        "error_snapshot",
        """
        CREATE TABLE IF NOT EXISTS error_snapshot (
            error_uuid TEXT PRIMARY KEY CHECK (TRIM(error_uuid) <> ''),
            job_uuid TEXT NOT NULL CHECK (TRIM(job_uuid) <> ''),
            gate_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(gate_uuid) <> ''),
            source_event_uuid TEXT NOT NULL CHECK (TRIM(source_event_uuid) <> ''),
            error_type TEXT NOT NULL CHECK (TRIM(error_type) <> ''),
            error_code TEXT,
            message TEXT NOT NULL CHECK (TRIM(message) <> ''),
            stack_payload_uuid TEXT,
            device_state_payload_uuid TEXT,
            action_context_payload_uuid TEXT,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            UNIQUE(job_uuid, source_event_uuid),
            FOREIGN KEY(stack_payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(device_state_payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(action_context_payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_error_snapshot_job
            ON error_snapshot(job_uuid, observed_at_ms)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_error_snapshot_retention
            ON error_snapshot(observed_at_ms, error_uuid)
            """,
        ),
    ),
    TableSpec(
        "decision_audit",
        """
        CREATE TABLE IF NOT EXISTS decision_audit (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(audit_uuid) <> ''),
            decision_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(decision_uuid) <> ''),
            gate_uuid TEXT NOT NULL CHECK (TRIM(gate_uuid) <> ''),
            job_uuid TEXT NOT NULL CHECK (TRIM(job_uuid) <> ''),
            actor_type TEXT NOT NULL CHECK (TRIM(actor_type) <> ''),
            actor_uuid TEXT,
            action TEXT NOT NULL CHECK (action IN ('release_failed','replace_result')),
            scheduler_revision INTEGER CHECK (scheduler_revision >= 0),
            request_fingerprint TEXT NOT NULL CHECK (TRIM(request_fingerprint) <> ''),
            replacement_result_uuid TEXT,
            replacement_result_version INTEGER CHECK (replacement_result_version > 0),
            before_payload_uuid TEXT,
            after_payload_uuid TEXT,
            occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            CHECK (
                (action = 'replace_result'
                    AND replacement_result_uuid IS NOT NULL
                    AND replacement_result_version IS NOT NULL)
                OR
                (action = 'release_failed'
                    AND scheduler_revision IS NOT NULL
                    AND replacement_result_uuid IS NULL
                    AND replacement_result_version IS NULL)
            ),
            FOREIGN KEY(
                replacement_result_uuid, job_uuid, replacement_result_version
            ) REFERENCES job_result(result_uuid, job_uuid, result_version)
                ON DELETE RESTRICT,
            FOREIGN KEY(before_payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(after_payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_decision_audit_job
            ON decision_audit(job_uuid, occurred_at_ms, sequence)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_decision_audit_gate
            ON decision_audit(gate_uuid, occurred_at_ms, sequence)
            """,
        ),
    ),
    TableSpec(
        "history_maintenance",
        """
        CREATE TABLE IF NOT EXISTS history_maintenance (
            dataset_key TEXT PRIMARY KEY CHECK (TRIM(dataset_key) <> ''),
            retention_action TEXT NOT NULL CHECK (
                retention_action IN ('delete','archive_then_delete')
            ),
            keep_days INTEGER CHECK (keep_days > 0),
            max_size_bytes INTEGER CHECK (max_size_bytes > 0),
            archive_uri_prefix TEXT,
            watermark_occurred_at_ms INTEGER NOT NULL DEFAULT 0
                CHECK (watermark_occurred_at_ms >= 0),
            maintenance_state TEXT NOT NULL DEFAULT 'idle' CHECK (
                maintenance_state IN ('idle','archiving','deleting','failed')
            ),
            inflight_run_uuid TEXT,
            inflight_cutoff_at_ms INTEGER CHECK (inflight_cutoff_at_ms >= 0),
            inflight_archive_uri TEXT,
            inflight_archive_sha256 TEXT CHECK (
                inflight_archive_sha256 IS NULL
                OR (
                    length(inflight_archive_sha256) = 64
                    AND inflight_archive_sha256 = lower(inflight_archive_sha256)
                    AND inflight_archive_sha256 NOT GLOB '*[^0-9a-f]*'
                )
            ),
            last_completed_run_uuid TEXT,
            last_completed_at_ms INTEGER CHECK (last_completed_at_ms >= 0),
            last_error TEXT,
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (keep_days IS NOT NULL OR max_size_bytes IS NOT NULL),
            CHECK (
                (retention_action = 'archive_then_delete'
                    AND archive_uri_prefix IS NOT NULL
                    AND TRIM(archive_uri_prefix) <> '')
                OR
                (retention_action = 'delete' AND archive_uri_prefix IS NULL)
            ),
            CHECK (
                (maintenance_state = 'idle'
                    AND inflight_run_uuid IS NULL
                    AND inflight_cutoff_at_ms IS NULL
                    AND inflight_archive_uri IS NULL
                    AND inflight_archive_sha256 IS NULL)
                OR
                (maintenance_state <> 'idle'
                    AND inflight_run_uuid IS NOT NULL
                    AND TRIM(inflight_run_uuid) <> ''
                    AND inflight_cutoff_at_ms IS NOT NULL)
            ),
            CHECK (
                (retention_action = 'delete'
                    AND inflight_archive_uri IS NULL
                    AND inflight_archive_sha256 IS NULL)
                OR
                (retention_action = 'archive_then_delete'
                    AND (maintenance_state = 'idle'
                        OR (inflight_archive_uri IS NOT NULL
                            AND TRIM(inflight_archive_uri) <> '')))
            ),
            CHECK (
                retention_action <> 'archive_then_delete'
                OR maintenance_state <> 'deleting'
                OR inflight_archive_sha256 IS NOT NULL
            ),
            CHECK (
                (maintenance_state = 'failed' AND last_error IS NOT NULL
                    AND TRIM(last_error) <> '')
                OR (maintenance_state <> 'failed' AND last_error IS NULL)
            )
        )
        """,
    ),
)


HISTORY_DATABASE = DatabaseSpec(
    key="history",
    filename="history.db",
    role="append-only execution history and large payload storage",
    version=1,
    synchronous="NORMAL",
    tables=HISTORY_TABLES,
)


__all__ = [
    "HISTORY_DATABASE",
    "HISTORY_TABLES",
    "MAX_INLINE_PAYLOAD_BYTES",
]
