"""runtime.db 的 endpoint、job、错误闸门和可靠收发测试。"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from unilabos.server.database.runtime import RUNTIME_DATABASE
from unilabos.server.database.schema import initialize_database
from unilabos.server.models.runtime import (
    DeviceActionCapability,
    DeviceRoute,
    ExecutionJobRecord,
    ExecutorEndpointRecord,
)


def _open(tmp_path) -> sqlite3.Connection:
    return initialize_database(tmp_path / "runtime.db", RUNTIME_DATABASE)


def _insert_session(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO backend_session(
            session_uuid,edge_uuid,backend_uri,authority_epoch,connection_epoch,
            state,last_seen_at_ms
        ) VALUES ('session','edge','wss://backend','authority','connection','active',1)
        """
    )


def _insert_endpoint(
    connection: sqlite3.Connection,
    uuid: str,
    transport: str,
) -> None:
    connection.execute(
        """
        INSERT INTO executor_endpoint(
            endpoint_uuid,transport,host_uuid,instance_name,authority_epoch,state,
            device_routes_json,action_capabilities_json,
            registered_at_ms,last_seen_at_ms
        ) VALUES (?,?, 'host',?, 'authority','online','[]','[]',1,1)
        """,
        (uuid, transport, uuid),
    )


def _insert_command(
    connection: sqlite3.Connection,
    uuid: str,
    sequence: int,
    command_type: str,
    *,
    job_uuid: str | None,
    status: str = "applied",
) -> None:
    applied_at = 1 if status in {"applied", "rejected"} else None
    connection.execute(
        """
        INSERT INTO command_inbox(
            command_uuid,session_uuid,backend_sequence,command_type,job_uuid,
            payload_sha256,command_fingerprint,status,received_at_ms,applied_at_ms
        ) VALUES (?, 'session',?,?,?,?,?,?,1,?)
        """,
        (
            uuid,
            sequence,
            command_type,
            job_uuid,
            f"sha-{uuid}",
            f"fingerprint-{uuid}",
            status,
            applied_at,
        ),
    )


def _insert_job(
    connection: sqlite3.Connection,
    uuid: str,
    command_uuid: str,
    *,
    attempt_no: int = 1,
    retry_of: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO execution_job(
            job_uuid,task_uuid,node_uuid,attempt_group_uuid,retry_of_job_uuid,
            attempt_no,execute_command_uuid,device_uuid,action_name,
            action_payload_uuid,scheduler_revision,status,accepted_at_ms
        ) VALUES (?, 'task','node', 'attempt-group', ?, ?, ?, 'device',
                  'transfer','payload',1,'accepted',1)
        """,
        (uuid, retry_of, attempt_no, command_uuid),
    )


def test_endpoint_aggregates_routes_and_action_capabilities(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            _insert_endpoint(connection, "host-endpoint", "hostlink")
            _insert_endpoint(connection, "ros-endpoint", "ros2")
            connection.execute(
                """
                UPDATE executor_endpoint
                SET device_routes_json='[{"route_uuid":"route","device_uuid":"d"}]',
                    action_capabilities_json=
                    '[{"device_uuid":"d","action_name":"move","availability":"free"}]'
                WHERE endpoint_uuid='host-endpoint'
                """
            )

        assert tuple(
            connection.execute(
                """
                SELECT transport,
                       json_extract(device_routes_json,'$[0].route_uuid'),
                       json_extract(action_capabilities_json,'$[0].availability')
                FROM executor_endpoint WHERE endpoint_uuid='host-endpoint'
                """
            ).fetchone()
        ) == ("hostlink", "route", "free")

        endpoint = ExecutorEndpointRecord(
            endpoint_uuid="endpoint",
            transport="hostlink",
            host_uuid="host",
            instance_name="edge",
            authority_epoch="epoch",
            state="online",
            device_routes=[
                DeviceRoute(
                    route_uuid="route",
                    device_uuid="device",
                    driver_key="driver",
                    config_hash="hash",
                )
            ],
            action_capabilities=[
                DeviceActionCapability(
                    device_uuid="device",
                    action_name="move",
                    concurrency_mode="exclusive",
                    availability="free",
                    descriptor_hash="hash",
                    observed_at_ms=1,
                )
            ],
            registered_at_ms=1,
            last_seen_at_ms=1,
        )
        assert endpoint.device_routes[0].route_uuid == "route"
    finally:
        connection.close()


def test_retry_is_a_new_backend_job_not_a_local_attempt(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            _insert_session(connection)
            _insert_command(connection, "command-1", 1, "execute_job", job_uuid="job-1")
            _insert_command(connection, "command-2", 2, "execute_job", job_uuid="job-2")
            _insert_job(connection, "job-1", "command-1")
            _insert_job(
                connection,
                "job-2",
                "command-2",
                attempt_no=2,
                retry_of="job-1",
            )

        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT job_uuid,retry_of_job_uuid,attempt_no FROM execution_job "
                "ORDER BY attempt_no"
            )
        ] == [("job-1", None, 1), ("job-2", "job-1", 2)]
    finally:
        connection.close()


def test_failed_report_is_released_only_after_backend_decision(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            _insert_session(connection)
            _insert_command(connection, "execute", 1, "execute_job", job_uuid="job")
            _insert_job(connection, "job", "execute")
            connection.execute(
                """
                UPDATE execution_job
                SET status='terminal_waiting',
                    terminal_gate_state='waiting_backend',
                    terminal_error_uuid='error',
                    terminal_required_scheduler_revision=2,
                    terminal_request_event_uuid='request-event',
                    terminal_opened_at_ms=2,version=2
                WHERE job_uuid='job'
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "UPDATE execution_job SET status='failed',finished_at_ms=3 "
                    "WHERE job_uuid='job'"
                )

        with connection:
            _insert_command(connection, "release", 2, "release_failed", job_uuid="job")
            connection.execute(
                """
                UPDATE execution_job
                SET terminal_gate_state='released_failed',
                    terminal_confirmed_scheduler_revision=2,
                    terminal_decision_command_uuid='release',
                    terminal_decision_json='{"action":"release_failed"}',
                    terminal_resolved_at_ms=3,status='failed',finished_at_ms=3,
                    version=3
                WHERE job_uuid='job'
                """
            )

        assert tuple(
            connection.execute(
                "SELECT status,terminal_gate_state FROM execution_job"
            ).fetchone()
        ) == ("failed", "released_failed")
    finally:
        connection.close()


def test_adapter_event_sequence_is_scoped_by_adapter_epoch(tmp_path) -> None:
    connection = _open(tmp_path)
    try:
        with connection:
            _insert_endpoint(connection, "endpoint", "ros2")
            for event_uuid, epoch in (("event-1", "epoch-1"), ("event-2", "epoch-2")):
                connection.execute(
                    """
                    INSERT INTO adapter_event_inbox(
                        adapter_event_uuid,endpoint_uuid,adapter_epoch,
                        adapter_sequence,event_type,payload_sha256,status,received_at_ms
                    ) VALUES (?, 'endpoint',?,1,'endpoint_ready','sha','received',1)
                    """,
                    (event_uuid, epoch),
                )

        with pytest.raises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    """
                    INSERT INTO adapter_event_inbox(
                        adapter_event_uuid,endpoint_uuid,adapter_epoch,
                        adapter_sequence,event_type,payload_sha256,status,received_at_ms
                    ) VALUES (
                        'duplicate','endpoint','epoch-2',1,
                        'endpoint_ready','sha','received',1
                    )
                    """
                )
    finally:
        connection.close()


def test_job_model_requires_new_identity_for_retry() -> None:
    with pytest.raises(ValidationError, match="retry link"):
        ExecutionJobRecord(
            job_uuid="job",
            task_uuid="task",
            node_uuid="node",
            attempt_group_uuid="attempt-group",
            attempt_no=2,
            execute_command_uuid="command",
            device_uuid="device",
            action_name="action",
            action_payload_uuid="payload",
            scheduler_revision=1,
            status="accepted",
            accepted_at_ms=1,
        )
