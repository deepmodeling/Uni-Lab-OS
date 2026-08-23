"""``history.db`` 的不可变 v1 SQLite migration snapshot。"""

from unilabos.server.database.schema import (
    SCHEMA_MIGRATION_TABLE,
    DatabaseSpec,
    TableSpec,
)


INLINE_PAYLOAD_LIMIT_BYTES = 262_144


HISTORY_TABLES = (
    SCHEMA_MIGRATION_TABLE,
    TableSpec(
        "payload_object",
        f"""
        CREATE TABLE IF NOT EXISTS payload_object (
            payload_uuid TEXT PRIMARY KEY CHECK (TRIM(payload_uuid) <> ''),
            media_type TEXT NOT NULL CHECK (TRIM(media_type) <> ''),
            encoding TEXT NOT NULL CHECK (TRIM(encoding) <> ''),
            compression TEXT,
            byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
            sha256 TEXT NOT NULL CHECK (TRIM(sha256) <> ''),
            storage_kind TEXT NOT NULL CHECK (storage_kind IN ('inline','external')),
            inline_payload BLOB,
            external_uri TEXT,
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            expires_at_ms INTEGER CHECK (expires_at_ms >= created_at_ms),
            CHECK (
                (storage_kind = 'inline' AND inline_payload IS NOT NULL
                    AND external_uri IS NULL
                    AND length(inline_payload) = byte_length
                    AND byte_length <= {INLINE_PAYLOAD_LIMIT_BYTES})
                OR (storage_kind = 'external' AND inline_payload IS NULL
                    AND external_uri IS NOT NULL AND TRIM(external_uri) <> '')
            )
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_payload_expiry
            ON payload_object(expires_at_ms, payload_uuid)
            WHERE expires_at_ms IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "history_event",
        """
        CREATE TABLE IF NOT EXISTS history_event (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'job_transition','action_availability','job_feedback','job_result',
                'job_log','error_snapshot','decision_audit'
            )),
            job_uuid TEXT,
            endpoint_uuid TEXT,
            device_uuid TEXT,
            action_name TEXT,
            event_key TEXT,
            job_sequence INTEGER CHECK (job_sequence >= 0),
            state_version INTEGER CHECK (state_version > 0),
            payload_uuid TEXT,
            summary_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(summary_json) AND json_type(summary_json) = 'object'
            ),
            severity TEXT,
            actor_type TEXT,
            actor_uuid TEXT,
            supersedes_event_uuid TEXT,
            occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
            recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0),
            CHECK (event_key IS NULL OR TRIM(event_key) <> ''),
            CHECK (recorded_at_ms >= occurred_at_ms),
            FOREIGN KEY(payload_uuid) REFERENCES payload_object(payload_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(supersedes_event_uuid) REFERENCES history_event(event_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_history_event_job
            ON history_event(job_uuid, occurred_at_ms, sequence)
            WHERE job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_history_event_type_time
            ON history_event(event_type, occurred_at_ms, sequence)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_history_event_job_sequence
            ON history_event(job_uuid, event_type, job_sequence)
            WHERE job_uuid IS NOT NULL AND job_sequence IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_history_event_state_version
            ON history_event(job_uuid, event_type, state_version)
            WHERE job_uuid IS NOT NULL AND state_version IS NOT NULL
            """,
        ),
    ),
)


HISTORY_DATABASE = DatabaseSpec(
    key="history",
    filename="history.db",
    role="large payload and append-only audit history",
    version=1,
    synchronous="NORMAL",
    tables=HISTORY_TABLES,
)


__all__ = ["HISTORY_DATABASE", "HISTORY_TABLES", "INLINE_PAYLOAD_LIMIT_BYTES"]
