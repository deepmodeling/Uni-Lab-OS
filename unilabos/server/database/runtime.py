"""微后端关键控制状态的 ``runtime.db`` v1 schema。"""

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
            connection_epoch TEXT NOT NULL DEFAULT 'initial'
                CHECK (TRIM(connection_epoch) <> ''),
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
            capabilities_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(capabilities_json)),
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
        "device_route",
        """
        CREATE TABLE IF NOT EXISTS device_route (
            route_uuid TEXT PRIMARY KEY CHECK (TRIM(route_uuid) <> ''),
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            endpoint_uuid TEXT NOT NULL,
            transport TEXT NOT NULL CHECK (transport IN ('hostlink','ros2')),
            driver_key TEXT NOT NULL CHECK (TRIM(driver_key) <> ''),
            priority INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
            selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0,1)),
            config_hash TEXT NOT NULL CHECK (TRIM(config_hash) <> ''),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (selected = 0 OR enabled = 1),
            UNIQUE(device_uuid, endpoint_uuid),
            UNIQUE(route_uuid, device_uuid, endpoint_uuid, transport),
            FOREIGN KEY(endpoint_uuid, transport)
                REFERENCES executor_endpoint(endpoint_uuid, transport)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_device_route_selected
            ON device_route(device_uuid) WHERE selected = 1
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_route_endpoint
            ON device_route(endpoint_uuid, enabled, priority DESC)
            """,
        ),
    ),
    TableSpec(
        "device_action_capability",
        """
        CREATE TABLE IF NOT EXISTS device_action_capability (
            capability_uuid TEXT PRIMARY KEY CHECK (TRIM(capability_uuid) <> ''),
            endpoint_uuid TEXT NOT NULL,
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            action_name TEXT NOT NULL CHECK (TRIM(action_name) <> ''),
            action_type TEXT,
            concurrency_mode TEXT NOT NULL CHECK (
                concurrency_mode IN ('exclusive','unbounded')
            ),
            state TEXT NOT NULL CHECK (state IN ('active','retired')),
            descriptor_json TEXT NOT NULL DEFAULT '{}'
                CHECK (json_valid(descriptor_json)),
            descriptor_hash TEXT NOT NULL CHECK (TRIM(descriptor_hash) <> ''),
            discovery_epoch TEXT NOT NULL CHECK (TRIM(discovery_epoch) <> ''),
            discovery_generation INTEGER NOT NULL
                CHECK (discovery_generation >= 0),
            discovered_at_ms INTEGER NOT NULL CHECK (discovered_at_ms >= 0),
            last_seen_at_ms INTEGER NOT NULL CHECK (last_seen_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            UNIQUE(endpoint_uuid, device_uuid, action_name),
            FOREIGN KEY(endpoint_uuid) REFERENCES executor_endpoint(endpoint_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_action_capability_active
            ON device_action_capability(
                endpoint_uuid, device_uuid, state, action_name
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_action_capability_generation
            ON device_action_capability(
                endpoint_uuid, discovery_epoch, discovery_generation
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_action_capability_device
            ON device_action_capability(
                device_uuid, action_name, state, endpoint_uuid
            )
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
            command_fingerprint TEXT NOT NULL
                CHECK (TRIM(command_fingerprint) <> ''),
            summary_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(summary_json)),
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
            attempt_group_uuid TEXT NOT NULL
                CHECK (TRIM(attempt_group_uuid) <> ''),
            retry_of_job_uuid TEXT,
            attempt_no INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
            execute_command_uuid TEXT NOT NULL UNIQUE,
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            action_name TEXT NOT NULL CHECK (TRIM(action_name) <> ''),
            action_payload_uuid TEXT NOT NULL CHECK (TRIM(action_payload_uuid) <> ''),
            route_uuid TEXT,
            endpoint_uuid TEXT,
            transport TEXT CHECK (transport IN ('hostlink','ros2')),
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
                (route_uuid IS NULL AND endpoint_uuid IS NULL AND transport IS NULL)
                OR (route_uuid IS NOT NULL AND endpoint_uuid IS NOT NULL
                    AND transport IS NOT NULL)
            ),
            CHECK (
                (job_access_token_ciphertext IS NULL AND token_key_id IS NULL)
                OR (job_access_token_ciphertext IS NOT NULL
                    AND token_key_id IS NOT NULL AND TRIM(token_key_id) <> '')
            ),
            CHECK (
                (status IN ('succeeded','failed','canceled','rejected')
                    AND finished_at_ms IS NOT NULL)
                OR (status NOT IN ('succeeded','failed','canceled','rejected')
                    AND finished_at_ms IS NULL)
            ),
            CHECK (
                dispatched_at_ms IS NULL OR dispatched_at_ms >= accepted_at_ms
            ),
            CHECK (
                started_at_ms IS NULL OR dispatched_at_ms IS NULL
                OR started_at_ms >= dispatched_at_ms
            ),
            CHECK (
                finished_at_ms IS NULL OR started_at_ms IS NULL
                OR finished_at_ms >= started_at_ms
            ),
            UNIQUE(attempt_group_uuid, attempt_no),
            UNIQUE(task_uuid, node_uuid, attempt_no),
            FOREIGN KEY(execute_command_uuid) REFERENCES command_inbox(command_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(endpoint_uuid, transport)
                REFERENCES executor_endpoint(endpoint_uuid, transport)
                ON DELETE RESTRICT,
            FOREIGN KEY(route_uuid, device_uuid, endpoint_uuid, transport)
                REFERENCES device_route(
                    route_uuid, device_uuid, endpoint_uuid, transport
                )
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_execution_job_task_node
            ON execution_job(task_uuid, node_uuid, attempt_no)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_job_attempt_group
            ON execution_job(attempt_group_uuid, attempt_no DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_job_device_active
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
            CREATE INDEX IF NOT EXISTS idx_execution_job_endpoint_active
            ON execution_job(endpoint_uuid, status, accepted_at_ms)
            WHERE endpoint_uuid IS NOT NULL AND status IN (
                'dispatch_pending','dispatched','running','failure_waiting',
                'terminal_waiting','execution_unknown'
            )
            """,
        ),
    ),
    TableSpec(
        "device_action_availability",
        """
        CREATE TABLE IF NOT EXISTS device_action_availability (
            endpoint_uuid TEXT NOT NULL,
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            action_name TEXT NOT NULL CHECK (TRIM(action_name) <> ''),
            state TEXT NOT NULL CHECK (state IN ('free','busy','unknown')),
            active_job_uuid TEXT,
            source TEXT NOT NULL CHECK (TRIM(source) <> ''),
            source_event_uuid TEXT NOT NULL
                CHECK (TRIM(source_event_uuid) <> ''),
            state_epoch TEXT NOT NULL DEFAULT 'initial'
                CHECK (TRIM(state_epoch) <> ''),
            state_sequence INTEGER NOT NULL DEFAULT 0 CHECK (state_sequence >= 0),
            discovery_epoch TEXT NOT NULL CHECK (TRIM(discovery_epoch) <> ''),
            discovery_generation INTEGER NOT NULL
                CHECK (discovery_generation >= 0),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            PRIMARY KEY(endpoint_uuid, device_uuid, action_name),
            CHECK (state <> 'free' OR active_job_uuid IS NULL),
            FOREIGN KEY(endpoint_uuid) REFERENCES executor_endpoint(endpoint_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(active_job_uuid) REFERENCES execution_job(job_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_action_availability_state
            ON device_action_availability(endpoint_uuid, state, observed_at_ms)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_action_availability_job
            ON device_action_availability(active_job_uuid)
            WHERE active_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_action_availability_device
            ON device_action_availability(device_uuid, action_name, state)
            """,
        ),
    ),
    TableSpec(
        "job_material_binding",
        """
        CREATE TABLE IF NOT EXISTS job_material_binding (
            binding_uuid TEXT PRIMARY KEY CHECK (TRIM(binding_uuid) <> ''),
            job_uuid TEXT NOT NULL,
            binding_key TEXT NOT NULL CHECK (TRIM(binding_key) <> ''),
            binding_role TEXT NOT NULL CHECK (TRIM(binding_role) <> ''),
            material_uuid TEXT,
            site_uuid TEXT,
            reservation_uuid TEXT,
            quantity REAL CHECK (quantity >= 0),
            unit TEXT,
            snapshot_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(snapshot_json)),
            snapshot_hash TEXT NOT NULL CHECK (TRIM(snapshot_hash) <> ''),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            UNIQUE(job_uuid, binding_key),
            FOREIGN KEY(job_uuid) REFERENCES execution_job(job_uuid)
                ON DELETE CASCADE
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_job_material_binding_material
            ON job_material_binding(material_uuid, job_uuid)
            WHERE material_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "terminal_gate",
        """
        CREATE TABLE IF NOT EXISTS terminal_gate (
            gate_uuid TEXT PRIMARY KEY CHECK (TRIM(gate_uuid) <> ''),
            job_uuid TEXT NOT NULL,
            error_uuid TEXT NOT NULL CHECK (TRIM(error_uuid) <> ''),
            state TEXT NOT NULL CHECK (state IN (
                'waiting_backend','backend_confirmed','released_failed',
                'result_replaced','canceled'
            )),
            required_scheduler_revision INTEGER NOT NULL
                CHECK (required_scheduler_revision >= 0),
            confirmed_scheduler_revision INTEGER
                CHECK (confirmed_scheduler_revision >= 0),
            request_event_uuid TEXT NOT NULL CHECK (TRIM(request_event_uuid) <> ''),
            decision_command_uuid TEXT,
            opened_at_ms INTEGER NOT NULL CHECK (opened_at_ms >= 0),
            resolved_at_ms INTEGER CHECK (resolved_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (
                confirmed_scheduler_revision IS NULL OR
                confirmed_scheduler_revision >= required_scheduler_revision
            ),
            CHECK (
                (state = 'waiting_backend'
                    AND confirmed_scheduler_revision IS NULL
                    AND decision_command_uuid IS NULL
                    AND resolved_at_ms IS NULL)
                OR
                (state = 'backend_confirmed'
                    AND confirmed_scheduler_revision IS NOT NULL
                    AND decision_command_uuid IS NULL
                    AND resolved_at_ms IS NULL)
                OR
                (state = 'released_failed'
                    AND confirmed_scheduler_revision IS NOT NULL
                    AND decision_command_uuid IS NOT NULL
                    AND resolved_at_ms IS NOT NULL)
                OR
                (state = 'result_replaced'
                    AND decision_command_uuid IS NOT NULL
                    AND resolved_at_ms IS NOT NULL)
                OR
                (state = 'canceled' AND decision_command_uuid IS NOT NULL
                    AND resolved_at_ms IS NOT NULL)
            ),
            UNIQUE(error_uuid),
            UNIQUE(gate_uuid, job_uuid),
            FOREIGN KEY(job_uuid) REFERENCES execution_job(job_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(decision_command_uuid) REFERENCES command_inbox(command_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_terminal_gate_open_job
            ON terminal_gate(job_uuid)
            WHERE state IN ('waiting_backend','backend_confirmed')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_terminal_gate_waiting
            ON terminal_gate(state, opened_at_ms)
            WHERE state IN ('waiting_backend','backend_confirmed')
            """,
        ),
    ),
    TableSpec(
        "terminal_decision",
        """
        CREATE TABLE IF NOT EXISTS terminal_decision (
            decision_uuid TEXT PRIMARY KEY CHECK (TRIM(decision_uuid) <> ''),
            gate_uuid TEXT NOT NULL UNIQUE,
            job_uuid TEXT NOT NULL,
            command_uuid TEXT NOT NULL UNIQUE,
            action TEXT NOT NULL CHECK (action IN ('release_failed','replace_result')),
            trusted_actor_type TEXT NOT NULL
                CHECK (trusted_actor_type IN ('backend','user')),
            trusted_actor_uuid TEXT,
            scheduler_revision INTEGER CHECK (scheduler_revision >= 0),
            replacement_result_uuid TEXT,
            reason TEXT,
            request_fingerprint TEXT NOT NULL CHECK (TRIM(request_fingerprint) <> ''),
            decided_at_ms INTEGER NOT NULL CHECK (decided_at_ms >= 0),
            CHECK (
                (action = 'replace_result' AND replacement_result_uuid IS NOT NULL)
                OR (action = 'release_failed' AND replacement_result_uuid IS NULL
                    AND scheduler_revision IS NOT NULL)
            ),
            FOREIGN KEY(gate_uuid, job_uuid)
                REFERENCES terminal_gate(gate_uuid, job_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(command_uuid) REFERENCES command_inbox(command_uuid)
                ON DELETE RESTRICT
        )
        """,
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
            command_type TEXT NOT NULL CHECK (
                command_type IN (
                    'execute','cancel','release_failed','replace_result',
                    'reconcile_state'
                )
            ),
            payload_uuid TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('pending','sent','acknowledged','failed')
            ),
            delivery_attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_attempt_count >= 0),
            created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
            available_at_ms INTEGER NOT NULL DEFAULT 0
                CHECK (available_at_ms >= 0),
            last_sent_at_ms INTEGER CHECK (last_sent_at_ms >= 0),
            acked_at_ms INTEGER CHECK (acked_at_ms >= 0),
            ack_event_uuid TEXT,
            last_error TEXT,
            CHECK (
                target_adapter_epoch IS NULL
                OR TRIM(target_adapter_epoch) <> ''
            ),
            CHECK (
                (command_type = 'reconcile_state' AND job_uuid IS NULL
                    AND source_command_uuid IS NULL
                    AND trigger_event_uuid IS NOT NULL)
                OR
                (command_type <> 'reconcile_state' AND job_uuid IS NOT NULL
                    AND source_command_uuid IS NOT NULL)
            ),
            CHECK (
                (status = 'pending' AND last_sent_at_ms IS NULL
                    AND acked_at_ms IS NULL AND ack_event_uuid IS NULL)
                OR (status = 'sent' AND target_adapter_epoch IS NOT NULL
                    AND last_sent_at_ms IS NOT NULL AND acked_at_ms IS NULL
                    AND ack_event_uuid IS NULL)
                OR (status = 'acknowledged' AND target_adapter_epoch IS NOT NULL
                    AND last_sent_at_ms IS NOT NULL AND acked_at_ms IS NOT NULL
                    AND ack_event_uuid IS NOT NULL)
                OR (status = 'failed' AND acked_at_ms IS NULL
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
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_adapter_command_trigger
            ON adapter_command_outbox(trigger_event_uuid)
            WHERE trigger_event_uuid IS NOT NULL
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
                'accepted','running','feedback','error_pending',
                'succeeded','failed','canceled','endpoint_ready',
                'capability_snapshot','action_availability_snapshot',
                'action_availability_changed','endpoint_offline','command_ack'
            )),
            payload_uuid TEXT,
            payload_sha256 TEXT NOT NULL DEFAULT
                'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
                CHECK (TRIM(payload_sha256) <> ''),
            status TEXT NOT NULL CHECK (
                status IN ('received','processing','processed','rejected')
            ),
            occurred_at_ms INTEGER CHECK (occurred_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            processed_at_ms INTEGER CHECK (processed_at_ms >= 0),
            error_message TEXT,
            CHECK (
                (event_type IN (
                    'accepted','running','feedback','error_pending',
                    'succeeded','failed','canceled'
                ) AND job_uuid IS NOT NULL)
                OR
                (event_type IN (
                    'endpoint_ready','capability_snapshot',
                    'action_availability_snapshot',
                    'action_availability_changed','endpoint_offline'
                ) AND job_uuid IS NULL)
                OR (event_type = 'command_ack'
                    AND adapter_command_uuid IS NOT NULL)
            ),
            CHECK (
                (status IN ('received','processing') AND processed_at_ms IS NULL)
                OR (status IN ('processed','rejected')
                    AND processed_at_ms IS NOT NULL)
            ),
            UNIQUE(endpoint_uuid, adapter_epoch, adapter_sequence),
            FOREIGN KEY(endpoint_uuid) REFERENCES executor_endpoint(endpoint_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_adapter_event_unprocessed
            ON adapter_event_inbox(
                endpoint_uuid, adapter_epoch, status, adapter_sequence
            )
            WHERE status IN ('received','processing')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_adapter_event_job
            ON adapter_event_inbox(job_uuid, adapter_sequence)
            WHERE job_uuid IS NOT NULL
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
            summary_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(summary_json)),
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
            last_session_uuid TEXT,
            acked_session_uuid TEXT,
            delivery_attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK (delivery_attempt_count >= 0),
            last_error TEXT,
            CHECK (
                (status = 'pending' AND acked_at_ms IS NULL
                    AND acked_session_uuid IS NULL)
                OR (status = 'sent' AND last_sent_at_ms IS NOT NULL
                    AND last_session_uuid IS NOT NULL AND acked_at_ms IS NULL
                    AND acked_session_uuid IS NULL)
                OR (status = 'acknowledged' AND last_sent_at_ms IS NOT NULL
                    AND last_session_uuid IS NOT NULL
                    AND acked_at_ms IS NOT NULL
                    AND acked_session_uuid IS NOT NULL)
                OR (status = 'dead_letter' AND acked_at_ms IS NULL
                    AND acked_session_uuid IS NULL)
            ),
            UNIQUE(aggregate_type, aggregate_uuid, aggregate_version, event_type),
            FOREIGN KEY(job_uuid) REFERENCES execution_job(job_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(last_session_uuid) REFERENCES backend_session(session_uuid)
                ON DELETE RESTRICT,
            FOREIGN KEY(acked_session_uuid) REFERENCES backend_session(session_uuid)
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
