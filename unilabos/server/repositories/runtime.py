"""``runtime.db`` 的同步 Repository 与唯一写事务边界。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from unilabos.server.database.runtime import RUNTIME_DATABASE
from unilabos.server.database.schema import initialize_database
from unilabos.server.models.runtime import (
    AdapterCommandOutboxRecord,
    BackendEventOutboxRecord,
    BackendSessionRecord,
    CommandInboxRecord,
    ExecutionJobRecord,
    ExecutorEndpointRecord,
)
from unilabos.server.protocol.common import canonical_json


def _load_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return fallback
    return json.loads(str(value))


def _placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)


class RuntimeRepository:
    """runtime 控制表 CRUD。

    一个实例独占一个 SQLite connection。所有复合写入必须从 ``write()``
    进入，以 ``BEGIN IMMEDIATE`` 和进程内可重入锁保证单 writer。
    """

    def __init__(self, database: str | Path | sqlite3.Connection):
        if isinstance(database, sqlite3.Connection):
            self.connection = database
            self._owns_connection = False
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
        else:
            self.connection = initialize_database(database, RUNTIME_DATABASE)
            self._owns_connection = True
        self._write_lock = threading.RLock()

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def __enter__(self) -> "RuntimeRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """runtime.db 的唯一进程内 writer 入口。"""

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    # -- Backend session -------------------------------------------------

    @staticmethod
    def _session(row: sqlite3.Row) -> BackendSessionRecord:
        return BackendSessionRecord.model_validate(dict(row))

    def get_session(self, session_uuid: str) -> Optional[BackendSessionRecord]:
        row = self.connection.execute(
            "SELECT * FROM backend_session WHERE session_uuid=?", (session_uuid,)
        ).fetchone()
        return self._session(row) if row is not None else None

    def list_sessions(
        self,
        *,
        edge_uuid: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
    ) -> list[BackendSessionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if edge_uuid is not None:
            clauses.append("edge_uuid=?")
            params.append(edge_uuid)
        if state is not None:
            clauses.append("state=?")
            params.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT * FROM backend_session{where}
            ORDER BY last_seen_at_ms DESC,session_uuid LIMIT ?
            """,
            [*params, limit],
        )
        return [self._session(row) for row in rows]

    def insert_session(self, record: BackendSessionRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO backend_session(
                session_uuid,edge_uuid,backend_uri,authority_epoch,connection_epoch,
                state,command_cursor,event_send_cursor,event_ack_sequence,
                connected_at_ms,disconnected_at_ms,last_seen_at_ms,version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            tuple(record.model_dump(mode="json").values()),
        )

    def update_session(
        self, record: BackendSessionRecord, *, expected_version: int
    ) -> None:
        values = record.model_dump(mode="json")
        cursor = self.connection.execute(
            """
            UPDATE backend_session SET
                edge_uuid=:edge_uuid,backend_uri=:backend_uri,
                authority_epoch=:authority_epoch,connection_epoch=:connection_epoch,
                state=:state,command_cursor=:command_cursor,
                event_send_cursor=:event_send_cursor,
                event_ack_sequence=:event_ack_sequence,
                connected_at_ms=:connected_at_ms,
                disconnected_at_ms=:disconnected_at_ms,
                last_seen_at_ms=:last_seen_at_ms,version=:version
            WHERE session_uuid=:session_uuid AND version=:expected_version
            """,
            {**values, "expected_version": expected_version},
        )
        if cursor.rowcount != 1:
            raise RuntimeError("backend session version conflict")

    def disconnect_other_active_sessions(
        self, edge_uuid: str, session_uuid: str, *, disconnected_at_ms: int
    ) -> None:
        self.connection.execute(
            """
            UPDATE backend_session
            SET state='disconnected',disconnected_at_ms=?,last_seen_at_ms=?,
                version=version+1
            WHERE edge_uuid=? AND session_uuid<>? AND state='active'
            """,
            (disconnected_at_ms, disconnected_at_ms, edge_uuid, session_uuid),
        )

    # -- Endpoint snapshot ----------------------------------------------

    @staticmethod
    def _endpoint(row: sqlite3.Row) -> ExecutorEndpointRecord:
        values = dict(row)
        values["device_routes"] = _load_json(values.pop("device_routes_json"), [])
        values["action_capabilities"] = _load_json(
            values.pop("action_capabilities_json"), []
        )
        values["config"] = _load_json(values.pop("config_json"), {})
        return ExecutorEndpointRecord.model_validate(values)

    def get_endpoint(self, endpoint_uuid: str) -> Optional[ExecutorEndpointRecord]:
        row = self.connection.execute(
            "SELECT * FROM executor_endpoint WHERE endpoint_uuid=?", (endpoint_uuid,)
        ).fetchone()
        return self._endpoint(row) if row is not None else None

    def get_endpoint_by_identity(
        self, transport: str, host_uuid: str, instance_name: str
    ) -> Optional[ExecutorEndpointRecord]:
        row = self.connection.execute(
            """
            SELECT * FROM executor_endpoint
            WHERE transport=? AND host_uuid=? AND instance_name=?
            """,
            (transport, host_uuid, instance_name),
        ).fetchone()
        return self._endpoint(row) if row is not None else None

    def list_endpoints(
        self,
        *,
        transport: Optional[str] = None,
        state: Optional[str] = None,
        host_uuid: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutorEndpointRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        for field, value in (
            ("transport", transport),
            ("state", state),
            ("host_uuid", host_uuid),
        ):
            if value is not None:
                clauses.append(f"{field}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT * FROM executor_endpoint{where}
            ORDER BY last_seen_at_ms DESC,endpoint_uuid LIMIT ?
            """,
            [*params, limit],
        )
        return [self._endpoint(row) for row in rows]

    @staticmethod
    def _endpoint_values(record: ExecutorEndpointRecord) -> dict[str, Any]:
        values = record.model_dump(mode="json")
        values["device_routes_json"] = canonical_json(values.pop("device_routes"))
        values["action_capabilities_json"] = canonical_json(
            values.pop("action_capabilities")
        )
        values["config_json"] = canonical_json(values.pop("config"))
        return values

    def insert_endpoint(self, record: ExecutorEndpointRecord) -> None:
        values = self._endpoint_values(record)
        self.connection.execute(
            """
            INSERT INTO executor_endpoint(
                endpoint_uuid,transport,host_uuid,instance_name,authority_epoch,
                adapter_epoch,adapter_event_cursor,reconciliation_generation,state,
                device_routes_json,action_capabilities_json,config_json,snapshot_hash,
                registered_at_ms,last_seen_at_ms,reconciled_at_ms,version
            ) VALUES (
                :endpoint_uuid,:transport,:host_uuid,:instance_name,:authority_epoch,
                :adapter_epoch,:adapter_event_cursor,:reconciliation_generation,:state,
                :device_routes_json,:action_capabilities_json,:config_json,:snapshot_hash,
                :registered_at_ms,:last_seen_at_ms,:reconciled_at_ms,:version
            )
            """,
            values,
        )

    def update_endpoint(
        self, record: ExecutorEndpointRecord, *, expected_version: int
    ) -> None:
        values = self._endpoint_values(record)
        cursor = self.connection.execute(
            """
            UPDATE executor_endpoint SET
                transport=:transport,host_uuid=:host_uuid,
                instance_name=:instance_name,authority_epoch=:authority_epoch,
                adapter_epoch=:adapter_epoch,
                adapter_event_cursor=:adapter_event_cursor,
                reconciliation_generation=:reconciliation_generation,state=:state,
                device_routes_json=:device_routes_json,
                action_capabilities_json=:action_capabilities_json,
                config_json=:config_json,snapshot_hash=:snapshot_hash,
                registered_at_ms=:registered_at_ms,last_seen_at_ms=:last_seen_at_ms,
                reconciled_at_ms=:reconciled_at_ms,version=:version
            WHERE endpoint_uuid=:endpoint_uuid AND version=:expected_version
            """,
            {**values, "expected_version": expected_version},
        )
        if cursor.rowcount != 1:
            raise RuntimeError("executor endpoint version conflict")

    # -- Command inbox ---------------------------------------------------

    @staticmethod
    def _command(row: sqlite3.Row) -> CommandInboxRecord:
        values = dict(row)
        values["summary"] = _load_json(values.pop("summary_json"), {})
        return CommandInboxRecord.model_validate(values)

    def get_command(self, command_uuid: str) -> Optional[CommandInboxRecord]:
        row = self.connection.execute(
            "SELECT * FROM command_inbox WHERE command_uuid=?", (command_uuid,)
        ).fetchone()
        return self._command(row) if row is not None else None

    def get_command_by_sequence(
        self, session_uuid: str, backend_sequence: int
    ) -> Optional[CommandInboxRecord]:
        row = self.connection.execute(
            """
            SELECT * FROM command_inbox
            WHERE session_uuid=? AND backend_sequence=?
            """,
            (session_uuid, backend_sequence),
        ).fetchone()
        return self._command(row) if row is not None else None

    def list_commands(
        self,
        *,
        session_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        command_type: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[CommandInboxRecord]:
        clauses = ["backend_sequence>?"]
        params: list[Any] = [after_sequence]
        for field, value in (
            ("session_uuid", session_uuid),
            ("status", status),
            ("job_uuid", job_uuid),
            ("command_type", command_type),
        ):
            if value is not None:
                clauses.append(f"{field}=?")
                params.append(value)
        rows = self.connection.execute(
            "SELECT * FROM command_inbox WHERE "
            + " AND ".join(clauses)
            + " ORDER BY session_uuid,backend_sequence LIMIT ?",
            [*params, limit],
        )
        return [self._command(row) for row in rows]

    def insert_command(self, record: CommandInboxRecord) -> None:
        values = record.model_dump(mode="json")
        values["summary_json"] = canonical_json(values.pop("summary"))
        self.connection.execute(
            """
            INSERT INTO command_inbox(
                command_uuid,session_uuid,backend_sequence,command_type,job_uuid,
                payload_uuid,payload_sha256,command_fingerprint,summary_json,
                traceparent,status,received_at_ms,applied_at_ms,error_code,
                error_message,version
            ) VALUES (
                :command_uuid,:session_uuid,:backend_sequence,:command_type,:job_uuid,
                :payload_uuid,:payload_sha256,:command_fingerprint,:summary_json,
                :traceparent,:status,:received_at_ms,:applied_at_ms,:error_code,
                :error_message,:version
            )
            """,
            values,
        )

    def update_command(
        self, record: CommandInboxRecord, *, expected_version: int
    ) -> None:
        values = record.model_dump(mode="json")
        values["summary_json"] = canonical_json(values.pop("summary"))
        cursor = self.connection.execute(
            """
            UPDATE command_inbox SET
                status=:status,applied_at_ms=:applied_at_ms,
                error_code=:error_code,error_message=:error_message,version=:version
            WHERE command_uuid=:command_uuid AND version=:expected_version
            """,
            {**values, "expected_version": expected_version},
        )
        if cursor.rowcount != 1:
            raise RuntimeError("command inbox version conflict")

    # -- Execution job ---------------------------------------------------

    @staticmethod
    def _job(row: sqlite3.Row) -> ExecutionJobRecord:
        values = dict(row)
        values.pop("job_access_token_ciphertext", None)
        values.pop("token_key_id", None)
        values["material_bindings"] = _load_json(
            values.pop("material_bindings_json"), []
        )
        values["terminal_decision"] = _load_json(
            values.pop("terminal_decision_json"), {}
        )
        return ExecutionJobRecord.model_validate(values)

    def get_job(self, job_uuid: str) -> Optional[ExecutionJobRecord]:
        row = self.connection.execute(
            "SELECT * FROM execution_job WHERE job_uuid=?", (job_uuid,)
        ).fetchone()
        return self._job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        device_uuid: Optional[str] = None,
        endpoint_uuid: Optional[str] = None,
        retry_of_job_uuid: Optional[str] = None,
        attempt_group_uuid: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutionJobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        for field, value in (
            ("status", status),
            ("device_uuid", device_uuid),
            ("endpoint_uuid", endpoint_uuid),
            ("retry_of_job_uuid", retry_of_job_uuid),
            ("attempt_group_uuid", attempt_group_uuid),
        ):
            if value is not None:
                clauses.append(f"{field}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"""
            SELECT * FROM execution_job{where}
            ORDER BY accepted_at_ms DESC,job_uuid LIMIT ?
            """,
            [*params, limit],
        )
        return [self._job(row) for row in rows]

    @staticmethod
    def _job_values(record: ExecutionJobRecord) -> dict[str, Any]:
        values = record.model_dump(mode="json")
        values["material_bindings_json"] = canonical_json(
            values.pop("material_bindings")
        )
        values["terminal_decision_json"] = canonical_json(
            values.pop("terminal_decision")
        )
        return values

    def insert_job(self, record: ExecutionJobRecord) -> None:
        values = self._job_values(record)
        self.connection.execute(
            """
            INSERT INTO execution_job(
                job_uuid,task_uuid,node_uuid,attempt_group_uuid,retry_of_job_uuid,
                attempt_no,execute_command_uuid,device_uuid,action_name,
                action_payload_uuid,route_uuid,endpoint_uuid,transport,
                material_bindings_json,scheduler_revision,scheduler_status_version,
                status,feedback_sequence,result_uuid,error_code,error_summary,
                terminal_gate_state,terminal_error_uuid,
                terminal_required_scheduler_revision,
                terminal_confirmed_scheduler_revision,terminal_request_event_uuid,
                terminal_decision_command_uuid,terminal_decision_json,
                terminal_opened_at_ms,terminal_resolved_at_ms,accepted_at_ms,
                dispatched_at_ms,started_at_ms,finished_at_ms,version
            ) VALUES (
                :job_uuid,:task_uuid,:node_uuid,:attempt_group_uuid,:retry_of_job_uuid,
                :attempt_no,:execute_command_uuid,:device_uuid,:action_name,
                :action_payload_uuid,:route_uuid,:endpoint_uuid,:transport,
                :material_bindings_json,:scheduler_revision,:scheduler_status_version,
                :status,:feedback_sequence,:result_uuid,:error_code,:error_summary,
                :terminal_gate_state,:terminal_error_uuid,
                :terminal_required_scheduler_revision,
                :terminal_confirmed_scheduler_revision,:terminal_request_event_uuid,
                :terminal_decision_command_uuid,:terminal_decision_json,
                :terminal_opened_at_ms,:terminal_resolved_at_ms,:accepted_at_ms,
                :dispatched_at_ms,:started_at_ms,:finished_at_ms,:version
            )
            """,
            values,
        )

    def update_job(self, record: ExecutionJobRecord, *, expected_version: int) -> None:
        values = self._job_values(record)
        cursor = self.connection.execute(
            """
            UPDATE execution_job SET
                route_uuid=:route_uuid,endpoint_uuid=:endpoint_uuid,
                transport=:transport,scheduler_revision=:scheduler_revision,
                scheduler_status_version=:scheduler_status_version,status=:status,
                feedback_sequence=:feedback_sequence,result_uuid=:result_uuid,
                error_code=:error_code,error_summary=:error_summary,
                terminal_gate_state=:terminal_gate_state,
                terminal_error_uuid=:terminal_error_uuid,
                terminal_required_scheduler_revision=
                    :terminal_required_scheduler_revision,
                terminal_confirmed_scheduler_revision=
                    :terminal_confirmed_scheduler_revision,
                terminal_request_event_uuid=:terminal_request_event_uuid,
                terminal_decision_command_uuid=:terminal_decision_command_uuid,
                terminal_decision_json=:terminal_decision_json,
                terminal_opened_at_ms=:terminal_opened_at_ms,
                terminal_resolved_at_ms=:terminal_resolved_at_ms,
                dispatched_at_ms=:dispatched_at_ms,started_at_ms=:started_at_ms,
                finished_at_ms=:finished_at_ms,version=:version
            WHERE job_uuid=:job_uuid AND version=:expected_version
            """,
            {**values, "expected_version": expected_version},
        )
        if cursor.rowcount != 1:
            raise RuntimeError("execution job version conflict")

    # -- Adapter command outbox -----------------------------------------

    @staticmethod
    def _adapter_command(row: sqlite3.Row) -> AdapterCommandOutboxRecord:
        return AdapterCommandOutboxRecord.model_validate(dict(row))

    def get_adapter_command(
        self, adapter_command_uuid: str
    ) -> Optional[AdapterCommandOutboxRecord]:
        row = self.connection.execute(
            "SELECT * FROM adapter_command_outbox WHERE adapter_command_uuid=?",
            (adapter_command_uuid,),
        ).fetchone()
        return self._adapter_command(row) if row is not None else None

    def list_adapter_commands(
        self,
        *,
        endpoint_uuid: Optional[str] = None,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[AdapterCommandOutboxRecord]:
        clauses = ["sequence>?"]
        params: list[Any] = [after_sequence]
        for field, value in (
            ("endpoint_uuid", endpoint_uuid),
            ("status", status),
            ("job_uuid", job_uuid),
        ):
            if value is not None:
                clauses.append(f"{field}=?")
                params.append(value)
        rows = self.connection.execute(
            "SELECT * FROM adapter_command_outbox WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence LIMIT ?",
            [*params, limit],
        )
        return [self._adapter_command(row) for row in rows]

    def insert_adapter_command(self, record: AdapterCommandOutboxRecord) -> int:
        values = record.model_dump(mode="json", exclude={"sequence"})
        cursor = self.connection.execute(
            """
            INSERT INTO adapter_command_outbox(
                adapter_command_uuid,job_uuid,endpoint_uuid,source_command_uuid,
                trigger_event_uuid,target_adapter_epoch,command_type,payload_uuid,
                status,delivery_attempt_count,created_at_ms,available_at_ms,
                last_sent_at_ms,acked_at_ms,ack_event_uuid,last_error
            ) VALUES (
                :adapter_command_uuid,:job_uuid,:endpoint_uuid,:source_command_uuid,
                :trigger_event_uuid,:target_adapter_epoch,:command_type,:payload_uuid,
                :status,:delivery_attempt_count,:created_at_ms,:available_at_ms,
                :last_sent_at_ms,:acked_at_ms,:ack_event_uuid,:last_error
            )
            """,
            values,
        )
        return int(cursor.lastrowid)

    def claim_adapter_commands(
        self,
        endpoint_uuid: str,
        *,
        now_ms: int,
        lease_until_ms: int,
        limit: int,
    ) -> list[AdapterCommandOutboxRecord]:
        rows = self.connection.execute(
            """
            SELECT sequence FROM adapter_command_outbox
            WHERE endpoint_uuid=? AND status IN ('pending','sent')
                AND available_at_ms<=?
            ORDER BY sequence LIMIT ?
            """,
            (endpoint_uuid, now_ms, limit),
        ).fetchall()
        sequences = [int(row[0]) for row in rows]
        if not sequences:
            return []
        params: list[Any] = [now_ms, lease_until_ms, *sequences]
        self.connection.execute(
            f"""
            UPDATE adapter_command_outbox
            SET status='sent',delivery_attempt_count=delivery_attempt_count+1,
                last_sent_at_ms=?,available_at_ms=?,last_error=NULL
            WHERE sequence IN ({_placeholders(sequences)})
            """,
            params,
        )
        claimed = self.connection.execute(
            f"""
            SELECT * FROM adapter_command_outbox
            WHERE sequence IN ({_placeholders(sequences)}) ORDER BY sequence
            """,
            sequences,
        )
        return [self._adapter_command(row) for row in claimed]

    def acknowledge_adapter_command(
        self, adapter_command_uuid: str, *, ack_event_uuid: str, acked_at_ms: int
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE adapter_command_outbox
            SET status='acknowledged',acked_at_ms=?,ack_event_uuid=?
            WHERE adapter_command_uuid=? AND status='sent'
            """,
            (acked_at_ms, ack_event_uuid, adapter_command_uuid),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("adapter command is not claimable for ACK")

    # -- Backend event outbox -------------------------------------------

    @staticmethod
    def _backend_event(row: sqlite3.Row) -> BackendEventOutboxRecord:
        values = dict(row)
        values["summary"] = _load_json(values.pop("summary_json"), {})
        return BackendEventOutboxRecord.model_validate(values)

    def get_backend_event(self, event_uuid: str) -> Optional[BackendEventOutboxRecord]:
        row = self.connection.execute(
            "SELECT * FROM backend_event_outbox WHERE event_uuid=?", (event_uuid,)
        ).fetchone()
        return self._backend_event(row) if row is not None else None

    def list_backend_events(
        self,
        *,
        status: Optional[str] = None,
        job_uuid: Optional[str] = None,
        aggregate_type: Optional[str] = None,
        aggregate_uuid: Optional[str] = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[BackendEventOutboxRecord]:
        clauses = ["sequence>?"]
        params: list[Any] = [after_sequence]
        for field, value in (
            ("status", status),
            ("job_uuid", job_uuid),
            ("aggregate_type", aggregate_type),
            ("aggregate_uuid", aggregate_uuid),
        ):
            if value is not None:
                clauses.append(f"{field}=?")
                params.append(value)
        rows = self.connection.execute(
            "SELECT * FROM backend_event_outbox WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence LIMIT ?",
            [*params, limit],
        )
        return [self._backend_event(row) for row in rows]

    def insert_backend_event(self, record: BackendEventOutboxRecord) -> int:
        values = record.model_dump(mode="json", exclude={"sequence"})
        values["summary_json"] = canonical_json(values.pop("summary"))
        cursor = self.connection.execute(
            """
            INSERT INTO backend_event_outbox(
                event_uuid,event_type,aggregate_type,aggregate_uuid,
                aggregate_version,job_uuid,summary_json,detail_payload_uuid,
                traceparent,tracestate,status,created_at_ms,available_at_ms,
                last_sent_at_ms,acked_at_ms,delivery_attempt_count,last_error
            ) VALUES (
                :event_uuid,:event_type,:aggregate_type,:aggregate_uuid,
                :aggregate_version,:job_uuid,:summary_json,:detail_payload_uuid,
                :traceparent,:tracestate,:status,:created_at_ms,:available_at_ms,
                :last_sent_at_ms,:acked_at_ms,:delivery_attempt_count,:last_error
            )
            """,
            values,
        )
        return int(cursor.lastrowid)

    def claim_backend_events(
        self, *, now_ms: int, lease_until_ms: int, limit: int
    ) -> list[BackendEventOutboxRecord]:
        rows = self.connection.execute(
            """
            SELECT sequence FROM backend_event_outbox
            WHERE status IN ('pending','sent') AND available_at_ms<=?
            ORDER BY sequence LIMIT ?
            """,
            (now_ms, limit),
        ).fetchall()
        sequences = [int(row[0]) for row in rows]
        if not sequences:
            return []
        params: list[Any] = [now_ms, lease_until_ms, *sequences]
        self.connection.execute(
            f"""
            UPDATE backend_event_outbox
            SET status='sent',delivery_attempt_count=delivery_attempt_count+1,
                last_sent_at_ms=?,available_at_ms=?,last_error=NULL
            WHERE sequence IN ({_placeholders(sequences)})
            """,
            params,
        )
        claimed = self.connection.execute(
            f"""
            SELECT * FROM backend_event_outbox
            WHERE sequence IN ({_placeholders(sequences)}) ORDER BY sequence
            """,
            sequences,
        )
        return [self._backend_event(row) for row in claimed]

    def acknowledge_backend_events(
        self, *, through_sequence: int, acked_at_ms: int
    ) -> int:
        cursor = self.connection.execute(
            """
            UPDATE backend_event_outbox
            SET status='acknowledged',acked_at_ms=?
            WHERE sequence<=? AND status='sent'
            """,
            (acked_at_ms, through_sequence),
        )
        return int(cursor.rowcount)


__all__ = ["RuntimeRepository"]
