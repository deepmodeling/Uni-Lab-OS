"""微后端关键控制状态的聚合式 ``runtime.db`` v1 schema。"""

from unilabos.server.database.schema import (
    SCHEMA_MIGRATION_TABLE,
    DatabaseSpec,
    TableSpec,
)


RUNTIME_TABLES = (
    SCHEMA_MIGRATION_TABLE,
    TableSpec(
        "backend_session",
        """
        CREATE TABLE IF NOT EXISTS backend_session (
            session_uuid TEXT PRIMARY KEY CHECK (TRIM(session_uuid) <> ''),
            edge_uuid TEXT NOT NULL CHECK (TRIM(edge_uuid) <> ''),
            backend_uri TEXT NOT NULL CHECK (TRIM(backend_uri) <> ''),
            authority_epoch TEXT NOT NULL CHECK (TRIM(authority_epoch) <> ''),
            connection_epoch TEXT NOT NULL CHECK (TRIM(connection_epoch) <> ''),
            state TEXT NOT NULL CHECK (
                state IN ('connecting','active','reconciling','disconnected')
            ),
            command_cursor INTEGER NOT NULL DEFAULT 0 CHECK (command_cursor >= 0),
            event_send_cursor INTEGER NOT NULL DEFAULT 0
                CHECK (event_send_cursor >= 0),
            event_ack_sequence INTEGER NOT NULL DEFAULT 0
                CHECK (event_ack_sequence >= 0),
            connected_at_ms INTEGER CHECK (connected_at_ms >= 0),
            disconnected_at_ms INTEGER CHECK (disconnected_at_ms >= 0),
            last_seen_at_ms INTEGER NOT NULL CHECK (last_seen_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (event_ack_sequence <= event_send_cursor),
            CHECK (
                (state = 'disconnected' AND disconnected_at_ms IS NOT NULL)
                OR (state <> 'disconnected' AND disconnected_at_ms IS NULL)
            ),
            UNIQUE(edge_uuid, authority_epoch, connection_epoch)
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_backend_session_active_edge
            ON backend_session(edge_uuid) WHERE state = 'active'
            """,
        ),
    ),
    TableSpec(
        "executor_endpoint",
        """
        CREATE TABLE IF NOT EXISTS executor_endpoint (
            endpoint_uuid TEXT PRIMARY KEY CHECK (TRIM(endpoint_uuid) <> ''),
            transport TEXT NOT NULL CHECK (transport IN ('hostlink','ros2')),
            host_uuid TEXT NOT NULL CHECK (TRIM(host_uuid) <> ''),
            instance_name TEXT NOT NULL CHECK (TRIM(instance_name) <> ''),
            authority_epoch TEXT NOT NULL CHECK (TRIM(authority_epoch) <> ''),
            adapter_epoch TEXT,
            adapter_event_cursor INTEGER NOT NULL DEFAULT 0
                CHECK (adapter_event_cursor >= 0),
            reconciliation_generation INTEGER NOT NULL DEFAULT 0
                CHECK (reconciliation_generation >= 0),
            state TEXT NOT NULL CHECK (state IN ('online','offline','reconciling')),
            device_routes_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(device_routes_json)
                AND json_type(device_routes_json) = 'array'
            ),
            action_capabilities_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(action_capabilities_json)
                AND json_type(action_capabilities_json) = 'array'
            ),
            config_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(config_json) AND json_type(config_json) = 'object'
            ),
            snapshot_hash TEXT NOT NULL DEFAULT '',
            registered_at_ms INTEGER NOT NULL CHECK (registered_at_ms >= 0),
            last_seen_at_ms INTEGER NOT NULL CHECK (last_seen_at_ms >= 0),
            reconciled_at_ms INTEGER CHECK (reconciled_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (adapter_epoch IS NULL OR TRIM(adapter_epoch) <> ''),
            UNIQUE(transport, host_uuid, instance_name),
            UNIQUE(endpoint_uuid, transport)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_executor_endpoint_state_seen
            ON executor_endpoint(state, last_seen_at_ms DESC)
            """,
        ),
    ),
    TableSpec(
        "command_inbox",
        """
        CREATE TABLE IF NOT EXISTS command_inbox (
            command_uuid TEXT PRIMARY KEY CHECK (TRIM(command_uuid) <> ''),
            session_uuid TEXT NOT NULL,
            backend_sequence INTEGER NOT NULL CHECK (backend_sequence > 0),
            command_type TEXT NOT NULL CHECK (command_type IN (
                'execute_job','cancel_job','release_failed','replace_result',
                'inventory_apply','reconcile'
            )),
            job_uuid TEXT,
            payload_uuid TEXT,
            payload_sha256 TEXT NOT NULL CHECK (TRIM(payload_sha256) <> ''),
            command_fingerprint TEXT NOT NULL CHECK (TRIM(command_fingerprint) <> ''),
            summary_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(summary_json) AND json_type(summary_json) = 'object'
            ),
            traceparent TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('received','applying','applied','rejected')
            ),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            applied_at_ms INTEGER CHECK (applied_at_ms >= 0),
            error_code TEXT,
            error_message TEXT,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (
                (command_type IN (
                    'execute_job','cancel_job','release_failed','replace_result'
                ) AND job_uuid IS NOT NULL)
                OR command_type = 'inventory_apply'
                OR (command_type = 'reconcile' AND job_uuid IS NULL)
            ),
            CHECK (
                (status IN ('received','applying') AND applied_at_ms IS NULL)
                OR (status IN ('applied','rejected') AND applied_at_ms IS NOT NULL)
            ),
            UNIQUE(session_uuid, backend_sequence),
            FOREIGN KEY(session_uuid) REFERENCES backend_session(session_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_command_inbox_pending
            ON command_inbox(status, received_at_ms, command_uuid)
            WHERE status IN ('received','applying')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_command_inbox_job
            ON command_inbox(job_uuid, received_at_ms) WHERE job_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "execution_job",
        """
        CREATE TABLE IF NOT EXISTS execution_job (
            job_uuid TEXT PRIMARY KEY CHECK (TRIM(job_uuid) <> ''),
            task_uuid TEXT NOT NULL CHECK (TRIM(task_uuid) <> ''),
            node_uuid TEXT NOT NULL CHECK (TRIM(node_uuid) <> ''),
            attempt_group_uuid TEXT NOT NULL CHECK (TRIM(attempt_group_uuid) <> ''),
            retry_of_job_uuid TEXT,
            attempt_no INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
            execute_command_uuid TEXT NOT NULL UNIQUE,
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            action_name TEXT NOT NULL CHECK (TRIM(action_name) <> ''),
            action_payload_uuid TEXT NOT NULL CHECK (TRIM(action_payload_uuid) <> ''),
            route_uuid TEXT,
            endpoint_uuid TEXT,
            transport TEXT CHECK (transport IN ('hostlink','ros2')),
            material_bindings_json TEXT NOT NULL DEFAULT '[]' CHECK (
                json_valid(material_bindings_json)
                AND json_type(material_bindings_json) = 'array'
            ),
            scheduler_revision INTEGER NOT NULL CHECK (scheduler_revision >= 0),
            scheduler_status_version INTEGER NOT NULL DEFAULT 0
                CHECK (scheduler_status_version >= 0),
            status TEXT NOT NULL CHECK (status IN (
                'accepted','dispatch_pending','dispatched','running',
                'failure_waiting','terminal_waiting','succeeded','failed',
                'canceled','execution_unknown','rejected'
            )),
            feedback_sequence INTEGER NOT NULL DEFAULT 0 CHECK (feedback_sequence >= 0),
            job_access_token_ciphertext BLOB,
            token_key_id TEXT,
            result_uuid TEXT,
            error_code TEXT,
            error_summary TEXT,
            terminal_gate_state TEXT NOT NULL DEFAULT 'none' CHECK (
                terminal_gate_state IN (
                    'none','waiting_backend','backend_confirmed','released_failed',
                    'result_replaced','canceled'
                )
            ),
            terminal_error_uuid TEXT,
            terminal_required_scheduler_revision INTEGER
                CHECK (terminal_required_scheduler_revision >= 0),
            terminal_confirmed_scheduler_revision INTEGER
                CHECK (terminal_confirmed_scheduler_revision >= 0),
            terminal_request_event_uuid TEXT,
            terminal_decision_command_uuid TEXT,
            terminal_decision_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(terminal_decision_json)
                AND json_type(terminal_decision_json) = 'object'
            ),
            terminal_opened_at_ms INTEGER CHECK (terminal_opened_at_ms >= 0),
            terminal_resolved_at_ms INTEGER CHECK (terminal_resolved_at_ms >= 0),
            accepted_at_ms INTEGER NOT NULL CHECK (accepted_at_ms >= 0),
            dispatched_at_ms INTEGER CHECK (dispatched_at_ms >= 0),
            started_at_ms INTEGER CHECK (started_at_ms >= 0),
            finished_at_ms INTEGER CHECK (finished_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (retry_of_job_uuid IS NULL OR retry_of_job_uuid <> job_uuid),
            CHECK (
                (retry_of_job_uuid IS NULL AND attempt_no = 1)
                OR (retry_of_job_uuid IS NOT NULL AND attempt_no > 1)
            ),
            CHECK (
                (endpoint_uuid IS NULL AND transport IS NULL AND route_uuid IS NULL)
                OR (endpoint_uuid IS NOT NULL AND transport IS NOT NULL
                    AND route_uuid IS NOT NULL)
            ),
            CHECK (
                (status IN ('succeeded','failed','canceled','rejected')
                    AND finished_at_ms IS NOT NULL)
                OR (status NOT IN ('succeeded','failed','canceled','rejected')
                    AND finished_at_ms IS NULL)
            ),
            CHECK (status <> 'failed' OR terminal_gate_state = 'released_failed'),
            CHECK (
                (terminal_gate_state = 'none'
                    AND terminal_error_uuid IS NULL
                    AND terminal_request_event_uuid IS NULL
                    AND terminal_opened_at_ms IS NULL)
                OR (terminal_gate_state <> 'none'
                    AND terminal_error_uuid IS NOT NULL
                    AND terminal_request_event_uuid IS NOT NULL
                    AND terminal_opened_at_ms IS NOT NULL)
            ),
            CHECK (
                terminal_confirmed_scheduler_revision IS NULL
                OR terminal_required_scheduler_revision IS NULL
                OR terminal_confirmed_scheduler_revision
                    >= terminal_required_scheduler_revision
            ),
            CHECK (
                (terminal_gate_state IN ('waiting_backend','backend_confirmed')
                    AND terminal_resolved_at_ms IS NULL)
                OR (terminal_gate_state IN (
                        'released_failed','result_replaced','canceled'
                    ) AND terminal_resolved_at_ms IS NOT NULL)
                OR terminal_gate_state = 'none'
            ),
            UNIQUE(attempt_group_uuid, attempt_no),
            UNIQUE(task_uuid, node_uuid, attempt_no),
            FOREIGN KEY(execute_command_uuid) REFERENCES command_inbox(command_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(endpoint_uuid, transport)
                REFERENCES executor_endpoint(endpoint_uuid, transport)
                ON DELETE RESTRICT,
            FOREIGN KEY(terminal_decision_command_uuid)
                REFERENCES command_inbox(command_uuid) ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_execution_job_active
            ON execution_job(device_uuid, status, accepted_at_ms)
            WHERE status IN (
                'accepted','dispatch_pending','dispatched','running',
                'failure_waiting','terminal_waiting','execution_unknown'
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_job_retry
            ON execution_job(retry_of_job_uuid) WHERE retry_of_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_job_terminal_waiting
            ON execution_job(terminal_gate_state, terminal_opened_at_ms)
            WHERE terminal_gate_state IN ('waiting_backend','backend_confirmed')
            """,
        ),
    ),
    TableSpec(
        "adapter_command_outbox",
        """
        CREATE TABLE IF NOT EXISTS adapter_command_outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            adapter_command_uuid TEXT NOT NULL UNIQUE
                CHECK (TRIM(adapter_command_uuid) <> ''),
            job_uuid TEXT,
            endpoint_uuid TEXT NOT NULL,
            source_command_uuid TEXT,
            trigger_event_uuid TEXT,
            target_adapter_epoch TEXT,
            command_type TEXT NOT NULL CHECK (command_type IN (
                'execute','cancel','release_failed','replace_result','reconcile_state'
            )),
            payload_uuid TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('pending','sent','acknowledged','failed')
            ),
            delivery_attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_attempt_count >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            available_at_ms INTEGER NOT NULL DEFAULT 0 CHECK (available_at_ms >= 0),
            last_sent_at_ms INTEGER CHECK (last_sent_at_ms >= 0),
            acked_at_ms INTEGER CHECK (acked_at_ms >= 0),
            ack_event_uuid TEXT,
            last_error TEXT,
            CHECK (
                (command_type = 'reconcile_state' AND job_uuid IS NULL
                    AND source_command_uuid IS NULL AND trigger_event_uuid IS NOT NULL)
                OR (command_type <> 'reconcile_state' AND job_uuid IS NOT NULL
                    AND source_command_uuid IS NOT NULL)
            ),
            CHECK (
                (status = 'acknowledged' AND acked_at_ms IS NOT NULL
                    AND ack_event_uuid IS NOT NULL)
                OR (status <> 'acknowledged' AND acked_at_ms IS NULL
                    AND ack_event_uuid IS NULL)
            ),
            UNIQUE(endpoint_uuid, command_type, source_command_uuid),
            FOREIGN KEY(job_uuid) REFERENCES execution_job(job_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(endpoint_uuid) REFERENCES executor_endpoint(endpoint_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(source_command_uuid) REFERENCES command_inbox(command_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_adapter_command_pending
            ON adapter_command_outbox(status, available_at_ms, sequence)
            WHERE status IN ('pending','sent')
            """,
        ),
    ),
    TableSpec(
        "adapter_event_inbox",
        """
        CREATE TABLE IF NOT EXISTS adapter_event_inbox (
            adapter_event_uuid TEXT PRIMARY KEY CHECK (TRIM(adapter_event_uuid) <> ''),
            endpoint_uuid TEXT NOT NULL,
            adapter_epoch TEXT NOT NULL CHECK (TRIM(adapter_epoch) <> ''),
            job_uuid TEXT,
            adapter_command_uuid TEXT,
            adapter_sequence INTEGER NOT NULL CHECK (adapter_sequence >= 0),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'accepted','running','feedback','error_pending','succeeded','failed',
                'canceled','endpoint_ready','endpoint_snapshot','endpoint_offline',
                'command_ack'
            )),
            payload_uuid TEXT,
            payload_sha256 TEXT NOT NULL CHECK (TRIM(payload_sha256) <> ''),
            status TEXT NOT NULL CHECK (
                status IN ('received','processing','processed','rejected')
            ),
            occurred_at_ms INTEGER CHECK (occurred_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            processed_at_ms INTEGER CHECK (processed_at_ms >= 0),
            error_message TEXT,
            CHECK (
                (status IN ('received','processing') AND processed_at_ms IS NULL)
                OR (status IN ('processed','rejected') AND processed_at_ms IS NOT NULL)
            ),
            UNIQUE(endpoint_uuid, adapter_epoch, adapter_sequence),
            FOREIGN KEY(endpoint_uuid) REFERENCES executor_endpoint(endpoint_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_adapter_event_unprocessed
            ON adapter_event_inbox(endpoint_uuid, adapter_epoch, status, adapter_sequence)
            WHERE status IN ('received','processing')
            """,
        ),
    ),
    TableSpec(
        "backend_event_outbox",
        """
        CREATE TABLE IF NOT EXISTS backend_event_outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            event_type TEXT NOT NULL CHECK (TRIM(event_type) <> ''),
            aggregate_type TEXT NOT NULL CHECK (TRIM(aggregate_type) <> ''),
            aggregate_uuid TEXT NOT NULL CHECK (TRIM(aggregate_uuid) <> ''),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
            job_uuid TEXT,
            summary_json TEXT NOT NULL DEFAULT '{}' CHECK (
                json_valid(summary_json) AND json_type(summary_json) = 'object'
            ),
            detail_payload_uuid TEXT,
            traceparent TEXT,
            tracestate TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('pending','sent','acknowledged','dead_letter')
            ),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            available_at_ms INTEGER NOT NULL CHECK (available_at_ms >= 0),
            last_sent_at_ms INTEGER CHECK (last_sent_at_ms >= 0),
            acked_at_ms INTEGER CHECK (acked_at_ms >= 0),
            delivery_attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_attempt_count >= 0),
            last_error TEXT,
            CHECK (
                (status = 'acknowledged' AND acked_at_ms IS NOT NULL)
                OR (status <> 'acknowledged' AND acked_at_ms IS NULL)
            ),
            UNIQUE(aggregate_type, aggregate_uuid, aggregate_version, event_type),
            FOREIGN KEY(job_uuid) REFERENCES execution_job(job_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_backend_event_pending
            ON backend_event_outbox(status, available_at_ms, sequence)
            WHERE status IN ('pending','sent')
            """,
        ),
    ),
)


RUNTIME_DATABASE = DatabaseSpec(
    key="runtime",
    filename="runtime.db",
    role="critical microbackend command and execution control",
    version=1,
    synchronous="FULL",
    tables=RUNTIME_TABLES,
)


__all__ = ["RUNTIME_DATABASE", "RUNTIME_TABLES"]
