"""``runtime.db`` 的幂等、重连和终态门控约束。"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from unilabos.server.database.runtime import RUNTIME_DATABASE
from unilabos.server.database.schema import initialize_database
from unilabos.server.models.runtime import (
    AdapterCommandOutboxRecord,
    ExecutionJobRecord,
    TerminalGateRecord,
)


def _open_runtime(tmp_path):
    return initialize_database(tmp_path / RUNTIME_DATABASE.filename, RUNTIME_DATABASE)


def _insert_session(
    connection: sqlite3.Connection,
    session_uuid: str,
    connection_epoch: str,
    *,
    state: str = "active",
) -> None:
    disconnected_at_ms = 2 if state == "disconnected" else None
    connection.execute(
        """
        INSERT INTO backend_session(
            session_uuid,edge_uuid,backend_uri,authority_epoch,
            connection_epoch,state,connected_at_ms,disconnected_at_ms,
            last_seen_at_ms
        ) VALUES (?, 'edge', 'wss://backend', 'authority-1', ?, ?, 1, ?, 2)
        """,
        (session_uuid, connection_epoch, state, disconnected_at_ms),
    )


def _insert_endpoint(
    connection: sqlite3.Connection,
    endpoint_uuid: str,
    transport: str,
) -> None:
    connection.execute(
        """
        INSERT INTO executor_endpoint(
            endpoint_uuid,transport,host_uuid,instance_name,authority_epoch,
            adapter_epoch,state,registered_at_ms,last_seen_at_ms
        ) VALUES (?, ?, 'host', ?, 'authority-1', 'adapter-1', 'online', 1, 1)
        """,
        (endpoint_uuid, transport, endpoint_uuid),
    )


def _insert_command(
    connection: sqlite3.Connection,
    command_uuid: str,
    sequence: int,
    command_type: str,
    *,
    job_uuid: str | None = None,
    session_uuid: str = "session-1",
) -> None:
    connection.execute(
        """
        INSERT INTO command_inbox(
            command_uuid,session_uuid,backend_sequence,command_type,job_uuid,
            payload_sha256,command_fingerprint,status,received_at_ms
        ) VALUES (?, ?, ?, ?, ?, 'sha256', ?, 'received', 1)
        """,
        (
            command_uuid,
            session_uuid,
            sequence,
            command_type,
            job_uuid,
            f"fingerprint-{command_uuid}",
        ),
    )


def _insert_job(
    connection: sqlite3.Connection,
    job_uuid: str,
    command_uuid: str,
    *,
    task_uuid: str = "task",
    node_uuid: str = "node",
    attempt_group_uuid: str = "attempt-group",
    retry_of_job_uuid: str | None = None,
    attempt_no: int = 1,
    status: str = "accepted",
) -> None:
    connection.execute(
        """
        INSERT INTO execution_job(
            job_uuid,task_uuid,node_uuid,attempt_group_uuid,retry_of_job_uuid,
            attempt_no,execute_command_uuid,device_uuid,action_name,
            action_payload_uuid,scheduler_revision,status,accepted_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'device', 'transfer', 'payload', 1, ?, 1)
        """,
        (
            job_uuid,
            task_uuid,
            node_uuid,
            attempt_group_uuid,
            retry_of_job_uuid,
            attempt_no,
            command_uuid,
            status,
        ),
    )


def test_backend_reconnect_has_a_new_connection_epoch_and_command_stream(
    tmp_path,
) -> None:
    connection = _open_runtime(tmp_path)
    try:
        with connection:
            _insert_session(
                connection,
                "session-old",
                "connection-1",
                state="disconnected",
            )
            _insert_session(connection, "session-1", "connection-2")
            _insert_command(connection, "command-1", 1, "reconcile")

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                _insert_session(connection, "session-duplicate", "connection-2")

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                _insert_session(connection, "session-active-2", "connection-3")

        with connection:
            _insert_command(
                connection,
                "command-replayed-sequence",
                1,
                "reconcile",
                session_uuid="session-old",
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                _insert_command(connection, "command-sequence-conflict", 1, "reconcile")
    finally:
        connection.close()


def test_route_transport_and_backend_attempt_identity_are_database_invariants(
    tmp_path,
) -> None:
    connection = _open_runtime(tmp_path)
    try:
        with connection:
            _insert_session(connection, "session-1", "connection-1")
            _insert_endpoint(connection, "host-endpoint", "hostlink")
            _insert_endpoint(connection, "ros-endpoint", "ros2")

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO device_route(
                        route_uuid,device_uuid,endpoint_uuid,transport,driver_key,
                        config_hash,created_at_ms,updated_at_ms
                    ) VALUES (
                        'bad-route','device','host-endpoint','ros2','driver',
                        'hash',1,1
                    )
                    """
                )

        with connection:
            _insert_command(
                connection, "retry-command", 1, "execute_job", job_uuid="retry"
            )
            # 原 job 可以在另一 edge；这里只保存后端规范 UUID，不要求本库存在父 job。
            _insert_job(
                connection,
                "retry",
                "retry-command",
                retry_of_job_uuid="job-on-another-edge",
                attempt_no=2,
            )
            _insert_command(
                connection,
                "duplicate-attempt-command",
                2,
                "execute_job",
                job_uuid="duplicate-attempt",
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                _insert_job(
                    connection,
                    "duplicate-attempt",
                    "duplicate-attempt-command",
                    attempt_group_uuid="different-group",
                    retry_of_job_uuid="other-parent",
                    attempt_no=2,
                )
    finally:
        connection.close()


def test_release_failed_requires_confirmed_scheduler_revision(tmp_path) -> None:
    connection = _open_runtime(tmp_path)
    try:
        with connection:
            _insert_session(connection, "session-1", "connection-1")
            _insert_command(connection, "execute", 1, "execute_job", job_uuid="job")
            _insert_job(connection, "job", "execute", status="failure_waiting")
            _insert_command(
                connection,
                "release",
                2,
                "release_failed",
                job_uuid="job",
            )
            connection.execute(
                """
                INSERT INTO terminal_gate(
                    gate_uuid,job_uuid,error_uuid,state,
                    required_scheduler_revision,request_event_uuid,opened_at_ms
                ) VALUES (
                    'gate','job','error','waiting_backend',2,'request-event',1
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    UPDATE terminal_gate
                    SET state='released_failed',decision_command_uuid='release',
                        resolved_at_ms=2
                    WHERE gate_uuid='gate'
                    """
                )

        with connection:
            connection.execute(
                """
                UPDATE terminal_gate
                SET state='backend_confirmed',confirmed_scheduler_revision=2
                WHERE gate_uuid='gate'
                """
            )
            connection.execute(
                """
                INSERT INTO terminal_decision(
                    decision_uuid,gate_uuid,job_uuid,command_uuid,action,
                    trusted_actor_type,scheduler_revision,request_fingerprint,
                    decided_at_ms
                ) VALUES (
                    'decision','gate','job','release','release_failed',
                    'backend',2,'release-fingerprint',2
                )
                """
            )
            connection.execute(
                """
                UPDATE terminal_gate
                SET state='released_failed',decision_command_uuid='release',
                    resolved_at_ms=2
                WHERE gate_uuid='gate'
                """
            )

        row = connection.execute(
            """
            SELECT state,confirmed_scheduler_revision
            FROM terminal_gate WHERE gate_uuid='gate'
            """
        ).fetchone()
        assert tuple(row) == ("released_failed", 2)
        assert "expires_at_ms" not in {
            column[1]
            for column in connection.execute("PRAGMA table_info(terminal_gate)")
        }
    finally:
        connection.close()


def test_adapter_epoch_resets_sequence_without_creating_a_business_attempt(
    tmp_path,
) -> None:
    connection = _open_runtime(tmp_path)
    try:
        with connection:
            _insert_endpoint(connection, "endpoint", "hostlink")
            connection.execute(
                """
                INSERT INTO adapter_event_inbox(
                    adapter_event_uuid,endpoint_uuid,adapter_epoch,
                    adapter_sequence,event_type,status,received_at_ms
                ) VALUES (
                    'ready-1','endpoint','adapter-1',1,'endpoint_ready','received',1
                )
                """
            )
            connection.execute(
                """
                INSERT INTO adapter_command_outbox(
                    adapter_command_uuid,endpoint_uuid,trigger_event_uuid,
                    command_type,status,created_at_ms
                ) VALUES (
                    'reconcile','endpoint','ready-1','reconcile_state','pending',1
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO adapter_event_inbox(
                        adapter_event_uuid,endpoint_uuid,adapter_epoch,
                        adapter_sequence,event_type,status,received_at_ms
                    ) VALUES (
                        'same-position','endpoint','adapter-1',1,
                        'endpoint_ready','received',1
                    )
                    """
                )

        with connection:
            # 进程重启后 epoch 改变，adapter_sequence 可以从 1 重新开始。
            connection.execute(
                """
                INSERT INTO adapter_event_inbox(
                    adapter_event_uuid,endpoint_uuid,adapter_epoch,
                    adapter_sequence,event_type,status,received_at_ms
                ) VALUES (
                    'ready-2','endpoint','adapter-2',1,'endpoint_ready','received',2
                )
                """
            )
            connection.execute(
                """
                UPDATE adapter_command_outbox
                SET status='sent',target_adapter_epoch='adapter-2',
                    last_sent_at_ms=2,delivery_attempt_count=1
                WHERE adapter_command_uuid='reconcile'
                """
            )
            connection.execute(
                """
                INSERT INTO adapter_event_inbox(
                    adapter_event_uuid,endpoint_uuid,adapter_epoch,
                    adapter_command_uuid,adapter_sequence,event_type,status,
                    received_at_ms
                ) VALUES (
                    'ack','endpoint','adapter-2','reconcile',2,
                    'command_ack','received',3
                )
                """
            )
            connection.execute(
                """
                UPDATE adapter_command_outbox
                SET status='acknowledged',acked_at_ms=3,ack_event_uuid='ack'
                WHERE adapter_command_uuid='reconcile'
                """
            )

        columns = {
            column[1]
            for column in connection.execute(
                "PRAGMA table_info(adapter_command_outbox)"
            )
        }
        assert "delivery_attempt_count" in columns
        assert "attempt_count" not in columns
        assert (
            connection.execute(
                """
            SELECT delivery_attempt_count
            FROM adapter_command_outbox WHERE adapter_command_uuid='reconcile'
            """
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_runtime_models_distinguish_business_attempts_from_delivery_retries() -> None:
    with pytest.raises(ValidationError, match="later attempt"):
        ExecutionJobRecord(
            job_uuid="retry",
            task_uuid="task",
            node_uuid="node",
            attempt_group_uuid="attempt-group",
            retry_of_job_uuid="parent",
            attempt_no=1,
            execute_command_uuid="command",
            device_uuid="device",
            action_name="transfer",
            action_payload_uuid="payload",
            scheduler_revision=1,
            status="accepted",
            accepted_at_ms=1,
        )

    with pytest.raises(ValidationError, match="scheduler confirmation"):
        TerminalGateRecord(
            gate_uuid="gate",
            job_uuid="job",
            error_uuid="error",
            state="released_failed",
            required_scheduler_revision=1,
            request_event_uuid="request",
            decision_command_uuid="release",
            opened_at_ms=1,
            resolved_at_ms=2,
        )

    command = AdapterCommandOutboxRecord(
        adapter_command_uuid="reconcile",
        endpoint_uuid="endpoint",
        trigger_event_uuid="ready-event",
        command_type="reconcile_state",
        status="pending",
        delivery_attempt_count=3,
        created_at_ms=1,
    )
    assert command.delivery_attempt_count == 3
